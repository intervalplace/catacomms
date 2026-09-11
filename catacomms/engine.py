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
DRAWN = ["Bow", "Sling", "Cord", "Recurve"]
ROD = ["Rod", "Stave", "Wand", "Spark"]

# How far a weapon reaches, and how it behaves when it gets there. Derived
# from the noun rather than stored, so nothing new goes on the wire and the
# state hash is unchanged: every machine already agreed on the name.
#
#   blade  one square, full damage, can be turned by armour
#   drawn  four squares, less damage, can be turned by armour
#   rod    three squares, less again, but armour cannot turn it
#
# Armour here is a chance to be missed, so a rod is the answer to something
# well armoured and a blade is the answer to something in front of you.
STYLES = {"blade": (1, 0, True), "drawn": (4, -3, True), "rod": (3, -4, False)}


def weapon_style(name: str) -> str:
    noun = (name or "").split(" ")[-1]
    if noun in DRAWN:
        return "drawn"
    if noun in ROD:
        return "rod"
    return "blade"
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
        if self.kind == "weapon":
            return {"blade": "/", "drawn": ")", "rod": "|"}[self.style]
        return {"armour": "]", "charm": "o", "coin": "$"}[self.kind]

    @property
    def style(self) -> str:
        return weapon_style(self.name) if self.kind == "weapon" else ""

    @property
    def reach(self) -> int:
        return STYLES[self.style][0] if self.kind == "weapon" else 0

    @property
    def damage(self) -> int:
        """A bow that hit as hard as a sword would simply be a better sword."""
        if self.kind != "weapon":
            return 0
        return max(2, self.value + STYLES[self.style][1])

    @property
    def turnable(self) -> bool:
        return STYLES[self.style][2] if self.kind == "weapon" else True


# Thresholds on a roll out of a thousand. It was out of a hundred, with the
# monster's whole hit points added on, which meant an ogre's +20 vaulted the
# top threshold and named items came out commoner than rare ones. Finer
# granularity lets the depth bonus tilt the curve instead of leaping it.
TIER_GATES = ((985, "named"), (940, "rare"), (720, "fine"))


def roll_item(rng: "Rng", depth: int = 0) -> Item:
    """Tier first, then kind, then name. Fixed draw order, always."""
    pick = rng.below(1000) + depth
    tier = "plain"
    for gate, name in TIER_GATES:
        if pick >= gate:
            tier = name
            break
    step_up = TIERS.index(tier)
    kind = ("coin" if rng.below(100) < 30 else
            ["weapon", "armour", "charm"][rng.below(3)])

    if kind == "coin":
        return Item("coin", "coins", "plain", rng.roll(8) + step_up * 6)
    if kind == "weapon":
        # Two thirds of what you find is something to swing. A bow or a rod is
        # a find, not a default.
        pick = rng.below(100)
        noun = BLADE if pick < 62 else (DRAWN if pick < 82 else ROD)
    else:
        noun = {"armour": PLATE, "charm": TRINKET}[kind]
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
    busy_until: int = 0  # the tick at which a long action finishes
    doing: str = ""      # what is being done until then, e.g. "chop:n"

    def due(self, tick: int) -> bool:
        return tick >= self.busy_until

    @property
    def alive(self) -> bool:
        return self.hp > 0

    def best(self, kind: str) -> int:
        """You fight with the best thing you are carrying. No equip turn: at
        seventeen seconds a turn, a menu is a worse game."""
        held = [i.value for i in self.pack if i.kind == kind]
        return max(held) if held else 0

    def armoury(self) -> list:
        """Everything you could strike with, bare hands included, best first.

        There is no equipped weapon. You use whatever reaches, and if two
        things reach you use the one that hurts more, which is what a person
        would do and costs no turn to decide."""
        kit = [(self.attack, 1, True, "fists")]
        for item in self.pack:
            if item.kind == "weapon":
                kit.append((item.damage, item.reach, item.turnable, item.name))
        return sorted(kit, key=lambda k: (-k[0], k[1], k[3]))

    @property
    def swing(self) -> int:
        return max(k[0] for k in self.armoury())

    @property
    def reach(self) -> int:
        return max(k[1] for k in self.armoury())

    @property
    def guard(self) -> int:
        return max(self.armour, self.best("armour"))

    @property
    def vigour(self) -> int:
        """A charm keeps you on your feet. Passive, because using an item
        would cost a turn, and a menu at seventeen seconds a turn is a worse
        game than no menu at all."""
        return self.best("charm")

    @property
    def hp_cap(self) -> int:
        return self.max_hp + self.vigour

    @property
    def coins(self) -> int:
        return sum(i.value for i in self.pack if i.kind == "coin")

    @property
    def carried(self) -> int:
        return sum(1 for i in self.pack if i.kind != "coin")

    def spare(self):
        """The thing you would part with: the least useful item you hold.

        Anything that is not the best of its kind does nothing for you at all,
        so those go first, cheapest first. If everything you hold is your best
        of something, the cheapest of those goes. One rule, no menu; the point
        of dropping is to hand your old blade to whoever is behind you."""
        goods = [i for i in self.pack if i.kind != "coin"]
        if not goods:
            return None
        redundant = [i for i in goods if i.value < self.best(i.kind)]
        return min(redundant or goods, key=lambda i: (i.value, i.kind, i.name))

    def spend(self, amount: int) -> tuple:
        """Return a pack with `amount` of coin removed, collapsed into one
        entry so the state stays small and canonical."""
        keep = tuple(i for i in self.pack if i.kind != "coin")
        left = max(0, self.coins - amount)
        return keep + ((Item("coin", "coins", "plain", left),) if left else ())


@dataclass(frozen=True)
class Room:
    seed: str
    width: int
    height: int
    walls: frozenset
    entities: dict           # id -> Entity, always read in sorted order
    kind: str = "delve"                         # delve | camp
    floor: dict = field(default_factory=dict)   # (x, y) -> tuple of Items
    shrines: frozenset = frozenset()            # spent when used
    tick: int = 0
    depth: int = 1           # how far down; deeper is worse and richer
    limit: int = 20          # turns before the light gives out
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
        is part of the delve, and the turn limit is what keeps it honest.

        A camp has nothing to kill, so it ends when the light does or when
        everybody has said they are finished."""
        if self.tick >= self.limit:
            return True
        if self.kind == "camp":
            people = self.living("player")
            return bool(people) and all(p.doing == "done" for p in people)
        if not self.living("player"):
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

    def due_players(self) -> list:
        """Who the world is waiting on. Somebody twelve ticks into a chop is
        not waited for, which is the point of giving actions a length."""
        return [p for p in self.living("player") if p.due(self.tick)]

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
    """Wire form is deliberately tiny: "m:n", "a:e", "d", "w".

    Airtime is the scarce resource, and an action is the thing every player
    sends every turn.
    """
    text = (text or "").strip().lower()
    if text.startswith("m:") and text[2:3] in DIRECTIONS:
        return ("move", text[2])
    if text.startswith("a:") and text[2:3] in DIRECTIONS:
        return ("attack", text[2])
    if text == "d":
        return ("drop", "")
    if text.startswith("c:") and text[2:3] in DIRECTIONS:
        return ("chop", text[2])
    if text == "x":
        return ("leave", "")
    return ("wait", "")


def encode_action(verb: str, direction: str = "") -> str:
    if verb == "move":
        return f"m:{direction}"
    if verb == "attack":
        return f"a:{direction}"
    if verb == "drop":
        return "d"
    if verb == "chop":
        return f"c:{direction}"
    if verb == "leave":
        return "x"
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
    shrines = set(room.shrines)
    events: list = []

    def current(eid: str) -> Entity:
        return entities[eid]

    def resolve(actor_id: str, action: tuple) -> None:
        actor = current(actor_id)
        if not actor.alive:
            return
        verb, direction = action
        if verb == "chop":
            dx2, dy2 = DIRECTIONS[direction]
            tree = _at(actor.x + dx2, actor.y + dy2)
            if tree is None or tree.kind != "tree" or not tree.alive:
                events.append(Event("blocked", actor_id,
                                    text=f"{actor.name} finds nothing to cut there"))
                return
            entities[actor_id] = replace(
                actor, doing=f"chop:{direction}",
                busy_until=room.tick + ACTION_UNITS["chop"])
            events.append(Event("working", actor_id, tree.id,
                                ACTION_UNITS["chop"],
                                f"{actor.name} sets to work on the {tree.name}"))
            return

        if verb == "leave":
            entities[actor_id] = replace(actor, doing="done")
            events.append(Event("note", actor_id,
                                text=f"{actor.name} is done here"))
            return

        if verb == "drop":
            spare = actor.spare()
            if spare is None:
                events.append(Event("blocked", actor_id,
                                    text=f"{actor.name} has nothing to spare"))
                return
            rest = list(actor.pack)
            rest.remove(spare)
            entities[actor_id] = replace(actor, pack=tuple(rest))
            floor[(actor.x, actor.y)] = floor.get((actor.x, actor.y), ()) + (spare,)
            events.append(Event("dropped", actor_id, spare.name,
                                text=f"{actor.name} sets down {spare.label}"))
            return

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
            # Take what fits, leave the rest where it lies. No prompt: a
            # prompt costs a turn, and the floor remembers.
            offered = floor.get((tx, ty), ())
            room_left = PACK_LIMIT - actor.carried
            picked, left = [], []
            for item in offered:
                if item.kind == "coin" or room_left > 0:
                    picked.append(item)
                    if item.kind != "coin":
                        room_left -= 1
                else:
                    left.append(item)
            picked, left = tuple(picked), tuple(left)
            if left:
                floor[(tx, ty)] = left
            else:
                floor.pop((tx, ty), None)
            moved = replace(actor, x=tx, y=ty, pack=actor.pack + picked)
            # A better charm mends you by what it adds, so finding one in the
            # middle of a fight is worth the detour rather than worth noting.
            gained = moved.vigour - actor.vigour
            if gained > 0:
                moved = replace(moved, hp=min(moved.hp + gained, moved.hp_cap))

            # A shrine turns coin into health, at two for one, and is spent
            # when used. This is the whole point of coin: it is both what you
            # carry out and what keeps you alive long enough to carry it. No
            # prompt, because a prompt would cost a turn.
            mended = 0
            if (tx, ty) in shrines and moved.hp < moved.hp_cap and moved.coins:
                mended = min(moved.hp_cap - moved.hp, moved.coins // COIN_PER_HP)
                if mended:
                    moved = replace(moved, hp=moved.hp + mended,
                                    pack=moved.spend(mended * COIN_PER_HP))
                    shrines.discard((tx, ty))
            entities[actor_id] = moved
            events.append(Event("move", actor_id,
                                text=f"{actor.name} moves {direction}"))
            for item in picked:
                events.append(Event("pickup", actor_id, item.name,
                                    item.value,
                                    f"{actor.name} picks up {item.label}"))
            if left:
                events.append(Event("full", actor_id,
                                    text=f"{actor.name}'s pack is full; "
                                         f"{left[0].label} stays where it is"))
            if gained > 0:
                events.append(Event("mend", actor_id, amount=gained,
                                    text=f"{actor.name} is steadied "
                                         f"({moved.hp}/{moved.hp_cap})"))
            if left:
                events.append(Event("full", actor_id,
                                    text=f"{actor.name}'s pack is full; "
                                         f"{left[0].label} stays where it is"))
            if mended:
                events.append(Event("shrine", actor_id, amount=mended,
                                    text=f"{actor.name} pays the shrine "
                                         f"{mended * COIN_PER_HP} and mends "
                                         f"{mended} ({moved.hp}/{moved.hp_cap})"))
            return

        # An attack flies along the direction until it meets something. With
        # a blade that is the next square; with a bow it is up to four. The
        # action string is unchanged, so nothing extra crosses the radio: what
        # you are carrying decides how far "attack east" goes.
        far = actor.reach
        victim, distance = None, 0
        for step_out in range(1, far + 1):
            sx, sy = actor.x + dx * step_out, actor.y + dy * step_out
            if not room.passable(sx, sy):
                break
            found = _at(sx, sy)
            if found is not None:
                victim, distance = found, step_out
                break

        if victim is None:
            events.append(Event("miss", actor_id,
                                text=f"{actor.name} strikes at nothing"))
            return
        if not room.friendly_fire and victim.kind == actor.kind:
            # The first thing in the line is the thing you hit, so a friend
            # standing between you and the ogre is a friend you do not shoot.
            events.append(Event("held", actor_id, victim.id,
                                text=(f"{actor.name} holds the swing"
                                      if distance == 1 else
                                      f"{actor.name} holds fire; {victim.name} is in the way")))
            return

        usable = [k for k in actor.armoury() if k[1] >= distance]
        if not usable:
            events.append(Event("miss", actor_id,
                                text=f"{actor.name} cannot reach {victim.name}"))
            return
        power, _, turnable, weapon = usable[0]

        if turnable and rng.roll(20) <= victim.guard:
            events.append(Event("miss", actor_id, victim.id,
                                text=f"{actor.name} misses {victim.name}"))
            return
        damage = rng.roll(power)
        hurt = replace(victim, hp=max(0, victim.hp - damage))
        entities[victim.id] = hurt
        verb = "hits" if distance == 1 else "strikes"
        events.append(Event("hit", actor_id, victim.id, damage,
                            f"{actor.name} {verb} {victim.name} for {damage}"))
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

    # Anything that finishes on this tick pays out first.
    for eid in sorted(entities):
        actor = entities[eid]
        if not actor.doing or actor.doing == "done" or actor.busy_until > room.tick:
            continue
        verb, _, direction = actor.doing.partition(":")
        entities[eid] = replace(actor, doing="", busy_until=room.tick)
        if verb == "chop":
            dx, dy = DIRECTIONS.get(direction, (0, 0))
            tree = _at(actor.x + dx, actor.y + dy)
            if tree is not None and tree.kind == "tree" and tree.alive:
                entities[tree.id] = replace(tree, hp=tree.hp - 1)
                log = Item("wood", "log", "plain", WOOD_PER_CHOP)
                entities[eid] = replace(entities[eid], pack=entities[eid].pack + (log,))
                events.append(Event("chopped", eid, tree.id, WOOD_PER_CHOP,
                                    f"{actor.name} cuts a log from the {tree.name}"))
                if entities[tree.id].hp <= 0:
                    events.append(Event("felled", eid, tree.id,
                                        text=f"the {tree.name} comes down"))
            else:
                events.append(Event("blocked", eid,
                                    text=f"{actor.name} has nothing left to cut"))

    for eid in sorted(entities):
        actor = entities[eid]
        if actor.kind == "player" and actor.alive and actor.due(room.tick):
            resolve(eid, parse_action(inputs.get(eid, "w")))

    for eid in sorted(entities):
        monster = entities[eid]
        if monster.kind != "monster" or not monster.alive:
            continue
        resolve(eid, _monster_action(monster, entities, _passable, _at))

    # If nothing hostile is about and nobody who could act wants to, there is
    # no reason to grind through the ticks one at a time: jump to whenever the
    # next thing actually happens.
    #
    # Without this, one person chopping for sixty ticks obliges everybody else
    # to send sixty waits, which is sixty frames of airtime to watch somebody
    # work. Waiting therefore means waiting until something happens, and costs
    # one frame however long that turns out to be. With a monster alive it
    # never fires, because a monster is due every tick.
    tick = room.tick + 1
    if not room.living("monster"):
        pending = [e.busy_until for e in entities.values()
                   if e.kind == "player" and e.alive and e.doing
                   and e.doing != "done" and e.busy_until > tick]
        idle = all(
            e.doing == "done" or not e.due(room.tick)
            or parse_action(inputs.get(e.id, "w"))[0] == "wait"
            for e in entities.values()
            if e.kind == "player" and e.alive)
        if pending and idle:
            tick = min(pending)
    advanced = replace(room, entities=entities, floor=floor,
                       shrines=frozenset(shrines), tick=tick,
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
             f"|ff{int(room.friendly_fire)}|d{room.depth}"]
    parts.append(",".join(f"{x}.{y}" for x, y in sorted(room.walls)))
    for eid in sorted(room.entities):
        e = room.entities[eid]
        parts.append(f"{e.id}:{e.kind}:{e.x}:{e.y}:{e.hp}:{e.max_hp}:"
                     f"{e.attack}:{e.armour}:{_items(e.pack)}:{_items(e.drop)}")
    for spot in sorted(room.floor):
        parts.append(f"@{spot[0]}.{spot[1]}:{_items(room.floor[spot])}")
    parts.append("+" + ",".join(f"{x}.{y}" for x, y in sorted(room.shrines)))
    parts.append(room.kind)
    for eid in sorted(room.entities):
        e = room.entities[eid]
        if e.doing or e.busy_until:
            parts.append(f"~{e.id}:{e.doing}:{e.busy_until}")
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

COIN_PER_HP = 2

# How long an action takes, in ticks. A tick is about a second of wall time.
#
# This is the whole pacing mechanism. At one percent duty every millisecond
# transmitted owes a hundred of silence, so a party swinging at each other
# every four seconds burns eight times the hourly allowance and jams. An
# action that covers sixty ticks costs the same single frame as one that
# covers one, which makes fighting expensive per second of game time and
# working nearly free.
#
# Sixty for a chop is not a taste decision. A thirty-character line of chat
# costs about as much airtime as an action, and the budget grants ten
# milliseconds a second, so a minute of work is the length at which the work
# pays for the conversation that happens over it.
ACTION_UNITS = {"move": 1, "attack": 1, "drop": 1, "wait": 1, "leave": 1,
                "chop": 60}
WOOD_PER_CHOP = 1

# What a person can carry, not counting coin, which collapses to one entry.
# Generous on purpose: in a single room it almost never binds. It exists so
# that dropping has teeth once delves start chaining, and so that a pack is a
# thing with edges rather than a list that only grows.
PACK_LIMIT = 6

TREES = ["pine", "birch", "ash", "thorn", "rowan"]

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


def build_camp(seed: str, players: list, width: int = 13, height: int = 9,
               trees: int | None = None) -> Room:
    """A place above ground with things to cut and nothing that bites.

    The same engine, the same grid, the same turn lock. The only new content is
    an entity kind that is worked rather than fought, which is why a camp costs
    almost no code: a camp is a room whose monsters are trees and whose danger
    is that you are not delving.
    """
    if trees is None:
        trees = max(2, len(players) + 1)
    room = build(seed, players, width, height, monster_count=0)
    free = [(x, y) for y in range(1, height - 1) for x in range(1, width - 1)
            if (x, y) not in room.walls and room.at(x, y) is None]
    rng = Rng(seed_for(seed, -2))
    entities = dict(room.entities)
    for index in range(trees):
        if not free:
            break
        spot = free.pop(rng.below(len(free)))
        logs = 3 + rng.below(3)
        tid = f"t{index}"
        entities[tid] = Entity(tid, "tree", f"{TREES[rng.below(len(TREES))]}",
                               spot[0], spot[1], hp=logs, max_hp=logs,
                               attack=0, armour=0)
    # Light lasts longer above ground, and a camp is not a race.
    return replace(room, kind="camp", entities=entities,
                   shrines=frozenset(), floor={}, limit=600)


# Light was a stalemate guard at sixty ticks, which no room ever came close to
# using, so torches bought nothing. Tightened until running out is a real way
# to lose, which is the only thing that makes carrying more of them a decision.
BASE_LIGHT = 20
TORCH_TICKS = 7          # how much further one torch carries you


def build(seed: str, players: list, width: int = 0, height: int = 0,
          monster_count: int | None = None, depth: int = 1,
          torches: int = 0) -> Room:
    """Generate a room from a seed. Same seed, same room, on every machine.

    `players` is a list of (id, name); ids are normally loraline addresses.

    Everything is placed inside one connected region. A monster walled off
    behind rubble can never be reached, and since the room only ends when the
    monsters are dead, that room would never end. Two percent of seeds did
    exactly that before this check existed.

    One monster per person unless told otherwise. It used to be three however
    many of you turned up, which made a lone delver a 76% casualty and a party
    of four almost unkillable. The roster is in the start message, so every
    machine already knows how many are coming and builds the same room.
    """
    # Deeper rooms are bigger, which is what finally makes light a real
    # constraint rather than a stalemate timer: at depth one a party is done
    # in sixteen turns of sixty and never thinks about torches.
    depth = max(1, depth)
    width = width or 11 + 2 * (depth - 1)
    height = height or 9 + (depth - 1)
    if monster_count is None:
        # One each, and one more every second step down. Steeper than this
        # and depth four is a hundred percent casualties, because a party
        # cannot yet carry anything down with them to meet it.
        monster_count = max(1, len(players)) + max(0, depth - 1) // 2
    monster_count = max(0, monster_count)
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
        # The weakest things stop appearing as you go down, but slowly.
        floor_index = min(len(MONSTERS) - 1, max(0, depth - 1) // 2)
        choices = MONSTERS[floor_index:] or MONSTERS
        kind, hp, attack, armour = choices[rng.below(len(choices))]
        mid = f"m{index}"
        # Tougher things carry better things. Decided here, once, from the
        # seed, so every machine already knows what is down there.
        drop = (roll_item(rng, depth=hp + depth * 6),) if rng.below(100) < 70 else ()
        entities[mid] = Entity(mid, "monster", f"{kind} {index + 1}",
                               spot[0], spot[1], hp=hp, max_hp=hp,
                               attack=attack, armour=armour, drop=drop)

    shrines = set()
    if free and rng.below(100) < 60:
        shrines.add(free.pop(rng.below(len(free))))

    return Room(seed=seed, width=width, height=height,
                walls=frozenset(walls), entities=entities,
                shrines=frozenset(shrines), depth=max(1, depth),
                limit=BASE_LIGHT + max(0, torches) * TORCH_TICKS)
