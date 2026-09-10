"""Terminal view for delve. Standard library curses, same restraint as loraline.

The grid is the middle of the screen and everything else is a margin. Chat and
the game share one log, because they share one radio and the party talking is
part of the game.
"""

from __future__ import annotations

import curses
import locale
import time

from .engine import DIRECTIONS
from .party import IDLE, INVITED, PLAYING, Party

SIDEBAR_W = 22
MIN_SIDEBAR_COLS = 62

XTERM = {"person": [72, 67, 175, 179, 108, 174, 146, 109],
         "accent": 168, "muted": 244, "rule": 240, "warn": 179, "danger": 167}
BASIC = {"person": [curses.COLOR_GREEN, curses.COLOR_CYAN, curses.COLOR_MAGENTA,
                    curses.COLOR_YELLOW, curses.COLOR_BLUE, curses.COLOR_RED,
                    curses.COLOR_WHITE, curses.COLOR_CYAN],
         "accent": curses.COLOR_MAGENTA, "muted": curses.COLOR_WHITE,
         "rule": curses.COLOR_WHITE, "warn": curses.COLOR_YELLOW,
         "danger": curses.COLOR_RED}
ROLES = ("accent", "muted", "rule", "warn", "danger")

MOVE_KEYS = {"k": "n", "j": "s", "l": "e", "h": "w",
             curses.KEY_UP: "n", curses.KEY_DOWN: "s",
             curses.KEY_RIGHT: "e", curses.KEY_LEFT: "w"}
ATTACK_KEYS = {"K": "n", "J": "s", "L": "e", "H": "w"}

HELP = [
    "hjkl or the arrows move. HJKL attack in that direction. Space waits.",
    "/tiles switches between letters and pixels. Letters are faster to read; "
    "pixels are nicer to look at.",
    "/delve calls one, /begin sets off, /join and /stay answer somebody else's.",
    "Anything else you type is said out loud to the party.",
]


def unicode_ok() -> bool:
    return "utf" in (locale.getpreferredencoding(False) or "").lower()


class Glyphs:
    def __init__(self, fancy: bool) -> None:
        self.wall = "\u2588" if fancy else "#"
        self.floor = "\u00b7" if fancy else "."
        self.dead = "\u2717" if fancy else "x"
        self.full = "\u2588" if fancy else "="
        self.empty = "\u2500" if fancy else "-"




# --------------------------------------------------------------------------
# Tile rendering
# --------------------------------------------------------------------------
# A terminal cell is about twice as tall as it is wide, so writing an upper
# half block with one colour in the foreground and another in the background
# gives two square pixels per cell. That is a framebuffer, in any 256-colour
# terminal, with nothing installed.
#
# Tiles are 4x4 pixels, which is 4 cells wide and 2 tall. An 11x9 room comes
# to 44 columns by 18 rows. Three rules came out of drawing them:
#   * nothing touches the tile edge, or neighbours merge into one blob
#   * bipeds stand tall and narrow, beasts crouch low and wide
#   * walls must be plainly lighter than floor, not a shade apart
# At this size a silhouette says what kind of thing it is and colour says
# which one. Exact identity stays in the sidebar; there is no room for it here.

BODY = ("\u00b7X\u00b7\u00b7", "XXX\u00b7", "\u00b7X\u00b7\u00b7", "X\u00b7X\u00b7")
BEAST = ("\u00b7\u00b7\u00b7\u00b7", "\u00b7X\u00b7\u00b7", "XXX\u00b7", "X\u00b7X\u00b7")
VERMIN = ("\u00b7\u00b7\u00b7\u00b7", "\u00b7\u00b7\u00b7\u00b7", "\u00b7XX\u00b7", "X\u00b7X\u00b7")
BRUTE = ("\u00b7XX\u00b7", "XXXX", "\u00b7XX\u00b7", "X\u00b7\u00b7X")
LOOT = ("\u00b7\u00b7\u00b7\u00b7", "\u00b7XX\u00b7", "\u00b7XX\u00b7", "\u00b7\u00b7\u00b7\u00b7")
SOLID = ("XXXX",) * 4
RUBBLE = ("\u00b7\u00b7\u00b7\u00b7", "\u00b7\u00b7X\u00b7", "\u00b7\u00b7\u00b7\u00b7", "\u00b7\u00b7\u00b7\u00b7")

MONSTER_SHAPE = {"rat": (VERMIN, 137), "goblin": (BODY, 107),
                 "hound": (BEAST, 173), "ogre": (BRUTE, 167)}
WALL_COLOUR, FLOOR_COLOUR, LOOT_COLOUR = 238, 236, 179
TRANSPARENT = -1


class Pairs:
    """Curses wants a pair index per (foreground, background) combination, and
    there are only so many. A room needs about a dozen colours, so they are
    allocated on demand and the renderer falls back to letters if the terminal
    runs out."""

    def __init__(self, first: int = 20) -> None:
        self.first = first
        self.cache: dict = {}
        self.exhausted = False

    def get(self, fg: int, bg: int) -> int:
        key = (fg, bg)
        if key in self.cache:
            return self.cache[key]
        index = self.first + len(self.cache)
        if index >= min(curses.COLOR_PAIRS, 256):
            self.exhausted = True
            return 0
        try:
            curses.init_pair(index, fg, bg)
        except (curses.error, ValueError):
            # init_pair raises ValueError, not curses.error, when a colour is
            # out of range. An eight-colour terminal took the client down here
            # rather than falling back.
            self.exhausted = True
            return 0
        self.cache[key] = index
        return index


class DelveUI:
    def __init__(self, party: Party, nick: str) -> None:
        self.party = party
        self.nick = nick
        self.buffer = ""
        self.log: list = []
        self.running = True
        self.glyphs = Glyphs(unicode_ok())
        self.on_say = lambda text: None
        self.on_command = lambda text: []
        self.tiles = False
        self.pairs = Pairs()

    # -- log ---------------------------------------------------------------

    def note(self, text: str, role="muted") -> None:
        self.log.append((time.time(), text, role))
        del self.log[:-300]

    # Movement is already on the grid. Logging it as well buries the events
    # that decide the fight under three monsters shuffling every turn.
    QUIET = {"move"}

    def absorb(self, events) -> None:
        for event in events:
            if event.kind in self.QUIET:
                continue
            role = "muted"
            if event.kind in ("hit", "death"):
                role = "danger"
            elif event.kind == "diverged":
                role = "warn"
            elif event.kind == "end":
                role = "accent"
            if event.text:
                self.note(event.text, role)

    # -- drawing -----------------------------------------------------------

    def draw(self, stdscr) -> None:
        stdscr.erase()
        rows, cols = stdscr.getmaxyx()
        sidebar = cols >= MIN_SIDEBAR_COLS
        left = SIDEBAR_W + 1 if sidebar else 0
        main_w = max(20, cols - left - 1)

        if sidebar:
            self._sidebar(stdscr, rows)
            for y in range(rows - 1):
                self._put(stdscr, y, SIDEBAR_W, "\u2502", self._attr("rule"))

        top = 0
        if self.party.state == PLAYING and self.party.table is not None:
            if self.tiles and self.glyphs.wall != "#":
                top = self._tile_grid(stdscr, left, main_w)
            if not top:
                top = self._grid(stdscr, left, main_w)

        log_h = max(1, rows - top - 3)
        lines: list = []
        for _, text, role in self.log:
            wrapped = self._wrap(text, main_w - 2)
            lines.append([("* ", "muted"), (wrapped[0], role)])
            lines += [[("  ", "muted"), (w, role)] for w in wrapped[1:]]
        for i, segments in enumerate(lines[-log_h:]):
            x = left
            for text, role in segments:
                self._put(stdscr, top + i, x, text[:main_w - (x - left)],
                          self._attr(role))
                x += len(text)

        self._put(stdscr, rows - 2, left, "> ", self._attr("muted"))
        self._put(stdscr, rows - 2, left + 2, self.buffer[-(main_w - 3):])
        self._put(stdscr, rows - 1, 0, self.status()[:cols], self._attr("muted"))
        try:
            stdscr.move(rows - 2, min(cols - 1, left + 2 + len(self.buffer)))
        except curses.error:
            pass
        stdscr.refresh()

    def _tile_grid(self, stdscr, left: int, main_w: int) -> int:
        """Draw the room as pixels. Returns the row below it, or 0 if it will
        not fit, so the caller can fall back."""
        room = self.party.table.room
        rows, _ = stdscr.getmaxyx()
        # The sprite colours are xterm-256 indices, so below that the tiles
        # would be wrong even if they drew. Letters are better there anyway.
        if not curses.has_colors() or curses.COLORS < 256:
            return 0
        if room.width * 4 > main_w or room.height * 2 > rows - 6 or self.pairs.exhausted:
            return 0

        order = sorted(self.party.table.players)
        occupants = {}
        for eid in sorted(room.entities):
            e = room.entities[eid]
            if e.alive:
                occupants[(e.x, e.y)] = e

        def tile_at(gx: int, gy: int):
            entity = occupants.get((gx, gy))
            if entity is not None:
                if entity.kind == "player":
                    idx = order.index(entity.id) if entity.id in order else 0
                    return BODY, XTERM["person"][idx % 8]
                return MONSTER_SHAPE.get(entity.name.split()[0], (BODY, 167))
            if (gx, gy) in room.floor:
                return LOOT, LOOT_COLOUR
            if (gx, gy) in room.walls:
                return SOLID, WALL_COLOUR
            return RUBBLE, FLOOR_COLOUR

        # Two pixel rows per terminal row, drawn as one half block.
        for gy in range(room.height):
            for half in range(2):
                y = gy * 2 + half
                for gx in range(room.width):
                    shape, colour = tile_at(gx, gy)
                    top_row, bottom_row = shape[half * 2], shape[half * 2 + 1]
                    for px in range(4):
                        top = colour if top_row[px] == "X" else TRANSPARENT
                        bottom = colour if bottom_row[px] == "X" else TRANSPARENT
                        char, fg, bg = self._halfblock(top, bottom)
                        if char == " ":
                            continue
                        attr = (curses.color_pair(self.pairs.get(fg, bg))
                                if curses.has_colors() else curses.A_NORMAL)
                        self._put(stdscr, y, left + gx * 4 + px, char, attr)
        return room.height * 2 + 1

    @staticmethod
    def _halfblock(top: int, bottom: int):
        if top == TRANSPARENT and bottom == TRANSPARENT:
            return " ", 0, 0
        if bottom == TRANSPARENT:
            return "\u2580", top, -1
        if top == TRANSPARENT:
            return "\u2584", bottom, -1
        if top == bottom:
            return "\u2588", top, -1
        return "\u2580", top, bottom

    def _grid(self, stdscr, left: int, main_w: int) -> int:
        room = self.party.table.room
        occupants = {}
        for eid in sorted(room.entities):
            e = room.entities[eid]
            if e.alive:
                occupants[(e.x, e.y)] = e

        for y in range(room.height):
            x = left
            for gx in range(room.width):
                entity = occupants.get((gx, y))
                if entity is not None:
                    char = (entity.name[0].upper() if entity.kind == "player"
                            else entity.name[0].lower())
                    role = (self._person_role(entity.id) if entity.kind == "player"
                            else "danger")
                elif (gx, y) in room.walls:
                    char, role = self.glyphs.wall, "rule"
                else:
                    char, role = self.glyphs.floor, "muted"
                self._put(stdscr, y, x, char + " ", self._attr(role))
                x += 2
            _ = main_w
        return room.height + 1

    def _sidebar(self, stdscr, rows: int) -> None:
        party = self.party
        lines: list = [[(self.nick, "accent")]]

        if party.state == PLAYING and party.table is not None:
            room = party.table.room
            phase = "gathering" if room.sweeping else f"turn {room.tick} of {room.limit}"
            lines.append([(phase, "accent" if room.sweeping else "muted")])
            lines.append([("", "muted")])
            lines.append([("\u2500\u2500 party \u2500\u2500", "rule")])
            for eid in sorted(party.table.players):
                entity = room.entities.get(eid)
                if entity is None:
                    continue
                role = self._person_role(eid)
                mark = "" if entity.alive else f" {self.glyphs.dead}"
                lines.append([(entity.name[:12] + mark, role)])
                lines.append([("  " + self._bar(entity), "muted")])
            carried = [i for eid in sorted(party.table.players)
                       for i in room.entities[eid].pack if i.kind != "coin"]
            coin = sum(room.entities[e].coins for e in party.table.players)
            if carried or coin:
                lines.append([("", "muted")])
                lines.append([("\u2500\u2500 carried \u2500\u2500", "rule")])
                for item in carried[-6:]:
                    lines.append([(f"{item.glyph} {item.label[:18]}", "accent")])
                if coin:
                    lines.append([(f"$ {coin} coins", "accent")])
            loose = sum(len(v) for v in room.floor.values())
            if loose:
                lines.append([(f"{loose} still on the floor", "warn")])
            monsters = room.living("monster")
            if monsters:
                lines.append([("", "muted")])
                lines.append([("\u2500\u2500 down here \u2500\u2500", "rule")])
                for m in monsters:
                    lines.append([(m.name[:14], "danger")])
                    lines.append([("  " + self._bar(m), "muted")])
        else:
            lines.append([("", "muted")])
            lines.append([(party.status()[:SIDEBAR_W], "muted")])

        for y, segments in enumerate(lines):
            if y >= rows - 1:
                break
            x = 0
            for text, role in segments:
                if not text:
                    continue
                self._put(stdscr, y, x, text[:SIDEBAR_W - x], self._attr(role))
                x += len(text)

    def _bar(self, entity, width: int = 10) -> str:
        filled = 0 if entity.max_hp <= 0 else entity.hp * width // entity.max_hp
        if entity.alive and filled == 0:
            filled = 1          # never show a living thing as empty
        return (self.glyphs.full * filled + self.glyphs.empty * (width - filled)
                + f" {entity.hp}")

    def _person_role(self, eid: str):
        order = sorted(self.party.table.players) if self.party.table else [eid]
        return order.index(eid) if eid in order else 0

    def status(self) -> str:
        bits = [self.party.status()]
        if self.party.state == PLAYING and self.party.table is not None:
            table = self.party.table
            if not table.room.over and not table.has_acted():
                bits.append("hjkl move, HJKL attack, space wait")
        return "   ".join(bits)

    # -- input -------------------------------------------------------------

    def handle_key(self, key) -> None:
        if key == 17:                                    # ctrl+q
            self.running = False
            return
        if isinstance(key, str) and key in ATTACK_KEYS:
            self._act("a", ATTACK_KEYS[key])
            return
        if key in MOVE_KEYS:
            self._act("m", MOVE_KEYS[key])
            return
        if key == " " and not self.buffer:
            self._act("w", "")
            return
        if key in (curses.KEY_ENTER, "\n", "\r", 10, 13):
            text, self.buffer = self.buffer.strip(), ""
            if text.startswith("/"):
                self.absorb(self.on_command(text))
            elif text:
                self.on_say(text)
                self.note(f"{self.nick}: {text}", "accent")
            return
        if key in (curses.KEY_BACKSPACE, "\x7f", "\b", 127, 8):
            self.buffer = self.buffer[:-1]
            return
        if isinstance(key, str) and key.isprintable():
            self.buffer += key

    def _act(self, verb: str, direction: str) -> None:
        if self.party.state != PLAYING or self.party.table is None:
            return
        if self.party.table.room.over:
            return
        if self.party.table.has_acted():
            self.note("You have already moved this turn.")
            return
        action = "w" if verb == "w" else f"{verb}:{direction}"
        self.absorb(self.party.submit(action))

    # -- plumbing ----------------------------------------------------------

    @staticmethod
    def _wrap(text: str, width: int) -> list:
        if width <= 1:
            return [text[:1]]
        lines, current = [], ""
        for word in text.split(" "):
            candidate = word if not current else current + " " + word
            if len(candidate) <= width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                while len(word) > width:
                    lines.append(word[:width])
                    word = word[width:]
                current = word
        lines.append(current)
        return lines or [""]

    @staticmethod
    def _put(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
        if not text or y < 0 or x < 0:
            return
        try:
            stdscr.addnstr(y, x, text, max(0, stdscr.getmaxyx()[1] - x - 1), attr)
        except curses.error:
            pass

    @staticmethod
    def _attr(role) -> int:
        if not curses.has_colors():
            return curses.A_DIM if role == "muted" else curses.A_NORMAL
        if role in ROLES:
            attr = curses.color_pair(9 + ROLES.index(role))
            return attr | curses.A_DIM if role in ("muted", "rule") else attr
        return curses.color_pair((int(role) % 8) + 1)


def init_colours() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    palette = XTERM if curses.COLORS >= 256 else BASIC
    for i, colour in enumerate(palette["person"]):
        curses.init_pair(i + 1, colour, bg)
    for i, role in enumerate(ROLES):
        curses.init_pair(9 + i, palette[role], bg)
