# delve

A turn-locked dungeon for two to four people, played over LoRa radio.

Working name. Uses [loraline](https://github.com/intervalplace/loraline) as its
transport: the same modules, the same keys, the same link.

## Why a dungeon and not a farm

You get roughly one action every seventeen seconds under the European duty
cycle. A Diablo loop at seventeen seconds a swing is not a slower Diablo, it is
a worse one, because that loop's pleasure is rate.

So a delve is an expedition, not a grind. You go in as a party, it is dangerous,
and you come out or you do not. Seventeen seconds to choose a move is
deliberation rather than lag. A median room runs about fourteen turns, which is
four minutes of real time.

Your party is whoever you can hear. That is not a rule the engine applies, it is
a fact about buildings and hills.

## How three machines share one world

`engine.py` is a pure deterministic state machine: `step(room, inputs)` in,
`(room, events)` out. No clock, no floats, no ambient randomness. Every machine
given the same room and the same inputs computes the same next room, byte for
byte. Three rules keep it that way, and breaking any one is how divergence gets
in:

- Integer arithmetic only. Floats round differently across platforms.
- Every iteration over entities is in sorted order. Dictionary order is not a
  specification.
- Randomness comes from a SplitMix64 generator seeded from `(room seed, tick)`,
  drawn in a fixed order.

`table.py` handles the part that is actually hard: the link loses packets.

- A tick does not advance until every living player's input for it has arrived.
  loraline retransmits until acknowledged, so this terminates.
- After advancing, every player publishes an eight-byte hash of the resulting
  state. Anyone whose hash disagrees knows at once.

That is divergence *detection*, not consensus. With three players on a half
duplex channel there is no quorum worth having. For a party who can see each
other it is enough: the game stops and says so, rather than drifting quietly
into two different worlds.

## Balance, measured

300 rooms played by a simple competent policy:

| outcome  |     |
|----------|-----|
| cleared  | 74% |
| withdrew | 17% |
| wiped    |  9% |

Median 14 turns. Dangerous enough to matter, winnable enough to be worth going.

## Two things the tests forced

**Every room must end.** Two entities can block each other in a corridor and
both wait forever, and 17% of rooms did. Rather than teach the monsters to path
around obstacles, a room has a turn limit: the torches burn down and the party
withdraws. A hang became a rule.

**Everything is placed in one connected region.** Two percent of seeds walled a
monster off behind rubble, and since a room only clears when the monsters are
dead, that room could never end.

## Two rules the playtest forced

**No friendly fire.** Players could hit each other, and in a three-person party
taking a turn every seventeen seconds that is frustration with no decision
attached. A swing at an ally is held and costs the turn. It is a room rule
rather than a law, and it is part of the state hash, so a client that quietly
enabled it would be caught as divergence.

**The roster comes from one message.** A room is built from a seed and a player
list, so if each client used its own idea of who was online, two slightly
different contact lists would build two different rooms before the first turn.
One player calls the delve, collects acceptances, and broadcasts a single start
message naming the seed and the exact roster. That message is the constitution
of that room. The tests prove it by handing a client deliberately wrong local
state and checking it still builds the right room.

## Installing

delve uses loraline as its transport, so you need both:

```
git clone https://github.com/intervalplace/loraline
git clone https://github.com/intervalplace/delve
pip install -r delve/requirements.txt
cd delve && PYTHONPATH=../loraline python tests.py
```

The tests need no hardware and no radio.

## Playing

```
python -m delve --port /dev/ttyUSB0 --band eu868 --nick hank
```

Every loraline argument works, including `--tcp-connect` for somebody joining
from elsewhere. `hjkl` or the arrows move, `HJKL` attack in that direction,
space waits. `/delve` calls one, `/begin` sets off, `/join` and `/stay` answer
somebody else's. Anything else you type is said out loud to the party, on the
same radio, in the same window.

`/tiles` switches the room from letters to pixels. A terminal cell is about
twice as tall as it is wide, so an upper half block with one colour in front
and another behind gives two square pixels per cell: a framebuffer, in any
256-colour terminal, with nothing installed. Tiles are 4x4 pixels, so an 11x9
room is 44 columns by 18 rows.

At that size a silhouette says what kind of thing something is and colour says
which one; exact identity stays in the sidebar. It falls back to letters on a
narrow terminal, on fewer than 256 colours, on a non-UTF-8 locale, or if curses
runs out of colour pairs. Letters remain the default because they are faster to
read and never ambiguous.

## Tests

```
python tests.py
```

Determinism, ordering independence, no floats in the state, termination under
the worst possible play, three tables agreeing after a full fight over a link
dropping one packet in three, and a tampered client being caught.

No hardware required.

## Licence

MIT. Do what you like with it.
