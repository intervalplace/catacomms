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

from .party import Party
from .table import APP
from .ui import HELP, DelveUI, init_colours
from .web import WebView, snapshot


def run(client: Client, warnings: list) -> None:
    locale.setlocale(locale.LC_ALL, "")
    session = client.session
    party = Party(session.address, session.nick)

    def name_of(address: str) -> str:
        peer = session.peers.get(address)
        return peer.label if peer is not None else address

    def command(text: str) -> list:
        word, _, rest = text[1:].partition(" ")
        word = word.lower()
        if word == "delve":
            return party.call(rest.strip() or str(int(time.time())), session.nick)
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
            party.resend(time.time())
            ui.absorb(party.tick())
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


def run_web(client: Client, warnings: list, port: int) -> None:
    """The browser is the view; this process is still the only engine.

    Nothing here decides anything about the world. It drains the radio, hands
    frames to the party, hands typed actions back, and publishes a snapshot
    for anybody watching.
    """
    session = client.session
    party = Party(session.address, session.nick)
    log: list = []

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

    view = WebView(port=port)
    view.start()
    note(f"catacomms. You are {session.nick} ({session.address}).")
    note(f"Open http://localhost:{port} here, or the machine's address from a phone.")
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
                        absorb(web_command(party, session, said, note))
                    elif said:
                        session.compose(said, GROUP, time.time())
                        note(f"{session.nick}: {said}", "accent")
                elif typed:
                    absorb(party.submit(typed))

            party.resend(time.time())
            absorb(party.tick())
            for payload in party.drain():
                session.send_app(APP, payload)
            view.publish(snapshot(party, session, log, recent))
            time.sleep(0.12)
    except KeyboardInterrupt:
        pass
    finally:
        view.close()


def web_command(party, session, text, note) -> list:
    from .engine import Event
    word, _, rest = text[1:].partition(" ")
    word = word.lower()
    if word == "delve":
        return party.call(rest.strip() or str(int(time.time())), session.nick)
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
    if word == "help":
        return [Event("note", text=line) for line in HELP]
    return [Event("note", text=f"There is no /{word}.")]


def main(argv=None) -> int:
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
    parser.add_argument("--web", type=int, metavar="PORT", nargs="?", const=8080,
                        help="serve a browser view on this port instead of "
                             "drawing in the terminal (default 8080)")
    args = parser.parse_args(argv)

    cfg, duty, identity, keyring, warnings = build_radio(args)
    link = open_link(args, cfg, duty, keyring)
    client = Client(link, identity, keyring, nick=args.nick)
    try:
        if args.web:
            run_web(client, warnings, args.web)
        else:
            run(client, warnings)
    finally:
        client.shutdown()
        link.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
