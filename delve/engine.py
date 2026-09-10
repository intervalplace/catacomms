"""A turn-locked dungeon room. Pure deterministic state machine.

    step(room, inputs, ) -> (room, events)

No clock, no floats, no ambient randomness. Every machine given the same room
and the same inputs computes the same next room, byte for byte, which is the
only reason players separated by a lossy radio link can share a world at all.

Three rules keep it that way, and breaking any one of them is how divergence
gets in:

  * Integer arithmetic only. Floats round differently across platforms.
  * Every iteration over entities is in sorted order. Dictionary order is not
    a specification.
  * Randomness comes from a generator seeded by (room seed, tick), drawn in a
    fixed order. Nobody rolls dice out of turn.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace

MASK64 = (1 << 64) - 1

DIRECTIONS = {"n": (0, -1), "s": (0, 1), "e": (1, 0), "w": (-1, 0)}


# --------------------------------------------------------------------------
# Deterministic randomness
# --------------------------------------------------------------------------

class Rng:
    """SplitMix64. Written out rather than imported so that the sequence is
    part of this file, and therefore part of the rules."""

    def __init__(self, seed: int) -> None:
        self.state = seed & MASK64

    def next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        return (z ^ (z >> 31)) & MASK64

    def below(self, n: int) -> int:
        """Uniform in [0, n). Rejection sampled, so no modulo bias."""
        if n <= 1:
            return 0
        limit = MASK64 - (MASK64 % n)
        while True:
            value = self.next()
            if value <= limit:
                return value % n

    def roll(self, sides: int) -> int:
        return self.below(sides) + 1


def seed_for(room_seed: str, tick: int) -> int:
    digest = hashlib.blake2b(f"{room_seed}|{tick}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

# Loot is generated from the room seed at build time, never rolled on death.
# That keeps it free: nobody transmits an item, because every machine already
# derives the same one. What costs is deciding whether to go and get it.
EDGE = ["Ashen", "Pitted", "Bright", "Cold", "Crooked", "Hollow", "Old",
        "Quiet", "Red", "Salt", "Thin", "Worn", "Black", "Green"]
BLADE = ["Fang", "Sliver", "Tooth", "Edge", "Splinter", "Hook", "Needle"]
PLATE = ["Mail", "Coat", "Hide", "Scale", "Shell", "Guard", "Weave"]
TRINKET = ["Coin", "Knot", "Feather", "Ember", "Seed", "Bead", "Thread"]

TIERS = ("plain", "fine", "rare", "named")
TIER_MARK = {"plain": " ", "fine": "+", "rare": "*", "named": "!"}


@dataclass(frozen=True)
class Item:
    """One thing, doing one clear thing.

    No affix soup. A delve is fourteen turns and you will find one or two
    things, so each has to be legible at a glance. Texture comes from the
    name, which is generated, and the name is what survives the evening.
    """
    kind: str            # "weapon" | "armour" | "charm" | "coin"
    name: str
    tier: str
    value: int

    @property
    def label(self) -> str:
        mark = TIER_MARK[self.tier]
        return f"{self.name}{mark}".strip() if mark != " " else self.name

    @property
    def glyph(self) -> str:
        return {"weapon": "/", "armour": "]", "charm": "o", "coin": "$"}[self.kind]


def roll_item(rng: "Rng", depth: int = 0) -> Item:
    """Tier first, then kind, then name. Fixed draw order, always."""
    pick = rng.below(100) + depth
    tier = ("named" if pick >= 96 else "rare" if pick >= 84
            else "fine" if pick >= 55 else "plain")
    step_up = TIERS.index(tier)
    kind = ("coin" if rng.below(100) < 30 else
            ["weapon", "armour", "charm"][rng.below(3)])

    if kind == "coin":
        return Item("coin", "coins", "plain", rng.roll(8) + step_up * 6)
    noun = {"weapon": BLADE, "armour": PLATE, "charm": TRINKET}[kind]
    name = f"{EDGE[rng.below(len(EDGE))]} {noun[rng.below(len(noun))]}"
    if kind == "weapon":
        return Item(kind, name, tier, 6 + step_up * 2)
    if kind == "armour":
        return Item(kind, name, tier, 4 + step_up * 2)
    return Item(kind, name, tier, 4 + step_up * 3)


@dataclass(frozen=True)
class Entity:
    id: str
    kind: str            # "player" or "monster"
    name: str
    x: int
    y: int
    hp: int
    max_hp: int
    attack: int          # damage is 1..attack
    armour: int = 0      # chance to be missed, out of 20
    pack: tuple = ()     # what you are carrying; best of each is what you use
    drop: tuple = ()     # what falls when this dies

    @property
    def alive(self) -> bool:
        return self.hp > 0

    def best(self, kind: str) -> int:
        """You fight with the best thing you are carrying. No equip turn: at
        seventeen seconds a turn, a menu is a worse game."""
        held = [i.value for i in self.pack if i.kind == kind]
        return max(held) if held else 0

    @property
    def swing(self) -> int:
        return max(self.attack, self.best("weapon"))

    @property
    def guard(self) -> int:
        return max(self.armour, self.best("armour"))

    @property
    def coins(self) -> int:
        return sum(i.value for i in self.pack if i.kind == "coin")


@dataclass(frozen=True)
class Room:
    seed: str
    width: int
    height: int
    walls: frozenset
    entities: dict           # id -> Entity, always read in sorted order
    floor: dict = field(default_factory=dict)   # (x, y) -> tuple of Items
    tick: int = 0
    limit: int = 60          # turns before the party has to leave
    friendly_fire: bool = False
    log: tuple = ()

    def at(self, x: int, y: int):
        for eid in sorted(self.entities):
            entity = self.entities[eid]
            if entity.alive and entity.x == x and entity.y == y:
                return entity
        return None

    def passable(self, x: int, y: int) -> bool:
        return (0 <= x < self.width and 0 <= y < self.height
                and (x, y) not in self.walls)

    def living(self, kind: str) -> list:
        return [self.entities[i] for i in sorted(self.entities)
                if self.entities[i].kind == kind and self.entities[i].alive]

    @property
    def over(self) -> bool:
        """A delve ends when the party is dead, when the torches run out, or
        when the room is quiet and everything has been picked up.

        Not the instant the last monster falls: gathering what you killed for
        is part of the delve, and the turn limit is what keeps it honest."""
        if not self.living("player") or self.tick >= self.limit:
            return True
        return not self.living("monster") and not self.floor

    @property
    def won(self) -> bool:
        return not self.living("monster") and bool(self.living("player"))

    @property
    def sweeping(self) -> bool:
        """Monsters down, loot still on the floor, torches still burning."""
        return (not self.living("monster") and bool(self.floor)
                and bool(self.living("player")) and self.tick < self.limit)

    @property
    def outcome(self) -> str:
        """Three ways a delve ends, and the third one is why there is a limit.

        Two entities can block each other in a corridor and both wait, which
        would otherwise hang the room forever. Rather than teach the monsters
        to path around, the room simply runs out: torches burn down, and the
        party leaves with whatever they are still carrying."""
        if self.won:
            return "cleared"
        if not self.living("player"):
            return "wiped"
        return "withdrew"


@dataclass
class Event:
    kind: str            # move | hit | miss | death | end
    actor: str = ""
    target: str = ""
    amount: int = 0
    text: str = ""


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

def parse_action(text: str) -> tuple:
    """Wire form is deliberately tiny: "m:n", "a:e", "w".

    Airtime is the scarce resource, and an action is the thing every player
    sends every turn.
    """
    text = (text or "").strip().lower()
    if text.startswith("m:") and text[2:3] in DIRECTIONS:
        return ("move", text[2])
    if text.startswith("a:") and text[2:3] in DIRECTIONS:
        return ("attack", text[2])
    return ("wait", "")


def encode_action(verb: str, direction: str = "") -> str:
    if verb == "move":
        return f"m:{direction}"
    if verb == "attack":
        return f"a:{direction}"
    return "w"


# --------------------------------------------------------------------------
# The transition
# --------------------------------------------------------------------------

def step(room: Room, inputs: dict) -> tuple:
    """Advance one tick. `inputs` maps entity id to a wire action string.

    Players resolve in sorted id order, then monsters. Fixed order matters as
    much as the rules themselves: if two players move into the same square,
    who gets there has to be decided the same way on every machine.
    """
    if room.over:
        return room, []

    rng = Rng(seed_for(room.seed, room.tick))
    entities = dict(room.entities)
    floor = dict(room.floor)
    events: list = []

    def current(eid: str) -> Entity:
        return entities[eid]

    def resolve(actor_id: str, action: tuple) -> None:
        actor = current(actor_id)
        if not actor.alive:
            return
        verb, direction = action
        if verb == "wait":
            return

        dx, dy = DIRECTIONS[direction]
        tx, ty = actor.x + dx, actor.y + dy

        if verb == "move":
            if not _passable(tx, ty):
                # A blocked move still costs the turn. That is a rule, so it
                # gets an event rather than silence.
                events.append(Event("blocked", actor_id,
                                    text=f"{actor.name} cannot go {direction}"))
                return
            picked = floor.pop((tx, ty), ())
            moved = replace(actor, x=tx, y=ty, pack=actor.pack + picked)
            entities[actor_id] = moved
            events.append(Event("move", actor_id,
                                text=f"{actor.name} moves {direction}"))
            for item in picked:
                events.append(Event("pickup", actor_id, item.name,
                                    item.value,
                                    f"{actor.name} picks up {item.label}"))
            return

        victim = _at(tx, ty)
        if victim is None:
            events.append(Event("miss", actor_id,
                                text=f"{actor.name} swings at nothing"))
            return
        # A party is three friends taking a turn every seventeen seconds.
        # Hitting each other by accident is frustration with no decision
        # attached, so the swing is held. It is a room rule rather than a
        # law of the world, in case somewhere wants it otherwise.
        if not room.friendly_fire and victim.kind == actor.kind:
            events.append(Event("held", actor_id, victim.id,
                                text=f"{actor.name} holds the swing"))
            return
        # Armour is a chance to be missed, not damage subtracted from the blow.
        if rng.roll(20) <= victim.guard:
            events.append(Event("miss", actor_id, victim.id,
                                text=f"{actor.name} misses {victim.name}"))
            return
        damage = rng.roll(actor.swing)
        hurt = replace(victim, hp=max(0, victim.hp - damage))
        entities[victim.id] = hurt
        events.append(Event("hit", actor_id, victim.id, damage,
                            f"{actor.name} hits {victim.name} for {damage}"))
        if not hurt.alive:
            events.append(Event("death", actor_id, victim.id,
                                text=f"{victim.name} falls"))
            # Everything it carried, and everything it was carrying for you.
            spoils = tuple(hurt.drop) + tuple(hurt.pack)
            if spoils:
                floor[(hurt.x, hurt.y)] = floor.get((hurt.x, hurt.y), ()) + spoils
                entities[hurt.id] = replace(hurt, pack=())
                events.append(Event("drop", victim.id,
                                    text=f"{hurt.name} drops "
                                         f"{', '.join(i.label for i in spoils)}"))

    def _passable(x: int, y: int) -> bool:
        return room.passable(x, y) and _at(x, y) is None

    def _at(x: int, y: int):
        for eid in sorted(entities):
            entity = entities[eid]
            if entity.alive and entity.x == x and entity.y == y:
                return entity
        return None

    for eid in sorted(entities):
        if entities[eid].kind == "player" and entities[eid].alive:
            resolve(eid, parse_action(inputs.get(eid, "w")))

    for eid in sorted(entities):
        monster = entities[eid]
        if monster.kind != "monster" or not monster.alive:
            continue
        resolve(eid, _monster_action(monster, entities, _passable, _at))

    tick = room.tick + 1
    advanced = replace(room, entities=entities, floor=floor, tick=tick,
                       log=room.log + tuple(e.text for e in events))
    if advanced.over:
        outcome = {
            "cleared": "The room is clear.",
            "wiped": "The party has fallen.",
            "withdrew": "The torches burn low. The party withdraws with what "
                        "they have.",
        }[advanced.outcome]
        events.append(Event("end", text=outcome))
        advanced = replace(advanced, log=advanced.log + (outcome,))
    return advanced, events


def _monster_action(monster: Entity, entities: dict, passable, at) -> tuple:
    """Walk toward the nearest living player, hit them when adjacent.

    Ties break on id, never on iteration order, so every machine picks the
    same target.
    """
    targets = [entities[i] for i in sorted(entities)
               if entities[i].kind == "player" and entities[i].alive]
    if not targets:
        return ("wait", "")

    def distance(t: Entity) -> tuple:
        return (abs(t.x - monster.x) + abs(t.y - monster.y), t.id)

    target = min(targets, key=distance)
    dx, dy = target.x - monster.x, target.y - monster.y

    if abs(dx) + abs(dy) == 1:
        return ("attack", "e" if dx > 0 else "w" if dx < 0 else
                          "s" if dy > 0 else "n")

    # Prefer the longer axis; on a tie, prefer horizontal. Arbitrary, but
    # written down, which is what makes it a rule rather than a coincidence.
    order = []
    if abs(dx) >= abs(dy):
        order = [("e" if dx > 0 else "w"), ("s" if dy > 0 else "n")]
    else:
        order = [("s" if dy > 0 else "n"), ("e" if dx > 0 else "w")]
    for direction in order:
        if dx == 0 and direction in ("e", "w"):
            continue
        if dy == 0 and direction in ("n", "s"):
            continue
        ddx, ddy = DIRECTIONS[direction]
        if passable(monster.x + ddx, monster.y + ddy):
            return ("move", direction)
    return ("wait", "")


# --------------------------------------------------------------------------
# Identity of a state
# --------------------------------------------------------------------------

def canonical(room: Room) -> bytes:
    """A byte string that depends on everything the rules depend on, and on
    nothing else. The log is excluded: it is a rendering of history, not part
    of the state, and two players may hold different amounts of it."""
    parts = [f"{room.seed}|{room.width}x{room.height}|{room.tick}/{room.limit}"
             f"|ff{int(room.friendly_fire)}"]
    parts.append(",".join(f"{x}.{y}" for x, y in sorted(room.walls)))
    for eid in sorted(room.entities):
        e = room.entities[eid]
        parts.append(f"{e.id}:{e.kind}:{e.x}:{e.y}:{e.hp}:{e.max_hp}:"
                     f"{e.attack}:{e.armour}:{_items(e.pack)}:{_items(e.drop)}")
    for spot in sorted(room.floor):
        parts.append(f"@{spot[0]}.{spot[1]}:{_items(room.floor[spot])}")
    return "|".join(parts).encode("utf-8")


def _items(items) -> str:
    return ";".join(f"{i.kind},{i.name},{i.tier},{i.value}" for i in items)


def state_hash(room: Room) -> str:
    """Eight bytes, published every tick. Enough to notice divergence, cheap
    enough to send on a radio that is already paying for the inputs."""
    return hashlib.blake2b(canonical(room), digest_size=8).hexdigest()


# --------------------------------------------------------------------------
# Building a room
# --------------------------------------------------------------------------

MONSTERS = [
    ("rat", 6, 3, 2),
    ("goblin", 10, 4, 5),
    ("hound", 12, 5, 4),
    ("ogre", 20, 7, 8),
]


def reachable_from(width: int, height: int, walls: set, start: tuple) -> set:
    """Flood fill. Pure and deterministic: the frontier is a queue, and the
    neighbours are visited in a fixed order."""
    seen = {start}
    frontier = [start]
    while frontier:
        x, y = frontier.pop(0)
        for dx, dy in ((0, -1), (0, 1), (1, 0), (-1, 0)):
            spot = (x + dx, y + dy)
            if spot in seen:
                continue
            if 0 <= spot[0] < width and 0 <= spot[1] < height and spot not in walls:
                seen.add(spot)
                frontier.append(spot)
    return seen


def build(seed: str, players: list, width: int = 11, height: int = 9,
          monster_count: int = 3) -> Room:
    """Generate a room from a seed. Same seed, same room, on every machine.

    `players` is a list of (id, name); ids are normally loraline addresses.

    Everything is placed inside one connected region. A monster walled off
    behind rubble can never be reached, and since the room only ends when the
    monsters are dead, that room would never end. Two percent of seeds did
    exactly that before this check existed.
    """
    rng = Rng(seed_for(seed, -1))
    walls = set()
    for x in range(width):
        walls.add((x, 0))
        walls.add((x, height - 1))
    for y in range(height):
        walls.add((0, y))
        walls.add((width - 1, y))
    for _ in range((width * height) // 12):
        walls.add((rng.below(width - 2) + 1, rng.below(height - 2) + 1))

    entities: dict = {}
    open_floor = [(x, y) for y in range(1, height - 1)
                  for x in range(1, width - 1) if (x, y) not in walls]
    if not open_floor:
        open_floor = [(1, 1)]
        walls.discard((1, 1))

    # Keep only the largest connected region, so nothing can be sealed off.
    regions: list = []
    unassigned = set(open_floor)
    while unassigned:
        start = min(unassigned)
        region = reachable_from(width, height, walls, start) & set(open_floor)
        regions.append(sorted(region))
        unassigned -= region
    free = max(regions, key=lambda r: (len(r), r[0]))

    for index, (pid, name) in enumerate(sorted(players)):
        spot = free.pop(0) if free else (1, 1)
        entities[pid] = Entity(pid, "player", name, spot[0], spot[1],
                               hp=20, max_hp=20, attack=6, armour=4)
        _ = index

    for index in range(monster_count):
        if not free:
            break
        spot = free.pop(rng.below(len(free)))
        kind, hp, attack, armour = MONSTERS[rng.below(len(MONSTERS))]
        mid = f"m{index}"
        # Tougher things carry better things. Decided here, once, from the
        # seed, so every machine already knows what is down there.
        drop = (roll_item(rng, depth=hp),) if rng.below(100) < 70 else ()
        entities[mid] = Entity(mid, "monster", f"{kind} {index + 1}",
                               spot[0], spot[1], hp=hp, max_hp=hp,
                               attack=attack, armour=armour, drop=drop)

    return Room(seed=seed, width=width, height=height,
                walls=frozenset(walls), entities=entities)
