# Vet — event contract & demo script

**Product line:** Vet is the pre-flight check for agents that spend money. Before an agent commits to a
vendor, Vet compiles a private exam for *that buyer's* job, races real vendors under a cost budget,
returns a signed verdict — then keeps a canary running so it notices when a vendor quietly gets worse.

**Tagline:** Public benchmarks are dyno tests. Vet does road tests.

---

## The demo (3 acts, ~4 min)

**The need (typed by the user):**
> "After-hours calls for urgent-care clinics in Houston. Callers switch between Spanish and English
> mid-sentence, they're on cell phones in noisy rooms, and they read out prescriptions and callback
> numbers. I need the transcription layer that gets **drug names and callback numbers** right —
> I dial those numbers back."

**Act 1 — Compile the exam (~60s).** An LLM agent drafts probe *strata* with a rationale for each
(code-switch density, entity classes, SNR, phone codec). Each probe is then *synthesized*: script →
TTS (Spanish + English voices interleaved) → DSP (call-center babble at target SNR, G.711 band-limit +
μ-law, cell-phone artifacts). Ground truth is possessed **by construction** — we wrote the script.

**Act 2 — The race (~90s).** Probes stream to real vendor stacks in parallel. Live scoreboard:
decision-relevant error (callback-number exact-match, drug-name exact-match) alongside WER, latency,
price. Sequential elimination (LUCB) greys out losers early and concentrates spend where leaders
overlap. A cost ticker runs in cents. Output: a decision memo with P(best) and playable failure clips.

**Act 3 — The sting (~90s).** A sixth vendor, `ShadyTranscribe`, is added. It runs a *defeat device*:
it classifies incoming traffic as benchmark-shaped vs real, and serves a premium stack to tests and a
cheap stack to everyone else. Naive exam → it wins. Vet stealth mode → probes are camouflaged to the
buyer's production audio statistics; the discriminator's AUC collapses toward 0.5, CUSUM on the quality
residual alarms, and Vet returns **DEFEAT DEVICE DETECTED** with the receipts.

---

## Transport

Server → UI over WebSocket `/ws`. Every event is a JSON object with `seq` (int), `t` (float, seconds
since run start), `type` (string). Events are also appended to `runs/<run_id>.jsonl` so the whole demo
can be **replayed** (`?replay=<run_id>&speed=1.5`) — this is the stage-safe path; live mode hits real APIs.

UI → server: `{"cmd":"start","need":"...","mode":"live"|"sim"}`, `{"cmd":"act3"}`, `{"cmd":"reset"}`.

## Event types

```jsonc
{"type":"phase","phase":"compile|race|verdict|sting","title":"…","subtitle":"…"}

// streaming rationale from the exam compiler (typewriter effect)
{"type":"thought","text":"…partial tokens…"}

// a probe family
{"type":"stratum","id":"s1","name":"Callback numbers under babble",
 "rationale":"You dial these back — a single digit error is a wrong patient.",
 "n":6,"weight":0.35,"features":{"snr_db":8,"codeswitch":"high","codec":"g711","entities":["phone"]}}

// one synthesized probe (audio is served at /audio/<id>.wav)
{"type":"probe","id":"p12","stratum":"s1","text":"ground truth transcript",
 "entities":[{"kind":"phone","value":"713-555-0142"}],
 "audio_url":"/audio/p12.wav","duration_s":6.4,
 "features":{"snr_db":8,"codeswitch":"high","codec":"g711","disfluency":true},
 "stealth":{"mode":"stealth|naive","score":0.83}}

// vendor registration
{"type":"vendor","id":"deepgram","name":"Deepgram Nova-3",
 "stack":{"transcriber":"deepgram/nova-3"},"price_per_hr":0.26,
 "public_rank":3,"status":"active"}

// one cell of the probe × vendor matrix
{"type":"result","probe_id":"p12","vendor_id":"deepgram","hyp":"…",
 "wer":0.11,"entity_ok":false,"entity_detail":[{"kind":"phone","ref":"713-555-0142","hyp":"713-555-0142","ok":true}],
 "latency_ms":410,"cost_usd":0.0009,
 "diff":[["ok","llame al "],["sub","713","713"],["sub","555-0142","555-0148"]]}

// running estimate per vendor (drives the CI bars)
{"type":"standing","vendor_id":"deepgram","metric":"entity_error",
 "mean":0.18,"lo":0.09,"hi":0.31,"n":14,"spend_usd":0.031,"p_best":0.42,"eliminated":false}

{"type":"eliminate","vendor_id":"gladia","reason":"LCB above leader UCB","at_probe":"p22"}
{"type":"cost","total_usd":0.41,"probes_run":34,"budget_usd":2.00,"elapsed_s":88}

{"type":"verdict","winner":"assemblyai","p_best":0.96,"cost_usd":0.41,"elapsed_s":88,
 "headline":"…","memo_md":"…markdown…","evidence":["p12","p19"],
 "public_leader":"elevenlabs","public_leader_rank_here":4}

// Act 3
{"type":"sting_mode","mode":"naive|stealth","explain":"…"}
{"type":"sting_signal","vendor_id":"shady","auc":0.94,"n":18,
 "features":[{"name":"SNR variance","weight":0.31},{"name":"No disfluencies","weight":0.27}]}
{"type":"cusum","vendor_id":"shady","point":{"i":12,"s":3.4},"threshold":5.0,"alarm":false}
{"type":"alarm","vendor_id":"shady","kind":"defeat_device",
 "detail":"Premium stack served to 17/18 probe-shaped requests, 2/40 production-shaped.",
 "evidence":{"quality_gap_wer":0.19,"auc_naive":0.94,"auc_stealth":0.52}}
```

## UI layout

- **Header:** VET wordmark, mode chip (LIVE / SIM / REPLAY), elapsed, big cost ticker in cents.
- **Left rail (Act 1):** the need, streaming agent rationale, stratum cards with feature chips.
- **Center (Act 2):** vendor leaderboard. Each row: name, stack chips, **CI bar** (dot = mean,
  whiskers = 95% CI, animated collapse), decision-error %, WER %, latency, spend. Eliminated rows
  desaturate and slide down. Above it: current probe waveform + play button + diff strip
  (ref vs hyp with substitutions in red).
- **Right rail:** cost ticker, probes run, budget bar, then the decision memo when it lands.
- **Act 3 takeover:** two-panel — left "naive exam" (Shady on top, gold), right "stealth exam"
  (Shady exposed). AUC gauge animating 0.94 → 0.52, CUSUM chart crossing threshold, then a full-width
  red `DEFEAT DEVICE DETECTED` banner with the receipts line.

Design: dark, high-contrast, generous type; this is projected on a big screen from ~15 feet.
Everything animates on arrival — nothing pops in.
