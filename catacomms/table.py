"""Turn-lock over a lossy link, and divergence detection.

The engine is deterministic, so every machine will compute the same world
provided every machine has the same inputs. On a radio that loses packets,
that proviso is the entire problem, and it is what this file exists to solve.

The approach is deliberately not consensus. With three players on a half
duplex channel there is no quorum worth having, so instead:

  * A tick does not advance until every present player's input for it has
    arrived. loraline retransmits until acknowledged, so this terminates.
  * After advancing, every player publishes an eight-byte hash of the
    resulting state. Anyone whose hash disagrees knows immediately.

That buys detection rather than agreement. For a party who can see each
other it is enough: the game stops and says so, instead of quietly drifting
into two different worlds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .engine import Room, state_hash, step

APP = "catacomms"

# Wire forms, kept short because every player sends one of these every turn.
#   i|<tick>|<action>     an input
#   h|<tick>|<hash>       the state after that tick, as the sender computed it
#   j|<name>              joining, before the room is built
MSG_INPUT = "i"
MSG_HASH = "h"
MSG_JOIN = "j"


@dataclass
class Table:
    """One room, and the bookkeeping that keeps several machines on the same one."""

    room: Room
    me: str
    players: dict                              # id -> name
    inputs: dict = field(default_factory=dict)  # tick -> {player id: action}
    hashes: dict = field(default_factory=dict)  # tick -> {player id: hash}
    diverged: set = field(default_factory=set)
    pending_send: list = field(default_factory=list)
    log: list = field(default_factory=list)
    _last_input_send: float = 0.0    # when we last (re)broadcast our current input
    _last_sent_tick: int = -1        # which tick that input was for

    # -- local play --------------------------------------------------------

    def submit(self, action: str) -> bool:
        """Record my action for the current tick and queue it for broadcast."""
        if self.room.over or self.has_acted():
            return False
        self.inputs.setdefault(self.room.tick, {})[self.me] = action
        self.pending_send.append(f"{MSG_INPUT}|{self.room.tick}|{action}")
        self._last_input_send = 0.0        # force a fresh send window
        self._last_sent_tick = self.room.tick
        return True

    def has_acted(self) -> bool:
        return self.me in self.inputs.get(self.room.tick, {})

    def waiting_for(self) -> list:
        """Who has not yet submitted for this tick."""
        have = self.inputs.get(self.room.tick, {})
        return sorted(p for p in self.players
                      if p not in have and self._alive(p))

    def _alive(self, pid: str) -> bool:
        entity = self.room.entities.get(pid)
        return entity is not None and entity.alive

    def resend_due(self, now: float, interval: float = 1.5) -> None:
        """Re-broadcast our inputs for any tick still missing somebody's move.

        loraline sends application frames once and does not retransmit them, so
        a dropped input would otherwise freeze a tick forever. A peer can be
        stuck several ticks behind us, still missing an input we sent long ago,
        so we resend our input for every tick from the lowest incomplete one up
        to our own current tick. Inputs are keyed by tick and deduplicated on
        receipt, so resending only costs airtime and can never double-apply. We
        keep our own inputs indefinitely for exactly this reason, and we keep
        resending even after our own game is over, because a peer still catching
        up needs those inputs to reach the end at all.
        """
        if now - self._last_input_send < interval:
            return
        # Find every tick still missing somebody's hash (proof they advanced
        # past it holding our input). The lowest such tick is the one blocking
        # a lagging peer; resend our input for all of them, but prioritise the
        # oldest by sending it first, since nothing downstream can resolve
        # until it does. One peer can be many ticks behind us over a lossy
        # link, so this spans from the oldest gap up to our own tick.
        outstanding = []
        for tick in sorted(self.inputs):
            if tick > self.room.tick:
                break
            mine = self.inputs.get(tick, {}).get(self.me)
            if mine is None:
                continue
            done = self.hashes.get(tick, {})
            # A player needs our input for this tick unless they have already
            # published a hash for it (proof they advanced past it). We must
            # NOT skip a player just because they are dead in our *current*
            # view: a peer stuck behind us is still alive at their tick and
            # cannot reach the tick of their death without our inputs. Only a
            # published hash proves they no longer need it.
            if any(p not in done for p in self.players):
                outstanding.append((tick, mine))
        if outstanding:
            for tick, mine in outstanding:
                self.pending_send.append(f"{MSG_INPUT}|{tick}|{mine}")
            self._last_input_send = now

    # -- the network -------------------------------------------------------

    def on_payload(self, src: str, payload: str) -> list:
        """Handle one application frame from another player."""
        parts = payload.split("|", 2)
        kind = parts[0] if parts else ""

        if kind == MSG_INPUT and len(parts) == 3 and parts[1].lstrip("-").isdigit():
            tick = int(parts[1])
            # An input for a tick already resolved is a retransmission of
            # something we acted on, so it is dropped rather than replayed.
            if tick >= self.room.tick:
                self.inputs.setdefault(tick, {}).setdefault(src, parts[2])
            return self.advance()

        if kind == MSG_HASH and len(parts) == 3 and parts[1].lstrip("-").isdigit():
            tick = int(parts[1])
            self.hashes.setdefault(tick, {})[src] = parts[2]
            return self._check(tick)

        return []

    def advance(self) -> list:
        """Advance as far as the inputs allow. Usually zero or one tick."""
        events: list = []
        while not self.room.over:
            tick = self.room.tick
            have = self.inputs.get(tick, {})
            if any(p not in have for p in self.players if self._alive(p)):
                break
            self.room, stepped = step(self.room, have)
            events += stepped
            digest = state_hash(self.room)
            self.hashes.setdefault(tick, {})[self.me] = digest
            self.pending_send.append(f"{MSG_HASH}|{tick}|{digest}")
            events += self._check(tick)
        return events

    def _check(self, tick: int) -> list:
        """Compare published hashes for a tick. Silence unless they disagree."""
        published = self.hashes.get(tick, {})
        mine = published.get(self.me)
        if mine is None:
            return []
        odd = sorted(p for p, h in published.items() if h != mine)
        new = [p for p in odd if (tick, p) not in self.diverged]
        for player in new:
            self.diverged.add((tick, player))
        if not new:
            return []
        from .engine import Event
        names = ", ".join(self.players.get(p, p) for p in new)
        return [Event("diverged", text=(
            f"{names} computed a different world at turn {tick}. "
            f"Somebody is running different rules; the game cannot continue "
            f"honestly from here."
        ))]

    def drain(self) -> list:
        out, self.pending_send = self.pending_send, []
        return out

    # -- views -------------------------------------------------------------

    def summary(self) -> str:
        if self.room.over:
            return "the room is clear" if self.room.won else "the party has fallen"
        if self.diverged:
            return "DIVERGED"
        if not self.has_acted():
            return "your move"
        waiting = self.waiting_for()
        if waiting:
            names = ", ".join(self.players.get(p, p) for p in waiting)
            return f"waiting for {names}"
        return "resolving"
