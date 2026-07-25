"""The exam compiler: turn a buyer's sentence into an executable exam.

This is the one place a language model is allowed to make a decision, and it is
deliberately fenced in. The model does not write probes and it does not grade
anything — it chooses *strata*: which failure modes matter for this buyer, which
generator template and channel profile realise them, how many probes each gets,
and how much each should weigh in the final decision.

That fence is the lesson from the Benchmark Agent line of work: a planner must
ground every subtask in a transformation the executor can actually run, or the
plan is fiction. So the model picks from a menu of things that exist.

We stream the model's own summarized reasoning to the UI. It is not decoration —
it is the audit trail for why this exam looks the way it does.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import anthropic

from .probes import TEMPLATES

MODEL = "claude-opus-5"

CHANNELS = {
    "clinic_quiet": "quiet room, cell phone, mild codec — the easy end of your traffic",
    "clinic_phone": "typical after-hours call: background babble ~9 dB SNR, G.711, packet loss",
    "clinic_harsh": "worst realistic case: 4 dB SNR, heavy dropouts, reverberant room",
}

TEMPLATE_DOC = {
    "callback": "caller leaves a callback number, digits spoken in Spanish or English",
    "prescription": "discharge + drug name + dose + callback number, code-switched mid-sentence",
    "symptom": "symptom description with drug/dose embedded, heavy code-switching, no phone",
    "correction": "caller says a wrong number, self-corrects mid-utterance — only the corrected one counts",
    "pharmacy": "pharmacy calls in about a refill: English frame, Spanish tail, drug + dose + callback",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "decision_metric": {"type": "string"},
        "strata": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "rationale": {"type": "string"},
                    "template": {"type": "string", "enum": sorted(TEMPLATES)},
                    "channel": {"type": "string", "enum": sorted(CHANNELS)},
                    "n": {"type": "integer"},
                    "weight": {"type": "number"},
                },
                "required": ["id", "name", "rationale", "template", "channel", "n", "weight"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "decision_metric", "strata"],
    "additionalProperties": False,
}

SYSTEM = """You are Vet's exam compiler. A buyer is about to spend money on a vendor \
and wants a private, disposable exam that will change their purchase decision correctly \
at minimum cost.

You are not writing a research benchmark. Coverage and difficulty are not the goal. \
The goal is separation between the specific vendors on this buyer's actual traffic \
distribution, weighted by what it costs them when a vendor gets it wrong.

You choose strata only. A stratum is (generator template x channel profile x quota x weight). \
The executor synthesizes the audio and owns the answer key by construction, so every \
stratum you name must map to a template and channel that exist.

Rules:
- 3 to 5 strata. Total probes across strata between 24 and 40. Fewer than ~24
cannot separate this many vendors: the intervals stay overlapped and you end
up buying on noise, which is worse than not testing.
- Weights must sum to about 1.0 and should reflect business cost, not frequency. \
A field the buyer acts on automatically (dialling a number back, sending a prescription) \
is worth more than one a human reads.
- At least one stratum must sit at the hard end of the buyer's real channel, because \
that is where vendors separate. At least one should be near the easy end, or you cannot \
tell a vendor that is broadly bad from one that only fails under stress.
- Each rationale is one sentence, addressed to the buyer, naming the consequence of \
failure in their words. No benchmark jargon.

Think briefly about what actually breaks for this buyer before choosing."""


@dataclass
class Stratum:
    id: str
    name: str
    rationale: str
    template: str
    channel: str
    n: int
    weight: float


@dataclass
class ExamPlan:
    headline: str
    decision_metric: str
    strata: list[Stratum] = field(default_factory=list)
    source: str = "agent"


# A plan that is always valid, so a network hiccup can't kill the demo.
FALLBACK = ExamPlan(
    headline="Callback numbers and drug names under real after-hours conditions",
    decision_metric="entity error rate (callback digits + drug + dose), stratum-weighted",
    source="fallback",
    strata=[
        Stratum("s1", "Callback numbers under babble",
                "You dial these back — one wrong digit is a call to a stranger.",
                "callback", "clinic_phone", 10, 0.35),
        Stratum("s2", "Prescription + dose, code-switched",
                "A misheard drug or dose goes into a chart before anyone re-reads it.",
                "prescription", "clinic_phone", 10, 0.30),
        Stratum("s3", "Self-corrected numbers",
                "Callers correct themselves constantly; taking the first number is a wrong callback.",
                "correction", "clinic_quiet", 8, 0.15),
        Stratum("s4", "Worst-case waiting room",
                "Your loudest calls are the ones that most need a callback.",
                "symptom", "clinic_harsh", 8, 0.20),
    ],
)


def _menu() -> str:
    t = "\n".join(f"  - {k}: {v}" for k, v in TEMPLATE_DOC.items())
    c = "\n".join(f"  - {k}: {v}" for k, v in CHANNELS.items())
    return f"Generator templates available:\n{t}\n\nChannel profiles available:\n{c}"


def compile_exam(need: str, on_thought=None) -> ExamPlan:
    """Stream the compiler's reasoning to `on_thought`; return an executable plan."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return FALLBACK
    try:
        client = anthropic.Anthropic()
        text = ""
        with client.messages.stream(
            model=MODEL,
            max_tokens=8000,
            system=SYSTEM,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": f"{_menu()}\n\nThe buyer's need:\n{need}"}],
        ) as stream:
            for event in stream:
                if event.type != "content_block_delta":
                    continue
                if event.delta.type == "thinking_delta" and on_thought:
                    on_thought(event.delta.thinking)
                elif event.delta.type == "text_delta":
                    text += event.delta.text
        data = json.loads(text)
        plan = ExamPlan(
            headline=data["headline"],
            decision_metric=data["decision_metric"],
            strata=[Stratum(**s) for s in data["strata"]],
        )
        return plan if plan.strata else FALLBACK
    except Exception as exc:                      # demo must never die on the network
        if on_thought:
            on_thought(f"\n[compiler unavailable: {type(exc).__name__} — using cached plan]\n")
        return FALLBACK
