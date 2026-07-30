"""`python -m vet` — run a full buyer-side exam from the terminal.

Compiles a private exam for the buyer's need, synthesizes the probes locally,
races the vendors (SIM by default; LIVE dials real speech stacks through Vapi),
prints the decision memo, then runs the sting and reports whether the planted
defeat device was caught. Every event also lands in vet/runs/<id>.jsonl.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from .exam import DEFAULT_NEED, run_exam

CHECK, CROSS = "✓", "✗"


def printer(ev: dict) -> None:
    t = ev["type"]
    out = sys.stdout
    if t == "mode":
        live = ev.get("live_vendors") or []
        out.write(f"mode: {ev['mode'].upper()}"
                  + (f" (live vendors: {', '.join(live)})" if live else "")
                  + f" — vendors: {', '.join(ev['vendors'])}\n")
        if ev.get("degraded"):
            out.write(f"  ! degraded to SIM: {ev.get('reason', '')}\n")
    elif t == "phase":
        out.write(f"\n═══ {ev.get('title', ev.get('phase', ''))}"
                  + (f" — {ev['subtitle']}" if ev.get("subtitle") else "") + "\n")
    elif t == "thought":
        out.write(ev.get("text", ""))
    elif t == "plan":
        out.write(f"\nplan ({ev.get('source')}): {ev.get('headline', '')}\n"
                  f"  metric: {ev.get('decision_metric', '')}\n")
    elif t == "stratum":
        out.write(f"  {ev['id']} · {ev['name']} — n={ev['n']}, weight {ev['weight']:.0%} "
                  f"[{ev.get('template')} @ {ev.get('channel')}]\n")
    elif t == "preflight":
        out.write(f"  preflight: {ev['ok']}/{ev['total']} canaries usable "
                  f"({'healthy' if ev.get('healthy') else 'unhealthy'})\n")
    elif t == "synth":
        out.write(f"\r  synthesizing probes… {ev['done']}/{ev['of']}")
        if ev["done"] == ev["of"]:
            out.write("\n")
    elif t == "probe":
        out.write(f"probe {ev['index']}/{ev['total']} {ev['id']} "
                  f"[{ev.get('stratum')}] {ev.get('duration_s', 0):.1f}s\n")
    elif t == "result":
        bad = [d["kind"] for d in ev.get("entity_detail", []) if not d["ok"]]
        mark = CHECK if ev.get("entity_ok") else f"{CROSS} ({', '.join(bad)})"
        out.write(f"    {ev['vendor_id']:<9} {mark:<14} wer {ev['wer']:>6.1%}  "
                  f"{ev['latency_ms']} ms  [{ev.get('source')}]\n")
    elif t == "eliminate":
        out.write(f"  ✕ {ev['vendor_id']} eliminated — {ev.get('reason', '')}\n")
    elif t == "stop":
        out.write(f"  race stopped — {ev.get('reason', '')}\n")
    elif t == "verdict":
        out.write("\n" + ev.get("memo_md", "") + "\n")
    elif t == "sting_signal":
        feats = ", ".join(f"{f['name']} ({f['direction']})" for f in ev.get("features", [])[:3])
        out.write(f"\n  discriminator AUC [{ev.get('mode')}] = {ev.get('auc'):.3f} "
                  f"over {ev.get('n')} probes\n    top tells: {feats}\n"
                  f"    {ev.get('explain', '')}\n")
    elif t == "naive_result":
        out.write(f"  naive exam: Shady finished #{ev.get('shady_place')}, "
                  f"premium stack served to {ev.get('premium_served')} probes\n")
    elif t == "sting_mode":
        out.write(f"\n  → {ev.get('mode', '').upper()}: {ev.get('explain', '')}\n")
    elif t == "alarm":
        e = ev.get("evidence", {})
        out.write("\n╔═ DEFEAT DEVICE DETECTED ═══════════════════════════════\n")
        out.write(f"║ {ev.get('detail', '')}\n")
        out.write(f"║ AUC {e.get('auc_naive')} naive → {e.get('auc_stealth')} stealth · "
                  f"entity error {e.get('naive_error', 0):.1%} → {e.get('stealth_error', 0):.1%} · "
                  f"CUSUM alarm at probe {e.get('cusum_alarm_at')}\n")
        out.write("╚════════════════════════════════════════════════════════\n")
    out.flush()


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="python -m vet", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["sim", "live"], default="sim",
                    help="sim = calibrated local vendor models; live = real vendors via Vapi")
    ap.add_argument("--need", default=DEFAULT_NEED,
                    help="the buyer's need, in their own words")
    ap.add_argument("--no-sting", action="store_true",
                    help="stop after the verdict; skip Act 3")
    ap.add_argument("--budget", type=float, default=2.0,
                    help="evaluation budget in dollars (default 2.00)")
    args = ap.parse_args()

    path = asyncio.run(run_exam(need=args.need, mode=args.mode,
                                sting=not args.no_sting, budget=args.budget,
                                printer=printer))
    print(f"\nrun log: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
