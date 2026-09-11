"""Forming a party, and binding a table to a live loraline client.

The delicate part is the roster. Every machine has to build the identical
room, and a room is built from a seed plus a player list. If each client used
its own idea of who is online, two clients with slightly different contact
lists would build different rooms and diverge before the first turn.

So the roster is not assembled locally. One player calls the delve, collects
acceptances, and then broadcasts a single start message naming the seed and
the exact roster. That message is the constitution of that room: anyone who
receives it builds the same world, and anyone not named in it is not in the
delve, however online they happen to be.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from . import records as R
from .engine import Event, build, build_camp
from .table import APP, Table

# Lobby wire forms. Short, because they cross the same radio as everything else.
#   v|<seed>                        a delve is called
#   y|<seed>                        I am coming
#   n|<seed>                        I am not
#   s|<seed>|<id>:<name>,...|<kind>|<depth>|<torches>   the roster is fixed
CALL = "v"
ACCEPT = "y"
DECLINE = "n"
START = "s"
SIGN = "g"        # g|<record id>|<signature>

IDLE, CALLING, INVITED, PLAYING = "idle", "calling", "invited", "playing"


def make_seed(caller: str, salt: str) -> str:
    """Seeds are short and derived, so the same string names the same room
    everywhere and costs almost nothing to send."""
    return hashlib.blake2b(f"{caller}|{salt}".encode(), digest_size=4).hexdigest()


@dataclass
class Party:
    """Lobby state machine, then a table once the delve begins.

    Pure in the same sense as the rest: payloads and a clock go in, events come
    out, and anything to transmit is left on `pending_send` for the caller.
    """

    me: str
    my_name: str
    seed: str = ""
    caller: str = ""
    state: str = IDLE
    roster: dict = field(default_factory=dict)     # id -> name, once fixed
    accepted: dict = field(default_factory=dict)   # id -> name, while calling
    table: Table | None = None
    pending_send: list = field(default_factory=list)
    _start_msg: str = ""          # the START we broadcast, resent until all arrive
    _last_start_send: float = 0.0
    _accept_msg: str = ""         # our ACCEPT, resent until the START names us
    _last_accept_send: float = 0.0
    record: object = None
    identity: object = None
    keyring: object = None
    on_air: object = None       # callable: address -> was heard on the radio
    store: object = None        # where records are kept
    afford: object = None       # callable: may we spend a frame yet
    depth: int = 1
    torches: int = 0
    kind: str = "delve"
    # Signatures can arrive before this machine has closed out its own record,
    # because rooms end on whichever move lands last. Held rather than dropped,
    # or whoever finishes last would lose their witnesses.
    early: dict = field(default_factory=dict)

    # -- calling one ------------------------------------------------------

    def call(self, salt: str, my_name: str = "", depth: int = 1,
             torches: int = 0, kind: str = "delve") -> list:
        if self.state == PLAYING:
            return [Event("note", text="You are already in a delve.")]
        self.my_name = my_name or self.my_name
        self.seed = make_seed(self.me, salt)
        self.caller = self.me
        self.state = CALLING
        self.depth, self.torches, self.kind = max(1, depth), max(0, torches), kind
        self.accepted = {self.me: self.my_name}
        self.pending_send.append(f"{CALL}|{self.seed}")
        return [Event("note", text=(
            f"You called a delve into room {self.seed}. Waiting for takers; "
            f"/begin when you are ready."))]

    def begin(self) -> list:
        """Fix the roster and send it. Nothing before this message is binding."""
        if self.state != CALLING:
            return [Event("note", text="You have not called a delve.")]
        if len(self.accepted) < 1:
            return [Event("note", text="Nobody is coming.")]
        roster = ",".join(f"{pid}:{name}" for pid, name in sorted(self.accepted.items()))
        self._start_msg = (f"{START}|{self.seed}|{roster}"
                           f"|{self.kind}|{self.depth}|{self.torches}")
        self.pending_send.append(self._start_msg)
        self._last_start_send = 0.0
        return self._start(self.seed, dict(self.accepted),
                           self.kind, self.depth, self.torches)

    # -- answering one ----------------------------------------------------

    def accept(self) -> list:
        if self.state != INVITED:
            return [Event("note", text="Nobody has called a delve.")]
        self._accept_msg = f"{ACCEPT}|{self.seed}"
        self._last_accept_send = 0.0
        self.pending_send.append(self._accept_msg)
        return [Event("note", text=f"You are in. Waiting for {self.caller} to begin.")]

    def decline(self) -> list:
        if self.state != INVITED:
            return []
        self.pending_send.append(f"{DECLINE}|{self.seed}")
        self.state = IDLE
        return [Event("note", text="You stayed behind.")]

    # -- the wire ---------------------------------------------------------

    def on_payload(self, src: str, payload: str, name_of) -> list:
        parts = payload.split("|", 2)
        kind = parts[0] if parts else ""

        if kind == CALL and len(parts) >= 2:
            # Only an idle client can be invited. If we are already calling our
            # own delve, playing one, or invited to someone else's, a fresh CALL
            # must not clobber that state, or two overlapping calls leave both
            # players pointed at different rooms and the turn-lock waits forever
            # for an input meant for a room the other never built. The caller
            # whose delve "wins" is simply whoever the others were idle for; the
            # rest can /stay and re-answer, or the caller retries.
            their_seed = parts[1]
            if self.state == PLAYING:
                return []                # a game in progress is never interrupted
            if self.state == INVITED and src == self.caller \
                    and their_seed == self.seed:
                return []                # duplicate of the call we already saw
            if self.state == CALLING:
                # Two people called at once. Rather than deadlock (each refusing
                # the other) or corrupt state (each overwriting the other), both
                # sides deterministically defer to the lower seed. Whoever's
                # seed loses abandons their own call and becomes invited to the
                # winner, so both machines converge on one room with no manual
                # step. Ties (same seed) cannot happen: a seed includes the
                # caller's address.
                if their_seed < self.seed:
                    self.seed, self.caller, self.state = their_seed, src, INVITED
                    self.accepted = {}
                    self._start_msg = ""
                    return [Event("note", text=(
                        f"{name_of(src)} also called; their room {their_seed} "
                        f"takes precedence. /join to go, /stay to sit it out."))]
                return []                # ours wins; they will defer to us
            # IDLE: a normal invitation.
            self.seed, self.caller, self.state = their_seed, src, INVITED
            return [Event("note", text=(
                f"{name_of(src)} is calling a delve into room {their_seed}. "
                f"/join to go, /stay to sit it out."))]

        if kind == ACCEPT and len(parts) >= 2 and self.state == CALLING:
            if parts[1] == self.seed:
                self.accepted[src] = name_of(src)
                return [Event("note", text=f"{name_of(src)} is coming.")]
            return []

        if kind == DECLINE and len(parts) >= 2 and self.state == CALLING:
            self.accepted.pop(src, None)
            return [Event("note", text=f"{name_of(src)} stayed behind.")]

        if kind == SIGN and len(parts) == 3:
            if self.record is None:
                self.early[src] = (parts[1], parts[2])
                return []
            if parts[1] != self.record.id:
                return []
            if not self.record.accept(src, parts[2], self.keyring):
                return [Event("note", text=(
                    f"{name_of(src)} sent a signature that does not check out. "
                    f"Nothing of theirs is being counted."))]
            R.save(self.record, self.store) if self.store else R.save(self.record)
            seen = len(self.record.witnesses(self.keyring))
            return [Event("note", text=(
                f"{name_of(src)} signed for the delve "
                f"({seen} of {len(self.roster)} witnesses)."))]

        if kind == START and len(parts) == 3:
            tail = parts[2].split("|")
            roster_text = tail[0]
            place = tail[1] if len(tail) > 1 else "delve"
            depth = int(tail[2]) if len(tail) > 2 and tail[2].isdigit() else 1
            torches = int(tail[3]) if len(tail) > 3 and tail[3].isdigit() else 0
            roster = {}
            for pair in roster_text.split(","):
                pid, _, name = pair.partition(":")
                if pid:
                    roster[pid] = name or pid
            if self.me not in roster:
                return []                 # a delve that is not ours
            self._accept_msg = ""         # the caller heard us; stop resending
            if self.state == PLAYING and self.table is not None \
                    and self.seed == parts[1]:
                return []                 # already in this room; START resent
            return self._start(parts[1], roster, place, depth, torches)

        if self.state == PLAYING and self.table is not None:
            # A room usually ends on somebody else's move arriving rather than
            # on your own clock, so the record has to be closed out here too.
            return self.table.on_payload(src, payload) + self.close_out()
        return []

    def _start(self, seed: str, roster: dict, kind: str = "delve",
               depth: int = 1, torches: int = 0) -> list:
        self.seed, self.roster, self.state = seed, roster, PLAYING
        self.kind, self.depth, self.torches = kind, depth, torches
        if kind == "camp":
            room = build_camp(seed, sorted(roster.items()))
        else:
            room = build(seed, sorted(roster.items()), depth=depth, torches=torches)
        self.table = Table(room, self.me, roster)
        names = ", ".join(roster[p] for p in sorted(roster))
        if kind == "camp":
            trees = len([e for e in room.entities.values() if e.kind == "tree"])
            return [Event("note", text=(
                f"A clearing, {names}. {trees} worth cutting. "
                f"c and a direction to start, x when you are done."))]
        return [Event("note", text=(
            f"Room {seed}, depth {depth}. The party is {names}. "
            f"{len(room.living('monster'))} down there, and light for "
            f"{room.limit} turns."))]

    # -- play -------------------------------------------------------------

    def submit(self, action: str) -> list:
        if self.state != PLAYING or self.table is None:
            return []
        # The radio has to be able to afford the frame. At one percent duty a
        # party acting as fast as it can decide burns eight times the hourly
        # allowance and jams within minutes, so the client paces itself rather
        # than discovering the wall.
        if self.afford is not None and not self.afford():
            return [Event("note", text="The radio is still recovering.")]
        if not self.table.submit(action):
            return []
        return self.table.advance() + self.close_out()

    def tick(self) -> list:
        if self.table is None:
            return []
        return self.table.advance() + self.close_out()

    def close_out(self) -> list:
        """When the room ends, write down what happened and sign it.

        Everyone present builds the same record from the same final state, so
        the signatures are over identical bytes without the record itself ever
        being sent. Only the signature crosses the radio.
        """
        if (self.table is None or not self.table.room.over
                or self.record is not None or self.identity is None):
            return []
        air = {a: bool(self.on_air(a)) if self.on_air else False for a in self.roster}
        air[self.me] = True          # you were certainly where you were
        self.record = R.build(self.table.room, self.roster, air)
        signature = self.record.sign(self.identity)
        self.pending_send.append(f"{SIGN}|{self.record.id}|{signature}")

        events: list = []
        for who, (record_id, sig) in list(self.early.items()):
            if record_id != self.record.id:
                continue
            if self.record.accept(who, sig, self.keyring):
                events.append(Event("note", text=(
                    f"{self.roster.get(who, who)} had already signed for it.")))
            self.early.pop(who, None)
        R.save(self.record, self.store) if self.store else R.save(self.record)

        mine = self.record.carried.get(self.me, [])
        goods = [i["name"] for i in mine if i["kind"] != "coin"]
        coin = sum(i["value"] for i in mine if i["kind"] == "coin")
        haul = ", ".join(goods) if goods else "nothing"
        return events + [Event("note", text=(
            f"Record {self.record.id}: {self.table.room.outcome}. "
            f"You carried out {haul}"
            + (f" and {coin} coin." if coin else ".")))]

    def resend(self, now: float) -> None:
        """Re-broadcast anything the handshake still needs. loraline does not
        retransmit application frames, so a single dropped ACCEPT, START or
        turn input would otherwise hang a delve forever."""
        # Resend our ACCEPT until a START names us. An ACCEPT lost on the air
        # means the caller begins without us and builds a room we are not in.
        if self._accept_msg and now - self._last_accept_send >= 4.0:
            self.pending_send.append(self._accept_msg)
            self._last_accept_send = now
        if self.table is None:
            return
        self.table.resend_due(now)
        # The START message is also a fire-and-forget app frame. If it was
        # dropped, a roster member never built the room and will never send an
        # input, so the caller would wait forever. Keep re-broadcasting START
        # until everyone has appeared in the table (any input or hash from
        # them proves they got it). Building a room from a repeated START is
        # idempotent: _start rebuilds the same deterministic room.
        if self._start_msg and now - self._last_start_send >= 4.0:
            seen = set(self.table.inputs.get(0, {})) | {
                p for h in self.table.hashes.values() for p in h
            }
            missing = [p for p in self.roster if p != self.me and p not in seen]
            if missing:
                self.pending_send.append(self._start_msg)
                self._last_start_send = now
            else:
                self._start_msg = ""      # everyone is in; stop resending

    def leave(self) -> list:
        self.state, self.table, self.record = IDLE, None, None
        self.roster, self.accepted = {}, {}
        self.kind, self.depth, self.torches = "delve", 1, 0
        self.early.clear()
        return [Event("note", text="You are out of the delve.")]

    def drain(self) -> list:
        out = list(self.pending_send)
        self.pending_send.clear()
        if self.table is not None:
            out += self.table.drain()
        return out

    # -- views ------------------------------------------------------------

    def status(self) -> str:
        if self.state == IDLE:
            return "no delve"
        if self.state == CALLING:
            coming = len(self.accepted)
            return f"calling room {self.seed}, {coming} coming, /begin to go"
        if self.state == INVITED:
            return f"invited to room {self.seed}, /join or /stay"
        return self.table.summary() if self.table else "playing"
