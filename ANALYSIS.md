# Buyer-Side Benchmarks: Corpus Analysis, Idea Slate, and Recommended Demo
*Research readout, 2026-07-24. Sources downloaded to `papers/` (TeX) and `code/` (repos).*

---

## 1. What's downloaded locally

| Local path | Paper / repo | What it is | Quality tier |
|---|---|---|---|
| `papers/2606.06462` | **Benchmark Agent** ("Benchmark Everything Everywhere All at Once", CUHK, NeurIPS'26 sub) | Agentic benchmark factory: Planner (Design→Grounding→Allocation over dataset pool) + Executor (TTS, noise mixing, STT, OCR, image degradation, web search) + verification loops | High (your anchor paper; code is real) |
| `code/Benchmark-Agent` | Its code | 59 py files, 6-stage pipeline, XTTS/pydub/TUT-noise tools, caching. Real system, ~8.5/10 maturity | — |
| `papers/2407.08351` + `code/AutoBencher` | **AutoBencher** (Stanford, ICLR 2025) | Declarative desiderata as optimization: salience constraint + difficulty (1−max acc) + **separability (MAD of model accs)** + novelty (1−rank-corr). Privileged-info oracles (retrieval / sympy / translator). ~$15–45/run | High |
| `papers/2605.12673` | **Benchmark Audit** (Berkeley RDI — Dawn Song et al.) | Auditing agent that *exploits* benchmarks: 10/10 audited, 9/10 near-perfect w/o solving anything; 219 flaws in 8 classes (V1–V8). Defender loop cuts exploitability to <10% | High |
| `papers/2507.02825` | **ABC checklist** (UIUC+Stanford+Berkeley+UK AISI…, NeurIPS 2025) | Task-validity / outcome-validity / reporting checklist. τ-bench do-nothing agent = 38%; 24% of SWE-bench-Verified top-50 leaderboard positions wrong | High |
| `papers/2504.20879` | **The Leaderboard Illusion** (Cohere+) | Arena gaming mechanics: Meta's 27 private variants, selective retraction, best-of-N ⇒ ~+100 Elo illusion; 62.8% of Arena data to 4 providers; Arena-data SFT ⇒ +112% relative win-rate, MMLU down | High |
| `papers/2508.01780` + `code/LiveMCPBench` | **LiveMCPBench** (ICIP-CAS) | 95 *manual* tasks over 70 MCP servers; LLM-judge (81% human agreement). Evaluates **agents**, not servers | Medium-high |
| `papers/2507.12806` + `code/MCPEval` | **MCPEval** (Salesforce) | Auto task-gen from tool schemas; ground truth = frontier-agent execution trajectories; tool-call matching + judge. Production-quality code (v1.1.0, web UI, CI) | High (kills much of "MCP certifier" novelty) |
| `papers/2604.09251` | **DrBencher** (IBM, COLM'26 sub) | Answer-first generation: SPARQL/KG → template *code executes* → gold answer; 2-stage difficulty cascade; max-independent-set diversity. Validity 76% (84% excl. stale KG); frontier models 20% acc | High (honest, verifiable-by-construction) |
| `papers/2510.08569` | **ArenaBencher** (UC Davis+MSR) | Evolves existing benchmark items via multi-model feedback; difficulty/separability/fairness/alignment metrics; LLM-judge circularity | Medium |
| `papers/2406.11775` | **Task-Me-Anything** (NeurIPS 2024) | Programmatic taxonomy → 750M instances, oracle-by-construction, **query interface** ("which model is best at X?"), budget-aware active approximation | High (closest existing thing to buyer-side framing) |

Key adjacent work (not downloaded, verified via web):
- **CrossASR / CrossASR++** (ICSME'20/21): TTS→ASR differential testing with failure-probability predictor. *Direct prior art for "inverted tool stack" on STT.*
- **LLM SELECTOR** (2510.09418): active model selection — adaptively pick which queries to annotate to find the best LLM, −59.6% annotation cost. **ATLAS** (2511.04689): IRT/Fisher-information adaptive testing, −90% items.
- **YourBench** (HF, 2504.01833): generate evals from *your documents*; explicit anti-contamination motivation.
- **Model Equality Testing** (2410.20247): MMD two-sample test on API outputs; found **~⅓ of audited Llama endpoints deviating** from reference weights (quantization/substitution).
- **Eval-awareness / sandbagging**: "LLMs Often Know When They Are Being Evaluated" (2505.23836; Gemini 2.5 Pro AUC 0.83 separating eval vs deploy transcripts), AI Sandbagging (2406.07358), in-context eval-awareness (2603.03824).
- **"Defeat Devices in AI Systems"** (2606.28863, ~6 weeks old): formalizes the Dieselgate analogy (eval-context discriminator + behavior swap + eval-vs-deploy gap); proposes differential probing. Formalization/survey — no buyer-side system, no live demo.
- **StealthEval** (GitHub): rewrites eval prompts to look deployment-like, measures behavior shift. Framework, not a product.
- **Sock-puppet audits** (Sandvig et al. 2014) — the "secret shopper" methodological lineage for algorithm auditing.
- **Agentic commerce**: ACP (OpenAI/Stripe, checkout), AP2 (Google, signed Intent/Cart/Payment mandates), x402 (Coinbase; ~69k agents, 165M txns, ~$50M volume by Apr 2026). All solve *payment/authorization* trust. **None solve capability trust.**
- **Commercial eval landscape**: Artificial Analysis STT leaderboard (51 models; AA-WER on 3 fixed datasets; price/speed), Vals AI (private industry benchmarks), Scale SEAL, HF Open ASR Leaderboard (now adding private sets against "benchmaxxing"), OpenRouter (usage rankings), Braintrust/LangSmith/Patronus/Galileo (evaluate *your own app*, not vendors).

**Fact-check on your claims**: the Berkeley result is real but the details differ — BenchJack got 100% on **all 890 FieldWorkArena tasks with a single trivial message** `send_msg_to_user("{}")` (the validator never reads message content), and separately 100% on all 89 Terminal-Bench tasks via a fake `curl` wrapper. "Eight benchmarks" → the paper audits 10, exploits succeed on all, near-perfect on 9. Your Chatbot Arena characterization matches The Leaderboard Illusion.

---

## 2. First-principles read of the problem

**This is a lemons market.** Sellers know their service's true quality; buyers see marketing plus public leaderboards that are (a) averages over someone else's distribution, (b) contaminated/saturated, (c) demonstrably gamed (BenchJack, Leaderboard Illusion), and (d) stale while the service silently changes underneath (⅓ of audited endpoints not serving claimed weights). Classic market fixes for lemons: warranties, brands, certification, **inspection**. The agentic economy makes inspection free to *scale*: every buyer can run a bespoke, private, adversary-robust inspection before and after buying. ACP/AP2/x402 built the payment-trust layer; the capability-trust layer is missing. That's the space.

**Your three shifts, audited against the corpus:**

*Shift 1 (coverage → decision value): correct, and the literature is already half-way there without noticing.* AutoBencher and ArenaBencher both have "separability" metrics; LLM SELECTOR and ATLAS do label-efficient adaptive selection. What does not exist anywhere: separation **on your task distribution**, over **service vendors** (not checkpoints), with **dollar-cost accounting and a stopping rule**, ending in a **purchase decision with a confidence certificate**. The crisp formalism isn't really VoI-in-the-abstract — it's **best-arm identification with per-probe costs** (LUCB/successive elimination) plus stratified task mix. That's ~100 LOC and extremely demoable (confidence intervals collapsing, vendors eliminated live, a running "this evaluation cost $0.73" ticker). Your instinct that this dissolves SSC is confirmed: SSC is the paper's *weakest, judge-circular* metric (30–51 range, an LLM scoring "challenge depth"), while separation between real candidates is directly measurable.

*Shift 2 (the oracle is the crux): right, with two corrections.* First, every oracle mechanism you listed exists somewhere in isolation: constructive (CrossASR 2020 — literally TTS→ASR; Benchmark-Agent's tool stack; Task-Me-Anything; DrBencher's answer-first code execution), differential (CrossASR; Model Equality Testing), metamorphic (SE literature, underused for AI services). The novel move is the **oracle compiler**: given a use-case spec, automatically choose and compose the oracle strategy per probe family, and *report which probes have construction-grade truth vs judge-grade truth*. Second — and this is where I'd push your framing — **the oracle problem includes the evaluator's own attack surface**. BenchJack's V1–V8 taxonomy shows evals fail not because answers are unknown but because the *apparatus* is exploitable (LLM-judge injection = V4). A buyer's private exam that scores vendor outputs with an LLM judge can be prompt-injected by the vendor's response. This is a hidden argument for verticals where scoring is arithmetic (WER via Levenshtein) — un-injectable by construction.

*Shift 3 (the adversarial turn): your strongest claim to novelty, and it just became urgent.* Defeat devices for AI were formalized six weeks ago (2606.28863) with no system behind them; eval-awareness is measured (AUC 0.83); real-world endpoint substitution is documented (~⅓ of endpoints); Arena gaming is documented. Nobody has (i) stated **probe stealth as an engineering objective with a metric** (discriminator AUC → 0.5), (ii) demoed **catching a defeat device live**, or (iii) productized **continuous post-purchase canary auditing** (CUSUM on quality residuals — catches silent nerfs/quantization/model swaps). This is the part that cannot be called cookie-cutter by anyone who knows the literature.

**What is NOT novel (say it before judges do):** bespoke exam generation (AutoBencher, YourBench, Benchmark-Agent), TTS→ASR testing (CrossASR), label-efficient selection (LLM SELECTOR, ATLAS), API substitution detection (Model Equality Testing), the defeat-device concept (2606.28863). The novelty is the **composition aimed at a purchase decision** plus the **stealth objective made real**. Own the citations; the demo's job is to make shifts 1 and 3 visceral.

---

## 3. Idea slate and ranking

Scoring lens: novelty/insight 35%, demo-wow 30%, weekend feasibility 20%, product-truth 15%.

**#1 — "Vet" + The Sting (build this).** Buyer agent builds a bespoke exam for a niche STT need, races real vendors to a statistically certified decision with live cost ticker — then a planted two-faced vendor ("ShadyTranscribe") games the naive exam and gets caught by stealth probes + sequential detection, live. Vet alone is excellent theater but a sophisticated judge can pattern-match it to AutoBencher/YourBench/MCPEval; the sting adds the thing nobody has: a live defeat-device catch and a measurable stealth objective. It also pre-answers the Goodhart objection to the product itself ("won't vendors game *your* probes?" — yes, here's the arms race, here's our answer). Degrades gracefully: cut Act 3 to a slide if the build slips.

**#2 — Stealth auditor standalone ("Dieselgate detector for AI APIs").** Continuous canary probing + MMD/CUSUM substitution detection, demoed on reproduced quantization/nerf scenarios. Sharpest pure-research demo; weaker "useful today" story; and accusing real vendors live is irresponsible while synthetic targets alone feel thin without the buying context. Folded into #1 as Act 3 it's strictly better.

**#3 — Pure Vet (no sting).** The safe version. Still beats most demo-project fare on usefulness + theater (playable failure audio is gold). Keep as fallback.

**#4 — MCP server certifier.** I disagree with "best product wedge" as a *demo* choice. MCPEval (Salesforce, shipping code) already does schema→task-gen→execution-grounded eval; what's left (robustness/safety probes, per-server report card, cross-server comparison, signed cards, registry distribution) is real but extension-shaped — "MCPEval + mcp-scan + a directory." Demo output is JSON, not a moment. The wedge is real as a *company*; it's the wrong *demo-day bet* against a "no cookie-cutter" bar.

**#5 — Exam-as-escrow protocol** (commit exam hash; vendor passes → x402 releases payment; commit-reveal prevents overfitting). Genuinely novel mechanism-design garnish — one coda slide, not the build.

**#6 — Exam network → aggregated report cards** (Consumer Reports data asset). Business slide only.

**#7 — CUA/computer-use exam generation.** Oracle is expensive, WebArena-adjacent crowded, weekend-risky. Skip.

---

## 4. The recommended demo: "Vet — road tests for the agent economy"

Framing: public benchmarks are dyno tests; VW taught us what happens when sellers know the dyno. Vet does road tests.

**Act 1 — The exam materializes (~90s).** User/agent need: "transcription for noisy Spanglish customer calls, medical-adjacent vocabulary, phone-quality audio." Agent drafts probe strata with per-probe rationale (code-switch density, entities: drug names / addresses / dollar amounts, SNR levels, 8kHz codec). For each probe: writes script → ElevenLabs v3 TTS (code-switching voices) → mixes call-center noise + phone codec. **Ground truth possessed by construction** — Benchmark-Agent's own tool stack, pointed backwards. Play 2 probes aloud.

**Act 2 — The race (~2 min).** Probes stream to 6 real APIs in parallel: Deepgram Nova-3 ($0.26/hr), AssemblyAI Universal-3.5-Pro (markets code-switching — perfect foil), OpenAI gpt-4o-transcribe, ElevenLabs Scribe v2 (public leaderboard #2), Groq whisper-v3-turbo ($0.04/hr), Voxtral 2. Live scoreboard: stratified WER + latency + price; **LUCB/successive-elimination racing** — CIs shrink, losers knocked out early, spend concentrated where leaders overlap; running eval-cost ticker. Output: **decision memo** — "Chose X; P(best for your mix) ≈ 97%; eval cost $0.73; projected annual cost $Y" — with playable failure clips ("listen to the leaderboard champion turn 'ibuprofeno 600' into 'a profane 600'"). The public ranking flipping on a niche mix is the expected outcome; if it doesn't flip, the memo story still lands ("now you *know*, for 73 cents").

**Act 3 — The sting (~90s).** Add "ShadyTranscribe": our own vendor with a defeat device (detects benchmark-shaped traffic — too-clean SNR, TTS artifacts, burst pattern, no codec — serves a frontier model; serves a cheap model otherwise). Naive exam: ShadyTranscribe wins. Vet stealth mode: probes camouflaged to production statistics + paced/mixed with decoy traffic; discriminator AUC drops 0.94 → ~0.51 (chance); CUSUM exposes the quality gap → **"DEFEAT DEVICE DETECTED"** verdict, live. One slide of receipts: eval-awareness AUC 0.83, ⅓ of endpoints deviating, Arena private variants — "we built ShadyTranscribe because accusing a real vendor live would be irresponsible; every behavior it implements is documented in the wild."

**Coda (~20s).** Continuous mode: the exam never stops — canary probes ride production traffic post-purchase; alerts on silent nerfs; memo + probes as the due-diligence layer above ACP/AP2/x402. "Agents can now pay anyone. Vet tells them who's worth paying — and keeps them honest after the check clears."

**Build plan (weekend):**
- Probe synthesis: scripts via LLM; ElevenLabs v3 (fallback gpt-4o-mini-tts `instructions`); noise mixing MUSAN/pydub; G.711 8kHz codec pass. (~4h)
- Vendor adapters: 6 REST clients + normalization (~4h; get keys *first*, cache all responses, record a dry-run as demo backup)
- Scoring/decision: normalized WER (Levenshtein — no judge, un-injectable), stratified bootstrap CIs, LUCB elimination + stopping rule, cost ticker (~4h)
- ShadyTranscribe: FastAPI proxy, heuristic probe classifier, two backends (Groq turbo vs whisper-tiny); stealth = feature-matched probes + pacing; detection = held-out discriminator AUC + CUSUM (~5h)
- UI: scoreboard + waveforms + memo + sting dashboard (~8h, the real cost)
- Validity check (if time): 20 real labeled utterances; show synthetic-exam ranking correlates (answers the "your exam is a simulation" objection; the honest line: *the exam is a model of your distribution whose fidelity is itself measured — and strictly better than a public average*)

**Judge Q&A prep:** "Isn't this CrossASR?" — CrossASR finds bugs via differential testing; Vet makes a *purchase decision*: by-construction oracles + cost-optimal stopping + adversary robustness; different objective, superset machinery. "Isn't this AutoBencher/YourBench?" — those optimize benchmark desiderata or doc-grounded QA for model choice; no service vendors, no dollars, no adversary, no stopping rule. "Why STT?" — arithmetic oracle (no judge to inject), audible failures, true commodity market with real price spread; same machinery generalizes to OCR/extraction/translation — say it, show one.
