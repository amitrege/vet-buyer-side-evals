"""Vet server: drives the three acts and streams them to the UI.

Every event is also appended to runs/<id>.jsonl, so any run can be replayed at
any speed. That is the stage-safe path — live mode hits real APIs, replay mode
cannot fail in front of an audience.
"""
from __future__ import annotations

import asyncio
import concurrent.futures as cf
import json
import os
import time
from dataclasses import asdict

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from . import sting as st
from . import vapi as vapi_mod
from .compiler import CHANNELS, FALLBACK, compile_exam
from .probes import AUDIO_DIR, CHANNELS as CHANNEL_SPECS, synth_probe
from .race import Race, Vendor
from .score import diff_tokens, score_cell
from .vendors import FLEET, PROFILES, SHADY, simulate

HERE = os.path.dirname(__file__)
UI = os.path.join(HERE, "ui")
RUNS = os.path.join(HERE, "runs")
os.makedirs(RUNS, exist_ok=True)

DEFAULT_NEED = (
    "After-hours calls for a chain of urgent-care clinics in Houston. Callers switch "
    "between Spanish and English mid-sentence, they're on cell phones in noisy rooms, "
    "and they read out prescriptions and callback numbers. I need the transcription "
    "layer that gets drug names and callback numbers right — I dial those numbers back."
)

POOL = cf.ThreadPoolExecutor(8)

# Which decision-relevant fields each template puts on the line.
ENTITY_KINDS = {"callback": ["phone"], "correction": ["phone"],
                "prescription": ["drug", "phone"], "pharmacy": ["drug", "phone"],
                "symptom": ["drug"]}


class Emitter:
    """Sends to the browser and writes the replay log."""

    def __init__(self, ws: WebSocket, run_id: str):
        self.ws = ws
        self.seq = 0
        self.t0 = time.time()
        self.path = os.path.join(RUNS, f"{run_id}.jsonl")
        self.fh = open(self.path, "w")

    async def emit(self, type: str, **kw):
        self.seq += 1
        ev = {"seq": self.seq, "t": round(time.time() - self.t0, 3), "type": type, **kw}
        self.fh.write(json.dumps(ev) + "\n")
        self.fh.flush()
        try:
            await self.ws.send_text(json.dumps(ev))
        except Exception:
            pass
        return ev

    def close(self):
        try:
            self.fh.close()
        except Exception:
            pass


async def to_thread(fn, *a, **kw):
    return await asyncio.get_running_loop().run_in_executor(POOL, lambda: fn(*a, **kw))


# ------------------------------------------------------------------ act one

async def act_compile(em: Emitter, need: str):
    await em.emit("phase", phase="compile", title="Compiling your exam",
                  subtitle="An agent decides what would actually change this decision")

    q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_thought(text: str):
        loop.call_soon_threadsafe(q.put_nowait, text)

    task = asyncio.create_task(to_thread(compile_exam, need, on_thought))
    while not task.done() or not q.empty():
        try:
            chunk = await asyncio.wait_for(q.get(), timeout=0.15)
            await em.emit("thought", text=chunk)
        except asyncio.TimeoutError:
            pass
    plan = await task

    await em.emit("plan", headline=plan.headline, decision_metric=plan.decision_metric,
                  source=plan.source)
    for s in plan.strata:
        ch = CHANNEL_SPECS.get(s.channel)
        await em.emit("stratum", id=s.id, name=s.name, rationale=s.rationale,
                      n=s.n, weight=s.weight, template=s.template, channel=s.channel,
                      channel_desc=CHANNELS.get(s.channel, ""),
                      features={"snr_db": round(ch.snr_db) if ch else None,
                                "codeswitch": "high",
                                "codec": "g711" if (ch and ch.codec) else None,
                                "entities": ENTITY_KINDS.get(s.template, [])})
        await asyncio.sleep(0.35)
    return plan


async def synthesize(em: Emitter, plan, stealth_mode="stealth", tag="", sampler=None):
    """Naive and stealth exams ask the *same questions* — only the channel differs.

    That is the controlled comparison Act 3 needs (any quality gap is attributable
    to the channel, not to a different exam), and it means the TTS cache hits.

    `sampler` is the stealth mechanism: instead of a fixed channel preset — which
    is a fingerprint however noisy it sounds — each probe draws its channel from
    the buyer's own traffic distribution. Matching the distribution, not just the
    loudness, is what takes the discriminator to chance.
    """
    jobs, out = [], []
    for s in plan.strata:
        for i in range(s.n):
            channel = sampler() if sampler else s.channel
            jobs.append((s.id, s.template, channel,
                         abs(hash((s.id, i))) % 10 ** 6, stealth_mode))
    futures = [asyncio.get_running_loop().run_in_executor(
        POOL, lambda a=a: synth_probe(*a)) for a in jobs]
    for fut in asyncio.as_completed(futures):
        p = await fut
        out.append(p)
        # Progress only — the probe itself is announced when it goes on the wire,
        # so Act 2 shows one probe at a time instead of flickering through all of them.
        await em.emit("cost", total_usd=0.0, probes_run=len(out), budget_usd=2.0,
                      synthesized=len(out), of=len(jobs),
                      elapsed_s=round(time.time() - em.t0, 1))
    out.sort(key=lambda p: (p.stratum, p.id))
    return out


# ------------------------------------------------------------------ act two

def memo(plan, race: Race, probes_run: int, saved: int, mode: str) -> dict:
    table = {**race.final, **race.stats}          # survivors override
    order = sorted(table.items(), key=lambda kv: kv[1]["mean"])
    live = sorted(race.stats.items(), key=lambda kv: kv[1]["mean"])
    win_id, win = live[0]
    winner = race.vendors[win_id]
    pub = min((v for v in race.vendors.values() if v.public_rank), key=lambda v: v.public_rank)
    pub_place = next((i + 1 for i, (vid, _) in enumerate(order) if vid == pub.id), None)
    conf = race.confidence()

    # A tie is a finding, not a failure. Reporting a 59%-confident winner as
    # though it were decided is exactly the overclaiming this whole system exists
    # to stop — so when the leaders overlap, say so and hand the buyer the
    # tiebreaker they can actually act on.
    tied = [race.vendors[v].name for v, s in live[1:]
            if s["lo"] <= win["hi"]] if len(live) > 1 else []
    if conf < 0.80 and tied:
        cheaper = min([winner] + [v for v in race.vendors.values() if v.name in tied],
                      key=lambda v: v.price_per_hr)
        lines = [
            f"# Too close to call: {winner.name} vs {', '.join(tied[:2])}",
            "",
            f"After {probes_run} probes and **${race.spend:.2f}**, the leaders' 95% intervals "
            f"still overlap — on your traffic these are **statistically tied** "
            f"(P(best) {conf:.0%}). More probes would cost more than the difference is worth.",
            "",
            f"**Recommendation:** take **{cheaper.name}** at ${cheaper.price_per_hr:.2f}/hr — "
            f"when quality is indistinguishable, price and latency are the decision.",
            "",
        ]
    else:
        lines = [
            f"# Verdict: {winner.name}",
            "",
            f"**{conf:.0%} confidence** it is the best of {len(race.vendors)} candidates "
            f"for *your* traffic. The exam cost **${race.spend:.2f}** and "
            f"{probes_run} probes ({saved} probes saved by stopping early).",
            "",
        ]
    lines += [
        f"- Decision metric: {plan.decision_metric}",
        f"- {winner.name}: **{win['mean']:.1%}** entity error "
        f"(95% CI {win['lo']:.1%}–{win['hi']:.1%}), {win['wer']:.1%} WER, "
        f"{win['p50_latency']} ms median, ${winner.price_per_hr:.2f}/hr",
    ]
    if pub_place and pub.id != win_id:
        lines += ["", f"**The public leaderboard would have picked wrong.** {pub.name} is "
                      f"ranked #{pub.public_rank} publicly and finished **#{pub_place} here** — "
                      f"it is good at clean read speech and bad at your waiting rooms."]
    lines += ["", "### Why it wins on your mix"]
    for s in plan.strata:
        per = [c for c in race.cells[win_id] if c.stratum == s.id]
        if per:
            e = sum(c.entity_error for c in per) / len(per)
            lines.append(f"- **{s.name}** (weight {s.weight:.0%}): {e:.0%} error — {s.rationale}")
    if mode == "sim":
        lines += ["", "_Vendor responses simulated locally (SIM mode). Probe synthesis, "
                      "scoring, and the decision policy are the real implementations._"]
    headline = (f"{winner.name} — {win['mean']:.1%} error on your mix, "
                f"decided for ${race.spend:.2f}")
    if pub_place and pub.id != win_id and pub_place >= 3:
        headline = (f"{winner.name} wins; {pub.name} (public #{pub.public_rank}) "
                    f"finished #{pub_place} on your traffic")
    return {"winner": win_id, "winner_name": winner.name, "p_best": conf,
            "headline": headline,
            "cost_usd": race.spend, "probes_run": probes_run, "probes_saved": saved,
            "public_leader": pub.name, "public_leader_place": pub_place,
            "memo_md": "\n".join(lines)}


LIVE: dict = {"client": None, "sem": None, "ids": set()}


async def preflight(em: Emitter, plan) -> tuple[bool, str]:
    """Check our own instrument before trusting anything it says.

    Live transcription over a real voice platform silently returns *nothing* for
    some clips — the call connects, bills, and yields an empty transcript. Scored
    naively that reads as "every vendor failed", which collapses all of them onto
    the same number and produces a confident-looking three-way tie made entirely
    of our own broken plumbing. So: fire a couple of canaries first, and if the
    apparatus can't hear its own audio, say so and fall back rather than report a
    verdict built on silence.
    """
    probes = [synth_probe(plan.strata[0].id, plan.strata[0].template,
                          plan.strata[0].channel, seed=990001 + i) for i in range(2)]
    vendors = sorted(LIVE["ids"])[:2]
    got = await asyncio.gather(*[LIVE["client"].transcribe(v, p.audio_path)
                                 for p in probes for v in vendors],
                               return_exceptions=True)
    ok = sum(1 for r in got
             if not isinstance(r, Exception) and (r.get("hyp") or "").strip())
    rate = ok / max(1, len(got))
    await em.emit("preflight", ok=ok, total=len(got), pass_rate=round(rate, 2),
                  healthy=rate >= 0.75)
    if rate >= 0.75:
        return True, ""
    return False, (f"live transcription returned usable audio for only {ok}/{len(got)} "
                   f"canary probes — measuring vendors through a broken instrument "
                   f"would score our own failures as theirs")


async def run_vendor(mode: str, vendor_id: str, probe):
    """One probe against one vendor. `live` really dials it; `sim` models it."""
    if mode == "live" and LIVE["client"] and vendor_id in LIVE["ids"]:
        async with LIVE["sem"]:
            try:
                r = await LIVE["client"].transcribe(vendor_id, probe.audio_path)
                return r["hyp"], r["latency_ms"], r.get("cost_usd", 0.0), "live"
            except Exception:
                pass                      # fall through rather than lose the vendor
    hyp, lat, cost = await to_thread(simulate, _key(vendor_id, probe), probe)
    return hyp, lat, cost, "sim"


async def act_race(em: Emitter, plan, probes, fleet, mode: str, budget=2.0, label="race"):
    weights = {s.id: s.weight for s in plan.strata}
    race = Race(list(fleet), weights, budget_usd=budget, target_conf=0.95, min_probes=16)
    for v in fleet:
        await em.emit("vendor", id=v.id, name=v.name, stack=v.stack,
                      price_per_hr=v.price_per_hr, public_rank=v.public_rank, note=v.note)

    planned = len(probes) * len(fleet)
    done = 0
    for i, p in enumerate(probes):
        focus = set(race.next_focus()) if race.stats else {v.id for v in race.active_vendors}
        await em.emit("probe", index=i + 1, total=len(probes), **p.public())
        targets = [v for v in race.active_vendors
                   if v.id in focus or len(race.probes_run) < race.min_probes]
        results = await asyncio.gather(*[run_vendor(mode, v.id, p) for v in targets],
                                       return_exceptions=True)
        for v, res in zip(targets, results):
            if isinstance(res, Exception):
                continue
            hyp, lat, cost, src = res
            cell, detail = score_cell(p, v.id, hyp, lat, cost)
            race.add(cell)
            done += 1
            await em.emit("result", probe_id=p.id, vendor_id=v.id, hyp=hyp,
                          wer=round(cell.wer, 4), entity_ok=cell.ok, entity_detail=detail,
                          latency_ms=lat, cost_usd=round(cost, 6), source=src,
                          diff=diff_tokens(p.text, hyp)[:60])
        race.recompute()
        for vid, s in race.stats.items():
            await em.emit("standing", vendor_id=vid, metric="entity_error",
                          mean=round(s["mean"], 4), lo=round(s["lo"], 4), hi=round(s["hi"], 4),
                          n=s["n"], spend_usd=round(s["spend"], 5),
                          p_best=round(s["p_best"], 4), wer=round(s["wer"], 4),
                          p50_latency=s["p50_latency"],
                          eliminated=not race.vendors[vid].active)
        for vid, reason in race.eliminate_check():
            await em.emit("eliminate", vendor_id=vid, reason=reason, at_probe=p.id)
        await em.emit("cost", total_usd=round(race.spend, 5), probes_run=len(race.probes_run),
                      budget_usd=budget, calls=done, calls_saved=planned - done,
                      elapsed_s=round(time.time() - em.t0, 1))
        stop, why = race.should_stop()
        if stop:
            await em.emit("stop", reason=why, at_probe=p.id)
            break
        await asyncio.sleep(0.05)
    return race, done, planned - done


def _key(vendor_id: str, probe) -> str:
    """Shady routes itself; everyone else is themselves."""
    if vendor_id != "shady":
        return vendor_id
    stack, _ = DEVICE.route(probe.features, True)
    return f"shady_{stack}"


DEVICE: st.DefeatDevice | None = None


# ---------------------------------------------------------------- act three

async def act_sting(em: Emitter, plan, mode: str):
    global DEVICE
    await em.emit("phase", phase="sting", title="The sting",
                  subtitle="A vendor that knows when it is being watched")

    # The buyer's real traffic, and the naive exam a normal benchmark would run.
    rng = np.random.default_rng(4)
    prod = await to_thread(_production_sample, rng, 40)
    naive = await synthesize(em, plan, stealth_mode="naive", tag="naive")

    X = np.array([st.vec(f) for f in prod] + [st.vec(p.features) for p in naive])
    y = np.array([0.0] * len(prod) + [1.0] * len(naive))
    disc = await to_thread(lambda: st.Discriminator().fit(X, y))
    auc_naive = st.auc(np.array([disc.score(f) for f in prod] +
                                [disc.score(p.features) for p in naive]), y)
    DEVICE = st.DefeatDevice(disc, threshold=0.5)
    for f in prod:                       # what the device does to genuine callers
        DEVICE.route(f, False)

    await em.emit("sting_signal", vendor_id="shady", mode="naive", auc=round(float(auc_naive), 3),
                  n=len(naive), features=disc.top_features(),
                  explain="ShadyTranscribe trained a classifier on its own traffic. "
                          "A studio-clean benchmark probe is trivially separable from a real call.")

    fleet = list(FLEET) + [SHADY]
    race_n, _, _ = await act_race(em, plan, naive, fleet, mode, budget=2.0, label="naive")
    order = sorted(race_n.stats.items(), key=lambda kv: kv[1]["mean"])
    naive_receipts = DEVICE.receipts()
    r = naive_receipts
    await em.emit("naive_result",
                  standings=[{"vendor_id": v, "mean": round(s["mean"], 4)} for v, s in order],
                  shady_place=next((i + 1 for i, (v, _) in enumerate(order) if v == "shady"), None),
                  premium_served=f"{r['probe_premium']}/{r['probe_total']}")

    # Now the same exam, camouflaged to the buyer's own traffic distribution.
    await em.emit("sting_mode", mode="stealth",
                  explain="Same questions. Same answer key. Probes now drawn from the buyer's "
                          "own channel distribution instead of a studio.")
    DEVICE.log.clear()
    srng = np.random.default_rng(21)
    stealth = await synthesize(em, plan, stealth_mode="stealth", tag="stealth",
                               sampler=lambda: st.sample_production_channel(srng))
    auc_stealth = st.auc(
        np.array([disc.score(f) for f in prod] + [disc.score(p.features) for p in stealth]),
        np.array([0.0] * len(prod) + [1.0] * len(stealth)))
    await em.emit("sting_signal", vendor_id="shady", mode="stealth",
                  auc=round(float(auc_stealth), 3), n=len(stealth),
                  features=disc.top_features(),
                  explain="The seller's own best detector now performs at chance. "
                          "Its probes and its real calls are the same distribution.")

    race_s, _, _ = await act_race(em, plan, stealth, fleet, mode, budget=2.0, label="stealth")

    # Sequential detection on the quality gap that stealth exposed.
    base = float(np.mean([c.entity_error for c in race_n.cells["shady"]]))
    cus = st.Cusum(baseline=base, drift=0.02, threshold=1.5)
    for i, c in enumerate(race_s.cells["shady"]):
        s, alarm = cus.update(c.entity_error)
        await em.emit("cusum", vendor_id="shady", point={"i": i, "s": round(s, 3)},
                      threshold=cus.threshold, alarm=alarm)
        await asyncio.sleep(0.03)

    r = DEVICE.receipts()
    gap = float(np.mean([c.entity_error for c in race_s.cells["shady"]])) - base
    await em.emit("alarm", vendor_id="shady", kind="defeat_device",
                  detail=f"Under the naive exam it served its premium stack to "
                         f"{naive_receipts['probe_premium']}/{naive_receipts['probe_total']} probes "
                         f"while giving it to {naive_receipts['real_premium']}/"
                         f"{max(1, naive_receipts['real_total'])} real callers. Under stealth it "
                         f"could no longer tell them apart: {r['probe_premium']}/{r['probe_total']}.",
                  evidence={"auc_naive": round(float(auc_naive), 3),
                            "auc_stealth": round(float(auc_stealth), 3),
                            "quality_gap": round(gap, 4),
                            "naive_error": round(base, 4),
                            "stealth_error": round(base + gap, 4),
                            "cusum_alarm_at": cus.alarm_at,
                            "naive_premium": f"{naive_receipts['probe_premium']}/{naive_receipts['probe_total']}",
                            "naive_real_premium": f"{naive_receipts['real_premium']}/{max(1, naive_receipts['real_total'])}",
                            "stealth_premium": f"{r['probe_premium']}/{r['probe_total']}"})
    return race_s


def _production_sample(rng, n: int):
    """Feature vectors for a sample of the buyer's real calls."""
    from .probes import TEMPLATES, synth_probe as sp
    out = []
    keys = sorted(TEMPLATES)
    for i in range(n):
        ch = st.sample_production_channel(rng)
        p = sp("prod", keys[i % len(keys)], ch, seed=90000 + i, stealth_mode="stealth")
        out.append(p.features)
    return out


# --------------------------------------------------------------------- app

app = FastAPI()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    em = Emitter(ws, f"run_{int(time.time())}")
    state: dict = {}
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            cmd = msg.get("cmd")
            if cmd == "start":
                need = msg.get("need") or DEFAULT_NEED
                mode = msg.get("mode", "sim")
                fleet = list(FLEET)
                if mode == "live":
                    key = vapi_mod.load_key()
                    if key:
                        LIVE["client"] = vapi_mod.VapiClient(key)
                        LIVE["ids"] = set(vapi_mod.VENDOR_TRANSCRIBERS)
                        LIVE["sem"] = asyncio.Semaphore(8)
                        try:
                            await LIVE["client"].ensure_assistant()
                            fleet = [v for v in FLEET if v.id in LIVE["ids"]]
                        except Exception:
                            mode = "sim"
                    else:
                        mode = "sim"
                state["fleet"] = fleet
                await em.emit("mode", mode=mode,
                              vendors=[v.id for v in fleet],
                              live_vendors=sorted(LIVE["ids"]) if mode == "live" else [])
                plan = await act_compile(em, need)
                state["plan"] = plan
                if mode == "live":
                    healthy, why = await preflight(em, plan)
                    if not healthy:
                        mode = "sim"
                        fleet = list(FLEET)
                        await em.emit("mode", mode="sim", degraded=True, reason=why,
                                      vendors=[v.id for v in fleet], live_vendors=[])
                await em.emit("phase", phase="race", title="The race",
                              subtitle="Spend only where the leaders still overlap")
                probes = await synthesize(em, plan)
                race, done, saved = await act_race(em, plan, probes, fleet, mode)
                await em.emit("phase", phase="verdict", title="Decision", subtitle="")
                await em.emit("verdict", **memo(plan, race, len(race.probes_run), saved, mode),
                              elapsed_s=round(time.time() - em.t0, 1))
            elif cmd == "act3":
                plan = state.get("plan") or FALLBACK
                await act_sting(em, plan, msg.get("mode", "sim"))
            elif cmd == "ping":
                await em.emit("pong")
    except WebSocketDisconnect:
        pass
    finally:
        em.close()


app.mount("/audio", StaticFiles(directory=AUDIO_DIR), name="audio")
app.mount("/runs", StaticFiles(directory=RUNS), name="runs")
app.mount("/", StaticFiles(directory=UI, html=True), name="ui")
