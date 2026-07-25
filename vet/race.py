"""The race: best-arm identification with per-probe costs.

A researcher's benchmark wants coverage and difficulty. A buyer's benchmark has
exactly one job — change the purchase decision correctly, at minimum spend. So
this is not dataset construction, it is a stopping problem: keep buying probes
only while they can still change who wins.

Concretely: successive elimination on a paired stratified bootstrap. A vendor is
dropped the moment its 95% lower bound clears the leader's upper bound, spend is
concentrated on whoever is still tied, and we halt as soon as P(best) crosses the
buyer's confidence bar — or the budget runs out, whichever comes first.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from .score import Cell, stratified_bootstrap


@dataclass
class Vendor:
    id: str
    name: str
    stack: dict
    price_per_hr: float
    public_rank: int | None = None
    note: str = ""
    eliminated_at: str | None = None
    reason: str = ""
    spend: float = 0.0
    latencies: list[int] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.eliminated_at is None


class Race:
    def __init__(self, vendors: list[Vendor], weights: dict[str, float],
                 budget_usd: float = 2.00, target_conf: float = 0.95,
                 min_probes: int = 16, seed: int = 11):
        self.vendors = {v.id: v for v in vendors}
        self.weights = weights
        self.budget = budget_usd
        self.target_conf = target_conf
        self.min_probes = min_probes
        self.cells: dict[str, list[Cell]] = {v.id: [] for v in vendors}
        self.probes_run: list[str] = []
        self.spend = 0.0
        self.t0 = time.time()
        self.rng = np.random.default_rng(seed)
        self.stats: dict[str, dict] = {}
        # Last known estimate for every vendor, including the eliminated —
        # a vendor knocked out early still has a place in the final table.
        self.final: dict[str, dict] = {}

    # ------------------------------------------------------------------ update
    def add(self, cell: Cell) -> None:
        self.cells[cell.vendor_id].append(cell)
        self.spend += cell.cost_usd
        v = self.vendors[cell.vendor_id]
        v.spend += cell.cost_usd
        v.latencies.append(cell.latency_ms)
        if cell.probe_id not in self.probes_run:
            self.probes_run.append(cell.probe_id)

    def recompute(self) -> dict[str, dict]:
        live = {vid: cs for vid, cs in self.cells.items()
                if self.vendors[vid].active and cs}
        if not live:
            return {}
        self.stats, _ = stratified_bootstrap(live, self.weights, "entity_error",
                                             n_boot=1500, rng=self.rng)
        for vid, s in self.stats.items():
            cs = self.cells[vid]
            s["wer"] = float(np.mean([c.wer for c in cs])) if cs else 1.0
            s["p50_latency"] = int(np.percentile(self.vendors[vid].latencies, 50)) \
                if self.vendors[vid].latencies else 0
            s["spend"] = self.vendors[vid].spend
        self.final.update({k: dict(v) for k, v in self.stats.items()})
        return self.stats

    # -------------------------------------------------------------- elimination
    def eliminate_check(self) -> list[tuple[str, str]]:
        """Drop any vendor that can no longer plausibly be the best."""
        if len(self.probes_run) < self.min_probes:
            return []
        live = {v: s for v, s in self.stats.items() if self.vendors[v].active}
        if len(live) < 2:
            return []
        leader = min(live, key=lambda v: live[v]["mean"])
        leader_hi = live[leader]["hi"]
        dropped = []
        for v, s in live.items():
            if v == leader:
                continue
            if s["lo"] > leader_hi:
                self.vendors[v].eliminated_at = self.probes_run[-1]
                self.vendors[v].reason = (
                    f"95% lower bound {s['lo']:.1%} clears {self.vendors[leader].name}'s "
                    f"upper bound {leader_hi:.1%}")
                dropped.append((v, self.vendors[v].reason))
        return dropped

    # ------------------------------------------------------------------ control
    @property
    def active_vendors(self) -> list[Vendor]:
        return [v for v in self.vendors.values() if v.active]

    def leader(self) -> str | None:
        live = {v: s for v, s in self.stats.items() if self.vendors[v].active}
        return min(live, key=lambda v: live[v]["mean"]) if live else None

    def confidence(self) -> float:
        """P(the current leader really is best), renormalised over survivors."""
        live = {v: s for v, s in self.stats.items() if self.vendors[v].active}
        if not live:
            return 0.0
        tot = sum(s["p_best"] for s in live.values()) or 1.0
        return max(s["p_best"] for s in live.values()) / tot

    def should_stop(self) -> tuple[bool, str]:
        if len(self.active_vendors) <= 1:
            return True, "one vendor left standing"
        if self.spend >= self.budget:
            return True, "evaluation budget exhausted"
        if len(self.probes_run) >= self.min_probes and self.confidence() >= self.target_conf:
            return True, f"confidence target {self.target_conf:.0%} reached"
        return False, ""

    def next_focus(self) -> list[str]:
        """Where the next probe's money should go: whoever is still tied.

        Vendors already separated get no more probes — that is the entire saving.
        """
        live = {v: s for v, s in self.stats.items() if self.vendors[v].active}
        if len(live) < 2:
            return [v.id for v in self.active_vendors]
        order = sorted(live, key=lambda v: live[v]["mean"])
        best_hi = live[order[0]]["hi"]
        return [v for v in order if live[v]["lo"] <= best_hi] or order[:2]
