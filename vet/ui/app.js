/* ═══════════════════════════════════════════════════════════════════
   VET — front-of-house instrument.
   Vanilla JS. No deps. Nothing here may throw mid-demo: every entry
   point (event dispatch, rAF, audio) is wrapped.
   ═══════════════════════════════════════════════════════════════════ */
'use strict';

/* ── tiny utils ─────────────────────────────────────────────────── */
const $  = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const safe = fn => function (...a) { try { return fn.apply(this, a); } catch (e) { console.warn('[vet]', e); } };

function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
}
function fnv(str) { // deterministic seed from probe id
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 0x01000193); }
  return h >>> 0;
}
function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const fmt = {
  cents: v => (v * 100).toFixed(1) + '¢',
  usd:   v => '$' + v.toFixed(2),
  pct1:  v => (v * 100).toFixed(1),
  clock: s => { s = Math.max(0, s); const m = Math.floor(s / 60); return m + ':' + String(Math.floor(s % 60)).padStart(2, '0'); },
};

/* ── config / url params ────────────────────────────────────────── */
const QS      = new URLSearchParams(location.search);
const truthy  = v => ['1', 'true', 'yes', 'on'].includes(String(v == null ? '' : v).toLowerCase());
/* ?demo=1 — the stage path: play the pre-recorded canonical run, no server. */
const DEMO    = truthy(QS.get('demo'));
const REPLAY  = truthy(QS.get('replay')) || !!(QS.get('replay') && QS.get('replay') !== '0');
const SPEED   = Math.max(0.1, parseFloat(QS.get('speed') || '1') || 1);
const AUTO    = QS.get('autostart') === '1';
const JUMP    = parseFloat(QS.get('jump') || '0') || 0;
const PAUSE   = QS.get('pause') === '1';
const WS_URL  = 'ws://localhost:8787/ws';

/* Two mutually exclusive worlds, and the header always says which one you are in:
   REPLAY  — a recording is driving the UI. Nothing can fail, nothing is computed.
   LIVE/SIM — the server is driving it over the websocket, for real, right now. */
let replayMode = REPLAY || DEMO;
let speed = SPEED;

const SLOT_COLORS = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'];
const CI_MAX = 0.60;          // shared x-scale for CI bars: 0..60 % error
const ROW_H  = 65;

/* ── global state ───────────────────────────────────────────────── */
const state = {
  started: false, demoStarted: false, act3: false, phase: null, seeking: false,
  details: false,
  modeSel: 'live',                                   // live | sim toggle
  vendors: new Map(), vendorOrder: [],
  strata: new Map(), probes: new Map(),
  results: new Map(),                                // probeId -> Map(vendorId -> result)
  standings: new Map(),                              // vendorId -> latest standing
  agg: new Map(),                                    // vendorId -> {werSum,n,lat}
  elimOrder: [],
  currentProbe: null, selectedVendor: null, autoFollow: true,
  verdict: null,
  sting: {
    mode: null, threshold: 5, cusum: [], alarmed: false,
    shadyPlace: null, premiumServed: null,
    agg: { naive: new Map(), stealth: new Map() },   // vendorId -> {werSum,n,entErr,entN}
  },
};

/* ── run clock (event t + wall interpolation) ───────────────────── */
const clock = {
  base: 0, anchor: performance.now(), scale: replayMode ? speed : 1, running: false,
  sync(t) { this.base = Math.max(this.base, t); this.anchor = performance.now(); },
  now() { return this.running ? this.base + (performance.now() - this.anchor) / 1000 * this.scale : this.base; },
  freeze() { this.base = this.now(); this.running = false; },
};

/* ── animated numbers ───────────────────────────────────────────── */
const tickers = [];
class Tick {
  constructor(node, format, rate = 6) { this.node = node; this.format = format; this.rate = rate; this.v = 0; this.t = 0; this.last = ''; tickers.push(this); }
  set(v, snap) { this.t = v; if (snap || state.seeking) this.v = v; }
  frame(dt) {
    this.v += (this.t - this.v) * Math.min(1, dt * this.rate);
    if (Math.abs(this.t - this.v) < 1e-4) this.v = this.t;
    const s = this.format(this.v);
    if (s !== this.last) { this.node.textContent = s; this.last = s; }
  }
}

/* ═══════════════ AUDIO + WAVEFORM ═══════════════ */
let _actx = null;
function actx() {
  if (_actx) return _actx;
  try { _actx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { _actx = null; }
  return _actx;
}

/* deterministic “phone call” synth — used whenever real audio is absent */
function synthSamples(id, durS) {
  const sr = 22050, len = Math.floor(clamp(durS || 6, 2, 10) * sr);
  const out = new Float32Array(len);
  const rnd = mulberry32(fnv(id));
  let i = 0;
  const f0base = 105 + rnd() * 95;
  while (i < len) {
    const syl = Math.floor((0.09 + rnd() * 0.16) * sr);       // syllable
    const gap = Math.floor((0.02 + rnd() * (rnd() < 0.16 ? 0.30 : 0.08)) * sr);
    const f0 = f0base * (0.9 + rnd() * 0.35);
    const noisy = rnd() < 0.3;
    const amp = 0.45 + rnd() * 0.55;                          // stress variation
    for (let j = 0; j < syl && i + j < len; j++) {
      const t = j / sr, w = Math.sin(Math.PI * j / syl);      // hann-ish
      let s = 0;
      if (!noisy) {
        const f = f0 * (1 + 0.02 * Math.sin(2 * Math.PI * 5.2 * t));
        for (let h = 1; h <= 5; h++) s += Math.sin(2 * Math.PI * f * h * t + h) / (h * 1.35);
        s *= 0.34;
      }
      s += (rnd() * 2 - 1) * (noisy ? 0.34 : 0.05) * w;
      out[i + j] += s * w * amp;
    }
    i += syl + gap;
  }
  // room babble + band-limit + μ-law grit  → sounds like a G.711 call
  let lp = 0, hp = 0, prev = 0;
  for (let n = 0; n < len; n++) {
    let s = out[n] + (mulberryNoise(n) * 0.018);
    lp += 0.42 * (s - lp);                 // ~3.4 kHz lowpass-ish
    const hpv = lp - prev; prev = lp; hp += 0.06 * (hpv - hp);
    s = lp - hp * 0.5;
    const c = Math.sign(s) * Math.log(1 + 255 * Math.min(1, Math.abs(s))) / Math.log(256);
    out[n] = c * 0.82;
  }
  return { samples: out, sr };
}
let _noiseSeed = mulberry32(1234), _noiseCache = new Float32Array(8192).map(() => _noiseSeed() * 2 - 1);
function mulberryNoise(n) { return _noiseCache[n & 8191]; }

function computePeaks(samples, buckets) {
  const peaks = new Float32Array(buckets * 2);
  const step = samples.length / buckets;
  for (let b = 0; b < buckets; b++) {
    let mn = 0, mx = 0;
    const s0 = Math.floor(b * step), s1 = Math.min(samples.length, Math.floor((b + 1) * step));
    for (let i = s0; i < s1; i += 2) { const v = samples[i]; if (v > mx) mx = v; if (v < mn) mn = v; }
    peaks[b * 2] = mn; peaks[b * 2 + 1] = mx;
  }
  return peaks;
}

class Waveform {
  constructor(canvas, btn, mini) {
    this.cv = canvas; this.btn = btn; this.mini = mini;
    this.probe = null; this.buffer = null; this.peaks = null;
    this.playing = false; this.progress = 0; this.gen = 0; this.pending = 0;
    this.idlePhase = 0; this.dirty = true;
    if (btn) btn.addEventListener('click', safe(() => this.toggle()));
  }
  setProbe(p) {
    this.gen++; this.stop();
    this.probe = p; this.buffer = null; this.peaks = null; this.progress = 0; this.dirty = true;
    if (this.btn) this.btn.disabled = true;
    // Seeking — and 4x playback — blow through hundreds of probes. Fetching a
    // clip for each one on the way past would stall the jump, so a probe has to
    // stay on screen long enough to be worth hearing before we load it.
    this.pending = this.gen; this.pendingAt = performance.now();
    if (!state.seeking) this.flush();
  }
  flush(force) {
    if (!this.pending || !this.probe) return;
    if (!force && performance.now() - this.pendingAt < 220) return;
    const gen = this.pending; this.pending = 0;
    this.load(this.probe, gen);
  }
  async load(p, gen) {
    let samples = null, sr = 22050;
    try {
      const res = await fetch(p.audio_url, { cache: 'force-cache' });
      if (!res.ok) throw 0;
      const ab = await res.arrayBuffer();
      const ctx = actx(); if (!ctx) throw 0;
      const buf = await ctx.decodeAudioData(ab);
      if (gen !== this.gen) return;
      this.buffer = buf; samples = buf.getChannelData(0); sr = buf.sampleRate;
    } catch (e) {
      if (gen !== this.gen) return;
      const syn = synthSamples(p.id, p.duration_s);
      samples = syn.samples; sr = syn.sr;
      const ctx = actx();
      if (ctx) {
        try {
          const buf = ctx.createBuffer(1, samples.length, sr);
          buf.copyToChannel(samples, 0);
          this.buffer = buf;
        } catch (err) { this.buffer = null; }
      }
    }
    this.peaks = computePeaks(samples, this.mini ? 220 : 640);
    if (this.btn) this.btn.disabled = !this.buffer;
    this.dirty = true;
  }
  toggle() { this.playing ? this.stop() : this.play(); }
  play() {
    const ctx = actx(); if (!ctx || !this.buffer) return;
    try {
      if (ctx.state === 'suspended') ctx.resume();
      this.stop();
      const src = ctx.createBufferSource(); src.buffer = this.buffer;
      const g = ctx.createGain(); g.gain.value = 0.9;
      src.connect(g); g.connect(ctx.destination); src.start();
      this.src = src; this.t0 = ctx.currentTime; this.playing = true;
      this.setBtnGlyph(true);
      src.onended = safe(() => { if (this.src === src) { this.playing = false; this.progress = 0; this.dirty = true; this.setBtnGlyph(false); } });
    } catch (e) { /* never throw on stage */ }
  }
  stop() {
    if (this.src) { try { this.src.onended = null; this.src.stop(); } catch (e) {} this.src = null; }
    this.playing = false; this.progress = 0; this.dirty = true; this.setBtnGlyph(false);
  }
  setBtnGlyph(playing) {
    if (!this.btn) return;
    const p = this.btn.querySelector('#icoPlay'), s = this.btn.querySelector('#icoStop');
    if (p && s) { p.classList.toggle('hidden', playing); s.classList.toggle('hidden', !playing); }
  }
  frame(dt) {
    if (this.pending && !state.seeking) this.flush();
    if (this.playing && this.buffer) {
      this.progress = clamp((actx().currentTime - this.t0) / this.buffer.duration, 0, 1);
      this.dirty = true;
    }
    if (!this.probe && !this.mini) { this.idlePhase += dt; this.drawIdle(); return; }
    if (this.dirty) { this.draw(); this.dirty = false; }
  }
  fit() {
    const dpr = window.devicePixelRatio || 1;
    const w = this.cv.clientWidth, h = this.cv.clientHeight;
    if (!w || !h) return null;
    if (this.cv.width !== Math.round(w * dpr) || this.cv.height !== Math.round(h * dpr)) {
      this.cv.width = Math.round(w * dpr); this.cv.height = Math.round(h * dpr);
    }
    const g = this.cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { g, w, h };
  }
  drawIdle() {
    const c = this.fit(); if (!c) return;
    const { g, w, h } = c, mid = h / 2;
    g.clearRect(0, 0, w, h);
    g.strokeStyle = 'rgba(144,133,233,0.30)'; g.lineWidth = 1.5; g.beginPath();
    for (let x = 0; x <= w; x += 3) {
      const a = Math.sin(x / 46 + this.idlePhase * 1.7) * Math.sin(x / 210 - this.idlePhase * 0.6);
      const y = mid + a * h * 0.10 * (1 + 0.4 * Math.sin(this.idlePhase * 0.8));
      x === 0 ? g.moveTo(x, y) : g.lineTo(x, y);
    }
    g.stroke();
  }
  draw() {
    const c = this.fit(); if (!c || !this.peaks) return;
    const { g, w, h } = c, mid = h / 2, n = this.peaks.length / 2;
    g.clearRect(0, 0, w, h);
    g.strokeStyle = 'rgba(255,255,255,0.07)'; g.lineWidth = 1;
    g.beginPath(); g.moveTo(0, mid + 0.5); g.lineTo(w, mid + 0.5); g.stroke();
    const px = this.progress * w;
    const bw = w / n;
    for (let i = 0; i < n; i++) {
      const x = i * bw;
      const mn = this.peaks[i * 2], mx = this.peaks[i * 2 + 1];
      const y0 = mid - mx * mid * 0.94, y1 = mid - mn * mid * 0.94;
      g.fillStyle = x <= px ? '#c9c2f6' : 'rgba(144,133,233,0.48)';
      g.fillRect(x, Math.min(y0, y1), Math.max(1, bw - 0.35), Math.max(1.4, Math.abs(y1 - y0)));
    }
    if (this.playing) {
      g.fillStyle = 'rgba(255,255,255,0.9)';
      g.fillRect(px, 2, 1.5, h - 4);
    }
  }
}

/* ═══════════════ HEADER / CLOCK / COST ═══════════════ */
const elapsedTick = new Tick($('#elapsed'), v => fmt.clock(v), 20);
const costHdr  = new Tick($('#costTicker'), fmt.cents, 5);
const costBig  = new Tick($('#costBig'), fmt.cents, 5);
const nodes = {
  phaseTitle: $('#phaseTitle'), phaseSub: $('#phaseSub'),
  budgetFill: $('#budgetFill'), budgetLabel: $('#budgetLabel'), probesRun: $('#probesRun'),
  modeChip: $('#modeChip'), reconChip: $('#reconChip'),
};

function setPhaseHeader(title, sub) {
  for (const n of [nodes.phaseTitle, nodes.phaseSub]) { n.classList.remove('phase-swap'); void n.offsetWidth; n.classList.add('phase-swap'); }
  nodes.phaseTitle.textContent = title;
  nodes.phaseSub.textContent = sub || '';
}
function setActNav(phase) {
  const order = ['compile', 'race', 'sting'];
  const key = phase === 'verdict' ? 'race' : phase;
  const idx = order.indexOf(key);
  $$('.act').forEach(a => {
    const i = order.indexOf(a.dataset.act);
    a.classList.toggle('on', i === idx);
    a.classList.toggle('done', i > -1 && idx > -1 && i < idx);
  });
}

/* ═══════════════ TYPEWRITER ═══════════════ */
const tw = {
  node: $('#thoughtText'), stream: $('#thoughtStream'), queue: '', shown: '',
  push(t) { this.queue += t; },
  frame(dt) {
    if (!this.queue) return;
    if (state.seeking) { this.shown += this.queue; this.queue = ''; }
    else {
      const burst = this.queue.length > 260 ? 5 : 1;
      let n = Math.max(1, Math.round(115 * dt * clock.scale * burst));
      this.shown += this.queue.slice(0, n); this.queue = this.queue.slice(n);
    }
    this.node.textContent = this.shown;
    this.stream.scrollTop = this.stream.scrollHeight;
  },
};

/* ═══════════════ LEADERBOARD ═══════════════ */
const board = { rows: new Map(), container: $('#boardRows') };

function shortName(v) { return (v.name || v.id).split(' ')[0]; }
function vendorColor(v) { return SLOT_COLORS[v.slot % SLOT_COLORS.length]; }

function buildCIAxis() {
  const ax = $('#ciAxis');
  ax.innerHTML = '';
  for (let p = 0; p <= 60; p += 15) {
    const s = el('span', null, p + (p === 60 ? '%+' : ''));
    s.style.left = (p / 60 * 100) + '%';
    ax.appendChild(s);
  }
}
buildCIAxis();

function logLine(html, cls) {
  const log = $('#boardLog'); if (!log) return;
  const line = el('div', 'log-line' + (cls ? ' ' + cls : ''),
    `<span class="t">${fmt.clock(clock.now())}</span>${html}`);
  log.prepend(line);
  while (log.childElementCount > 3) log.lastElementChild.remove();
}

function ensureRow(v) {
  if (board.rows.has(v.id)) return board.rows.get(v.id);
  $('#boardIdle').classList.add('hidden');
  $('#boardCols').classList.remove('hidden');
  const root = el('div', 'board-row');
  const color = vendorColor(v);
  root.innerHTML = `
    <div class="r-rank">–</div>
    <div class="r-id">
      <span class="v-dot" style="background:${color}"></span>
      <span class="r-name"><b>${esc(v.name)}</b>
        <span class="r-stack">${esc(Object.values(v.stack || {}).join(' · '))} · $${(v.price_per_hr || 0).toFixed(2)}/hr</span>
      </span>
    </div>
    <div class="r-ci"><div class="ci-grid"></div>
      <div class="ci-range" style="background:${color};left:0;width:0"></div>
      <div class="ci-dot" style="background:${color};left:0;opacity:0"></div>
    </div>
    <div class="r-num r-err"><span class="v">—</span><br><small class="n"></small></div>
    <div class="r-num r-wer">—</div>
    <div class="r-num r-lat">—</div>
    <div class="r-num r-spend">—</div>
    <div class="r-pb"><span class="num">—</span><span class="pb-meter"><span class="pb-fill" style="background:${color}"></span></span></div>`;
  const row = {
    root, v,
    rank: root.querySelector('.r-rank'),
    name: root.querySelector('.r-name'),
    range: root.querySelector('.ci-range'), dot: root.querySelector('.ci-dot'),
    err: root.querySelector('.r-err .v'), n: root.querySelector('.r-err .n'),
    wer: root.querySelector('.r-wer'), lat: root.querySelector('.r-lat'),
    spend: root.querySelector('.r-spend'), pb: root.querySelector('.r-pb .num'),
    pbFill: root.querySelector('.pb-fill'),
  };
  board.rows.set(v.id, row);
  board.container.appendChild(root);
  layoutBoard();
  return row;
}

function layoutBoard() {
  const ids = state.vendorOrder.filter(id => board.rows.has(id));
  const active = ids.filter(id => !state.vendors.get(id).eliminated);
  const elim = state.elimOrder.filter(id => board.rows.has(id));
  active.sort((a, b) => {
    const sa = state.standings.get(a), sb = state.standings.get(b);
    const ma = sa ? sa.mean : 9, mb = sb ? sb.mean : 9;
    return ma - mb || state.vendors.get(a).slot - state.vendors.get(b).slot;
  });
  const order = active.concat(elim);
  board.container.style.height = order.length * ROW_H + 'px';
  order.forEach((id, i) => {
    const row = board.rows.get(id);
    row.root.style.transform = `translateY(${i * ROW_H}px)`;
    const isElim = state.vendors.get(id).eliminated;
    row.root.classList.toggle('elim', !!isElim);
    row.root.classList.toggle('lead', !isElim && i === 0);
    row.rank.textContent = isElim ? '✕' : (i + 1);
  });
}

const handleStanding = safe(ev => {
  const v = state.vendors.get(ev.vendor_id); if (!v) return;
  state.standings.set(ev.vendor_id, ev);
  // Act 3 has its own two boards and the main one is off-screen — don't grow it
  // a seventh row for Shady behind the takeover.
  if (state.act3) return;
  const row = ensureRow(v);
  const x = p => clamp(p / CI_MAX, 0, 1.02) * 100;
  row.range.style.left = x(ev.lo) + '%';
  row.range.style.width = Math.max(0.5, x(ev.hi) - x(ev.lo)) + '%';
  row.dot.style.left = x(ev.mean) + '%';
  row.dot.style.opacity = 1;
  row.err.innerHTML = fmt.pct1(ev.mean) + '<small>%</small>';
  row.n.textContent = 'n=' + ev.n;
  row.spend.textContent = (ev.spend_usd * 100).toFixed(1) + '¢';
  row.pb.textContent = ev.p_best.toFixed(2);
  row.pbFill.style.width = (ev.p_best * 100) + '%';
  layoutBoard();
});

const handleResultAgg = safe(ev => {
  const a = state.agg.get(ev.vendor_id) || { werSum: 0, n: 0, lat: 0 };
  a.werSum += ev.wer; a.n++;
  a.lat = a.lat ? a.lat * 0.7 + ev.latency_ms * 0.3 : ev.latency_ms;
  state.agg.set(ev.vendor_id, a);
  const row = board.rows.get(ev.vendor_id);
  if (row) {
    row.wer.innerHTML = fmt.pct1(a.werSum / a.n) + '<small>%</small>';
    row.lat.innerHTML = Math.round(a.lat) + '<small>ms</small>';
  }
});

/* ═══════════════ PROBE VIEWER (center) ═══════════════ */
const waveMain = new Waveform($('#wave'), $('#btnPlay'), false);
const pv = {
  id: $('#probeId'), stratum: $('#probeStratum'), chips: $('#probeChips'),
  dur: $('#waveDur'), wrap: $('#diffWrap'), idle: $('#probeIdle'),
  tabs: $('#vendorTabs'), ref: $('#diffRef'), hyp: $('#diffHyp'),
  ents: $('#entChips'), stats: $('#resStats'),
};

function featureChips(f, extra) {
  const chips = [];
  if (f) {
    if (f.snr_db != null) chips.push([`SNR ${f.snr_db} dB`, f.snr_db <= 8]);
    if (f.codeswitch) chips.push([`CS ${String(f.codeswitch).toUpperCase()}`, f.codeswitch === 'extreme']);
    if (f.codec) chips.push([String(f.codec).toUpperCase() === 'G711' ? 'G.711 μ-LAW' : String(f.codec).toUpperCase(), false]);
    if (f.disfluency) chips.push(['DISFLUENT', false]);
  }
  (extra || []).forEach(c => chips.push(c));
  return chips.map(([t, warm]) => `<span class="f-chip${warm ? ' warm' : ''}">${esc(t)}</span>`).join('');
}

const showProbe = safe(p => {
  state.currentProbe = p; state.selectedVendor = null; state.autoFollow = true;
  pv.id.textContent = p.id;
  const st = state.strata.get(p.stratum);
  pv.stratum.textContent = st ? st.name : p.stratum;
  pv.chips.innerHTML = featureChips(p.features);
  pv.dur.textContent = (p.duration_s || 0).toFixed(1) + ' s';
  pv.idle.classList.add('hidden');
  pv.wrap.classList.remove('hidden');
  pv.tabs.innerHTML = '';
  pv.ents.innerHTML = ''; pv.stats.textContent = '';
  renderDiff([['ok', p.text]], null);          // ref only until first hyp arrives
  waveMain.setProbe(p);
});

/* score.py aligns *tokens*: ["ok","mg","mg"], ["sub","cinco","cuatro"],
   ["del","de",""], ["ins","","the"]. They carry no whitespace of their own, so
   the strip has to put it back — and an insertion's word lives in seg[2], not
   seg[1], or it renders as nothing at all. */
function renderDiff(diff, result) {
  const tok = (cls, s) => (s == null || s === '') ? '' : `<span class="${cls}">${esc(s)}</span> `;
  let refH = '', hypH = '';
  for (const seg of (diff || [])) {
    const op = seg[0];
    if (op === 'ok') { refH += tok('t-ok', seg[1]); hypH += tok('t-ok', seg[2] != null && seg[2] !== '' ? seg[2] : seg[1]); }
    else if (op === 'sub') { refH += tok('t-ref', seg[1]); hypH += tok('t-sub', seg[2]); }
    else if (op === 'del') { refH += tok('t-del', seg[1]); }
    else if (op === 'ins') { hypH += tok('t-ins', seg[2] != null && seg[2] !== '' ? seg[2] : seg[1]); }
    else if (seg[1] != null) { refH += tok('t-ok', seg[1]); hypH += tok('t-ok', seg[1]); }
  }
  pv.ref.innerHTML = refH;
  pv.hyp.innerHTML = result ? hypH : '<span class="t-ok" style="color:var(--muted)">waiting for vendor hypotheses…</span>';
  if (result) {
    pv.ents.innerHTML = (result.entity_detail || []).map(d => `
      <span class="e-chip ${d.ok ? 'ok' : 'bad'}">
        <span class="e-kind">${d.kind === 'phone' ? 'TEL' : 'RX'}</span>${esc(d.ref)}
        <span class="e-mark">${d.ok ? '✓' : '✗'}</span>${d.ok ? '' : `<span class="e-hyp">→ ${esc(d.hyp)}</span>`}
      </span>`).join('');
    pv.stats.innerHTML = `WER <b>${fmt.pct1(result.wer)}%</b> · <b>${result.latency_ms}</b> ms · <b>${(result.cost_usd * 100).toFixed(2)}¢</b>`;
  }
}

const selectResult = safe(vendorId => {
  const p = state.currentProbe; if (!p) return;
  const r = (state.results.get(p.id) || new Map()).get(vendorId); if (!r) return;
  state.selectedVendor = vendorId;
  $$('#vendorTabs .vtab').forEach(t => t.classList.toggle('on', t.dataset.v === vendorId));
  renderDiff(r.diff && r.diff.length ? r.diff : [['ok', p.text]], r);
});

const onProbeResult = safe(r => {
  const p = state.currentProbe;
  if (!p || r.probe_id !== p.id) return;
  const v = state.vendors.get(r.vendor_id); if (!v) return;
  const tab = el('button', 'vtab ' + (r.entity_ok ? 'pass' : 'fail'),
    `<span class="v-dot" style="background:${vendorColor(v)}"></span>${esc(shortName(v))}`);
  tab.dataset.v = r.vendor_id;
  tab.addEventListener('click', () => { state.autoFollow = false; selectResult(r.vendor_id); });
  pv.tabs.appendChild(tab);
  if (state.autoFollow) {
    const cur = state.selectedVendor && (state.results.get(p.id) || new Map()).get(state.selectedVendor);
    if (!cur || (cur.entity_ok && !r.entity_ok)) selectResult(r.vendor_id);
  }
});

/* ═══════════════ RIGHT RAIL — cost + memo ═══════════════ */
const handleCost = safe(ev => {
  costHdr.set(ev.total_usd); costBig.set(ev.total_usd);
  const frac = clamp(ev.total_usd / (ev.budget_usd || 2), 0, 1);
  nodes.budgetFill.style.width = (frac * 100) + '%';
  nodes.budgetFill.classList.toggle('warn', frac >= 0.6 && frac < 0.85);
  nodes.budgetFill.classList.toggle('crit', frac >= 0.85);
  nodes.budgetLabel.textContent = `${fmt.usd(ev.total_usd)} of ${fmt.usd(ev.budget_usd || 2)}`;
  nodes.probesRun.textContent = `${ev.probes_run} probes`;
  $('#budgetCap').textContent = 'cap ' + fmt.usd(ev.budget_usd || 2);
});

function renderMarkdown(md) {
  const inline = s => esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<i>$2</i>');
  const out = [];
  let list = null;
  const closeList = () => { if (list) { out.push(list === 'ul' ? '</ul>' : '</ol>'); list = null; } };
  for (const raw of String(md || '').split('\n')) {
    const line = raw.trimEnd();
    let m;
    if (!line.trim()) { closeList(); continue; }
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) { closeList(); const h = Math.min(4, m[1].length + 1); out.push(`<h${h}>${inline(m[2])}</h${h}>`); }
    else if ((m = line.match(/^[-*]\s+(.*)$/))) { if (list !== 'ul') { closeList(); out.push('<ul>'); list = 'ul'; } out.push(`<li>${inline(m[1])}</li>`); }
    else if ((m = line.match(/^\d+\.\s+(.*)$/))) { if (list !== 'ol') { closeList(); out.push('<ol>'); list = 'ol'; } out.push(`<li>${inline(m[1])}</li>`); }
    else { closeList(); out.push(`<p>${inline(line)}</p>`); }
  }
  closeList();
  return out.join('\n');
}

/* The one line the room reads: who won, how wrong it is on this buyer's calls,
   how sure we are, what the answer cost. Everything else on this screen is the
   working that backs it up. */
const showHero = safe(ev => {
  const v = state.vendors.get(ev.winner);
  const s = state.standings.get(ev.winner);
  $('#heroDot').style.background = v ? vendorColor(v) : 'var(--accent)';
  $('#heroName').textContent = (v ? v.name : ev.winner_name) || ev.winner;
  const bits = [];
  if (s) bits.push(`<em>${fmt.pct1(s.mean)}%</em> error on your calls`);
  bits.push(`<em>${Math.round(ev.p_best * 100)}%</em> confident`);
  bits.push(`cost you <em>${fmt.cents(ev.cost_usd)}</em>`);
  $('#heroRest').innerHTML = ' — ' + bits.join(' · ');
  $('#boardHero').classList.remove('hidden');
});

const handleVerdict = safe(ev => {
  state.verdict = ev;
  showHero(ev);
  const v = state.vendors.get(ev.winner);
  const card = $('#memoCard');
  card.classList.remove('hidden');
  card.classList.remove('pending');
  $('#memoMeta').textContent = 'signed verdict';
  logLine(`<b>verdict signed</b> — ${esc(v ? v.name : ev.winner)} · P(best) ${ev.p_best.toFixed(2)} · ${fmt.cents(ev.cost_usd)}`);
  $('#memoDot').style.background = v ? vendorColor(v) : 'var(--accent)';
  $('#memoName').textContent = v ? v.name : ev.winner;
  $('#memoSub').textContent = `winner · ${fmt.cents(ev.cost_usd)} · ${Math.round(ev.elapsed_s)} s`;
  $('#memoPB').textContent = ev.p_best.toFixed(2);
  $('#memoHeadline').textContent = ev.headline || '';
  $('#memoBody').innerHTML = renderMarkdown(ev.memo_md);
  $('#memoBody').querySelectorAll('code').forEach(c => {
    const id = c.textContent.trim();
    if (state.probes.has(id)) {
      c.classList.add('probe-link');
      c.addEventListener('click', safe(() => {
        showProbe(state.probes.get(id));
        const rs = state.results.get(id);
        if (rs) {
          for (const [vid, r] of rs) { onProbeResult(r); }
          const bad = [...rs.values()].find(r => !r.entity_ok);
          selectResult(bad ? bad.vendor_id : [...rs.keys()][0]);
          state.autoFollow = false;
        }
      }));
    }
  });
  const row = board.rows.get(ev.winner);
  if (row) {
    row.root.classList.add('winner');
    if (!row.name.querySelector('.r-tag')) {
      row.name.appendChild(el('span', 'r-tag win', `WINNER · P(BEST) ${ev.p_best.toFixed(2)}`));
    }
  }
  $('#railR').scrollTop = 0;
});

/* ═══════════════ ACT 3 — STING ═══════════════ */
const waveNaive = new Waveform($('#waveNaive'), $('#probeStripNaive .play-btn'), true);
const waveStealth = new Waveform($('#waveStealth'), $('#probeStripStealth .play-btn'), true);

class Gauge {
  constructor(svg, word) {
    this.svg = svg; this.word = word;
    this.v = 0.5; this.t = null; this.build();
  }
  pt(a, r) { // a: 0..1 across the dial, left→right over the top
    const th = Math.PI * (1 - a);
    return [110 + r * Math.cos(th), 118 - r * Math.sin(th)];
  }
  arc(a0, a1, r) {
    const [x0, y0] = this.pt(a0, r), [x1, y1] = this.pt(a1, r);
    return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${r} ${r} 0 0 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
  }
  build() {
    const s = this.svg;
    s.innerHTML = `
      <path class="gauge-track" d="${this.arc(0, 1, 88)}"></path>
      <path class="gauge-val" d="" stroke="var(--good)"></path>
      <text class="gauge-min" x="14" y="131">0.5</text>
      <text class="gauge-min" x="110" y="16" text-anchor="middle">0.75</text>
      <text class="gauge-min" x="206" y="131" text-anchor="end">1.0</text>
      <text class="gauge-num" x="110" y="112" text-anchor="middle" fill="var(--muted)">0.50</text>`;
    this.val = s.querySelector('.gauge-val');
    this.num = s.querySelector('.gauge-num');
  }
  set(auc) {
    if (this.t == null) this.num.setAttribute('fill', 'var(--ink)');
    this.t = clamp(auc, 0.5, 1);
    if (state.seeking) this.v = this.t;
  }
  frame(dt) {
    if (this.t == null) return;
    this.v += (this.t - this.v) * Math.min(1, dt * 3.2);
    if (Math.abs(this.t - this.v) < 0.0004) this.v = this.t;
    const a = (this.v - 0.5) / 0.5;
    this.val.setAttribute('d', a > 0.004 ? this.arc(0, a, 88) : '');
    const color = this.v >= 0.8 ? 'var(--crit)' : this.v >= 0.65 ? 'var(--serious)' : 'var(--good)';
    this.val.setAttribute('stroke', color);
    this.num.textContent = this.v.toFixed(2);
    if (this.word) {
      const w = this.v >= 0.8 ? 'OBVIOUS TELL' : this.v >= 0.65 ? 'SUSPICIOUS' : 'BLIND — CAN’T TELL';
      this.word.textContent = w;
      this.word.className = 'gauge-word ' + (this.v >= 0.8 ? 'bad' : this.v >= 0.65 ? '' : 'blind');
    }
  }
}
const gaugeNaive = new Gauge($('#gaugeNaive'), $('#gaugeWordNaive'));
const gaugeStealth = new Gauge($('#gaugeStealth'), $('#gaugeWordStealth'));

/* CUSUM chart */
const cusum = {
  svg: $('#cusum'), W: 360, H: 168, L: 34, R: 10, T: 16, B: 22,
  x(i) { return this.L + (i / 10) * (this.W - this.L - this.R); },
  y(s) { return this.T + (1 - s / 7) * (this.H - this.T - this.B); },
  render: safe(function () {
    const pts = state.sting.cusum, th = state.sting.threshold;
    const g = [];
    for (const yv of [0, 2, 4, 6]) {
      g.push(`<line class="gl" x1="${this.L}" y1="${this.y(yv)}" x2="${this.W - this.R}" y2="${this.y(yv)}"></line>`);
      g.push(`<text class="ax-t" x="${this.L - 6}" y="${this.y(yv) + 3}" text-anchor="end">${yv}</text>`);
    }
    for (const xv of [2, 4, 6, 8, 10]) {
      g.push(`<text class="ax-t" x="${this.x(xv)}" y="${this.H - 6}" text-anchor="middle">${xv}</text>`);
    }
    g.push(`<line class="thresh" x1="${this.L}" y1="${this.y(th)}" x2="${this.W - this.R}" y2="${this.y(th)}"></line>`);
    g.push(`<text class="thresh-t" x="${this.L + 4}" y="${this.y(th) - 6}">THRESHOLD ${th.toFixed(1)}</text>`);
    if (pts.length) {
      const path = ['M', this.x(0), this.y(0)];
      for (const p of pts) path.push('L', this.x(p.i), this.y(Math.min(p.s, 7)));
      g.push(`<path class="cusum-line" d="${path.join(' ')}"></path>`);
      for (const p of pts) {
        const alarm = p.s >= th;
        g.push(`<circle class="cusum-dot${alarm ? ' alarm' : ''}" cx="${this.x(p.i)}" cy="${this.y(Math.min(p.s, 7))}" r="${alarm ? 5 : 3.4}"></circle>`);
        if (alarm) g.push(`<text class="alarm-t" x="${this.x(p.i) - 10}" y="${this.y(Math.min(p.s, 7)) + 4}" text-anchor="end">ALARM ▸</text>`);
      }
    }
    this.svg.innerHTML = g.join('');
  }),
};
cusum.render();

/* per-mode mini leaderboards */
function stingRow(panelBoard, v) {
  const id = `mini-${panelBoard.id}-${v.id}`;
  let row = document.getElementById(id);
  if (row) return row;
  row = el('div', 'mini-row');
  row.id = id;
  row.innerHTML = `
    <span class="v-dot" style="background:${vendorColor(v)}"></span>
    <span class="mini-name"><b>${esc(v.name)}</b><small>${esc(Object.values(v.stack || {}).join(' · '))} · $${(v.price_per_hr || 0).toFixed(2)}/hr</small></span>
    <span class="mini-metric"><span class="num m-wer">—</span><label>WER</label></span>
    <span class="mini-metric"><span class="num m-err">—</span><label>ENT ERR</label></span>
    <span class="mini-badge"></span>`;
  panelBoard.appendChild(row);
  return row;
}

const updateStingBoards = safe(() => {
  for (const mode of ['naive', 'stealth']) {
    const panelBoard = mode === 'naive' ? $('#boardNaive') : $('#boardStealth');
    const agg = state.sting.agg[mode];
    for (const [vid, a] of agg) {
      const v = state.vendors.get(vid); if (!v) continue;
      const row = stingRow(panelBoard, v);
      const wer = a.werSum / a.n, err = a.entN ? a.entErr / a.entN : 0;
      row.querySelector('.m-wer').innerHTML = fmt.pct1(wer) + '<small>%</small>';
      row.querySelector('.m-err').innerHTML = fmt.pct1(err) + '<small>%</small>';
      const badge = row.querySelector('.mini-badge');
      if (vid === 'shady') {
        if (mode === 'naive') {
          const place = state.sting.shadyPlace || 1;
          row.classList.toggle('first', place === 1);
          badge.innerHTML = `<span class="r-tag gold">#${place} · TOO GOOD</span>`;
        } else {
          const na = state.sting.agg.naive.get('shady');
          if (na) {
            const d = wer - na.werSum / na.n;
            badge.innerHTML = `<span class="mini-delta up">▲ +${(d * 100).toFixed(1)} WER pts vs its own naive run</span>`;
          }
          if (state.sting.alarmed) {
            row.classList.add('busted');
            badge.innerHTML = '<span class="r-tag" style="background:var(--crit);color:#fff">DEFEAT DEVICE</span>';
          }
        }
      } else {
        badge.innerHTML = '<span class="mini-delta flat">— mode-invariant</span>';
      }
    }
  }
});

const handleStingMode = safe(ev => {
  state.sting.mode = ev.mode;
  const ex = $('#stingExplain');
  ex.classList.remove('phase-swap'); void ex.offsetWidth; ex.classList.add('phase-swap');
  ex.innerHTML = `<b style="color:var(--ink)">${ev.mode === 'naive' ? 'NAIVE EXAM' : 'STEALTH EXAM'}</b> — ${esc(ev.explain || '')}`;
  const live = ev.mode === 'naive' ? $('#panelNaive') : $('#panelStealth');
  const other = ev.mode === 'naive' ? $('#panelStealth') : $('#panelNaive');
  live.classList.add('live'); other.classList.toggle('live', ev.mode === 'stealth'); // keep naive visible once done
  if (ev.mode === 'stealth') $('#panelNaive').classList.add('live');
  const sn = $('#statusNaive'), ss = $('#statusStealth');
  if (ev.mode === 'naive') { sn.textContent = 'RUNNING'; sn.classList.add('on'); }
  else {
    sn.textContent = naiveStatus();
    sn.classList.remove('on'); sn.classList.add('done');
    ss.textContent = 'RUNNING'; ss.classList.add('on');
  }
});

function naiveStatus() {
  const p = state.sting.shadyPlace || 1;
  const served = state.sting.premiumServed;
  return `DONE · SHADY #${p}${served ? ' · PREMIUM ' + served : ''}`;
}

/* How the naive exam ended: where Shady placed, and how many probes its defeat
   device flagged as tests. Reported, not assumed — if it only got to #2 the
   panel says #2. */
const handleNaiveResult = safe(ev => {
  if (ev.shady_place) state.sting.shadyPlace = ev.shady_place;
  if (ev.premium_served) state.sting.premiumServed = ev.premium_served;
  const sn = $('#statusNaive');
  sn.textContent = naiveStatus();
  sn.classList.remove('on'); sn.classList.add('done');
  updateStingBoards();
});

const handleStingProbe = safe(p => {
  const mode = p.stealth.mode === 'naive' ? 'naive' : 'stealth';
  const wf = mode === 'naive' ? waveNaive : waveStealth;
  $(mode === 'naive' ? '#pidNaive' : '#pidStealth').textContent = p.id;
  const fill = $(mode === 'naive' ? '#camoNaive' : '#camoStealth');
  const val = $(mode === 'naive' ? '#camoValNaive' : '#camoValStealth');
  const sc = clamp(p.stealth.score || 0, 0, 1);
  fill.style.width = (sc * 100) + '%';
  fill.style.background = sc >= 0.7 ? 'var(--crit)' : sc >= 0.5 ? 'var(--serious)' : 'var(--good)';
  val.textContent = sc.toFixed(2);
  wf.setProbe(p);
});

const handleStingResult = safe(r => {
  const p = state.probes.get(r.probe_id);
  const mode = p && p.stealth ? (p.stealth.mode === 'naive' ? 'naive' : 'stealth') : (state.sting.mode || 'stealth');
  const agg = state.sting.agg[mode];
  const a = agg.get(r.vendor_id) || { werSum: 0, n: 0, entErr: 0, entN: 0 };
  a.werSum += r.wer; a.n++;
  for (const d of (r.entity_detail || [])) { a.entN++; if (!d.ok) a.entErr++; }
  agg.set(r.vendor_id, a);
  if (mode === 'stealth' && r.vendor_id === 'shady' && !r.entity_ok) {
    const bad = (r.entity_detail || []).find(d => !d.ok);
    if (bad) $('#lastMiss').textContent = `SHADY MISS · ${bad.ref} → ${bad.hyp}`;
  }
  updateStingBoards();
});

const handleStingSignal = safe(ev => {
  const gauge = (state.sting.mode === 'stealth') ? gaugeStealth : gaugeNaive;
  gauge.set(ev.auc);
  if (ev.features && ev.features.length) {
    const list = $('#featList');
    if (!list.childElementCount) {
      for (const f of ev.features) {
        const d = el('div', 'feat',
          `<div class="f-lab"><span>${esc(f.name)}</span><span class="num"></span></div>
           <div class="f-track"><div class="f-fill"></div></div>`);
        d.dataset.name = f.name;
        list.appendChild(d);
      }
    }
    for (const f of ev.features) {
      const d = [...list.children].find(c => c.dataset.name === f.name);
      if (d) {
        d.querySelector('.f-fill').style.width = clamp(f.weight / 0.35, 0, 1) * 100 + '%';
        d.querySelector('.f-lab .num').textContent = f.weight.toFixed(2);
      }
    }
  }
});

const handleCusum = safe(ev => {
  state.sting.threshold = ev.threshold || state.sting.threshold;
  state.sting.cusum.push(ev.point);
  cusum.render();
});

const handleAlarm = safe(ev => {
  state.sting.alarmed = true;
  $('#panelStealth').classList.add('alarmed');
  const ss = $('#statusStealth');
  ss.textContent = 'ALARM'; ss.classList.remove('on');
  ss.style.color = 'var(--crit)'; ss.style.borderColor = 'rgba(208,59,59,.6)';
  $('#bannerDetail').textContent = ev.detail || '';
  const evd = ev.evidence || {};
  // the server names this `quality_gap`; the contract in SPEC.md says
  // `quality_gap_wer` — accept either, this chip is the receipt.
  const gap = evd.quality_gap != null ? evd.quality_gap : evd.quality_gap_wer;
  $('#bannerChips').innerHTML = [
    (evd.auc_naive != null && evd.auc_stealth != null)
      ? `AUC ${evd.auc_naive.toFixed(2)} naive → ${evd.auc_stealth.toFixed(2)} stealth` : null,
    gap != null ? `ENTITY ERROR +${(gap * 100).toFixed(0)} pts UNDER STEALTH` : null,
    evd.cusum_alarm_at != null ? `CUSUM ALARM @ PROBE ${evd.cusum_alarm_at}` : null,
    'VERDICT: DISQUALIFIED',
  ].filter(Boolean).map(t => `<span class="f-chip">${esc(t)}</span>`).join('');
  $('#banner').classList.remove('hidden');
  updateStingBoards();
});

function enterAct3() {
  if (state.act3) return;
  state.act3 = true;
  document.body.classList.add('act3');
  $('#sting').classList.remove('hidden');
  $('#sting').setAttribute('aria-hidden', 'false');
}

/* ═══════════════ EVENT DISPATCH ═══════════════ */
const handlers = {
  phase: safe(ev => {
    state.phase = ev.phase;
    setPhaseHeader(ev.title || ev.phase, ev.subtitle || '');
    setActNav(ev.phase);
    if (ev.phase === 'race') logLine(`<b>race armed</b> — ${esc(ev.subtitle || '')}`);
    if (ev.phase === 'sting') enterAct3();
    if (ev.phase !== 'compile') $('#caret').classList.add('hidden');
  }),

  thought: safe(ev => {
    $('#thoughtCard').classList.remove('hidden');
    $('#thoughtStream').classList.remove('hidden');
    tw.push(ev.text || '');
  }),

  /* Which stack produced the numbers. A replayed run still says whether the
     vendors it raced were real or simulated — the recording never launders that. */
  mode: safe(ev => {
    state.runMode = ev.mode;
    const c = $('#srcChip');
    const n = (ev.live_vendors || []).length;
    c.textContent = ev.mode === 'live' ? `VENDORS · LIVE ×${n}` : 'VENDORS · SIM';
    c.classList.remove('hidden');
    c.classList.toggle('chip-mode', true);
    c.classList.toggle('live', ev.mode === 'live');
    c.classList.toggle('sim', ev.mode !== 'live');
  }),

  /* The compiled exam itself. The model's streamed reasoning is a bonus — some
     responses carry no summarised thinking at all — but the plan always lands,
     so the compiler card is never an empty box. */
  plan: safe(ev => {
    $('#thoughtCard').classList.remove('hidden');
    const line = $('#planLine');
    line.innerHTML = `<b>${esc(ev.headline || '')}</b>` +
      `<span>decision metric — ${esc(ev.decision_metric || '')}</span>`;
    line.classList.remove('hidden');
    $('#thoughtMeta').textContent =
      ev.source === 'agent' ? 'claude — streamed plan' : 'cached plan (compiler offline)';
    if (!tw.shown && !tw.queue) $('#thoughtStream').classList.add('hidden');
  }),

  stratum: safe(ev => {
    state.strata.set(ev.id, ev);
    const f = ev.features || {};
    const card = el('section', 's-card', `
      <div class="s-top"><h3>${esc(ev.name)}</h3><span class="s-w">${Math.round((ev.weight || 0) * 100)}% · n=${ev.n}</span></div>
      <p>${esc(ev.rationale || '')}</p>
      <div class="chip-row">${featureChips(f, (f.entities || []).map(e => [(e === 'phone' ? 'TEL' : 'RX') + ' ENTITIES', false]))}</div>`);
    $('#strataList').appendChild(card);
    const rail = $('#railL'); rail.scrollTop = rail.scrollHeight;
  }),

  vendor: safe(ev => {
    if (!state.vendors.has(ev.id)) {
      ev.slot = state.vendorOrder.length;
      state.vendors.set(ev.id, ev);
      state.vendorOrder.push(ev.id);
    }
    if (!state.act3) ensureRow(state.vendors.get(ev.id));
  }),

  probe: safe(ev => {
    state.probes.set(ev.id, ev);
    if (ev.stealth && state.act3) handleStingProbe(ev);
    else if (!state.act3) showProbe(ev);
  }),

  result: safe(ev => {
    if (!state.results.has(ev.probe_id)) state.results.set(ev.probe_id, new Map());
    state.results.get(ev.probe_id).set(ev.vendor_id, ev);
    // Which act we are in decides where a result goes — NOT whether the probe
    // carries a `stealth` block, because every probe does (Act 2's are stealth
    // probes too). Routing on the field sent Act 2's results into Act 3's boards
    // and left the race with no vendor tabs, no hypothesis diff and no WER.
    if (state.act3) { handleStingResult(ev); return; }
    handleResultAgg(ev);
    onProbeResult(ev);
  }),

  standing: handleStanding,

  eliminate: safe(ev => {
    const v = state.vendors.get(ev.vendor_id); if (!v) return;
    if (state.act3) return;                       // the sting's races have their own panels
    v.eliminated = ev;
    state.elimOrder.push(ev.vendor_id);
    const row = board.rows.get(ev.vendor_id);
    if (row && !row.name.querySelector('.r-tag')) {
      row.name.appendChild(el('span', 'r-tag out', `OUT · LCB &gt; LEADER UCB · @${esc(ev.at_probe || '')}`));
    }
    logLine(`<b>${esc(v.name)}</b> eliminated — ${esc(ev.reason || 'LCB above leader UCB')} at ${esc(ev.at_probe || '')} · spend concentrates on leaders`);
    layoutBoard();
  }),

  cost: handleCost,
  verdict: handleVerdict,
  sting_mode: handleStingMode,
  sting_signal: handleStingSignal,
  naive_result: handleNaiveResult,
  cusum: handleCusum,
  alarm: handleAlarm,

  stop: safe(ev => {
    logLine(`<b>race stopped</b> — ${esc(ev.reason || '')} at ${esc(ev.at_probe || '')}`);
  }),
};

function dispatch(ev) {
  if (!ev || typeof ev !== 'object' || typeof ev.type !== 'string') return;
  if (typeof ev.t === 'number') clock.sync(ev.t);
  const h = handlers[ev.type];
  if (h) { try { h(ev); } catch (e) { console.warn('[vet] handler', ev.type, e); } }
}

/* ═══════════════ TRANSPORT ═══════════════ */
/* The recorded run. `demo.jsonl` is the canonical one — a real run, captured by
   record_demo.py, time-compressed, with its audio inlined under clips/. It is
   loaded over fetch and played off the animation frame: no websocket, no server,
   nothing that can be down when the room is watching. */
const replay = {
  events: [], i: 0, playing: false, ended: false, vt: 0, anchor: 0, loaded: null,
  sources() {
    const rid = QS.get('replay');
    if (rid && !truthy(rid)) return [`runs/${encodeURIComponent(rid)}.jsonl`,   // SPEC: ?replay=<run_id>
                                     'demo.jsonl', 'fixture.jsonl'];
    return ['demo.jsonl', 'fixture.jsonl'];
  },
  async load() {
    if (this.loaded) return this.loaded;
    const sources = this.sources();
    this.loaded = (async () => {
      for (const src of sources) {
        try {
          const r = await fetch(src);
          if (!r.ok) continue;
          const text = await r.text();
          this.events = text.split('\n').filter(Boolean)
            .map(l => { try { return JSON.parse(l); } catch (e) { return null; } }).filter(Boolean);
          this.events.sort((a, b) => (a.t - b.t) || (a.seq - b.seq));
          if (this.events.length) { this.src = src; return; }
        } catch (e) { /* try next source */ }
      }
      console.warn('[vet] no replay source loaded');
      this.events = this.events || [];
    })();
    return this.loaded;
  },
  async start() {
    await this.load();
    this.playing = true; this.ended = false; this.vt = 0; this.anchor = performance.now();
    clock.running = true;
  },
  frame() {
    if (!this.playing) return;
    const now = performance.now();
    this.vt += (now - this.anchor) / 1000 * speed;
    this.anchor = now;
    while (this.i < this.events.length && this.events[this.i].t <= this.vt) dispatch(this.events[this.i++]);
    if (this.i >= this.events.length && this.events.length) {
      this.playing = false; this.ended = true; clock.freeze();
      nodes.modeChip.classList.add('done');
      nodes.modeChip.textContent = 'REPLAY · END';
      setDemoBtn('↻ REPLAY AGAIN', 'R');
    }
  },
  pause() {
    if (!this.playing) return;
    this.playing = false; clock.freeze();
  },
  resume() {
    if (this.ended || this.playing || !this.events.length) return;
    this.playing = true; this.anchor = performance.now();
    clock.anchor = performance.now(); clock.running = true;
  },
  seek(t) {
    state.seeking = true;
    try { while (this.i < this.events.length && this.events[this.i].t <= t) dispatch(this.events[this.i++]); }
    finally { state.seeking = false; }
    this.vt = Math.max(this.vt, t); this.anchor = performance.now();
    clock.base = Math.max(clock.base, t); clock.anchor = performance.now();
    tw.frame(0.01);
    for (const tk of tickers) tk.set(tk.t, true);
    gaugeNaive.v = gaugeNaive.t == null ? gaugeNaive.v : gaugeNaive.t;
    gaugeStealth.v = gaugeStealth.t == null ? gaugeStealth.v : gaugeStealth.t;
    waveMain.flush(true); waveNaive.flush(true); waveStealth.flush(true);
  },
  phaseTime(name) {
    const evn = this.events.find(e => e.type === 'phase' && e.phase === name);
    return evn ? evn.t + 0.01 : null;
  },
  seekToPhase(name) {
    const t = this.phaseTime(name);
    if (t != null) this.seek(t);
  },
};

let ws = null, wsBackoff = 800, wsQueue = [];
function connectWS() {
  try { ws = new WebSocket(WS_URL); } catch (e) { scheduleReconnect(); return; }
  ws.onopen = safe(() => {
    nodes.reconChip.classList.add('hidden');
    wsBackoff = 800;
    while (wsQueue.length) ws.send(wsQueue.shift());
  });
  ws.onmessage = safe(m => {
    let ev; try { ev = JSON.parse(m.data); } catch (e) { return; }
    if (!clock.running && state.started) clock.running = true;
    dispatch(ev);
  });
  ws.onclose = safe(() => { if (!replayMode) { nodes.reconChip.classList.remove('hidden'); scheduleReconnect(); } });
  ws.onerror = safe(() => { try { ws.close(); } catch (e) {} });
}
function scheduleReconnect() {
  if (replayMode) return;
  setTimeout(safe(() => { if (!replayMode) connectWS(); }), wsBackoff);
  wsBackoff = Math.min(5000, wsBackoff * 1.6);
}
function dropWS() {                    // a recording must not race a live socket
  nodes.reconChip.classList.add('hidden');
  wsQueue.length = 0;
  if (ws) { try { ws.onclose = null; ws.onmessage = null; ws.close(); } catch (e) {} ws = null; }
}
function sendCmd(obj) {
  const s = JSON.stringify(obj);
  if (ws && ws.readyState === 1) { try { ws.send(s); } catch (e) { wsQueue.push(s); } }
  else wsQueue.push(s);
}

/* ═══════════════ CONTROLS ═══════════════ */
function lockNeed() {
  const need = $('#needText').value.trim();
  const q = $('#needQuote');
  q.innerHTML = '“' + esc(need).replace(/drug names and callback numbers/g, '<b>drug names and callback numbers</b>') + '”';
  q.classList.remove('hidden');
  $('#needText').classList.add('hidden');
  $('#startRow').classList.add('hidden');
  $('#startNote').classList.add('hidden');
  $('#needMeta').textContent = 'locked for this run';
  $('#btnAct3').disabled = false;
  $('#thoughtCard').classList.remove('hidden');
  return need;
}

function setModeChip() {
  const c = nodes.modeChip;
  c.classList.remove('done');
  if (replayMode) {
    c.textContent = speed === 1 ? 'REPLAY' : `REPLAY ×${speed}`;
    c.classList.remove('live', 'sim');
  } else {
    c.textContent = state.modeSel.toUpperCase();
    c.classList.toggle('live', state.modeSel === 'live');
    c.classList.toggle('sim', state.modeSel === 'sim');
  }
}
function setDemoBtn(label, key) {
  $('#btnDemoLabel').innerHTML = label;
  $('#btnDemoKey').textContent = key;
}

/* the live path: real LLM, real synthesis, real vendor calls */
const startRun = safe(async () => {
  if (state.started) return;
  state.started = true;
  const need = lockNeed();
  if (replayMode) {
    await replay.start();
    if (JUMP > 0) { replay.seek(JUMP); if (PAUSE) replay.pause(); }
  } else {
    if (!ws) connectWS();
    clock.running = true;
    sendCmd({ cmd: 'start', need, mode: state.modeSel });
  }
});

/* the stage path: the recorded run, instantly, off the local file */
const startDemo = safe(async () => {
  if (state.demoStarted) return;        // two taps on the button must not double-play
  state.demoStarted = true;
  replayMode = true;
  dropWS();
  clock.scale = speed;
  setModeChip();
  $('#btnMode').classList.add('hidden');
  if (!state.started) { state.started = true; lockNeed(); }
  setDemoBtn('❚❚&nbsp;PAUSE', 'SPACE');
  $('#btnDemo').classList.add('playing');
  await replay.start();
  if (JUMP > 0) { replay.seek(JUMP); if (PAUSE) pauseDemo(); }
});

function pauseDemo() {
  replay.pause();
  setDemoBtn('▶&nbsp;RESUME', 'SPACE');
  $('#btnDemo').classList.remove('playing');
  nodes.modeChip.textContent = 'REPLAY · PAUSED';
}
function resumeDemo() {
  replay.resume();
  setDemoBtn('❚❚&nbsp;PAUSE', 'SPACE');
  $('#btnDemo').classList.add('playing');
  setModeChip();
}

/* SPACE / ENTER — start it, then pause and resume it. `explicit` is a click on
   the button: only that may abandon a live run that is already in flight. */
const toggleDemo = safe(explicit => {
  if (replay.ended) { location.href = location.pathname + '?demo=1'; return; }
  if (!replayMode && state.started) { if (explicit) location.href = location.pathname + '?demo=1'; return; }
  if (!state.started) { startDemo(); return; }
  replay.playing ? pauseDemo() : resumeDemo();
});

const setSpeed = safe(v => {
  speed = v;
  if (replayMode) clock.scale = v;
  $$('#speedSeg .seg-btn').forEach(b => b.classList.toggle('on', parseFloat(b.dataset.speed) === v));
  if (replayMode && !replay.ended) setModeChip();
  if (replayMode && !replay.playing && state.started && !replay.ended) {
    nodes.modeChip.textContent = 'REPLAY · PAUSED';
  }
});

const ACT_PHASE = { 1: 'compile', 2: 'race', 3: 'sting' };
const jumpToAct = safe(async n => {
  const phase = ACT_PHASE[n];
  if (!phase) return;
  $$('#jumpSeg .seg-btn').forEach(b => b.classList.toggle('on', +b.dataset.act === n));
  if (!replayMode) {                       // live: the only jump that exists is the Act 3 cue
    if (n === 3 && state.started) sendCmd({ cmd: 'act3' });
    return;
  }
  if (!state.started) { await startDemo(); }
  await replay.load();
  const t = replay.phaseTime(phase);
  if (t == null) return;
  if (t >= replay.vt) { replay.seek(t); return; }
  // Rewinding means replaying from a clean slate — reload straight into it.
  location.href = `${location.pathname}?demo=1&jump=${t.toFixed(2)}&speed=${speed}`;
});

/* ERR is the decision; WER / latency / spend / P(best) are the diligence behind
   it. Default to the decision, one key away from the diligence. */
const setDetails = safe(on => {
  state.details = !!on;
  $('#boardCard').classList.toggle('lean', !on);
  $('#btnDetails').classList.toggle('on', !!on);
  $('#btnDetails').textContent = on ? 'DETAILS ON' : 'DETAILS';
});

$('#btnDetails').addEventListener('click', () => setDetails(!state.details));
$('#btnDemo').addEventListener('click', () => toggleDemo(true));
$('#btnStart').addEventListener('click', startRun);
$('#btnMode').addEventListener('click', safe(() => {
  if (replayMode) return;
  state.modeSel = state.modeSel === 'live' ? 'sim' : 'live';
  $('#btnModeVal').textContent = state.modeSel.toUpperCase();
  setModeChip();
}));
$('#btnAct3').addEventListener('click', safe(() => {
  if (replayMode) replay.seekToPhase('sting');
  else sendCmd({ cmd: 'act3' });
}));
$('#btnReset').addEventListener('click', safe(() => {
  if (!replayMode) sendCmd({ cmd: 'reset' });
  setTimeout(() => location.reload(), 60);
}));
$$('#speedSeg .seg-btn').forEach(b =>
  b.addEventListener('click', () => setSpeed(parseFloat(b.dataset.speed) || 1)));
$$('#jumpSeg .seg-btn').forEach(b =>
  b.addEventListener('click', () => jumpToAct(+b.dataset.act)));

/* keyboard — everything the presenter needs without finding a button */
const SPEEDS = [1, 2, 4];
window.addEventListener('keydown', safe(e => {
  const tag = (e.target && e.target.tagName) || '';
  if (tag === 'TEXTAREA' || tag === 'INPUT' || e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key;
  if (k === ' ' || k === 'Spacebar' || k === 'Enter') { e.preventDefault(); toggleDemo(false); }
  else if (k === '1' || k === '2' || k === '3') { e.preventDefault(); jumpToAct(+k); }
  else if (k === ']' || k === '+' || k === '=') { setSpeed(SPEEDS[Math.min(SPEEDS.length - 1, SPEEDS.indexOf(speed) + 1)] || 4); }
  else if (k === '[' || k === '-') { setSpeed(SPEEDS[Math.max(0, SPEEDS.indexOf(speed) - 1)] || 1); }
  else if (k === 'd' || k === 'D') { setDetails(!state.details); }
  else if (k === 'r' || k === 'R') { location.href = location.pathname + '?demo=1'; }
  else if (k === 'l' || k === 'L') { location.href = location.pathname; }
}));

/* ═══════════════ MASTER LOOP ═══════════════ */
let _lastT = performance.now();
function loop(now) {
  const dt = Math.min(0.1, (now - _lastT) / 1000); _lastT = now;
  try {
    if (replayMode) replay.frame();
    elapsedTick.set(clock.now(), true);
    for (const t of tickers) t.frame(dt);
    tw.frame(dt);
    waveMain.frame(dt); waveNaive.frame(dt); waveStealth.frame(dt);
    gaugeNaive.frame(dt); gaugeStealth.frame(dt);
  } catch (e) { console.warn('[vet] loop', e); }
  requestAnimationFrame(loop);
}
requestAnimationFrame(loop);
window.addEventListener('error', e => console.warn('[vet] window', e.message));
window.addEventListener('unhandledrejection', e => { console.warn('[vet] promise', e.reason); e.preventDefault(); });

/* ═══════════════ BOOT ═══════════════ */
(function boot() {
  setDetails(truthy(QS.get('details')));
  setSpeed(SPEEDS.includes(SPEED) ? SPEED : speed);
  if (replayMode) {
    setModeChip();
    $('#btnMode').classList.add('hidden');
    replay.load();
  } else {
    nodes.modeChip.textContent = 'LIVE';
    nodes.modeChip.classList.add('live');
    connectWS();
    replay.load();          // warm the recording so ▶ PLAY DEMO is instant
  }
  if (DEMO) startDemo();
  else if (AUTO) startRun();
})();
