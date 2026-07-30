# Vet — the pre-flight check for agents that spend money

### ▶ [**Watch the demo**](https://amitrege.github.io/vet-buyer-side-evals/) — a ~2 minute slide walkthrough, plays in the browser with real audio

**Public benchmarks are dyno tests. Vet does road tests.**

An agent is about to commit to a vendor. Before it spends, Vet compiles a private,
disposable exam for *that buyer's* job, races real vendors under a cost budget,
returns a verdict with a confidence level and a price tag — then keeps a canary
running so it notices when the vendor quietly gets worse.

The demo is one self-contained page (`index.html`) plus the audio it plays
(`clips/`). Every number and transcript in it comes from a recorded run of the
engine in this repo.

## Run the engine

The pipeline uses macOS `say` and `afconvert` to create speech, so run it on
macOS with Python 3.11 or newer.

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./run.sh                      # = python -m vet: the full three-act exam, in the terminal
```

- **SIM** (the default) races calibrated local vendor models — no speech-vendor
  APIs are called. Probe synthesis, scoring, and the decision policy are the
  real implementations.
- **LIVE** (`./run.sh --mode live`) races real speech vendors through Vapi.
  Copy `.env.example` to `.env.vapi` and add credentials; never commit that
  file. If preflight cannot get usable transcripts, the run clearly marks the
  downgrade and falls back to SIM.
- Set `ANTHROPIC_API_KEY` to let the compiler write a fresh exam plan for your
  own need (`--need "..."`). Without it, Vet uses its built-in Houston
  urgent-care plan.
- Every run's full event stream lands in `vet/runs/<id>.jsonl`
  (format: `vet/SPEC.md`).

Run the regression tests with:

```bash
./.venv/bin/python -m unittest discover -s tests -v
```

## The three acts

**1 — Compile.** Claude reads the buyer's sentence and picks *strata*: which failure
modes matter, which generator template and channel profile realise them, how many
probes each gets, and what each is worth. It streams its own reasoning while it does.
It may only choose from templates and channels that exist, so every plan is executable.

**2 — Race.** Probes are synthesized locally (macOS `say` → code-switched Spanglish →
babble, G.711 μ-law, reverb, packet loss). **We wrote the script, so we own the answer
key** — no labelling, no LLM judge. Vendors are raced under successive elimination on a
paired stratified bootstrap: a vendor is dropped the moment its 95% lower bound clears
the leader's upper bound, spend concentrates on whoever is still tied, and the race
stops when P(best) crosses the bar or the money runs out.

**3 — The sting.** `ShadyTranscribe` runs a *defeat device*: it classifies incoming
traffic and serves a premium stack to anything that looks like a test. Against a
studio-clean benchmark it is caught 38/38 (discriminator **AUC 1.0**) and looks
suspiciously strong near the top. Against probes drawn from the buyer's own
channel distribution, the discriminator collapses to **AUC ≈ 0.50 — chance** —
the cheap stack gets served, error
jumps by about 28 percentage points in the recorded run, and CUSUM alarms within
a handful of probes.

## Why the scoring has no LLM in it

BenchJack's flaw class V4 is *LLM-judge prompt injection*: if the thing being evaluated
writes text your judge reads, it can score itself. Vet scores with Levenshtein and
digit normalisation. A vendor cannot prompt-inject an edit distance.

## Layout

| file | what it is |
|---|---|
| `index.html` | the demo — one self-contained slide deck, no dependencies |
| `clips/` | the real probe audio the deck plays |
| `vet/compiler.py` | Claude picks the strata; streams its reasoning; deterministic fallback |
| `vet/probes.py` | code-switched script generation + entity ground truth |
| `vet/dsp.py` | the channel: babble, G.711, reverb, dropouts, AGC — and the features a defeat device would read |
| `vet/score.py` | symmetric normalisation, WER, entity checks, Jeffreys-smoothed stratified bootstrap |
| `vet/race.py` | successive elimination, stopping rule, spend accounting |
| `vet/sting.py` | discriminator, AUC, defeat device, CUSUM |
| `vet/vapi.py` | live vendor calls over Vapi websocket transport |
| `vet/exam.py` | orchestration: the three acts, the event stream, the run log |
| `vet/__main__.py` | the terminal front door (`python -m vet`) |

## Known limits

- SIM vendor profiles are calibrated against real measurements, not scraped from
  vendor docs. LIVE mode is the ground truth; SIM is labelled as such everywhere.
- Latency in LIVE mode is Vapi's own `transcriberLatencyAverage`, which includes
  their pipeline, not just the vendor.
- The "buyer's production traffic" in Act 3 is itself synthetic. The stealth metric
  (discriminator AUC) is real; the traffic it is matched against is simulated.
