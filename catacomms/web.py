"""A browser view, served by the process that already holds the engine.

The important constraint is that the rules are not reimplemented. A delve only
works because every machine computes the same bytes from the same inputs, so a
second engine written in JavaScript would diverge on some integer division
nobody thought about, and the divergence detector would fire, correctly, for
ever. Here the browser is a screen and a keyboard: it receives snapshots over
Server-Sent Events and posts back actions, and `engine.py` remains the only
thing that decides anything.

Standard library only, like the rest.
"""

from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import PACK_LIMIT


class WebView:
    def __init__(self, port: int = 8080, host: str = "0.0.0.0") -> None:
        self.port, self.host = port, host
        self.inbox: "queue.Queue[str]" = queue.Queue()
        self._listeners: list = []
        self._lock = threading.Lock()
        self._latest: str = "{}"
        self._server = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        view = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):        # the terminal is not a log
                pass

            def handle(self):
                # Browsers keep HTTP/1.1 sockets open and reset them freely; a
                # reset mid-request surfaces as ConnectionResetError/BrokenPipe
                # from deep in the stdlib parser, which would otherwise dump a
                # traceback per closed tab. These are normal and carry no
                # information, so drop them silently.
                try:
                    super().handle()
                except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                    pass

            def do_GET(self):
                if self.path.startswith("/events"):
                    return view._stream(self)
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length).decode("utf-8", "replace")
                view.inbox.put(payload.strip())
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()

    def _stream(self, handler) -> None:
        channel: "queue.Queue[str]" = queue.Queue(maxsize=32)
        with self._lock:
            self._listeners.append(channel)
            first = self._latest
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.end_headers()
        try:
            handler.wfile.write(f"data: {first}\n\n".encode())
            handler.wfile.flush()
            while True:
                try:
                    message = channel.get(timeout=15)
                    handler.wfile.write(f"data: {message}\n\n".encode())
                except queue.Empty:
                    handler.wfile.write(b": still here\n\n")
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self._lock:
                if channel in self._listeners:
                    self._listeners.remove(channel)

    # -- talking to the browser -------------------------------------------

    def publish(self, snapshot: dict) -> None:
        message = json.dumps(snapshot, separators=(",", ":"))
        with self._lock:
            if message == self._latest:
                return               # nothing changed; do not wake anyone
            self._latest = message
            listeners = list(self._listeners)
        for channel in listeners:
            try:
                channel.put_nowait(message)
            except queue.Full:
                pass                 # a stalled tab is not the game's problem

    def drain(self) -> list:
        out = []
        while True:
            try:
                out.append(self.inbox.get_nowait())
            except queue.Empty:
                return out


def snapshot(party, session, log, recent=()) -> dict:
    """Everything the browser needs to draw, and nothing it could decide with."""
    state = {
        "you": session.address,
        "nick": session.nick,
        "status": party.status(),
        "log": [{"text": text, "role": role} for _, text, role in log[-40:]],
        "room": None,
        # Only the last resolved tick's events, and they stay put until the
        # next one. If they churned, every snapshot would differ and the
        # publisher would wake every open tab twelve times a second.
        "events": [{"kind": e.kind, "actor": e.actor, "target": e.target,
                    "amount": e.amount} for e in recent],
    }
    table = party.table
    if table is None:
        return state

    room = table.room
    order = sorted(table.players)
    state["room"] = {
        "w": room.width, "h": room.height,
        "tick": room.tick, "limit": room.limit,
        "over": room.over, "outcome": room.outcome if room.over else "",
        "sweeping": room.sweeping,
        "walls": sorted([x, y] for x, y in room.walls),
        "shrines": sorted([x, y] for x, y in room.shrines),
        # The name is the art: the client derives shape and hue from it, so a
        # picture never has to be transmitted.
        "floor": [{"x": x, "y": y, "n": len(items), "kind": items[0].kind,
                   "name": items[0].name, "tier": items[0].tier}
                  for (x, y), items in sorted(room.floor.items())],
        "acted": table.has_acted(),
        "waiting": [table.players.get(p, p) for p in table.waiting_for()],
        "entities": [],
        "pack_limit": PACK_LIMIT,
    }
    for eid in sorted(room.entities):
        e = room.entities[eid]
        state["room"]["entities"].append({
            "id": e.id, "kind": e.kind, "name": e.name,
            "x": e.x, "y": e.y, "hp": e.hp, "cap": e.hp_cap, "alive": e.alive,
            "seat": order.index(eid) if eid in order else -1,
            "me": eid == party.me,
            "swing": e.swing, "guard": e.guard, "coins": e.coins,
            "pack": [{"label": i.label, "kind": i.kind, "tier": i.tier}
                     for i in e.pack if i.kind != "coin"],
        })
    return state


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>catacomms</title>
<style>
:root{--ink:#dde5e7;--soft:#9aabb1;--faint:#74868d;--rule:#2c3d45;
      --paper:#101b21;--panel:#17252c;--mark:#ea8fb4;--danger:#d75f5f;
      --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--mono);
     font-size:14px;-webkit-user-select:none;user-select:none}
.wrap{max-width:760px;margin:0 auto;padding:12px;display:flex;flex-direction:column;
      gap:10px;min-height:100vh}
h1{font-size:15px;margin:0;font-weight:600;letter-spacing:-.01em}
h1 span{color:var(--mark)}
.bar{display:flex;justify-content:space-between;align-items:baseline;color:var(--faint);
     font-size:12px;gap:10px}
canvas{width:100%;height:auto;display:block;image-rendering:pixelated;
       background:var(--panel);border-radius:6px;touch-action:manipulation}
.cols{display:flex;gap:10px;align-items:flex-start}
.side{width:150px;flex:none;font-size:12px;line-height:1.5}
.side .who{margin-top:6px}
.hp{height:5px;background:var(--rule);border-radius:3px;overflow:hidden;margin:2px 0 4px}
.hp i{display:block;height:100%}
#log{flex:1;overflow-y:auto;max-height:30vh;font-size:12px;line-height:1.55;
     border-top:1px solid var(--rule);padding-top:8px}
#log div{margin-bottom:2px}
.muted{color:var(--soft)}.warn{color:#d7af5f}.danger{color:var(--danger)}
.accent{color:var(--mark)}
form{display:flex;gap:6px}
input{flex:1;background:var(--panel);border:1px solid var(--rule);color:var(--ink);
      font:inherit;padding:8px;border-radius:6px;min-width:0}
input:focus{outline:2px solid var(--mark);outline-offset:1px}
button{background:var(--panel);border:1px solid var(--rule);color:var(--ink);
       font:inherit;padding:8px 12px;border-radius:6px;cursor:pointer}
button:active{background:var(--rule)}
.controls{margin:10px auto 0;max-width:260px}
.pad{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.pad button{padding:12px 0}
.pad button.on{background:var(--mark);color:#10161a;border-color:var(--mark)}
.hint{color:var(--faint);font-size:11px;text-align:center;margin-top:8px}
#board{cursor:default}
#board.can{cursor:pointer}
</style></head><body>
<div class="wrap">
  <div class="bar"><h1><span>&#x2571;&#x2571;&#x2572;</span> catacomms</h1><span id="status"></span></div>
  <div class="cols">
    <canvas id="board" width="352" height="288"></canvas>
    <div class="side" id="side"></div>
  </div>
  <div class="controls">
    <div class="pad">
      <button data-k="drop">set down</button><button data-d="n">&#x2191;</button><button data-k="wait">wait</button>
      <button data-d="w">&#x2190;</button><button data-k="atk" id="atkbtn">attack</button><button data-d="e">&#x2192;</button>
      <button></button><button data-d="s">&#x2193;</button><button></button>
    </div>
    <div class="hint">Click a square next to you to move there, or onto a monster to strike it.
      Arrows/buttons also move; <b>attack</b> or a capital H/J/K/L strikes without moving. Space waits.</div>
  </div>
  <div id="log"></div>
  <form id="say"><input id="text" placeholder="say something, or /delve" autocomplete="off"><button>send</button></form>
</div>
<script>
const SPR=12, TILE=24;            // 12x12 art drawn at 2x
/* How dark the unlit parts of the room get, 0 to 1. Atmosphere only: it must
   never hide a monster, because the terminal view has no lighting and two
   windows onto one game must not show different information. Turn it up if
   you want the dark to bite. */
const GLOOM=0.45;
const ART={player:"....2222.......211112.....21133112....2113311 2...21111112.....211112.......3333.......344443.....34444443....3.4444.3......44 44......55..55...",rat:"......................................1.......11..11.....1.1.1122211.1..112232211....12222211.....122221......1....1.....1......1...............",goblin:"...1....1......11..11......111111.....11322311....11111111.....113311.......1111.......411114.....44111144....4.1111.4......11 11......55..55...",hound:"..............1......1....11....11...1111...11..1132211111..1122222211...122222221.1.12222222111..122222 1....11...11....111...111..............",ogre:"..22222222...2211111122..2113113112..2111111112..2143333412...21444412...2222222222.22211111222.22.111111.22...111111.....111..111....55....55..",coin:"................1111......11222211....12233221...1223333221..1223333221..1223333221...12233221....11222211......1111............................",blade:"..........1..........12.........122........122........122........122........122........122........1223.......13333......1333........3...........",plate:"..1111111....122222221...123333321...123222321...123222321...122222221....12222221....12222221.....122221.......1221.........11.................",bead:"................1111......11222211...1223333221..1233443321..1234444321..1233443321..1223333221...11222211......1111............................",shrine:".................11.........1221........1221......111221111...122222221...111221111.....1221........1221.......112211.....11222211...1122222211."};
const PAL={"rat":{"1":"#4a3a52","2":"#7a6482","3":"#c0b0c8"},"goblin":{"1":"#5f8f4a","2":"#111a12","3":"#c8e06a","4":"#3f5f35","5":"#2a3a24"},"hound":{"1":"#8a4f2a","2":"#c07a45","3":"#e8c07a"},"ogre":{"1":"#b04a4a","2":"#6e2a2a","3":"#f0e0d0","4":"#7a2a2a","5":"#4a1e1e"},"coin":{"1":"#8a6a20","2":"#d7af5f","3":"#f0d89a"},"blade":{"1":"#c8d4dc","2":"#8a99a4","3":"#6b4a2f"},"plate":{"1":"#5a6a74","2":"#93a4ae","3":"#c8d4dc"},"bead":{"1":"#5a4a7a","2":"#8f7ac0","3":"#c0aee8","4":"#f0e8ff"},"shrine":{"1":"#3f6f9f","2":"#8fc8f0"}};
const SEATS=[{"1":"#5faf87","2":"#2f6f52","3":"#c8f0dc","4":"#2f6f52","5":"#3a3a2a"},{"1":"#5f87af","2":"#2f5272","3":"#c8dcf0","4":"#2f5272","5":"#3a3a2a"},{"1":"#d787af","2":"#8f4a6f","3":"#f0d0e0","4":"#8f4a6f","5":"#3a3a2a"},{"1":"#d7af5f","2":"#8f6f2f","3":"#f0e0c0","4":"#8f6f2f","5":"#3a3a2a"}];
const SHAPES={"Fang":"...........1..........12........112........122........122........122........122........122........12213......133331.....1331........31..........","Sliver":".........111........1221.......12221......12221......12221......12221......12221......12221......12213......133331......3331........31..........","Tooth":"...111111.....12333321...1233333321.1233333333 11223333332 11222333322 1.122333221....12232221.....122211......122.11.....122...1.....11........","Edge":"..........11.........122........1221.......12212......12212......12212......12212......12212......12213......1333331....13331.......31..........","Splinter":"..........1..........12.........122.........122........1221.......12221.......1221.......1221.......1221.......12213......1333........31........","Hook":"........111........12221......1222.1.....1222.......1222.......1222........122.........122.........122.........1333........1331.........31......","Needle":"..........11.........122.........12.........122.........12.........122.........12.........122.........13.........133........1331........331.....","Bow":"......111........12.........12.........12.........12..........12..........12...........12...........12...........12...........111...............","Recurve":".....111........12..1......12....1....12..........12.........12...........12..........12...........12....1......12..1........111................","Sling":"...11...11....1221.1221...122111221....1222221......12221........121.........121..........1...........1..........121........12321.......12221...","Cord":"....1111.......12..21.....12....21...12......21..12......21...1......1....12....21.....12..21.......1221.........11.............................","Rod":"..........11.........122........122........122........122........122........122........122........122........122........122.........11..........","Stave":"....333........33333......3313133.....3311133......33333........121.........121.........121.........121.........121.........121.........111.....","Wand":"........333........33333......331333.......33333........333........121........121........121........121........121........121.........11........","Spark":".................3.........3.3.3........333.......33313 33......333........3.3.3.........3............121........121........121.........11......","Mail":"..11111111...1222222221.1223232322 11232323231 11223232322 11232323231 11223232322 1.1222222221...12222221.....122221.......1221.........11.....","Coat":".11......11.1221....1221122211112221122222222221122333332221122333332221122222222221.12222222221.12222222221..1222222221..1222222221...1......1.","Hide":"...111111.....12222221...1222222221.1222332222 11223333222 11222332222 11222222222 1.1222222221...122222221....1222221......12221........111....","Scale":"..11111111...1232323231.1223232322 11232323231 11223232322 11232323231 1.1232323231..1223232321...12323221.....123221.......1221.........11.....","Shell":"....1111......11222211...1222332221.1223333322 11233333332 11233333332 11223333322 1.1222332221...11222211......1111............................","Guard":"..11111111...1222222221..1233333321..1232222321..1232332321..1232222321..1233333321..1222222221...12222221.....122221.......1221.........11.....","Weave":".1111111111.1212121212 11121212121 11212121212 11121212121 11212121212 11121212121 11212121212 11121212121 11212121212 1.1111111111.............","Coin":"................1111......11222211....12233221...1223333221..1223333221..1223333221...12233221....11222211......1111............................","Knot":"...1111.......122221.....122.1221...122...1221..12.....122..122...1221...1221.1221....1221221......122221.......12221.......1221.........11.....","Feather":"..........1..........122........1223.......12233......122333.....1223331....122333.1...12233..1....1223.1.......12.1........1.1.........11......","Ember":".................11.........1221.......122321.....12233321...1223333221..1233333321..1223333221...12233221.....122221.......1111................","Seed":"..................1..........121........12221......1223221....122333221...122333221....12222221.....122221.......1221.........11................","Bead":"................1111......11222211...1223333221..1233333321..1233333321..1233333321..1223333221...11222211......1111............................","Thread":"......111........12221......1221.......1221.......1221.......1221.......1221........1221.........1221.........12221........1221.........11......"};
const HUES={"Ashen":"#b8bcc0","Pitted":"#8a7a62","Bright":"#e8dfa8","Cold":"#8fbcd8","Crooked":"#9a8fa8","Hollow":"#7f9a92","Old":"#a08a6a","Quiet":"#9aa8b0","Red":"#c86a5a","Salt":"#dcd8cc","Thin":"#c0c8b8","Worn":"#8f8478","Black":"#5a5f66","Green":"#7fae72"};
const GLOW={"plain":null,"fine":"#9aa8b0","rare":"#a07fd8","named":"#e8c05f"};
const SHAPE_PAL={"Stave":{"3":"#c8b06a"},"Wand":{"3":"#c8b06a"},"Spark":{"3":"#c8b06a"}};
const MON={rat:'rat',goblin:'goblin',hound:'hound',ogre:'ogre'};

/* An item's picture comes out of the same roll as its name: the noun is the
   shape, the adjective is the hue, and the tier is a glow behind it. Twenty
   one shapes and fourteen hues cover all 288 named things, so a Cold Fang is
   a cold-coloured fang and not a generic sword. */
function shades(hex){
  const r=parseInt(hex.slice(1,3),16),g=parseInt(hex.slice(3,5),16),b=parseInt(hex.slice(5,7),16);
  const hx=(a,bb,c)=>'#'+[a,bb,c].map(v=>Math.max(0,Math.min(255,Math.round(v)))
    .toString(16).padStart(2,'0')).join('');
  return {'1':hx(r*.42,g*.42,b*.42), '2':hex, '3':hx(r*.55+140,g*.55+140,b*.55+140)};
}
function itemArt(name, kind){
  const bits=(name||'').split(' ');
  const noun=bits[bits.length-1], adj=bits.length>1?bits[0]:'Quiet';
  // Coin is called "coins" and has no adjective, so it would otherwise come
  // out the default grey. It is the one item whose colour is not negotiable.
  const shape=SHAPES[noun]||SHAPES.Coin;
  const hue=kind==='coin'?'#d7af5f':(HUES[adj]||'#9aa8b0');
  const key='i'+noun+adj+(kind||'');
  if(baked[key])return baked[key];
  const pal=Object.assign(shades(hue), SHAPE_PAL[noun]||{});
  const c=document.createElement('canvas'); c.width=c.height=SPR;
  const g=c.getContext('2d');
  for(let i=0;i<shape.length;i++){
    const ch=shape[i]; if(ch==='.'||ch===' ')continue;
    g.fillStyle=pal[ch]; g.fillRect(i%SPR,Math.floor(i/SPR),1,1);
  }
  baked[key]=c; return c;
}


const board=document.getElementById('board'), ctx=board.getContext('2d');
let state={}, prev={}, attackMode=false, moveAt=0, floats=[], flash={}, swings={};
let hover=null;   // {x,y} cell the mouse is over, or null

/* Sprites are baked once into offscreen canvases. Drawing 144 rectangles per
   entity per frame at sixty frames a second is how a battery dies. */
const baked={};
function bake(name, pal, key){
  if(baked[key]) return baked[key];
  const c=document.createElement('canvas'); c.width=c.height=SPR;
  const g=c.getContext('2d'), art=ART[name];
  for(let i=0;i<art.length;i++){
    const ch=art[i]; if(ch==='.'||ch===' ')continue;
    const col=pal[ch]; if(!col)continue;
    g.fillStyle=col; g.fillRect(i%SPR, Math.floor(i/SPR), 1, 1);
  }
  baked[key]=c; return c;
}
function spriteFor(e){
  if(e.kind==='player'){const s=SEATS[e.seat%SEATS.length];
    return bake('player', s, 'p'+(e.seat%SEATS.length));}
  const n=MON[e.name.split(' ')[0]]||'goblin';
  return bake(n, PAL[n], n);
}

function ease(t){return t<0?0:t>1?1:t*t*(3-2*t);}
function hash(str){let h=2166136261;for(let i=0;i<str.length;i++){h^=str.charCodeAt(i);h=(h*16777619)>>>0;}return h;}

/* Darkness is punched out on its own canvas with radial gradients and then
   laid over the board in one go. Per tile it was a grid of translucent
   squares, which is the single thing that made the room look unfinished. */
let dark=null;
function lighting(r,w,h){
  if(!dark){dark=document.createElement('canvas');}
  if(dark.width!==w||dark.height!==h){dark.width=w;dark.height=h;}
  const g=dark.getContext('2d');
  g.globalCompositeOperation='source-over';
  g.fillStyle='rgba(6,11,15,'+GLOOM+')'; g.clearRect(0,0,w,h); g.fillRect(0,0,w,h);
  const burn=1-0.4*(r.tick/Math.max(r.limit,1));
  g.globalCompositeOperation='destination-out';
  r.entities.filter(e=>e.kind==='player'&&e.alive).forEach(e=>{
    const cx=e.x*TILE+TILE/2, cy=e.y*TILE+TILE/2, rad=TILE*(6.2*burn);
    const grad=g.createRadialGradient(cx,cy,TILE*0.4,cx,cy,rad);
    grad.addColorStop(0,'rgba(0,0,0,1)');
    grad.addColorStop(0.45,'rgba(0,0,0,0.80)');
    grad.addColorStop(1,'rgba(0,0,0,0.10)');
    g.fillStyle=grad; g.beginPath(); g.arc(cx,cy,rad,0,6.2832); g.fill();
  });
  /* The far corners keep a little light. A monster three rooms of darkness
     away that you cannot see is a different game from the one the terminal
     view is playing, and the two must agree. */
  g.fillStyle='rgba(0,0,0,0.22)'; g.fillRect(0,0,w,h);
  /* A shrine is lit even when nobody is near it. Something down here is
     still burning. */
  r.shrines.forEach(([x,y])=>{
    const cx=x*TILE+TILE/2, cy=y*TILE+TILE/2, rad=TILE*1.8;
    const grad=g.createRadialGradient(cx,cy,1,cx,cy,rad);
    grad.addColorStop(0,'rgba(0,0,0,0.75)'); grad.addColorStop(1,'rgba(0,0,0,0)');
    g.fillStyle=grad; g.beginPath(); g.arc(cx,cy,rad,0,6.2832); g.fill();
  });
  g.globalCompositeOperation='source-over';
  return dark;
}

/* Walls take their edges from their neighbours, so a run of them reads as one
   piece of masonry rather than a row of identical blocks. */
function masonry(r,isWall){
  for(let y=0;y<r.h;y++)for(let x=0;x<r.w;x++){
    if(!isWall(x,y))continue;
    const px=x*TILE, py=y*TILE;
    const n=isWall(x,y-1), s=isWall(x,y+1), w=isWall(x-1,y), e=isWall(x+1,y);
    ctx.fillStyle='#39464f'; ctx.fillRect(px,py,TILE,TILE);
    const grain=hash(x+':'+y)%5;
    ctx.fillStyle=grain<2?'#3d4b55':'#354149';
    ctx.fillRect(px+(grain*5)%TILE, py+((grain*7)%TILE), 5, 3);
    if(!n){ctx.fillStyle='#5d6f79'; ctx.fillRect(px,py,TILE,4);
           ctx.fillStyle='#6d818c'; ctx.fillRect(px,py,TILE,2);}
    if(!s){ctx.fillStyle='#1d262c'; ctx.fillRect(px,py+TILE-4,TILE,4);}
    if(!w){ctx.fillStyle='#2e3941'; ctx.fillRect(px,py,3,TILE);}
    if(!e){ctx.fillStyle='#2a343b'; ctx.fillRect(px+TILE-3,py,3,TILE);}
    if(!n&&!w){ctx.fillStyle='#6d818c'; ctx.fillRect(px,py,4,4);}
    if(!n&&!e){ctx.fillStyle='#6d818c'; ctx.fillRect(px+TILE-4,py,4,4);}
  }
}

function draw(){
  const r=state.room;
  if(!r){ctx.clearRect(0,0,board.width,board.height);return;}
  const w=r.w*TILE, h=r.h*TILE;
  if(board.width!==w||board.height!==h){board.width=w;board.height=h;}
  ctx.imageSmoothingEnabled=false;
  ctx.fillStyle='#11191e'; ctx.fillRect(0,0,w,h);

  const wall=new Set(r.walls.map(a=>a[0]+','+a[1]));
  const isWall=(x,y)=>wall.has(x+','+y);

  for(let y=0;y<r.h;y++)for(let x=0;x<r.w;x++){
    if(isWall(x,y))continue;
    const px=x*TILE, py=y*TILE, v=hash(x+'x'+y);
    ctx.fillStyle=((x+y)&1)?'#1b262c':'#1e2a31'; ctx.fillRect(px,py,TILE,TILE);
    if(v%9<2){ctx.fillStyle='#26343c'; ctx.fillRect(px+(v%5)*4+2, py+((v>>3)%5)*4+2, 2, 2);}
    if(v%17===0){ctx.fillStyle='#182229'; ctx.fillRect(px+(v%7)*3, py+((v>>4)%7)*3, 4, 2);}
    if(isWall(x,y-1)){ctx.fillStyle='rgba(0,0,0,0.30)'; ctx.fillRect(px,py,TILE,5);}
  }
  masonry(r,isWall);

  const put=(img,px,py)=>ctx.drawImage(img,0,0,SPR,SPR,px,py,TILE,TILE);
  r.shrines.forEach(([x,y])=>put(bake('shrine',PAL.shrine,'shrine'),x*TILE,y*TILE));

  const now=performance.now();
  r.floor.forEach(f=>{
    const glow=GLOW[f.tier];
    const bob=Math.sin(now/520+f.x*1.7+f.y)*1.2;
    if(glow){
      const cx=f.x*TILE+TILE/2, cy=f.y*TILE+TILE/2+bob;
      const grad=ctx.createRadialGradient(cx,cy,1,cx,cy,TILE*0.8);
      grad.addColorStop(0,glow+'aa'); grad.addColorStop(1,glow+'00');
      ctx.fillStyle=grad; ctx.fillRect(f.x*TILE-6,f.y*TILE-6,TILE+12,TILE+12);
    }
    ctx.fillStyle='rgba(0,0,0,0.35)';
    ctx.beginPath(); ctx.ellipse(f.x*TILE+TILE/2,f.y*TILE+TILE-4,TILE*0.26,3,0,0,6.2832); ctx.fill();
    put(itemArt(f.name,f.kind), f.x*TILE, f.y*TILE+bob);
    if(f.n>1){ctx.fillStyle='#dde5e7'; ctx.font='bold 9px ui-monospace,monospace';
      ctx.textAlign='right'; ctx.fillText(f.n, f.x*TILE+TILE-2, f.y*TILE+TILE-2);}
  });

  const t=ease((now-moveAt)/260);
  const was={}; (prev.room?prev.room.entities:[]).forEach(e=>was[e.id]=e);
  const at={}; r.entities.forEach(e=>at[e.id]=e);

  /* Where a thing is drawn: eased from where it was, plus an idle sway, plus
     a lunge toward whatever it swung at. A turn resolving should look like
     something happened. */
  const place=e=>{
    const from=was[e.id]&&was[e.id].alive?was[e.id]:e;
    let px=(from.x+(e.x-from.x)*t)*TILE, py=(from.y+(e.y-from.y)*t)*TILE;
    py+=Math.sin(now/700+hash(e.id)%628/100)*0.9;
    const sw=swings[e.id];
    if(sw&&now-sw.at<300){
      const k=Math.sin(Math.PI*ease((now-sw.at)/300))*0.32;
      px+=sw.dx*TILE*k; py+=sw.dy*TILE*k;
    }
    return [px,py];
  };

  r.entities.filter(e=>e.alive).forEach(e=>{
    const [px,py]=place(e);
    ctx.fillStyle='rgba(0,0,0,0.40)';
    ctx.beginPath(); ctx.ellipse(px+TILE/2,py+TILE-3,TILE*0.30,3.5,0,0,6.2832); ctx.fill();
    const img=spriteFor(e), hurt=flash[e.id]&&now-flash[e.id]<300;
    put(img,px,py);
    if(hurt){
      ctx.save(); ctx.globalCompositeOperation='lighter';
      ctx.globalAlpha=0.55*(1-(now-flash[e.id])/300);
      ctx.fillStyle='#ffd0d0'; put(img,px,py); ctx.fillRect(px,py,0,0); ctx.restore();
    }
  });

  ctx.drawImage(lighting(r,w,h),0,0);

  const me=r.entities.find(e=>e.me);
  if(me&&me.alive&&!r.over){
    // Show what the four neighbouring squares would do if clicked, so the
    // board reads as a control surface rather than just a picture. A square
    // holding a monster is a strike (red); an empty, non-wall square is a
    // step (green). The one under the cursor gets a brighter fill.
    const occupied={}; r.entities.filter(e=>e.alive).forEach(e=>occupied[e.x+','+e.y]=e);
    [['w',-1,0],['e',1,0],['n',0,-1],['s',0,1]].forEach(([d,dx,dy])=>{
      const nx=me.x+dx, ny=me.y+dy;
      if(nx<0||ny<0||nx>=r.w||ny>=r.h) return;
      if(isWall(nx,ny)) return;
      const occ=occupied[nx+','+ny];
      if(occ&&occ.kind==='player') return;         // never target an ally
      const strike=occ&&occ.kind==='monster';
      const hot=hover&&hover.x===nx&&hover.y===ny;
      ctx.save();
      ctx.fillStyle=strike?'#e06060':'#7fd08a';
      ctx.globalAlpha=hot?0.34:0.16;
      ctx.fillRect(nx*TILE+2,ny*TILE+2,TILE-4,TILE-4);
      ctx.globalAlpha=hot?0.9:0.5; ctx.lineWidth=hot?2:1;
      ctx.strokeStyle=strike?'#e06060':'#7fd08a';
      ctx.strokeRect(nx*TILE+2.5,ny*TILE+2.5,TILE-5,TILE-5);
      ctx.restore();
    });
    const [px,py]=place(me);
    ctx.strokeStyle=attackMode?'#e06060':'#ea8fb4'; ctx.lineWidth=2;
    ctx.globalAlpha=0.55+0.35*Math.sin(now/380);
    ctx.strokeRect(px+1,py+1,TILE-2,TILE-2);
    ctx.globalAlpha=1;
  }

  floats=floats.filter(f=>now-f.at<1100);
  ctx.textAlign='center'; ctx.font='bold 11px ui-monospace,monospace';
  floats.forEach(f=>{
    const age=(now-f.at)/1100;
    ctx.globalAlpha=1-age*age;
    ctx.fillStyle='rgba(0,0,0,0.65)';
    ctx.fillText(f.text, f.x*TILE+TILE/2+1, f.y*TILE+TILE/2-age*20+1);
    ctx.fillStyle=f.col;
    ctx.fillText(f.text, f.x*TILE+TILE/2, f.y*TILE+TILE/2-age*20);
    ctx.globalAlpha=1;
  });
  requestAnimationFrame(draw);       // the room breathes even between turns
}

function side(){
  const r=state.room, s=document.getElementById('side');
  if(!r){s.innerHTML='<div class="muted">'+(state.status||'')+'</div>';return;}
  let h='<div class="muted">turn '+r.tick+' of '+r.limit+'</div>';
  const bar=(e,c)=>'<div class="hp"><i style="width:'+Math.max(0,Math.round(100*e.hp/e.cap))
    +'%;background:'+c+'"></i></div>';
  r.entities.filter(e=>e.kind==='player').forEach(e=>{
    const c=SEAT[e.seat%SEAT.length];
    h+='<div class="who" style="color:'+c+'">'+e.name+(e.alive?'':' &#x2717;')+'</div>'+bar(e,c);
    if(e.me){h+='<div class="muted">swing '+e.swing+' &middot; guard '+e.guard
      +'<br>'+e.coins+' coin &middot; '+e.pack.length+'/'+r.pack_limit+'</div>';
      e.pack.forEach(i=>{const adj=i.label.split(' ')[0];
        h+='<div style="color:'+(HUES[adj]||'#9aa8b0')+'">'+i.label+'</div>';});}
  });
  const mobs=r.entities.filter(e=>e.kind==='monster'&&e.alive);
  if(mobs.length){h+='<div class="muted" style="margin-top:8px">down here</div>';
    mobs.forEach(e=>{h+='<div class="danger">'+e.name+'</div>'+bar(e,'#d75f5f');});}
  s.innerHTML=h;
}
function log(){
  const el=document.getElementById('log');
  el.innerHTML=(state.log||[]).map(l=>'<div class="'+l.role+'">'+
    l.text.replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))+'</div>').join('');
  el.scrollTop=el.scrollHeight;
}
function render(){draw();side();log();
  document.getElementById('status').textContent=state.status||'';}

const send=b=>fetch('/action',{method:'POST',body:b});
const syncAtk=()=>{const b=document.getElementById('atkbtn'); if(b)b.classList.toggle('on',attackMode);};
const act=a=>{attackMode=false;syncAtk();send(a);draw();};

addEventListener('keydown',e=>{
  if(document.activeElement.tagName==='INPUT')return;
  const mv={h:'w',j:'s',k:'n',l:'e',ArrowLeft:'w',ArrowDown:'s',ArrowUp:'n',ArrowRight:'e'};
  const at={H:'w',J:'s',K:'n',L:'e'};
  if(at[e.key])return act('a:'+at[e.key]);
  if(mv[e.key])return act('m:'+mv[e.key]);
  if(e.key===' ')  {e.preventDefault();return act('w');}
  if(e.key==='d')  return act('d');
});
document.querySelectorAll('.pad button').forEach(b=>b.onclick=()=>{
  if(b.dataset.d) return act((attackMode?'a:':'m:')+b.dataset.d);
  if(b.dataset.k==='wait') return act('w');
  if(b.dataset.k==='drop') return act('d');
  if(b.dataset.k==='atk'){attackMode=!attackMode;syncAtk();draw();}
});
// Which grid cell an event points at, accounting for the canvas being scaled
// down to fit its column.
const cellAt=ev=>{
  const r=state.room; if(!r)return null;
  const box=board.getBoundingClientRect();
  const gx=Math.floor((ev.clientX-box.left)/(box.width/r.w));
  const gy=Math.floor((ev.clientY-box.top)/(box.height/r.h));
  if(gx<0||gy<0||gx>=r.w||gy>=r.h)return null;
  return {x:gx,y:gy};
};
// Is this cell an adjacent, actionable square (a step or a strike)?
const actionableAt=c=>{
  const r=state.room; if(!r||!c)return false;
  const me=r.entities.find(e=>e.me); if(!me||!me.alive||r.over)return false;
  if(Math.abs(c.x-me.x)+Math.abs(c.y-me.y)!==1)return false;
  const there=r.entities.find(e=>e.alive&&e.x===c.x&&e.y===c.y);
  return !(there&&there.kind==='player');   // anything but an ally
};
board.onmousemove=ev=>{
  const c=cellAt(ev);
  hover = actionableAt(c) ? c : null;
  board.classList.toggle('can', !!hover);
};
board.onmouseleave=()=>{ hover=null; board.classList.remove('can'); };
board.onclick=ev=>{
  const r=state.room; if(!r)return;
  const me=r.entities.find(e=>e.me); if(!me||!me.alive)return;
  const c=cellAt(ev); if(!c)return;
  const dx=c.x-me.x, dy=c.y-me.y;
  if(Math.abs(dx)+Math.abs(dy)!==1)return;
  const dir=dx>0?'e':dx<0?'w':dy>0?'s':'n';
  const there=r.entities.find(e=>e.alive&&e.x===c.x&&e.y===c.y);
  act((there&&there.kind==='monster'?'a:':'m:')+dir);
};
document.getElementById('say').onsubmit=e=>{
  e.preventDefault();
  const box=document.getElementById('text');
  if(box.value.trim())send('say '+box.value.trim());
  box.value=''; box.blur();
};
new EventSource('/events').onmessage=m=>{
  const next=JSON.parse(m.data);
  const turned=!state.room||!next.room||next.room.tick!==state.room.tick;
  prev=state; state=next;
  if(turned){
    moveAt=performance.now(); flash={};
    const where={}; (next.room?next.room.entities:[]).forEach(e=>where[e.id]=e);
    swings={};
    (next.events||[]).forEach(ev=>{
      const at=where[ev.target]||where[ev.actor]; if(!at)return;
      if(ev.kind==='hit'||ev.kind==='miss'){
        const a=where[ev.actor], b=where[ev.target];
        if(a&&b) swings[ev.actor]={dx:Math.sign(b.x-a.x),dy:Math.sign(b.y-a.y),at:performance.now()};
      }
      if(ev.kind==='hit'){flash[ev.target]=performance.now();
        floats.push({text:'-'+ev.amount,x:at.x,y:at.y,col:'#ff9a9a',at:performance.now()});}
      if(ev.kind==='miss') floats.push({text:'miss',x:at.x,y:at.y,col:'#8fa0a8',at:performance.now()});
      if(ev.kind==='mend'||ev.kind==='shrine')
        floats.push({text:'+'+ev.amount,x:at.x,y:at.y,col:'#8fe0b0',at:performance.now()});
      if(ev.kind==='death') floats.push({text:'\u2717',x:at.x,y:at.y,col:'#ff6a6a',at:performance.now()});
    });
    setTimeout(()=>{flash={};},320);
  }
  render();
};
render();
</script></body></html>
"""
