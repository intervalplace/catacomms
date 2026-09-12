"""catacomms, riding on the loraline app.

The engine, the party and the table are untouched. What this does is the job
the standalone loop used to do: hand arriving frames to the party, advance the
clock, and turn what happened into something the page can draw.

It is not always on. A delve that carried on while nobody was looking would be
a delve somebody lost by opening another tab, and the turn lock means nothing
happens without you anyway.
"""

from __future__ import annotations

import time
from datetime import datetime

from loraline.host import Panel

from . import records as R
from .engine import Event
from .party import Party
from .table import APP
from .web import PAGE, snapshot


class DelvePanel(Panel):
    tag = APP
    title = "catacomms"
    route = "/catacomms"
    always = False

    def __init__(self) -> None:
        self.party = None
        self.host = None
        self.log: list = []
        self.recent: list = []
        self.last_resend = 0.0

    # -- housekeeping ------------------------------------------------------

    def note(self, text: str, role: str = "muted") -> None:
        self.log.append((text, role))
        del self.log[:-80]

    def absorb(self, events) -> None:
        interesting = [e for e in events
                       if e.kind in ("hit", "miss", "death", "mend", "shrine")]
        if interesting:
            self.recent[:] = interesting
        for event in events:
            if not event.text or event.kind == "move":
                continue
            role = ("danger" if event.kind in ("hit", "death") else
                    "warn" if event.kind == "diverged" else
                    "accent" if event.kind in ("end", "mend", "pickup",
                                               "shrine", "dropped") else "muted")
            self.note(event.text, role)

    def name_of(self, address: str) -> str:
        peer = self.host.client.session.peers.get(address)
        return peer.label if peer is not None else address

    # -- the panel ---------------------------------------------------------

    def start(self, host) -> None:
        self.host = host
        session = host.client.session
        self.party = Party(session.address, session.nick)
        self.party.identity = host.identity
        self.party.keyring = host.client.keyring
        self.party.on_air = session.on_air
        # Every panel shares the one budget, because they share the one radio.
        self.party.afford = lambda: host.affordable(self.tag, "i|0|w")
        self.note("catacomms. /delve calls one, /begin sets off.")

    def heard(self, src: str, payload: str) -> None:
        if self.party is not None:
            self.absorb(self.party.on_payload(src, payload, self.name_of))

    def tick(self, now: float) -> None:
        if self.party is None:
            return
        self.absorb(self.party.tick())
        # loraline retransmits chat until it is acknowledged and nothing at
        # all for application frames, so a lost input has to be resent here or
        # a turn waits for ever.
        self.party.resend(now)
        for payload in self.party.drain():
            if self.host.affordable(self.tag, payload):
                self.host.send(self.tag, payload)

    def handle(self, order: dict) -> None:
        if self.party is None:
            return
        what = order.get("do")
        if what == "act":
            self.absorb(self.party.submit(order.get("action", "w")))
        elif what == "say":
            text = (order.get("text") or "").strip()
            if text.startswith("/"):
                self.absorb(self.command(text))
            elif text:
                session = self.host.client.session
                session.compose(text, "*", time.time())
                self.note(f"{session.nick}: {text}", "accent")

    def command(self, text: str) -> list:
        from .engine import Event
        word, _, rest = text[1:].partition(" ")
        word = word.lower()
        nick = self.host.client.session.nick
        if word == "delve":
            bits = rest.split()
            depth = int(bits[0]) if bits and bits[0].isdigit() else 1
            torches = int(bits[1]) if len(bits) > 1 and bits[1].isdigit() else 0
            salt = next((b for b in bits if not b.isdigit()), "") or str(int(time.time()))
            return self.party.call(salt, nick, depth=depth, torches=torches)
        if word == "camp":
            return self.party.call(rest.strip() or str(int(time.time())),
                                   nick, kind="camp")
        if word == "begin":
            return self.party.begin()
        if word in ("join", "y"):
            return self.party.accept()
        if word in ("stay", "n"):
            return self.party.decline()
        if word == "leave":
            return self.party.leave()
        if word == "stash":
            held = R.stash(self.host.address, self.host.client.keyring)
            coin = R.purse(self.host.address, self.host.client.keyring)
            if not held and not coin:
                return [Event("note", text="Your stash is empty. Only what "
                                           "somebody else saw is counted.")]
            return [Event("note", text=f"{len(held)} item(s) and {coin} coin.")] + [
                Event("note", text=f"  {item['name']} ({item['tier']})")
                for item, _ in held[:12]]
        return [Event("note", text=f"There is no /{word}.")]

    def snapshot(self) -> dict:
        if self.party is None:
            return {"room": None, "log": []}
        state = snapshot(self.party, self.host.client.session,
                         [(0.0, t, r) for t, r in self.log], self.recent)
        return state

    def page(self) -> str:
        return PAGE
