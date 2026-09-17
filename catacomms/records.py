"""What a delve leaves behind.

A room is a pure function of its seed and roster, so anyone can rebuild it and
check that an Ashen Fang is genuinely what drops there. What they cannot check
alone is that you were the one who walked out with it. That is what the
signatures are for: everyone who was present signs the same record, and the
item becomes as real as the people who witnessed it.

Two consequences fall out of this rather than being decided:

  A delve alone produces a record signed only by you, and is therefore worth
  nothing to anybody else. You cannot witness your own loot.

  The record notes, per person, whether their transmissions were heard on air.
  Being reachable over a socket proves you exist; being heard on the radio
  proves you were somewhere. Only the second is scarce, and only the second is
  worth counting.

Nothing here talks to a network or a disk except through `save` and `load`.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DIR = Path.home() / ".catacomms" / "records"
VERSION = 1


@dataclass
class Record:
    """One finished delve, as everybody present agreed it went."""

    seed: str
    roster: dict                      # address -> name
    tick: int
    outcome: str                      # cleared | withdrew | wiped
    state_hash: str
    carried: dict                     # address -> [{"kind","name","tier","value"}]
    on_air: dict = field(default_factory=dict)      # address -> bool
    signatures: dict = field(default_factory=dict)  # address -> base64
    # Both public halves of every signer. Not sent over the air, since anyone
    # who was there already has them, but written down so that somebody who
    # was not there can still check the whole thing for themselves. An address
    # is the hash of the pair, so a forged key cannot be slipped in beside a
    # real address.
    keys: dict = field(default_factory=dict)        # address -> [public, verify]

    # -- identity ----------------------------------------------------------

    def canonical(self) -> bytes:
        """The exact bytes everybody signs. Sorted throughout, because two
        machines that serialised the same delve differently would produce two
        different signatures over one truth."""
        body = {
            "v": VERSION,
            "seed": self.seed,
            "tick": self.tick,
            "outcome": self.outcome,
            "state": self.state_hash,
            "roster": [[a, self.roster[a]] for a in sorted(self.roster)],
            "air": [[a, bool(self.on_air.get(a))] for a in sorted(self.roster)],
            "carried": [
                [a, sorted(
                    [[i["kind"], i["name"], i["tier"], int(i["value"])]
                     for i in self.carried.get(a, [])]
                )]
                for a in sorted(self.roster)
            ],
        }
        return json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")

    @property
    def id(self) -> str:
        return hashlib.blake2b(self.canonical(), digest_size=8).hexdigest()

    # -- signing -----------------------------------------------------------

    def sign(self, identity) -> str:
        signature = identity.sign(self.canonical())
        self.signatures[identity.address] = signature
        self.keys[identity.address] = [identity.public_b64, identity.verify_b64]
        return signature

    def accept(self, address: str, signature: str, keyring) -> bool:
        """Take somebody else's signature, but only if it is really theirs."""
        if address not in self.roster:
            return False
        if not keyring.verify(address, self.canonical(), signature):
            return False
        self.signatures[address] = signature
        pub = keyring.peer_keys.get(address)
        ver = keyring.verifiers.get(address)
        if pub and ver:
            import base64 as _b64
            self.keys[address] = [_b64.b64encode(pub).decode("ascii"),
                                  _b64.b64encode(ver).decode("ascii")]
        return True

    def witnesses(self, keyring) -> list:
        """Everyone whose signature checks out. Recomputed rather than
        trusted, because a stored record is a file anybody could edit."""
        return sorted(a for a, s in self.signatures.items()
                      if keyring.verify(a, self.canonical(), s))

    def attested(self, keyring, holder: str) -> bool:
        """Is what this person carried out worth anything to anyone else?

        Somebody other than the holder must have signed, and both must have
        been on air. A delve alone, or one played entirely over sockets,
        happened and is not disputed. It simply mints nothing."""
        seen = self.witnesses(keyring)
        others = [a for a in seen if a != holder]
        if not others or holder not in seen:
            return False
        return bool(self.on_air.get(holder)) and any(self.on_air.get(a) for a in others)

    # -- storage -----------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps({
            "v": VERSION, "seed": self.seed, "roster": self.roster,
            "tick": self.tick, "outcome": self.outcome,
            "state_hash": self.state_hash, "carried": self.carried,
            "on_air": self.on_air, "signatures": self.signatures,
            "keys": self.keys,
        }, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Record":
        raw = json.loads(text)
        return cls(seed=raw["seed"], roster=raw["roster"], tick=raw["tick"],
                   outcome=raw["outcome"], state_hash=raw["state_hash"],
                   carried=raw["carried"], on_air=raw.get("on_air", {}),
                   signatures=raw.get("signatures", {}), keys=raw.get("keys", {}))


def build(room, players: dict, on_air: dict) -> Record:
    """Make a record from a finished room."""
    carried = {}
    for address in sorted(players):
        entity = room.entities.get(address)
        if entity is None:
            continue
        carried[address] = [
            {"kind": i.kind, "name": i.name, "tier": i.tier, "value": i.value}
            for i in entity.pack
        ]
    from .engine import state_hash
    return Record(seed=room.seed, roster=dict(players), tick=room.tick,
                  outcome=room.outcome, state_hash=state_hash(room),
                  carried=carried, on_air={a: bool(on_air.get(a)) for a in players})


# --------------------------------------------------------------------------
# The stash
# --------------------------------------------------------------------------

def save(record: Record, directory=DEFAULT_DIR) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record.id}.json"
    path.write_text(record.to_json())
    return path


def load_all(directory=DEFAULT_DIR) -> list:
    directory = Path(directory)
    if not directory.exists():
        return []
    out = []
    for path in sorted(directory.glob("*.json")):
        try:
            out.append(Record.from_json(path.read_text()))
        except Exception:
            continue        # a corrupt file is not a reason to lose the rest
    return out


def stash(address: str, keyring, directory=DEFAULT_DIR) -> list:
    """Everything you can prove you own: (item, record) pairs.

    Only attested records count. What you found alone is yours in every sense
    that matters at the table, and worth nothing to anyone who was not there.
    """
    out = []
    for record in load_all(directory):
        if not record.attested(keyring, address):
            continue
        for item in record.carried.get(address, []):
            if item["kind"] != "coin":
                out.append((item, record))
    return out


def board(directory=DEFAULT_DIR) -> dict:
    """Everything a stranger needs to redo the arithmetic themselves.

    Deliberately not a ranking. It is the records, their canonical bytes, and
    the public keys of whoever signed them. Whoever reads it works out who has
    what by checking the signatures, and if they disagree with the tally they
    are right and the tally is wrong.
    """
    import base64 as _b64
    out = []
    for record in load_all(directory):
        out.append({
            "id": record.id,
            "canonical": _b64.b64encode(record.canonical()).decode("ascii"),
            "seed": record.seed,
            "tick": record.tick,
            "outcome": record.outcome,
            "roster": record.roster,
            "on_air": record.on_air,
            "carried": record.carried,
            "signatures": record.signatures,
            "keys": record.keys,
        })
    return {"v": VERSION, "records": sorted(out, key=lambda r: r["id"])}


def purse(address: str, keyring, directory=DEFAULT_DIR) -> int:
    total = 0
    for record in load_all(directory):
        if record.attested(keyring, address):
            total += sum(i["value"] for i in record.carried.get(address, [])
                         if i["kind"] == "coin")
    return total
