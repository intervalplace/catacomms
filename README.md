# catacomms

A turn-locked dungeon for one to four people, played over LoRa radio.

The name is the two halves of it: a catacomb, and comms. Uses [loraline](https://github.com/intervalplace/loraline) as its
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

## What the items do

Four kinds, and everything is passive: you fight with the best of each that you
are carrying. There is no equipping and no inventory screen, because using an
item would cost a turn, and a menu at seventeen seconds a turn is a worse game
than no menu at all. The decision an item creates is spatial — detour for the
drop, or press the attack.

| kind | what it changes | plain | fine | rare | named |
|------|-----------------|-------|------|------|-------|
| blade | damage, one square | 6 | 8 | 10 | 12 |
| bow | damage, four squares | 3 | 5 | 7 | 9 |
| rod | damage, three squares, always lands | 2 | 4 | 6 | 8 |
| armour | chance to be missed | 20% | 30% | 40% | 50% |
| charm | maximum health, and mends you by the difference when picked up | +4 | +7 | +10 | +13 |
| coin | pays a shrine: two coins mend one point of health | | | | |

A named blade doubles your damage and a named coat means half of everything
swung at you misses, so one good drop changes a fight.

**How far a weapon reaches comes from its noun**, so `a:e` means the same
thing it always did and nothing extra crosses the radio. A blade strikes the
next square. A bow flies up to four. A rod reaches three and cannot be turned
by armour, which makes it the answer to something well armoured and a blade
the answer to something in front of you.

There is no equipped weapon and nothing to choose. You strike with whatever
reaches, and with the harder one when two do, which is what a person would do
and costs no turn to decide.

**The first thing in the line is the thing you hit**, so a friend standing
between you and the ogre is a friend you do not shoot: the shot is held and
the turn is spent. That is the whole reason to care where anyone stands, and
the grid did not have a reason before.

Two thirds of what drops is still something to swing. A bow is a find.

**`d` sets something down**, and it is the only inventory verb there is. It
drops the item you need least: anything that is not your best of its kind does
nothing for you at all, so those go first, cheapest first. One rule, no menu.

The point of it is not tidiness. It is how you hand your old blade to whoever
is behind you: you set it down, they walk over it. Cooperation without a trade
screen.

A pack holds six things, coin aside, since coin collapses to a single entry.
Walk over more than fits and you take what you can and the rest stays where it
lies. In one room the limit almost never binds; it is there so that dropping
has weight once delves start chaining, and so a pack is a thing with edges
rather than a list that only grows.

**Coin is both the score and the medicine.** Sixty percent of rooms hold a
shrine. Walk onto it hurt and carrying coin and it mends you, two coins a
point, and is spent. There is no prompt, because a prompt would cost a turn.

That is the tension worth having: the thing you came down for is also the thing
that keeps you alive long enough to carry it out. Spend it here or take it
home.

Coin does nothing outside a room yet. It cannot, honestly: a local stash is
unverifiable until delves produce signed attestations, and inventing an
enforcement that does not enforce anything would be worse than the gap.

Every item looks like its name. The noun is the shape, the adjective is the
hue, and the tier is a glow behind it, so twenty one shapes and fourteen
colours cover all 288 named things and a Cold Fang is a cold-coloured fang
rather than a generic sword. Nothing about the picture is transmitted: the
client derives it from the name, which every machine already agreed on.

Generation is free and interaction is expensive, which is why there are 288
distinct named things from two short word lists but only four things they can
do. Loot is derived from the room seed at build time, so nobody transmits an
item: every machine already knows what is down there.

## What a delve leaves behind

A room is a pure function of its seed and roster, so anyone can rebuild it and
confirm that an Ashen Fang is genuinely what drops there. What they cannot
check alone is that you were the one who walked out with it.

So when a room ends, everyone present builds the same record from the same
final state and signs it. Only the signature crosses the radio, because the
record itself is something every machine already computed. Editing what you
carried out afterwards invalidates every signature on it at once.

Two things fall out of that rather than being decided:

**You cannot witness your own loot.** A delve alone produces a record signed
only by you, which is worth nothing to anybody else. It happened, nobody
disputes it, and it mints nothing.

**Being reachable is not being somewhere.** The record notes, per person,
whether their own transmissions were heard on air. A frame relayed to you over
a socket proves somebody exists; it does not prove they were near enough to
hear. A delve played entirely over TCP is witnessed and still mints nothing,
and in a mixed party the people who were physically there earn while the
person on a socket does not.

That is the whole point of the game stated as arithmetic. What is scarce is
not the radio, it is company: the people who were there are what make an item
real, and being somewhere is what cannot be faked.

`/stash` shows what you can prove you own, and who saw you get it.

## The board

Every node serves its own, live, at `/board` whenever the game is running with
`--web`. Nothing to export, nothing to keep up to date, and no internet
required: it is simply what that machine witnessed.

There is no central board and there cannot be. Point a page at several nodes
and it merges them, because two people who were both there hold the same delve
with different signatures on it:

```
http://your-host:8080/board
https://catacomms.org/board.html?from=http://host-a:8080,http://host-b:8080
```

### Filling a public page

A page served over https cannot fetch a board from a laptop with no
certificate: browsers refuse mixed content and nothing configures that away.
So a public board is pushed rather than pulled.

```
python -m catacomms --port /dev/ttyUSB0 --band eu868 --nick hank --web \
    --publish-to yourname/board
```

After every delve the board is written to that repository. The page then reads
it from `raw.githubusercontent.com`, which serves CORS headers, so **the
website itself never changes**: deploy it once and the numbers move on their
own. It does not matter how the site is hosted, or whether the host knows
about the repository at all.

```
https://catacomms.org/board.html?gh=yourname/board
```

Set `DEFAULT_GH` at the top of the page script to `'yourname/board'` and even
the query string goes away.

It needs a GitHub token with contents write on that repository and nothing
else, in `~/.catacomms/github-token` or `CATACOMMS_GITHUB_TOKEN`. The
repository has to be public, since the page reads it without one.

A failed push says so and is otherwise ignored, because losing a board update
is not a reason to lose the evening.

`python -m catacomms board > board.json` is still there for taking a snapshot
by hand.

Nothing on the page is asserted. Your browser recomputes each address from the
two public keys it was handed, verifies every signature against the exact
bytes that were signed, and counts only what survives.

An address is the hash of **both** halves of an identity, encryption and
signing, which is what makes this possible: given the two public keys a
stranger can recompute the address themselves, so an invented signing key
cannot be attached to a real name.

Everything the page displays is read out of the signed bytes, never out of the
readable fields sitting beside them. Those are there for a person to read; a
signature does not cover them. Displaying them would let anybody publish a
valid signature next to invented loot, which is exactly what the first version
did.

## Time, and why the budget used to run out

The European duty cycle is one percent: every millisecond transmitted owes a
hundred of silence. A turn used to cost 323 ms per player, an action and then a
state hash, which is one turn every 32 seconds if you want to keep playing. A
party pressing keys every four seconds burned eight times the allowance and
jammed after seven minutes.

Three things fixed it.

**The state hash rides on the next action.** 323 ms becomes 185 ms, a 43%
saving for one field, and one turn every 18 seconds. Divergence is caught a
turn later than before, which on a link where anything can arrive late costs
nothing.

**Actions have a length.** A swing is one tick, a chop is sixty. The engine
stays clockless: a tick is a unit, not a second, and somebody twelve ticks into
a chop is simply not asked for input. Since airtime is charged per
transmission and not per second of game time, that makes **fighting expensive
and working nearly free**.

**Waiting means waiting until something happens.** Without that, one person
chopping for sixty ticks obliged everybody else to send sixty waits, which is
sixty frames to watch somebody work. If nothing hostile is about and nobody who
could act wants to, the world jumps to whenever the next thing happens. Two
frames now cover a minute of game time instead of sixty.

Sixty ticks for a chop is not a taste decision. A thirty-character line of chat
costs about what an action costs, and the budget grants ten milliseconds a
second, so a minute of work is the length at which the work pays for the
conversation that happens over it.

## The camp

`/camp` goes above ground. Trees, no monsters, the same engine and the same
turn lock: a camp is a room whose monsters are trees and whose danger is that
you are not delving. `c` and a direction cuts, `x` when you are done.

A log is a torch, and a torch is light.

## Depth

`/delve 3 2` goes to depth three carrying two torches. Deeper rooms are bigger,
hold more, and hold worse; the weakest things stop appearing. Measured over 250
rooms at each setting with three players:

| depth | torches | light | cleared | withdrew | wiped |
|---|---|---|---|---|---|
| 1 | 0 | 20 | 61% | 27% | 11% |
| 1 | 2 | 34 | 68% | 19% | 12% |
| 3 | 0 | 20 | 24% | 49% | 26% |
| 3 | 2 | 34 | 35% | 26% | 38% |
| 4 | 0 | 20 | 20% | 60% | 18% |
| 4 | 2 | 34 | 33% | 33% | 33% |

Torches earn their place and then stop: the first two convert a third of your
withdrawals into clears, and a third changes nothing, because by then the fight
is decided rather than the light. A party learns to bring two.

Depth used to add a monster per step and shift the whole roster upward, which
made depth four a hundred percent casualties. It adds one every second step
now, because a party cannot yet carry anything down with them to meet it.

## Balance, measured

300 rooms played by a simple competent policy:

One monster per person, so a room asks how many of you turned up. Across 400
rooms at each size, played by a simple competent policy:

| party | cleared | withdrew | wiped |
|-------|---------|----------|-------|
| 1 | 67% | 12% | 20% |
| 2 | 65% | 18% | 16% |
| 3 | 69% | 21% | 10% |
| 4 | 67% | 25% |  7% |

It used to be three monsters however many of you turned up, which made a lone
delver a 76% casualty and a party of four almost unkillable. A delve is
soloable now, and dying alone is your own fault rather than arithmetic.

Median 15 turns. Dangerous enough to matter, winnable enough to be worth going.

Loot, measured across 5000 generated rooms:

| | |
|---|---|
| rooms holding a rare item | 5.7%, about one in 17 |
| rooms holding a named item | 3.8%, about one in 26 |
| items on the floor per room | 2.1 |

A named item is roughly ninety minutes of play. The tier roll gets a bonus
from what is carrying it, so an ogre is nearly twice as likely to be holding
something named as a rat.

That bonus used to be the monster's whole hit points added to a roll out of a
hundred, which meant an ogre's +20 vaulted the top threshold and named items
came out **commoner than rare ones** — 22% of rooms held one. The roll is out
of a thousand now, so depth tilts the curve instead of leaping it.

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

catacomms uses loraline as its transport, so you need both:

```
git clone https://github.com/intervalplace/loraline
git clone https://github.com/intervalplace/catacomms
pip install -r catacomms/requirements.txt
cd catacomms && PYTHONPATH=../loraline python tests.py
```

The tests need no hardware and no radio.

## Playing

```
python -m catacomms --port /dev/ttyUSB0 --band eu868 --nick hank
```

Every loraline argument works, including `--tcp-connect` for somebody joining
from elsewhere. `hjkl` or the arrows move, `HJKL` attack in that direction,
space waits. `/delve` calls one, `/begin` sets off, `/join` and `/stay` answer
somebody else's. Anything else you type is said out loud to the party, on the
same radio, in the same window.

### On your own, with no radio at all

A delve alone needs nobody to talk to, so it needs no radio either:

```
PYTHONPATH=../loraline python -m catacomms --nick yourname --tcp-listen 4242 --web
```

Open `localhost:8080`, type `/delve` then `/begin`. Rooms hold one monster per
person, so going down alone is a delve rather than a slaughter.

`--tcp-listen` gives the client something to talk through, and a door for
somebody to arrive by later.

### In a browser

```
python -m catacomms --port /dev/ttyUSB0 --band eu868 --nick hank --web
```

Then open `http://localhost:8080`, or the machine's address from a phone on
the same network, and play from the sofa while the dongle stays on the laptop.
Arrow keys or hjkl, HJKL to strike, `d` to set down, or tap a square next to
you. Anything typed in the box is said to the party; anything starting with a
slash is a command.

The browser has no character grid, so the art is not constrained the way the
terminal's is: 12x12 sprites drawn at 24 pixels a tile, walls with a lit top
face and a shadowed base, a floor that varies, eased movement between turns,
damage numbers that float and fade, and a hit flash on whoever took it.

Torchlight is punched out of a darkness layer with radial gradients and laid
over the board in one pass, so the falloff is per pixel rather than a grid of
translucent squares. The reach shortens as the turn limit approaches. Shrines
keep a light of their own whether anyone is near them or not.

It is atmosphere and nothing more. `GLOOM` at the top of the page script caps
how dark the unlit parts get, and it is set low enough that a monster across
the room stays visible, because the terminal view has no lighting and two
windows onto one game must not show different information. Turn it up if you
want the dark to bite.

Walls take their edges from their neighbours, so a run of them reads as one
piece of masonry rather than a row of identical blocks. Everything on the
floor casts a shadow, sways gently, and lunges at whatever it swung at.

The rules are not reimplemented. A delve only works because every machine
computes the same bytes from the same inputs, so a second engine in JavaScript
would diverge on some integer division nobody thought about and the divergence
detector would fire, correctly, for ever. The browser receives snapshots over
Server-Sent Events and posts back actions; `engine.py` remains the only thing
that decides anything. Standard library, no dependencies, one file.

### In the terminal

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
