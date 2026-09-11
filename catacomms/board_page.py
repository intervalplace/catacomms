"""The board, as one page.

Kept in the package rather than only on the website so a node can serve
its own board with no internet involved at all. The copy published at
catacomms.org is generated from this string, so the two cannot drift.
"""

PAGE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>catacomms: the board</title>
<meta name="description" content="Who has been down there and what they carried out. Every signature is checked in your own browser; nothing here is asserted.">
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<link rel="icon" href="favicon.ico" sizes="any">
<link rel="stylesheet" href="style.css">
<style>
  .who { font-family: var(--mono); font-size: .85rem; color: var(--flame); }
  .rank td { vertical-align: top; }
  .items { color: var(--soft); font-size: .92rem; }
  .items span { white-space: nowrap; }
  .verdict { font-family: var(--mono); font-size: .82rem; padding: .1rem .4rem;
             border-radius: 3px; }
  .v-ok { color: #8fd8a0; } .v-no { color: #d8867f; }
  #state { color: var(--soft); font-size: .95rem; }
  .bar { background: var(--panel); padding: .9rem 1.1rem; margin: 0 0 1.6rem;
         font-size: .95rem; color: var(--soft); border-left: 2px solid var(--flame); }
  .bar strong { color: var(--ink); }
</style>
</head>
<body>
<main class="page">

<nav><a href="./" aria-label="catacomms"><svg class="mark" viewBox="0 0 32 32" aria-hidden="true" focusable="false"><g fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round"><path d="M1.6 3.6 L10.4 28.4"/><path d="M12.2 3.6 L21 28.4"/><path d="M22.8 28.4 L31.6 3.6"/></g></svg></a><a href="./">What it is</a><a href="start.html">Play it</a><a href="board.html" aria-current="page">The board</a></nav>

<header>
  <h1>The board</h1>
  <p class="standfirst">Who has been down there, and what they carried back
  out. Checked by your own computer, not by anybody else's.</p>
</header>

<div class="bar" id="state">Reading the records&hellip;</div>

<p>There is no central board. Each node keeps the records of delves it was part
of and serves them at <code>/board</code>, so a board is what one machine
witnessed. Several can be merged: two people who were both there hold the same
delve with different signatures on it.</p>

<p>Nothing here is asserted. Your browser recomputes each address from the two
public keys it was handed, verifies every signature against the bytes that were
signed, and counts only what survives. <strong>If you disagree with a number
here, you are right and the number is wrong.</strong></p>

<p>Only delves with somebody else present and on air are counted. You cannot
witness your own loot, and being reachable over the internet is not being
somewhere.</p>

<table id="table"></table>

<h2>What is counted</h2>

<p>An address is derived from both halves of an identity, so a record cannot
carry an invented signing key beside a real name: the address would not come
out right, and the address is what the roster names.</p>

<p>A delve counts as cleared for you if the room was cleared, somebody else
signed for it, and both of you were heard on air. Items are what you walked out
carrying.</p>

<p class="note">Anyone can publish a different board from a different set of
records, and it would be no less true. This one is what one node witnessed.</p>

<footer>
<p>catacomms is free software, and not a product. There is nothing to buy, no
account to make and nobody keeping a copy of anything.</p>
<p>Source and bug reports:
<a href="https://github.com/intervalplace/catacomms">github.com/intervalplace/catacomms</a>
&middot; the radio underneath it is <a href="https://loraline.org">loraline</a>.</p>
</footer>

</main>

<script>
const TIERS = {plain:'', fine:'+', rare:'*', named:'!'};

/* Where a published copy of this page looks by default. Set it to
   'owner/repo' and the page reads the board out of that repository for ever
   after, with nothing to redeploy. Left empty, the page reads a board.json
   sitting beside it, or whatever ?from= or ?gh= asks for. */
const DEFAULT_GH = '';
const state = document.getElementById('state');
const table = document.getElementById('table');

const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
const hex = buf => [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2,'0')).join('');

/* An address is blake2b(x25519 || ed25519) truncated to three bytes, and
   blake2b is not in Web Crypto, so it is written out here. Fifty lines to
   avoid asking you to trust a name. */
function blake2b(input, outlen) {
  const IV = new Uint32Array([0xf3bcc908,0x6a09e667,0x84caa73b,0xbb67ae85,
    0xfe94f82b,0x3c6ef372,0x5f1d36f1,0xa54ff53a,0xade682d1,0x510e527f,
    0x2b3e6c1f,0x9b05688c,0xfb41bd6b,0x1f83d9ab,0x137e2179,0x5be0cd19]);
  const SIGMA = [[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15],
    [14,10,4,8,9,15,13,6,1,12,0,2,11,7,5,3],[11,8,12,0,5,2,15,13,10,14,3,6,7,1,9,4],
    [7,9,3,1,13,12,11,14,2,6,5,10,4,0,15,8],[9,0,5,7,2,4,10,15,14,1,11,12,6,8,3,13],
    [2,12,6,10,0,11,8,3,4,13,7,5,15,14,1,9],[12,5,1,15,14,13,4,10,0,7,6,3,9,2,8,11],
    [13,11,7,14,12,1,3,9,5,0,15,4,8,6,2,10],[6,15,14,9,11,3,0,8,12,2,13,7,1,4,10,5],
    [10,2,8,4,7,6,1,5,15,11,9,14,3,12,13,0],[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15],
    [14,10,4,8,9,15,13,6,1,12,0,2,11,7,5,3]];
  const v = new Uint32Array(32), m = new Uint32Array(32);
  let h = new Uint32Array(IV), t = 0, buf = new Uint8Array(128), c = 0;
  h[0] ^= 0x01010000 ^ outlen;
  const ADD64 = (a,i,b,j) => { const lo=(a[i]>>>0)+(b[j]>>>0);
    a[i]=lo>>>0; a[i+1]=(a[i+1]+b[j+1]+(lo>=0x100000000?1:0))>>>0; };
  const XOR64 = (a,i,b,j) => { a[i]^=b[j]; a[i+1]^=b[j+1]; };
  function ROT(a,i,n){ let lo=a[i],hi=a[i+1];
    if(n===32){ a[i]=hi; a[i+1]=lo; return; }
    if(n<32){ a[i]=(lo>>>n)^(hi<<(32-n)); a[i+1]=(hi>>>n)^(lo<<(32-n)); }
    else { const k=n-32; a[i]=(hi>>>k)^(lo<<(32-k)); a[i+1]=(lo>>>k)^(hi<<(32-k)); }
    a[i]>>>=0; a[i+1]>>>=0; }
  function G(r,i,a,bb,cc,d){
    const x=SIGMA[r][2*i], y=SIGMA[r][2*i+1];
    ADD64(v,a,v,bb); ADD64(v,a,m,2*x);
    XOR64(v,d,v,a); ROT(v,d,32);
    ADD64(v,cc,v,d); XOR64(v,bb,v,cc); ROT(v,bb,24);
    ADD64(v,a,v,bb); ADD64(v,a,m,2*y);
    XOR64(v,d,v,a); ROT(v,d,16);
    ADD64(v,cc,v,d); XOR64(v,bb,v,cc); ROT(v,bb,63);
  }
  function compress(last){
    for(let i=0;i<16;i++) v[i]=h[i];
    for(let i=0;i<16;i++) v[i+16]=IV[i];
    // The byte counter is XORed in, not added. With an empty message the
    // counter is zero and the two are indistinguishable, which is how a bug
    // here hides until the first real input.
    v[24] = (v[24] ^ (t>>>0))>>>0;
    v[25] = (v[25] ^ Math.floor(t/0x100000000))>>>0;
    if(last){ v[28]=~v[28]>>>0; v[29]=~v[29]>>>0; }
    for(let i=0;i<32;i++) m[i]=(buf[i*4]|(buf[i*4+1]<<8)|(buf[i*4+2]<<16)|(buf[i*4+3]<<24))>>>0;
    for(let r=0;r<12;r++){
      G(r,0,0,8,16,24); G(r,1,2,10,18,26); G(r,2,4,12,20,28); G(r,3,6,14,22,30);
      G(r,4,0,10,20,30); G(r,5,2,12,22,24); G(r,6,4,14,16,26); G(r,7,6,8,18,28);
    }
    for(let i=0;i<16;i++){ h[i]^=v[i]^v[i+16]; }
  }
  for(const byte of input){
    if(c===128){ t+=128; compress(false); c=0; }
    buf[c++]=byte;
  }
  t+=c; while(c<128) buf[c++]=0;
  compress(true);
  const out=new Uint8Array(outlen);
  for(let i=0;i<outlen;i++) out[i]=(h[i>>2]>>(8*(i&3)))&0xff;
  return out;
}

async function verifySignature(verifyKey, signature, message){
  if(!(crypto.subtle && crypto.subtle.importKey)) return null;
  try{
    const key = await crypto.subtle.importKey('raw', verifyKey, {name:'Ed25519'}, false, ['verify']);
    return await crypto.subtle.verify({name:'Ed25519'}, key, signature, message);
  }catch(e){ return null; }   // this browser cannot do Ed25519
}

/* Everything shown is read out of the bytes that were signed, never out of
   the loose fields sitting beside them. Those are there to be read by a
   person; a signature does not cover them, so trusting them would let anyone
   publish a valid signature next to invented loot. */
function signedBody(rec){
  try{
    const text = new TextDecoder().decode(b64(rec.canonical));
    const body = JSON.parse(text);
    return {
      outcome: body.outcome,
      roster: Object.fromEntries(body.roster),
      air: Object.fromEntries(body.air),
      carried: Object.fromEntries((body.carried||[]).map(([who, items]) =>
        [who, (items||[]).map(([kind,name,tier,value]) => ({kind,name,tier,value}))])),
    };
  }catch(e){ return null; }
}

function tally(records, checked){
  const people = {};
  const seat = a => people[a] || (people[a] = {name:a, cleared:0, delves:0, items:[], coin:0});
  records.forEach(rec => {
    const body = signedBody(rec);
    if(!body) return;
    const good = Object.keys(rec.signatures||{}).filter(a => checked[rec.id+':'+a]);
    Object.keys(body.roster).forEach(who => {
      if(!good.includes(who)) return;
      const others = good.filter(a => a !== who);
      const witnessed = others.some(a => body.air[a]) && body.air[who];
      if(!witnessed) return;
      const p = seat(who);
      p.name = body.roster[who] || who;
      p.delves++;
      if(body.outcome === 'cleared') p.cleared++;
      (body.carried[who]||[]).forEach(it => {
        if(it.kind === 'coin') p.coin += it.value;
        else p.items.push(it);
      });
    });
  });
  return Object.entries(people).map(([a,p]) => ({address:a, ...p}))
    .sort((x,y) => y.cleared - x.cleared || y.items.length - x.items.length
                   || x.address.localeCompare(y.address));
}

function render(rows, note){
  if(!rows.length){
    table.innerHTML = '';
    state.innerHTML = note || 'No witnessed delves in these records.';
    return;
  }
  state.innerHTML = note;
  const head = '<tr><th>who</th><th>cleared</th><th>delves</th><th>coin</th><th>carried out</th></tr>';
  table.innerHTML = head + rows.map(p => {
    const kit = p.items.slice(-14).map(i =>
      `<span>${i.name.replace(/[<>&]/g,'')}${TIERS[i.tier]||''}</span>`).join(', ');
    return `<tr class="rank"><td><div>${(p.name||'').replace(/[<>&]/g,'')}</div>` +
           `<div class="who">${p.address}</div></td>` +
           `<td class="num">${p.cleared}</td><td class="num">${p.delves}</td>` +
           `<td class="num">${p.coin}</td><td class="items">${kit || '&mdash;'}</td></tr>`;
  }).join('');
}

/* Where the records come from. A node serving its own board is the usual
   case and needs no arrangement at all. Several sources can be given and are
   merged: two people who were both there hold the same record with different
   signatures on it, and between them the picture is fuller than either had. */
/* A repository is fetched straight from raw.githubusercontent.com, which
   serves CORS headers, so a page on any host can read a board out of any
   public repository. That is what lets a static site show a live board with
   nothing to redeploy: the client pushes the file to the repository and this
   page reads it from there. */
function ghUrl(spec){
  const bits = spec.replace(/^https?:\/\//, '').split('/').filter(Boolean);
  if(bits.length < 2) return null;
  const [owner, repo, ...rest] = bits;
  const path = rest.length ? rest.join('/') : 'board.json';
  return `https://raw.githubusercontent.com/${owner}/${repo}/HEAD/${path}?t=${Date.now()}`;
}

function sources(){
  const q = new URLSearchParams(location.search);
  const gh = q.get('gh') || (q.get('from') ? '' : DEFAULT_GH);
  if(gh) return gh.split(',').map(s => ghUrl(s.trim())).filter(Boolean);
  const asked = q.get('from');
  if(asked) return asked.split(',').map(s => s.trim()).filter(Boolean)
    .map(s => s.endsWith('.json') ? s : s.replace(/\/$/, '') + '/board.json');
  return ['board.json'];
}

function merge(all){
  const byId = {};
  all.forEach(data => (data.records||[]).forEach(rec => {
    const seen = byId[rec.id];
    if(!seen){ byId[rec.id] = JSON.parse(JSON.stringify(rec)); return; }
    Object.assign(seen.signatures, rec.signatures||{});
    Object.assign(seen.keys, rec.keys||{});
  }));
  return Object.values(byId);
}

(async () => {
  const where = sources();
  const gathered = [];
  for(const url of where){
    try{
      const res = await fetch(url, {cache:'no-store'});
      if(res.ok) gathered.push(await res.json());
    }catch(e){ /* a node that is not running is not an error */ }
  }
  if(!gathered.length){
    state.innerHTML = where[0] === 'board.json'
      ? 'Nothing here. A board lives on a node: run the game with '
        + '<code>--web</code> and open <code>/board</code> on it. To fill a page '
        + 'like this one, add <code>--publish-to owner/repo</code> and point it '
        + 'at that repository with <code>?gh=owner/repo</code>.'
      : 'Could not reach ' + where.map(w => '<code>' + w.replace(/[<>&]/g,'') + '</code>').join(', ') + '.';
    return;
  }

  const records = merge(gathered);
  const checked = {};
  let verified = 0, unverifiable = 0, rejected = 0, engineMissing = false;

  for(const rec of records){
    const message = b64(rec.canonical);
    // The id must be the hash of the bytes that were signed, or the record
    // has been edited since.
    const digest = hex(blake2b(message, 8));
    const intact = digest === rec.id;
    for(const [who, sig] of Object.entries(rec.signatures || {})){
      const pair = (rec.keys || {})[who];
      if(!intact || !pair){ rejected++; continue; }
      const pub = b64(pair[0]), ver = b64(pair[1]);
      const both = new Uint8Array(pub.length + ver.length);
      both.set(pub); both.set(ver, pub.length);
      // Recompute the address from both keys. This is what stops an invented
      // signing key being attached to a real name.
      if(hex(blake2b(both, 3)) !== who){ rejected++; continue; }
      const ok = await verifySignature(ver, b64(sig), message);
      if(ok === null){ engineMissing = true; unverifiable++; checked[rec.id+':'+who] = true; }
      else if(ok){ verified++; checked[rec.id+':'+who] = true; }
      else rejected++;
    }
  }

  let note;
  if(engineMissing){
    note = `<strong>${records.length} record(s)</strong>, addresses checked, but this ` +
      `browser cannot verify Ed25519 signatures, so they have been taken on trust. ` +
      `A current Firefox, Safari or Chrome will check them properly.`;
  }else{
    note = `<strong>${records.length} record(s)</strong> from ${where.length} source(s), ` +
      `${verified} signature(s) verified in this browser` +
      (rejected ? `, ${rejected} rejected` : ``) + `.`;
  }
  render(tally(records, checked), note);
})();
</script>
</body>
</html>
'''
