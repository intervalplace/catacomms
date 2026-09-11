"""Run catacomms over a loraline link.

    python -m catacomms --port /dev/ttyUSB0 --band eu868 --nick hank

Every argument loraline's chat takes works here too, including --tcp-connect
for somebody joining from elsewhere. The game rides the same link as the
conversation.
"""

from __future__ import annotations

import argparse
import curses
import locale
import os
import sys
import time

from loraline import crypto
from loraline.client import Client
from loraline.crypto import GROUP, Identity, Keyring
from loraline.session import AppEvent, MessageEvent, SystemEvent
from loraline.__main__ import BANDS, build as build_radio, open_link

from . import records as R
from . import publish as PUB
from .party import Party
from .table import APP
from .ui import HELP, DelveUI, init_colours
from .web import WebView, snapshot


def wood_in_stash(client: Client) -> int:
    """Logs you can prove you cut. Torches are made of them."""
    if client is None:
        return 0
    try:
        return sum(1 for item, _ in R.stash(client.session.address, client.keyring)
                   if item["kind"] == "wood")
    except Exception:
        return 0


def parse_delve(rest: str, client: Client):
    """`/delve`, `/delve 3`, `/delve 3 2`: depth then torches.

    Torches are capped by what you can prove you cut, because light is the one
    thing the party brings down with them and it would be a poor first thing
    to let anybody invent.
    """
    bits = (rest or "").split()
    depth = int(bits[0]) if bits and bits[0].isdigit() else 1
    asked = int(bits[1]) if len(bits) > 1 and bits[1].isdigit() else 0
    torches = min(asked, wood_in_stash(client))
    salt = next((b for b in bits if not b.isdigit()), "") or str(int(time.time()))
    return max(1, depth), max(0, torches), salt


def identity_of(client: Client):
    return client.identity


def stash_lines(client: Client) -> list:
    """What you can prove you own, and what it took to prove it."""
    address, keyring = client.session.address, client.keyring
    held = R.stash(address, keyring)
    coin = R.purse(address, keyring)
    if not held and not coin:
        return ["Your stash is empty. Only what somebody else was there to see "
                "is counted, so a delve alone leaves you nothing to show."]
    lines = [f"{len(held)} item(s) and {coin} coin, witnessed."]
    for item, record in held[:12]:
        others = [record.roster.get(a, a) for a in record.witnesses(keyring)
                  if a != address]
        lines.append(f"  {item['name']} ({item['tier']}) from {record.seed}, "
                     f"seen by {', '.join(others)}")
    return lines


def run(client: Client, warnings: list) -> None:
    locale.setlocale(locale.LC_ALL, "")
    session = client.session
    party = Party(session.address, session.nick)
    party.identity, party.keyring = identity_of(client), client.keyring
    party.on_air = session.on_air

    def name_of(address: str) -> str:
        peer = session.peers.get(address)
        return peer.label if peer is not None else address

    def command(text: str) -> list:
        word, _, rest = text[1:].partition(" ")
        word = word.lower()
        if word == "delve":
            depth, torches, salt = parse_delve(rest, client)
            return party.call(salt, session.nick, depth=depth, torches=torches)
        if word == "camp":
            return party.call(rest.strip() or str(int(time.time())),
                              session.nick, kind="camp")
        if word == "begin":
            return party.begin()
        if word in ("join", "y"):
            return party.accept()
        if word in ("stay", "n"):
            return party.decline()
        if word == "leave":
            return party.leave()
        if word == "tiles":
            ui.tiles = not ui.tiles
            return _notes(["Tiles on." if ui.tiles else "Back to letters."])
        if word == "who":
            here = ", ".join(sorted(p.label for p in session.online_peers())) or "nobody"
            return _notes([f"In range: {here}."])
        if word == "stash":
            return _notes(stash_lines(client))
        if word == "wood":
            return _notes([f"{wood_in_stash(client)} log(s) you can prove you cut."])
        if word == "help":
            return _notes(HELP)
        if word == "quit":
            ui.running = False
            return []
        return _notes([f"There is no /{word}."])

    def _notes(lines):
        from .engine import Event
        return [Event("note", text=line) for line in lines]

    def main(stdscr):
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        init_colours()
        stdscr.timeout(120)
        stdscr.keypad(True)

        ui.note(f"catacomms. You are {session.nick} ({session.address}).")
        for warning in warnings:
            ui.note(warning, "warn")
        for line in HELP:
            ui.note(line)

        while ui.running:
            for event in client.pump():
                if isinstance(event, AppEvent):
                    if event.app == APP:
                        ui.absorb(party.on_payload(event.src, event.payload, name_of))
                elif isinstance(event, MessageEvent) and event.incoming:
                    ui.note(f"{event.who}: {event.text}", "accent")
                elif isinstance(event, SystemEvent):
                    ui.note(event.text, "warn" if event.level == "warn" else "muted")
            ui.absorb(party.tick())
            party.resend(time.time())
            for payload in party.drain():
                session.send_app(APP, payload)

            ui.draw(stdscr)
            try:
                key = stdscr.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                break
            ui.handle_key(key)

    ui = DelveUI(party, session.nick)
    ui.on_command = command
    ui.on_say = lambda text: session.compose(text, GROUP, time.time())
    curses.wrapper(main)


def run_web(client: Client, warnings: list, port: int,
            args_publish: str = "", args_publish_path: str = "board.json",
            args_publish_branch: str = "") -> None:
    """The browser is the view; this process is still the only engine.

    Nothing here decides anything about the world. It drains the radio, hands
    frames to the party, hands typed actions back, and publishes a snapshot
    for anybody watching.
    """
    session = client.session
    party = Party(session.address, session.nick)
    party.identity, party.keyring = identity_of(client), client.keyring
    party.on_air = session.on_air
    log: list = []
    party.identity, party.keyring = identity_of(client), client.keyring
    party.on_air = session.on_air

    def note(text, role="muted"):
        log.append((time.time(), text, role))
        del log[:-200]

    def name_of(address):
        peer = session.peers.get(address)
        return peer.label if peer is not None else address

    recent: list = []

    def absorb(events):
        interesting = [e for e in events
                       if e.kind in ("hit", "miss", "death", "mend", "shrine")]
        if interesting:
            recent[:] = interesting
        for event in events:
            if not event.text:
                continue
            role = ("danger" if event.kind in ("hit", "death") else
                    "warn" if event.kind == "diverged" else
                    "accent" if event.kind in ("end", "mend", "pickup",
                                               "shrine", "dropped") else "muted")
            if event.kind != "move":
                note(event.text, role)

    view = WebView(port=port)  # records come from the default store
    view.start()
    note(f"catacomms. You are {session.nick} ({session.address}).")
    note(f"Open http://localhost:{port} here, or the machine's address from a phone.")
    note(f"The board is at http://localhost:{port}/board and keeps itself up to date.")
    for warning in warnings:
        note(warning, "warn")
    for line in HELP:
        note(line)

    try:
        while True:
            for event in client.pump():
                if isinstance(event, AppEvent):
                    if event.app == APP:
                        absorb(party.on_payload(event.src, event.payload, name_of))
                elif isinstance(event, MessageEvent) and event.incoming:
                    note(f"{event.who}: {event.text}", "accent")
                elif isinstance(event, SystemEvent):
                    note(event.text, "warn" if event.level == "warn" else "muted")

            for typed in view.drain():
                if typed.startswith("say "):
                    said = typed[4:].strip()
                    if said.startswith("/"):
                        absorb(web_command(party, session, said, note, client))
                    elif said:
                        session.compose(said, GROUP, time.time())
                        note(f"{session.nick}: {said}", "accent")
                elif typed:
                    absorb(party.submit(typed))

            before = party.record.id if party.record else None
            absorb(party.tick())
            party.resend(time.time())
            if args_publish and party.record and party.record.id != before:
                publish_board(args_publish, args_publish_path,
                              args_publish_branch, note)
            for payload in party.drain():
                session.send_app(APP, payload)
            view.publish(snapshot(party, session, log, recent))
            time.sleep(0.12)
    except KeyboardInterrupt:
        pass
    finally:
        view.close()


def web_command(party, session, text, note, session_client=None) -> list:
    from .engine import Event
    word, _, rest = text[1:].partition(" ")
    word = word.lower()
    if word == "delve":
        depth, torches, salt = parse_delve(rest, session_client)
        return party.call(salt, session.nick, depth=depth, torches=torches)
    if word == "camp":
        return party.call(rest.strip() or str(int(time.time())),
                          session.nick, kind="camp")
    if word == "begin":
        return party.begin()
    if word in ("join", "y"):
        return party.accept()
    if word in ("stay", "n"):
        return party.decline()
    if word == "leave":
        return party.leave()
    if word == "who":
        here = ", ".join(sorted(p.label for p in session.online_peers())) or "nobody"
        return [Event("note", text=f"In range: {here}.")]
    if word == "stash":
        return [Event("note", text=line) for line in stash_lines(session_client)]
    if word == "help":
        return [Event("note", text=line) for line in HELP]
    return [Event("note", text=f"There is no /{word}.")]


def publish_board(repo: str, path: str, branch: str, note) -> None:
    """Push the current board somewhere public. Never fatal: a failed push is
    not a reason to lose the evening."""
    import json as _json
    try:
        note(PUB.publish(_json.dumps(R.board(), separators=(",", ":"),
                                     sort_keys=True),
                         repo=repo, path=path, branch=branch))
    except RuntimeError as exc:
        note(str(exc), "warn")


def cmd_board(argv) -> int:
    """Emit everything a board page needs, to stdout.

        python -m catacomms board > board.json

    Only for taking a snapshot to publish somewhere static. A running node
    already serves its own board at /board, live, with nothing to export and
    nothing to keep up to date.
    """
    import json
    print(json.dumps(R.board(), separators=(",", ":"), sort_keys=True))
    return 0


def main(argv=None) -> int:
    if (argv or sys.argv[1:])[:1] == ["board"]:
        return cmd_board(argv)
    parser = argparse.ArgumentParser(prog="catacomms")
    parser.add_argument("--port", help="serial port; omit for a node with no radio")
    parser.add_argument("--band", choices=sorted(BANDS))
    parser.add_argument("--mhz", type=int)
    parser.add_argument("--sf", type=int, choices=range(7, 13))
    parser.add_argument("--bw", type=int, default=125, choices=(125, 250, 500))
    parser.add_argument("--power", type=int)
    parser.add_argument("--address", type=int, default=258)
    parser.add_argument("--netid", type=int, default=0)
    parser.add_argument("--duty", type=float)
    parser.add_argument("--key", help="shared group passphrase, or LORALINE_KEY")
    parser.add_argument("--identity", help="path to the keypair file")
    parser.add_argument("--tcp-listen", type=int, metavar="PORT")
    parser.add_argument("--tcp-connect", metavar="HOST:PORT")
    parser.add_argument("--no-bridge", action="store_true")
    parser.add_argument("--no-config", action="store_true")
    parser.add_argument("--nick", required=True)
    parser.add_argument("--publish-to", metavar="OWNER/REPO",
                        help="after each delve, push the board to this "
                             "repository so a static site can show it")
    parser.add_argument("--publish-path", default="board.json",
                        help="where in the repository to write it")
    parser.add_argument("--publish-branch", default="",
                        help="branch to write to, if not the default")
    parser.add_argument("--web", type=int, metavar="PORT", nargs="?", const=8080,
                        help="serve a browser view on this port instead of "
                             "drawing in the terminal (default 8080)")
    args = parser.parse_args(argv)

    cfg, duty, identity, keyring, warnings = build_radio(args)
    link = open_link(args, cfg, duty, keyring)
    client = Client(link, identity, keyring, nick=args.nick)
    try:
        if args.web:
            run_web(client, warnings, args.web, args.publish_to or "",
                    args.publish_path, args.publish_branch or "")
        else:
            run(client, warnings)
    finally:
        client.shutdown()
        link.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
