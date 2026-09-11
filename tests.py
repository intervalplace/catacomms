"""Three players fighting through one room over a radio that loses packets."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, '/home/claude/loraline')

from catacomms import engine as E
from catacomms.engine import Room, build, step, state_hash, encode_action
from catacomms.table import Table, APP

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
from catacomms.party import Party, IDLE, CALLING, INVITED, PLAYING

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


# ---------- items actually do something ----------
from catacomms.engine import Item
bare = E.Entity("p", "player", "hank", 1, 1, hp=20, max_hp=20, attack=6, armour=4)
kitted = E.replace(bare, pack=(Item("weapon", "Red Edge", "named", 12),
                               Item("armour", "Cold Mail", "named", 10),
                               Item("charm", "Salt Bead", "named", 13)))
assert (kitted.swing, kitted.guard, kitted.hp_cap) == (12, 10, 33)
assert (bare.swing, bare.guard, bare.hp_cap) == (6, 4, 20)
ok("weapon, armour and charm each change a stat that the engine reads")

# picking a charm up mends you by what it adds
hurt = E.replace(bare, hp=10)
room_with_loot = E.replace(r1, entities={**r1.entities, "a1b2c3":
                           E.replace(r1.entities["a1b2c3"], hp=10)})
spot = None
for d, (dx, dy) in E.DIRECTIONS.items():
    p = (r1.entities["a1b2c3"].x + dx, r1.entities["a1b2c3"].y + dy)
    if room_with_loot.passable(*p) and room_with_loot.at(*p) is None:
        spot, way = p, d
        break
assert spot, "needed one open square next to the player"
room_with_loot = E.replace(room_with_loot,
                           floor={spot: (Item("charm", "Salt Bead", "rare", 10),)})
after, evs = step(room_with_loot, {"a1b2c3": f"m:{way}"})
me = after.entities["a1b2c3"]
assert me.hp_cap == 30 and me.hp == 20, (me.hp, me.hp_cap)
assert any(e.kind == "mend" for e in evs)
ok("a charm raises the ceiling and mends you by the difference on pickup")

# and none of that escapes the hash
assert state_hash(after) != state_hash(step(room_with_loot, {"a1b2c3": "w"})[0])
ok("carried items are part of the state, so a fabricated pack is divergence")


# ---------- coin buys health at a shrine ----------
shrine_room = next(build(f"shrine-{i}", PLAYERS) for i in range(50)
                   if build(f"shrine-{i}", PLAYERS).shrines)
sx, sy = sorted(shrine_room.shrines)[0]
who = sorted(shrine_room.entities)[0]
staged = E.replace(shrine_room, entities={
    **shrine_room.entities,
    who: E.replace(shrine_room.entities[who], x=sx, y=sy + 1, hp=8,
                   pack=(Item("coin", "coins", "plain", 30),))})
healed, evs = step(staged, {who: "m:n"})
paid = healed.entities[who]
assert paid.hp == paid.hp_cap and paid.coins == 30 - (paid.hp_cap - 8) * E.COIN_PER_HP
assert any(e.kind == "shrine" for e in evs)
assert (sx, sy) not in healed.shrines, "a shrine is spent when used"
ok(f"a shrine turns coin into health at {E.COIN_PER_HP} for one, and is spent")

# no coin, no mending, and the shrine is left standing
broke = E.replace(staged, entities={**staged.entities,
                  who: E.replace(staged.entities[who], pack=())})
after_broke, _ = step(broke, {who: "m:n"})
assert after_broke.entities[who].hp == 8
assert (sx, sy) in after_broke.shrines
ok("a shrine does nothing for someone with no coin, and stays for whoever has")

assert state_hash(healed) != state_hash(after_broke)
ok("shrines and coin are inside the state hash")


# ---------- setting things down ----------
holder = sorted(r1.entities)[0]
armed = E.replace(r1, entities={**r1.entities, holder: E.replace(
    r1.entities[holder],
    pack=(Item("weapon", "Old Fang", "plain", 6),
          Item("weapon", "Red Edge", "named", 12),
          Item("coin", "coins", "plain", 20)))})
spare = armed.entities[holder].spare()
assert spare.name == "Old Fang", spare.name
ok("the spare is the blade that does nothing for you, not the best one")

put_down, evs = step(armed, {holder: "d"})
me = put_down.entities[holder]
here = put_down.floor[(me.x, me.y)]
assert me.carried == 1 and me.coins == 20
assert any(i.name == "Old Fang" for i in here)
assert any(e.kind == "dropped" for e in evs)
ok("dropping costs the turn and leaves the item where you stand, for anyone")

# a full pack takes what fits and leaves the rest lying there
stuffed = tuple(Item("charm", f"Bead {n}", "plain", 4) for n in range(E.PACK_LIMIT))
spot = None
for d, (dx, dy) in E.DIRECTIONS.items():
    p = (r1.entities[holder].x + dx, r1.entities[holder].y + dy)
    if r1.passable(*p) and r1.at(*p) is None:
        spot, way = p, d
        break
loaded = E.replace(r1, entities={**r1.entities,
                   holder: E.replace(r1.entities[holder], pack=stuffed)},
                   floor={spot: (Item("weapon", "Bright Hook", "rare", 10),
                                 Item("coin", "coins", "plain", 9))})
after_full, evs = step(loaded, {holder: f"m:{way}"})
carrier = after_full.entities[holder]
assert carrier.carried == E.PACK_LIMIT, carrier.carried
assert carrier.coins == 9, "coin always fits; it collapses to one entry"
assert any(i.name == "Bright Hook" for i in after_full.floor.get(spot, ()))
assert any(e.kind == "full" for e in evs)
ok("a full pack takes the coin, leaves the item on the floor, and says so")


# ---------- the room asks how many of you there are ----------
for size in (1, 2, 3, 4):
    party_of = [(f"p{i:05x}", f"player{i}") for i in range(size)]
    sized = build("scaling", party_of)
    assert len(sized.living("monster")) == size, (size, len(sized.living("monster")))
ok("a room is built with one monster per person, so a lone delve is a delve")

solo = build("scaling", [("aaa111", "hank")])
trio = build("scaling", [("aaa111", "hank"), ("bbb222", "dave"), ("ccc333", "mira")])
assert state_hash(solo) != state_hash(trio)
ok("party size is part of the room, and so part of its hash")


# ---------- reach, and choosing what to strike with ----------
lane_room = build("reach-test", PLAYERS)
shooter = sorted(lane_room.entities)[0]
me0 = lane_room.entities[shooter]
lane = [(me0.x + i, me0.y) for i in range(1, 5)]
clear_walls = frozenset(set(lane_room.walls) - set(lane))
others = {p: E.replace(lane_room.entities[p], x=1, y=lane_room.height - 2)
          for p, _ in PLAYERS if p != shooter}
far = E.Entity("m9", "monster", "ogre 9", me0.x + 4, me0.y,
               hp=40, max_hp=40, attack=7, armour=0)
staged = E.replace(lane_room, walls=clear_walls,
                   entities={shooter: me0, **others, "m9": far})

def strike(pack):
    room = E.replace(staged, entities={**staged.entities,
                     shooter: E.replace(me0, pack=pack)})
    return step(room, {shooter: "a:e"})[1]

assert not any(e.kind == "hit" for e in strike(()))
assert not any(e.kind == "hit" for e in strike((Item("weapon", "Red Edge", "named", 12),)))
assert any(e.kind == "hit" for e in strike((Item("weapon", "Cold Bow", "named", 12),)))
ok("a blade cannot reach four squares and a bow can, with no change to the action")

# a rod reaches three, so not four
assert not any(e.kind == "hit" for e in strike((Item("weapon", "Salt Stave", "named", 12),)))
ok("a rod reaches three squares, a bow four")

# carrying both, the right one is used at each distance without any choosing
both = (Item("weapon", "Red Edge", "named", 12), Item("weapon", "Cold Bow", "fine", 8))
far_hits = [e for e in strike(both) if e.kind == "hit"]
assert far_hits and far_hits[0].amount <= Item("weapon", "Cold Bow", "fine", 8).damage
near = E.replace(staged, entities={**staged.entities,
                 "m9": E.replace(far, x=me0.x + 1),
                 shooter: E.replace(me0, pack=both)})
near_events = [e for e in step(near, {shooter: "a:e"})[1] if e.kind == "hit"]
assert near_events, "adjacent should connect"
ok("the weapon that reaches is the weapon used, and the harder one when both do")

# armour turns a blade or an arrow, never a rod
armoured = E.replace(far, armour=20, hp=200)
def versus(pack):
    room = E.replace(staged, entities={**staged.entities, "m9": E.replace(armoured, x=me0.x + 2),
                     shooter: E.replace(me0, pack=pack)})
    return [e.kind for e in step(room, {shooter: "a:e"})[1] if e.kind in ("hit", "miss")]
assert versus((Item("weapon", "Cold Bow", "named", 12),))[0] == "miss"
assert versus((Item("weapon", "Salt Stave", "named", 12),))[0] == "hit"
ok("armour turns arrows but never a rod, which is the whole reason a rod exists")

# and a friend in the line is a friend you do not shoot
blocked = E.replace(staged, entities={
    **staged.entities,
    "d4e5f6": E.replace(staged.entities["d4e5f6"], x=me0.x + 2, y=me0.y),
    shooter: E.replace(me0, pack=(Item("weapon", "Cold Bow", "named", 12),))})
evs = step(blocked, {shooter: "a:e"})[1]
assert any(e.kind == "held" for e in evs), [e.kind for e in evs]
assert not any(e.kind == "hit" for e in evs)
ok("a friend standing in the line stops the shot, so where you stand matters")


# ---------- what a delve leaves behind ----------
import tempfile, os
from catacomms import records as R
from loraline.crypto import Identity, Keyring

people = [Identity() for _ in range(3)]
rosters = {p.address: n for p, n in zip(people, ("hank", "dave", "mira"))}
rings = {}
for me in people:
    kr = Keyring(me, "shared")
    for other in people:
        kr.learn(other.address, other.public_b64, other.verify_b64)
    rings[me.address] = kr

finished = build("record-room", sorted(rosters.items()))
loot = Item("weapon", "Ashen Fang", "named", 12)
first = sorted(rosters)[0]
finished = E.replace(finished, entities={
    **finished.entities,
    first: E.replace(finished.entities[first], pack=(loot, Item("coin", "coins", "plain", 30))),
    **{m: E.replace(finished.entities[m], hp=0) for m in finished.entities
       if finished.entities[m].kind == "monster"}})

rec = R.build(finished, rosters, {a: True for a in rosters})
assert rec.outcome == "cleared"
for me in people:
    rec.sign(me)
assert rec.witnesses(rings[first]) == sorted(rosters)
ok(f"a finished delve makes a record every witness signs ({rec.id})")

# tampering after the fact breaks every signature at once
tampered = R.Record.from_json(rec.to_json())
tampered.carried[first][0]["tier"] = "named"
tampered.carried[first][0]["value"] = 99
assert tampered.witnesses(rings[first]) == []
ok("editing what you carried out invalidates every signature on the record")

# a signature from somebody who was not there is refused
outsider = Identity()
rings[first].learn(outsider.address, outsider.public_b64, outsider.verify_b64)
assert not rec.accept(outsider.address, outsider.sign(rec.canonical()), rings[first])
ok("a signature from somebody not on the roster is refused")

# you cannot witness your own loot
alone = {people[0].address: "hank"}
solo_room = build("solo-room", sorted(alone.items()))
solo_room = E.replace(solo_room, entities={
    **solo_room.entities,
    people[0].address: E.replace(solo_room.entities[people[0].address], pack=(loot,)),
    **{m: E.replace(solo_room.entities[m], hp=0) for m in solo_room.entities
       if solo_room.entities[m].kind == "monster"}})
solo = R.build(solo_room, alone, {people[0].address: True})
solo.sign(people[0])
assert solo.witnesses(rings[first]) == [people[0].address]
assert not solo.attested(rings[first], people[0].address)
ok("a delve alone is signed only by you, and mints nothing")

# and neither does one played entirely over sockets
socketed = R.build(finished, rosters, {a: False for a in rosters})
for me in people:
    socketed.sign(me)
assert socketed.witnesses(rings[first]) == sorted(rosters)
assert not socketed.attested(rings[first], first)
ok("a delve nobody was heard on air for is witnessed, and still mints nothing")

# a mixed party: those who were on air earn, the one on a socket does not
third = sorted(rosters)[2]
mixed = R.build(finished, rosters, {a: (a != third) for a in rosters})
for me in people:
    mixed.sign(me)
assert mixed.attested(rings[first], first)
assert not mixed.attested(rings[first], third)
ok("in a mixed party, being somewhere is what earns, per person")

# the stash is what survives all of that
with tempfile.TemporaryDirectory() as tmp:
    R.save(rec, tmp); R.save(solo, tmp); R.save(socketed, tmp)
    held = R.stash(first, rings[first], tmp)
    assert [i["name"] for i, _ in held] == ["Ashen Fang"], held
    assert R.purse(first, rings[first], tmp) == 30
    assert R.stash(people[0].address, rings[people[0].address], tmp) == [] \
        or people[0].address == first
ok("the stash counts only what somebody else was there to see")


# ---------- a whole delve, signed by everyone who was there ----------
with tempfile.TemporaryDirectory() as tmp:
    ids = {p.address: p for p in people}
    names2 = {p.address: n for p, n in zip(people, ("hank", "dave", "mira"))}
    parties = {}
    for p in people:
        party = Party(p.address, names2[p.address])
        party.identity, party.keyring, party.store = p, rings[p.address], tmp
        party.on_air = lambda a: True          # pretend everyone was heard
        parties[p.address] = party

    def hand_round():
        out = []
        for src, party in list(parties.items()):
            for payload in party.drain():
                for dst, other in parties.items():
                    if dst != src:
                        out += other.on_payload(src, payload, lambda a: names2.get(a, a))
        return out

    caller = sorted(parties)[0]
    hand_round()
    parties[caller].call("evening", names2[caller]); hand_round()
    for a, party in parties.items():
        if a != caller:
            party.accept()
    hand_round()
    parties[caller].begin(); hand_round()

    import itertools
    picks = {a: itertools.cycle(["a:n", "a:e", "m:e", "a:s", "m:s", "a:w", "w"])
             for a in parties}
    for _ in range(400):
        for a, party in parties.items():
            t = party.table
            if t and not t.room.over and not t.has_acted() and t._alive(a):
                party.submit(next(picks[a]))
        for party in parties.values():
            party.tick()
        hand_round()
        if all(p.table and p.table.room.over for p in parties.values()):
            break
    hand_round(); hand_round()

    made = {a: p.record for a, p in parties.items()}
    assert all(r is not None for r in made.values()), "every machine writes one"
    assert len({r.id for r in made.values()}) == 1, "and they agree on what happened"
    signed = made[caller].witnesses(rings[caller])
    assert signed == sorted(names2), signed
    ok(f"a played-out delve is recorded identically by all three and signed by all three")

    on_disk = R.load_all(tmp)
    assert any(r.id == made[caller].id for r in on_disk)
    ok(f"and it is on disk afterwards ({len(on_disk)} record(s) kept)")


# ---------- a signature arriving before you have closed out ----------
with tempfile.TemporaryDirectory() as tmp2:
    early_party = Party(people[1].address, "dave")
    early_party.identity, early_party.keyring, early_party.store = \
        people[1], rings[people[1].address], tmp2
    early_party.on_air = lambda a: True
    early_party.roster = dict(names2)
    early_party.state = "playing"
    ended = E.replace(finished, seed="early-room")
    from catacomms.table import Table
    early_party.table = Table(ended, people[1].address, dict(names2))

    ahead = R.build(ended, names2, {a: True for a in names2})
    theirs = ahead.sign(people[0])
    # their signature turns up first
    early_party.on_payload(people[0].address, f"g|{ahead.id}|{theirs}",
                           lambda a: names2.get(a, a))
    assert early_party.record is None and early_party.early
    early_party.close_out()
    assert early_party.record is not None
    assert people[0].address in early_party.record.witnesses(rings[people[1].address])
ok("a signature that lands before you have finished is kept, not lost")


# ---------- application frames are never retransmitted by loraline ----------
# so a dropped input must not be able to freeze a tick for ever
with tempfile.TemporaryDirectory() as tmp3:
    two = {people[0].address: "hank", people[1].address: "dave"}
    lossy = {}
    for p in people[:2]:
        party = Party(p.address, two[p.address])
        party.identity, party.keyring, party.store = p, rings[p.address], tmp3
        party.on_air = lambda a: True
        lossy[p.address] = party

    dropped = {"n": 0}
    def hand_round_lossy(drop_every=0):
        for src, party in list(lossy.items()):
            for payload in party.drain():
                dropped["n"] += 1
                if drop_every and dropped["n"] % drop_every == 0:
                    continue                      # lost on the air
                for dst, other in lossy.items():
                    if dst != src:
                        other.on_payload(src, payload, lambda a: two.get(a, a))

    first2 = sorted(lossy)[0]
    hand_round_lossy()
    lossy[first2].call("lossy", two[first2]); hand_round_lossy()
    for a, party in lossy.items():
        if a != first2:
            party.accept()
    hand_round_lossy()
    lossy[first2].begin(); hand_round_lossy()
    assert all(p.table for p in lossy.values()), "the handshake itself must survive"

    clock = [0.0]
    for _ in range(600):
        clock[0] += 2.0
        for a, party in lossy.items():
            t = party.table
            if t and not t.room.over and not t.has_acted() and t._alive(a):
                party.submit("a:e")
        for party in lossy.values():
            party.tick()
            party.resend(clock[0])
        hand_round_lossy(drop_every=3)
        if all(p.table.room.over for p in lossy.values()):
            break

    assert all(p.table.room.over for p in lossy.values()), \
        "a dropped input must not freeze a tick for ever"
    assert len({state_hash(p.table.room) for p in lossy.values()}) == 1
ok(f"a delve finishes over a link dropping 1 frame in 3, because inputs are resent")


# ---------- actions take time ----------
from catacomms.engine import build_camp, ACTION_UNITS, BASE_LIGHT, TORCH_TICKS

camp = build_camp("grove", PLAYERS)
assert camp.kind == "camp"
assert not camp.living("monster")
assert [e for e in camp.entities.values() if e.kind == "tree"]
ok(f"a camp has trees and nothing that bites "
   f"({len([e for e in camp.entities.values() if e.kind=='tree'])} of them)")

# walk somebody next to a tree, then cut
cutter = sorted(p for p, _ in PLAYERS)[0]
def beside_tree(room, pid):
    me = room.entities[pid]
    for d, (dx, dy) in E.DIRECTIONS.items():
        t = room.at(me.x + dx, me.y + dy)
        if t is not None and t.kind == "tree":
            return d
    return None

# Put the cutter beside a tree rather than pathing there; this is a test about
# chopping, and walking is already covered elsewhere.
tree = next(e for e in camp.entities.values() if e.kind == "tree")
spot = next(((tree.x + dx, tree.y + dy, d) for d, (dx, dy) in E.DIRECTIONS.items()
             if camp.passable(tree.x + dx, tree.y + dy)
             and camp.at(tree.x + dx, tree.y + dy) is None), None)
assert spot, "expected an open square beside a tree"
sx, sy, from_dir = spot
way = {"n": "s", "s": "n", "e": "w", "w": "e"}[from_dir]
walked = E.replace(camp, entities={
    **camp.entities,
    cutter: E.replace(camp.entities[cutter], x=sx, y=sy)})
assert beside_tree(walked, cutter) == way

started = walked.tick
chopping, evs = step(walked, {cutter: f"c:{way}",
                              **{p: "w" for p, _ in PLAYERS if p != cutter}})
worker = chopping.entities[cutter]
assert any(e.kind == "working" for e in evs)
assert worker.doing.startswith("chop") and not worker.due(started)
ok(f"a chop takes {ACTION_UNITS['chop']} ticks and the world stops waiting on you")

# everybody else waiting means the world jumps rather than grinding a tick at a time
assert chopping.tick > started + 1, chopping.tick
ok(f"with everyone else idle the world jumps {chopping.tick - started} ticks "
   f"on one frame each, instead of {ACTION_UNITS['chop']} frames apiece")

done, evs = step(chopping, {p: "w" for p, _ in PLAYERS if p != cutter})
assert any(e.kind == "chopped" for e in evs), [e.kind for e in evs]
assert any(i.kind == "wood" for i in done.entities[cutter].pack)
ok("and at the end of it there is a log in your pack")

# a chop with nothing to cut is refused rather than silently eaten
nothing, evs = step(walked, {cutter: "c:" + ("n" if way != "n" else "s")})
assert any(e.kind == "blocked" for e in evs) or not nothing.entities[cutter].doing
ok("chopping thin air is refused")

# ---------- depth and light ----------
shallow, deep = build("x", PLAYERS, depth=1), build("x", PLAYERS, depth=4)
assert len(deep.living("monster")) > len(shallow.living("monster"))
assert deep.width > shallow.width
assert state_hash(shallow) != state_hash(deep)
ok(f"depth four is bigger and busier than depth one "
   f"({shallow.width}x{shallow.height} and {len(shallow.living('monster'))} "
   f"against {deep.width}x{deep.height} and {len(deep.living('monster'))})")

dark, lit = build("y", PLAYERS, torches=0), build("y", PLAYERS, torches=3)
assert dark.limit == BASE_LIGHT and lit.limit == BASE_LIGHT + 3 * TORCH_TICKS
assert state_hash(dark) != state_hash(lit)
ok(f"torches are light: {dark.limit} turns bare, {lit.limit} carrying three, "
   f"and the count is inside the hash")

print("\nALL PASS")
