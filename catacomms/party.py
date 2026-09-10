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

from .engine import Event, build
from .table import APP, Table

# Lobby wire forms. Short, because they cross the same radio as everything else.
#   v|<seed>                        a delve is called
#   y|<seed>                        I am coming
#   n|<seed>                        I am not
#   s|<seed>|<id>:<name>,<id>:<name>  the roster is fixed; build this room
CALL = "v"
ACCEPT = "y"
DECLINE = "n"
START = "s"

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

    # -- calling one ------------------------------------------------------

    def call(self, salt: str, my_name: str = "") -> list:
        if self.state == PLAYING:
            return [Event("note", text="You are already in a delve.")]
        self.my_name = my_name or self.my_name
        self.seed = make_seed(self.me, salt)
        self.caller = self.me
        self.state = CALLING
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
        self.pending_send.append(f"{START}|{self.seed}|{roster}")
        return self._start(self.seed, dict(self.accepted))

    # -- answering one ----------------------------------------------------

    def accept(self) -> list:
        if self.state != INVITED:
            return [Event("note", text="Nobody has called a delve.")]
        self.pending_send.append(f"{ACCEPT}|{self.seed}")
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
            if self.state == PLAYING:
                return []
            self.seed, self.caller, self.state = parts[1], src, INVITED
            return [Event("note", text=(
                f"{name_of(src)} is calling a delve into room {parts[1]}. "
                f"/join to go, /stay to sit it out."))]

        if kind == ACCEPT and len(parts) >= 2 and self.state == CALLING:
            if parts[1] == self.seed:
                self.accepted[src] = name_of(src)
                return [Event("note", text=f"{name_of(src)} is coming.")]
            return []

        if kind == DECLINE and len(parts) >= 2 and self.state == CALLING:
            self.accepted.pop(src, None)
            return [Event("note", text=f"{name_of(src)} stayed behind.")]

        if kind == START and len(parts) == 3:
            roster = {}
            for pair in parts[2].split(","):
                pid, _, name = pair.partition(":")
                if pid:
                    roster[pid] = name or pid
            if self.me not in roster:
                return []                 # a delve that is not ours
            return self._start(parts[1], roster)

        if self.state == PLAYING and self.table is not None:
            return self.table.on_payload(src, payload)
        return []

    def _start(self, seed: str, roster: dict) -> list:
        self.seed, self.roster, self.state = seed, roster, PLAYING
        room = build(seed, sorted(roster.items()))
        self.table = Table(room, self.me, roster)
        names = ", ".join(roster[p] for p in sorted(roster))
        return [Event("note", text=(
            f"Room {seed}. The party is {names}. "
            f"{len(room.living('monster'))} down there with you."))]

    # -- play -------------------------------------------------------------

    def submit(self, action: str) -> list:
        if self.state != PLAYING or self.table is None:
            return []
        if not self.table.submit(action):
            return []
        return self.table.advance()

    def tick(self) -> list:
        return self.table.advance() if self.table is not None else []

    def resend(self, now: float) -> None:
        """Give the table a chance to re-broadcast a stuck input. See
        Table.resend_due: loraline does not retransmit application frames, so
        without this a single dropped turn input hangs the delve forever."""
        if self.table is not None:
            self.table.resend_due(now)

    def leave(self) -> list:
        self.state, self.table = IDLE, None
        self.roster, self.accepted = {}, {}
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
