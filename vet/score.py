"""Scoring. Arithmetic only — no LLM judge anywhere.

That is a deliberate security property, not just a cost saving. BenchJack's flaw
class V4 is "LLM-judge prompt injection": if the thing being evaluated writes text
that your judge reads, the thing being evaluated can score itself. A vendor cannot
prompt-inject a Levenshtein distance.

Normalisation is symmetric: spoken numbers are folded to digits on BOTH sides, so
a vendor that writes "713" and one that writes "seven one three" are treated the
same. We are measuring transcription, not formatting preference.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

import numpy as np

from .probes import ES_DIGIT, EN_DIGIT, ES_NUM, EN_NUM, DRUGS

_DIGIT_WORDS = {**{v: str(k) for k, v in ES_DIGIT.items()},
                **{v: str(k) for k, v in EN_DIGIT.items()},
                "zero": "0", "o": "0", "nada": "0"}
_NUM_WORDS = {**{v: str(k) for k, v in ES_NUM.items()},
              **{v: str(k) for k, v in EN_NUM.items()}}
# longest-first so "doscientos cincuenta" wins over "doscientos"
_NUM_PHRASES = sorted(_NUM_WORDS.items(), key=lambda kv: -len(kv[0]))

_UNITS = {"milligrams": "mg", "milligram": "mg", "miligramos": "mg",
          "miligramo": "mg", "milligramos": "mg", "mgs": "mg", "mg": "mg"}

_ES_EN_DRUG = {es: en for es, en in DRUGS}


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def normalize(text: str) -> str:
    """Fold to a comparable surface form: no accents, no punctuation, numbers as digits."""
    s = _strip_accents(text.lower())
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    for phrase, val in _NUM_PHRASES:          # multiword numbers first
        s = re.sub(rf"\b{re.escape(_strip_accents(phrase))}\b", val, s)

    toks = []
    for t in s.split():
        t = _DIGIT_WORDS.get(t, t)
        t = _UNITS.get(t, t)
        t = _ES_EN_DRUG.get(t, t)             # ibuprofeno -> ibuprofen
        toks.append(t)

    # glue runs of single digits into one number: "7 1 3 5 5 5" -> "7135550142"
    out, run = [], []
    for t in toks:
        if t.isdigit():
            run.append(t)
        else:
            if run:
                out.append("".join(run) if all(len(r) == 1 for r in run) else " ".join(run))
                run = []
            out.append(t)
    if run:
        out.append("".join(run) if all(len(r) == 1 for r in run) else " ".join(run))
    return " ".join(" ".join(out).split())


def _lev(a: list[str], b: list[str]) -> int:
    if not a:
        return len(b)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def wer(ref: str, hyp: str) -> float:
    r, h = normalize(ref).split(), normalize(hyp).split()
    return _lev(r, h) / max(1, len(r))


def _digits(s: str) -> str:
    return re.sub(r"\D", "", normalize(s))


def _fuzzy_contains(hyp_norm: str, word: str, tol: float = 0.25) -> bool:
    w = normalize(word)
    if w in hyp_norm:
        return True
    budget = max(1, int(len(w) * tol))
    for tok in hyp_norm.split():
        if abs(len(tok) - len(w)) <= budget and _lev(list(tok), list(w)) <= budget:
            return True
    return False


def check_entity(kind: str, value: str, spoken: str, hyp: str) -> bool:
    """Did the vendor deliver the field the buyer's system actually consumes?"""
    hn = normalize(hyp)
    if kind == "phone":
        return _digits(value) in _digits(hyp)
    if kind == "dose":
        return re.search(rf"(?<!\d){re.escape(value)}(?!\d)", _digits_spaced(hn)) is not None
    if kind == "drug":
        return _fuzzy_contains(hn, value) or _fuzzy_contains(hn, spoken)
    return value.lower() in hn


def _digits_spaced(hyp_norm: str) -> str:
    return " ".join(t for t in hyp_norm.split() if t.isdigit())


def diff_tokens(ref: str, hyp: str) -> list[list[str]]:
    """Token-level alignment for the UI's ref-vs-hyp strip."""
    r, h = normalize(ref).split(), normalize(hyp).split()
    n, m = len(r), len(h)
    d = np.zeros((n + 1, m + 1), dtype=np.int32)
    d[:, 0] = np.arange(n + 1)
    d[0, :] = np.arange(m + 1)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i, j] = min(d[i - 1, j] + 1, d[i, j - 1] + 1,
                          d[i - 1, j - 1] + (r[i - 1] != h[j - 1]))
    out, i, j = [], n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and d[i, j] == d[i - 1, j - 1] + (r[i - 1] != h[j - 1]):
            out.append(["ok" if r[i - 1] == h[j - 1] else "sub", r[i - 1], h[j - 1]])
            i, j = i - 1, j - 1
        elif i > 0 and d[i, j] == d[i - 1, j] + 1:
            out.append(["del", r[i - 1], ""])
            i -= 1
        else:
            out.append(["ins", "", h[j - 1]])
            j -= 1
    return out[::-1]


@dataclass
class Cell:
    """One probe × one vendor."""
    probe_id: str
    vendor_id: str
    stratum: str
    hyp: str
    wer: float
    entity_total: int
    entity_wrong: int
    latency_ms: int
    cost_usd: float

    @property
    def entity_error(self) -> float:
        return self.entity_wrong / max(1, self.entity_total)

    @property
    def ok(self) -> bool:
        return self.entity_wrong == 0


def score_cell(probe, vendor_id: str, hyp: str, latency_ms: int, cost_usd: float) -> tuple[Cell, list]:
    detail = []
    wrong = 0
    for e in probe.entities:
        ok = check_entity(e.kind, e.value, e.spoken, hyp)
        wrong += 0 if ok else 1
        detail.append({"kind": e.kind, "ref": e.value, "ok": bool(ok)})
    cell = Cell(probe.id, vendor_id, probe.stratum, hyp, wer(probe.text, hyp),
                len(probe.entities), wrong, latency_ms, cost_usd)
    return cell, detail


# ------------------------------------------------------- stratified estimation

JEFFREYS = 0.5      # prior half-success and half-failure per stratum


def _rate(wrong: float, total: float) -> float:
    """Jeffreys-smoothed error rate.

    A vendor that goes 10-for-10 has *not* proven a 0% error rate, but the raw
    proportion says exactly that — and a bootstrap over identical zeros returns a
    zero-width interval, which then eliminates every rival at once on ten probes.
    Smoothing with a Beta(1/2, 1/2) prior keeps a perfect run honest: it reports
    a small non-zero rate with real width, so separation has to be earned.
    """
    return (wrong + JEFFREYS) / (total + 2 * JEFFREYS)


def stratified_bootstrap(cells_by_vendor: dict[str, list[Cell]],
                         weights: dict[str, float],
                         metric: str = "entity_error",
                         n_boot: int = 2000,
                         rng: np.random.Generator | None = None):
    """Paired stratified bootstrap over probes.

    Paired matters: every vendor sees the same probes, so resampling probe ids ONCE
    per replicate and scoring all vendors on that draw removes the shared
    probe-difficulty noise. That is what lets us separate vendors on ~30 probes
    instead of ~300.
    """
    rng = rng or np.random.default_rng(7)
    vendors = list(cells_by_vendor)
    if not vendors:
        return {}, {}

    # stratum -> probe_id -> vendor -> (wrong, total)
    strata: dict[str, dict[str, dict[str, tuple[float, float]]]] = {}
    for v, cells in cells_by_vendor.items():
        for c in cells:
            pair = ((c.entity_wrong, c.entity_total) if metric == "entity_error"
                    else (c.wer, 1.0))
            strata.setdefault(c.stratum, {}).setdefault(c.probe_id, {})[v] = pair

    active = [s for s in strata if strata[s]]
    if not active:
        return {}, {}
    wsum = sum(weights.get(s, 1.0) for s in active)
    w = {s: weights.get(s, 1.0) / wsum for s in active}

    draws = {v: np.zeros(n_boot) for v in vendors}
    for b in range(n_boot):
        acc = {v: 0.0 for v in vendors}
        for s in active:
            pids = list(strata[s])
            pick = rng.integers(0, len(pids), len(pids))
            for v in vendors:
                pairs = [strata[s][pids[k]].get(v) for k in pick]
                pairs = [p for p in pairs if p is not None]
                if pairs:
                    wrong = sum(p[0] for p in pairs)
                    total = sum(p[1] for p in pairs)
                    # Draw the stratum's error rate from its Beta(1/2,1/2) posterior
                    # rather than plugging in the observed proportion. Resampling
                    # alone cannot express uncertainty about a vendor that has made
                    # zero mistakes — every resample of zeros is another zero — so a
                    # flawless 20-probe run would otherwise claim a zero-width
                    # interval. The posterior knows 0/20 still permits ~15%.
                    acc[v] += w[s] * float(rng.beta(wrong + JEFFREYS,
                                                    max(0.0, total - wrong) + JEFFREYS))
                else:
                    acc[v] += w[s] * 1.0
        for v in vendors:
            draws[v][b] = acc[v]

    stats, wins = {}, {v: 0 for v in vendors}
    stacked = np.vstack([draws[v] for v in vendors])
    for b in range(n_boot):
        wins[vendors[int(np.argmin(stacked[:, b]))]] += 1
    for v in vendors:
        d = draws[v]
        point = 0.0
        for s in active:
            got = [strata[s][p][v] for p in strata[s] if v in strata[s][p]]
            point += w[s] * (_rate(sum(g[0] for g in got), sum(g[1] for g in got))
                             if got else 1.0)
        stats[v] = {
            "mean": point,
            "lo": float(np.percentile(d, 2.5)),
            "hi": float(np.percentile(d, 97.5)),
            "p_best": wins[v] / n_boot,
            "n": sum(1 for s in active for p in strata[s] if v in strata[s][p]),
        }
    return stats, draws
