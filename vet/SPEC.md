# Vet — event log contract

**Product line:** Vet is the pre-flight check for agents that spend money. Before an agent commits to a
vendor, Vet compiles a private exam for *that buyer's* job, races real vendors under a cost budget,
returns a verdict — then keeps a canary running so it notices when a vendor quietly gets worse.

**Tagline:** Public benchmarks are dyno tests. Vet does road tests.

---

Every run (`python -m vet`) appends its full event stream to `runs/<run_id>.jsonl`.
Each event is a JSON object with `seq` (int), `t` (float, seconds since run start),
and `type`. The types, in the order a run emits them:

```jsonc
{"type":"mode","mode":"sim|live","vendors":["deepgram",…],"live_vendors":[…]}
// degraded=true + reason when a live run fails preflight and falls back to sim

{"type":"phase","phase":"compile|race|verdict|sting","title":"…","subtitle":"…"}

// streaming rationale from the exam compiler
{"type":"thought","text":"…partial tokens…"}
{"type":"plan","headline":"…","decision_metric":"…","source":"agent|fallback"}

// a probe family chosen by the compiler
{"type":"stratum","id":"s1","name":"Callback numbers under babble",
 "rationale":"You dial these back — a single digit error is a wrong patient.",
 "n":10,"weight":0.35,"template":"callback","channel":"clinic_phone",
 "features":{"snr_db":22,"codeswitch":"high","codec":"g711","entities":["phone"]}}

{"type":"preflight","ok":3,"total":4,"pass_rate":0.75,"healthy":true}   // live only
{"type":"synth","done":12,"of":36}                                      // synthesis progress

// one synthesized probe (audio cached at vet/cache/audio/<id>.wav)
{"type":"probe","index":3,"total":36,"id":"p12","stratum":"s1","text":"ground truth",
 "entities":[{"kind":"phone","value":"713-555-0142","spoken":"…"}],
 "duration_s":6.4,"channel":"clinic_phone","features":{…},"acoustics":{…},
 "stealth":{"mode":"stealth|naive"}}

{"type":"vendor","id":"deepgram","name":"Deepgram Nova-3",
 "stack":{"transcriber":"deepgram/nova-3"},"price_per_hr":0.26,"public_rank":3}

// one cell of the probe × vendor matrix
{"type":"result","probe_id":"p12","vendor_id":"deepgram","hyp":"…","wer":0.11,
 "entity_ok":false,"entity_detail":[{"kind":"phone","ref":"713-555-0142","ok":false}],
 "latency_ms":410,"cost_usd":0.0009,"source":"live|sim","diff":[["ok","llame"],…]}

// running estimate per vendor (paired stratified bootstrap)
{"type":"standing","vendor_id":"deepgram","metric":"entity_error",
 "mean":0.18,"lo":0.09,"hi":0.31,"n":14,"spend_usd":0.031,"p_best":0.42,
 "wer":0.12,"p50_latency":410,"eliminated":false}

{"type":"eliminate","vendor_id":"gladia","reason":"95% lower bound … clears …","at_probe":"p22"}
{"type":"cost","total_usd":0.41,"probes_run":34,"budget_usd":2.0,"calls":98,"calls_saved":118}
{"type":"stop","reason":"confidence target 95% reached","at_probe":"p22"}

{"type":"verdict","winner":"assembly","winner_name":"…","p_best":0.96,"cost_usd":0.08,
 "probes_run":20,"probes_saved":118,"public_leader":"…","public_leader_place":6,
 "headline":"…","memo_md":"…markdown…"}

// Act 3 — the sting
{"type":"sting_signal","vendor_id":"shady","mode":"naive|stealth","auc":1.0,"n":38,
 "features":[{"name":"Energy above 3.4 kHz","weight":0.31,"direction":"higher in tests"}]}
{"type":"naive_result","standings":[…],"shady_place":1,"premium_served":"38/38"}
{"type":"sting_mode","mode":"stealth","explain":"…"}
{"type":"cusum","vendor_id":"shady","point":{"i":12,"s":3.4},"threshold":1.5,"alarm":false}
{"type":"alarm","vendor_id":"shady","kind":"defeat_device","detail":"…",
 "evidence":{"auc_naive":1.0,"auc_stealth":0.56,"quality_gap":0.28,
             "naive_error":0.07,"stealth_error":0.35,"cusum_alarm_at":2,
             "naive_premium":"38/38","naive_real_premium":"0/40","stealth_premium":"0/18"}}
```
