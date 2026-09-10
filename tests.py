"""Three players fighting through one room over a radio that loses packets."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, '/home/claude/loraline')

from delve import engine as E
from delve.engine import Room, build, step, state_hash, encode_action
from delve.table import Table, APP

ok = lambda m: print(f"  ok  {m}")

PLAYERS = [("a1b2c3", "hank"), ("d4e5f6", "dave"), ("091a2b", "mira")]

# ---------- the room is a function of its seed ----------
r1 = build("north-shaft-7", PLAYERS)
r2 = build("north-shaft-7", PLAYERS)
assert state_hash(r1) == state_hash(r2)
assert state_hash(build("other-seed", PLAYERS)) != state_hash(r1)
ok(f"same seed builds the same room ({len(r1.entities)} entities, hash {state_hash(r1)})")

# ---------- the transition is deterministic ----------
moves = {"a1b2c3": "m:e", "d4e5f6": "a:n", "091a2b": "w"}
s1, ev1 = step(r1, moves)
s2, ev2 = step(r2, moves)
assert state_hash(s1) == state_hash(s2)
assert [e.text for e in ev1] == [e.text for e in ev2]
ok("same inputs give the same next state and the same events")

# ---------- and it is sensitive to every input ----------
open_dir = next(d for d, (dx, dy) in E.DIRECTIONS.items()
                if r1.passable(r1.entities["091a2b"].x + dx,
                               r1.entities["091a2b"].y + dy)
                and r1.at(r1.entities["091a2b"].x + dx,
                          r1.entities["091a2b"].y + dy) is None)
blocked_dir = next(d for d, (dx, dy) in E.DIRECTIONS.items()
                   if not r1.passable(r1.entities["091a2b"].x + dx,
                                      r1.entities["091a2b"].y + dy))
s3, _ = step(r1, {**moves, "091a2b": f"m:{open_dir}"})
assert state_hash(s3) != state_hash(s1), "a changed input must change the state"
ok("a single different action produces a different state hash")

# a move into a wall is a legal no-op, so it must NOT change the state
blocked, bev = step(r1, {**moves, "091a2b": f"m:{blocked_dir}"})
assert state_hash(blocked) == state_hash(s1)
assert any(e.kind == "blocked" for e in bev)
ok("a move into a wall costs the turn, changes nothing, and says so")

# ---------- dictionary order must not matter ----------
shuffled = dict(reversed(list(moves.items())))
s4, _ = step(r1, shuffled)
assert state_hash(s4) == state_hash(s1)
ok("input ordering does not affect the result")

# ---------- no floats anywhere in the state ----------
def walk(o):
    if isinstance(o, float): return True
    if isinstance(o, dict): return any(walk(v) for v in o.values())
    if isinstance(o, (list, tuple, set, frozenset)): return any(walk(v) for v in o)
    if hasattr(o, '__dataclass_fields__'):
        return any(walk(getattr(o, f)) for f in o.__dataclass_fields__)
    return False
assert not walk(s1), "a float in the state is a divergence waiting to happen"
ok("no floats reachable from the state")

# ---------- the random draws are reproducible and unbiased-ish ----------
rng = E.Rng(E.seed_for("x", 0))
first = [rng.roll(6) for _ in range(200)]
rng = E.Rng(E.seed_for("x", 0))
assert [rng.roll(6) for _ in range(200)] == first
assert set(first) == {1,2,3,4,5,6}, sorted(set(first))
ok("the generator repeats exactly and covers its range")

# ---------- a full fight, played out ----------
import itertools
def play(seed, script, ticks=60):
    room = build(seed, PLAYERS)
    for _ in range(ticks):
        if room.over: break
        room, _ = step(room, {p: next(script[p]) for p, _ in PLAYERS
                              if room.entities[p].alive})
    return room

script = {p: itertools.cycle(["a:n","a:e","a:s","a:w","m:e","m:s","m:w","m:n"])
          for p, _ in PLAYERS}
finished = play("north-shaft-7", script)
assert finished.tick > 1
ok(f"a fight resolves: {finished.tick} turns, outcome {finished.outcome}, "
   f"{len(finished.living('player'))} of 3 standing")

# every room must end, however badly the party plays
worst = [play(f"stalemate-{i}", {p: itertools.cycle(["w"]) for p, _ in PLAYERS},
              ticks=500) for i in range(40)]
assert all(r.over for r in worst), "a room that never ends is a hang"
assert all(r.tick <= r.limit for r in worst)
ok(f"40 rooms of players doing nothing at all still terminate "
   f"(outcomes: {sorted({r.outcome for r in worst})})")

# ---------- three tables over a lossy radio ----------
class Radio:
    """Broadcast bus that drops one packet in `drop`."""
    def __init__(self, tables, drop=0):
        self.tables, self.drop, self.n = tables, drop, 0
        self.dropped, self.sent = 0, 0
        self.retry = {}
    def carry(self):
        for src, table in self.tables.items():
            for payload in table.drain():
                self.retry.setdefault(src, []).append(payload)
        delivered = []
        for src, queue in self.retry.items():
            for payload in list(queue):
                self.n += 1; self.sent += 1
                if self.drop and self.n % self.drop == 0:
                    self.dropped += 1
                    continue            # lost; loraline would retransmit
                queue.remove(payload)
                for dst, table in self.tables.items():
                    if dst != src:
                        delivered += table.on_payload(src, payload)
        return delivered

names = dict(PLAYERS)
def new_tables(seed):
    return {pid: Table(build(seed, PLAYERS), pid, names) for pid, _ in PLAYERS}

tables = new_tables("north-shaft-7")
radio = Radio(tables, drop=3)
choices = {p: itertools.cycle(["a:n","a:e","m:e","a:s","m:s","a:w","m:n","w"])
           for p, _ in PLAYERS}

for _ in range(400):
    for pid, table in tables.items():
        if not table.room.over and not table.has_acted() and table._alive(pid):
            table.submit(next(choices[pid]))
    for table in tables.values():
        table.advance()
    radio.carry()
    if all(t.room.over for t in tables.values()):
        break

ticks = {pid: t.room.tick for pid, t in tables.items()}
hashes = {pid: state_hash(t.room) for pid, t in tables.items()}
assert len(set(ticks.values())) == 1, ticks
assert len(set(hashes.values())) == 1, hashes
assert all(t.room.over for t in tables.values())
assert not any(t.diverged for t in tables.values())
ok(f"three players agree after a full fight over a link dropping 1 in 3 "
   f"({radio.dropped} of {radio.sent} packets lost, ended turn {list(ticks.values())[0]})")
ok(f"final state identical on all three machines: {list(hashes.values())[0]}")

# ---------- divergence is caught, not absorbed ----------
tables = new_tables("north-shaft-7")
cheat = tables["091a2b"]
cheat.room = cheat.room.__class__(**{**cheat.room.__dict__,
    "entities": {**cheat.room.entities,
                 "a1b2c3": cheat.room.entities["a1b2c3"].__class__(
                     **{**cheat.room.entities["a1b2c3"].__dict__, "hp": 999})}})
radio = Radio(tables, drop=0)
caught = []
for _ in range(40):
    for pid, table in tables.items():
        if not table.room.over and not table.has_acted():
            table.submit("a:n")
    for table in tables.values():
        table.advance()
    caught += [e for e in radio.carry() if e.kind == "diverged"]
    if caught: break
assert caught, "a player running a different world must be noticed"
ok(f"tampering is detected: {caught[0].text[:58]}...")



# ---------- forming a party ----------
from delve.party import Party, IDLE, CALLING, INVITED, PLAYING

names = {"a1b2c3": "hank", "d4e5f6": "dave", "091a2b": "mira", "ff0011": "lena"}
name_of = lambda a: names.get(a, a)

parties = {a: Party(a, n) for a, n in names.items()}

def deliver(only=None):
    """Move every queued payload to everyone else. Returns the events raised."""
    out = []
    for src, party in parties.items():
        for payload in party.drain():
            for dst, other in parties.items():
                if dst != src and (only is None or dst in only or src in only):
                    out += other.on_payload(src, payload, name_of)
    return out

deliver()
parties["a1b2c3"].call("tuesday")
deliver()
assert parties["d4e5f6"].state == INVITED and parties["ff0011"].state == INVITED
ok("a called delve reaches everyone in range")

parties["d4e5f6"].accept()
parties["091a2b"].accept()
parties["ff0011"].decline()
deliver()
assert set(parties["a1b2c3"].accepted) == {"a1b2c3", "d4e5f6", "091a2b"}
ok("acceptances and refusals are tallied by the caller")

parties["a1b2c3"].begin()
deliver()
playing = {a: p for a, p in parties.items() if p.state == PLAYING}
assert set(playing) == {"a1b2c3", "d4e5f6", "091a2b"}, sorted(playing)
assert parties["ff0011"].state != PLAYING, "someone who declined must not be in the room"
ok("only the named roster ends up in the delve")

rooms = {a: state_hash(p.table.room) for a, p in playing.items()}
assert len(set(rooms.values())) == 1, rooms
assert all(set(p.table.players) == {"a1b2c3", "d4e5f6", "091a2b"} for p in playing.values())
ok(f"all three built the identical room from the start message ({list(rooms.values())[0]})")

# the roster comes from the message, not from local state: prove it by giving
# one client a wrong idea of who is around before the start arrives
stale = Party("d4e5f6", "dave")
stale.on_payload("a1b2c3", f"{'v'}|{parties['a1b2c3'].seed}", name_of)
stale.accepted = {"d4e5f6": "dave", "zzzzzz": "ghost"}     # nonsense local state
for payload in [f"s|{parties['a1b2c3'].seed}|" +
                ",".join(f"{p}:{names[p]}" for p in sorted(playing))]:
    stale.on_payload("a1b2c3", payload, name_of)
assert state_hash(stale.table.room) == list(rooms.values())[0]
ok("a client with a wrong local roster still builds the right room")

# ---------- play it out through the party layer ----------
choices = {p: itertools.cycle(["a:n","a:e","m:e","a:s","m:s","a:w","m:n","w"])
           for p in playing}
for _ in range(400):
    for pid, party in playing.items():
        t = party.table
        if not t.room.over and not t.has_acted() and t._alive(pid):
            party.submit(next(choices[pid]))
    for party in playing.values():
        party.tick()
    deliver()
    if all(p.table.room.over for p in playing.values()):
        break
finals = {a: state_hash(p.table.room) for a, p in playing.items()}
ticks = {a: p.table.room.tick for a, p in playing.items()}
assert len(set(finals.values())) == 1, finals
assert len(set(ticks.values())) == 1, ticks
assert not any(p.table.diverged for p in playing.values())
outcome = list(playing.values())[0].table.room.outcome
ok(f"a delve played through the party layer agrees on turn {list(ticks.values())[0]}, "
   f"outcome {outcome}")


# ---------- friendly fire is off by default ----------
pair = build("adjacent-test", PLAYERS)
a, b = sorted(pair.entities)[0], sorted(pair.entities)[1]
ea, eb = pair.entities[a], pair.entities[b]
# find an ally standing next to another, whatever the seed produced
adjacent = [(x.id, y.id, d) for x in pair.living("player") for y in pair.living("player")
            for d, (dx, dy) in E.DIRECTIONS.items()
            if x.id != y.id and (x.x + dx, x.y + dy) == (y.x, y.y)]
assert adjacent, "expected at least two party members side by side at the start"
attacker, victim, direction = adjacent[0]
after, evs = step(pair, {attacker: f"a:{direction}"})
assert after.entities[victim].hp == pair.entities[victim].hp
assert any(e.kind == "held" for e in evs)
ok("a swing at an ally is held, and costs the turn")

allowed = replace_room = pair.__class__(**{**pair.__dict__, "friendly_fire": True})
after2, evs2 = step(allowed, {attacker: f"a:{direction}"})
assert state_hash(after2) != state_hash(after), "the rule must be part of the state"
ok("friendly fire is a room rule, and changing it changes the state hash")

print("\nALL PASS")
