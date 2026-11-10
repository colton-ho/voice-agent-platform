# Azure Hosting Cost Analysis — Voicebot (Target Architecture)

> **Type: cost projection (model), not a billing reconciliation.** The app is
> mid-migration (AWS→Azure, Phase A complete); no Azure resources exist yet
> (Bicep/Container Apps land in Phase E), and no billing export or Cost
> Management access was available at analysis time. Every number below is
> modeled from the target architecture in
> `Azure_Voicebot_Implementation_Instructions_Corrected.md` §2/§12 and
> **current published Azure list prices retrieved 2026-11-06** (East US).
> Per spec §13, this does **not** repeat the "$102/month / 22% savings"
> headline and must not be quoted as a validated budget. Reconcile against the
> first real invoice via requirement R-A-020 before approving budget (G-4).
>
> Region is undecided (open item D-3). Prices are USD list, East US baseline;
> Asia-Pacific regions run roughly 0–25% higher depending on service.

---

## TL;DR

Monthly Azure cost by call volume (3 min/call, cascade engine
Azure Speech STT → LLM → Azure Speech TTS; production single-region):

| Calls/month | 500 | **1,000** | 3,000 | 5,000 |
|---:|---:|---:|---:|---:|
| **Azure total (gpt-4o-mini)** | **~$72** | **~$94** | **~$181** | **~$269** |
| Azure total (gpt-4o instead) | ~$85 | ~$119 | ~$257 | ~$395 |
| Azure cost per call (mini) | $0.145 | **$0.094** | $0.060 | $0.054 |
| ≈ RMB/月 (×7.2) | ¥520 | ¥677 | ¥1,301 | ¥1,934 |
| ≈ HKD/月 (×7.8) | HK$564 | HK$733 | HK$1,409 | HK$2,096 |

- **~$50/month is fixed** (warm compute + platform services) regardless of
  traffic — at 500 calls/month it is ~70% of the bill. At 5K calls the
  variable speech/LLM usage dominates (~78%).
- **Top cost drivers (at 1,000 calls):** Container Apps ~$39.5 (42%) ·
  Speech STT $25 (27%) · Speech TTS $12 (13%) · Service Bus $9.86 (10%) ·
  ACR $5.08 (5%) · LLM $1.6–27 (2–23% depending on model choice).
- **Adjacent non-Azure telephony (Twilio) adds ~$40/month at 1K calls**
  (inbound + Media Streams + number) — see §9. All-in ≈ **$134/month at 1K**.
- Biggest single misconfiguration risk: **Cosmos DB provisioned instead of
  serverless** — up to ~$117/month vs ~$0.10 at this scale (§8).

---

## 1. Method & scope

- **Architecture costed** (spec §2/§12): Azure Container Apps (control API,
  voice runtime, post-call worker), Azure AI Speech (STT + TTS), approved LLM
  (Azure OpenAI modeled), Cosmos DB (NoSQL), Blob Storage, Service Bus,
  Key Vault, ACR, Monitor/Log Analytics/Application Insights, Azure DNS.
  Optional Azure Managed Redis is **deferred** per decision D-9 and shown
  separately.
- **Engine:** cascade (`VOICE_ENGINE=cascade`) — the Azure baseline after ADR
  (a) dropped Nova Sonic. Azure Voice Live is a future slot
  (`VOICE_LIVE_ENABLED=false`) and is *not* costed here (it has a radically
  different audio-token cost shape; re-model at G-6 promotion time).
- **Usage basis:** `docs/cost-novasonic.md` baseline — 1,000 calls/month,
  3 min/call, caller and bot each speak ~50% — reused for comparability, with
  tiers 500/1,000/3,000/5,000.
- **Prices:** published list prices, retrieved 2026-11-06 from
  azure.microsoft.com pricing detail pages (Container Apps, Speech, Azure
  OpenAI, Cosmos DB serverless, Service Bus, Monitor, Blob, ACR, Key Vault,
  Managed Redis) and twilio.com/en-us/voice/pricing for the adjacent section.
  Cross-checked against the Azure Retail Prices API figures in secondary
  sources. **Verify in the Pricing Calculator for the chosen region before
  budgeting** (spec §13 requirement).
- **Not included:** taxes, support plans, Azure Front Door/WAF (optional edge),
  private endpoints/VNet NAT (add ~$0.10/hr each if the Bicep design goes
  private-only — ~$45/month per NAT), Entra ID P1/P2 licensing for human admin,
  Twilio number procurement outside one DID.

---

## 2. Key assumptions (all replaceable by measured usage per R-A-020)

| # | Assumption | Value | Provenance / note |
|---|---|---|---|
| A1 | Call volume & length | 500–5,000 calls/mo × 3 min, 50/50 speech | Reused from `cost-novasonic.md` baseline (its §5.1) |
| A2 | STT audio billed per call | 1.5 min (caller side) | **VAD does not shorten billable audio** (spec §13 warning) |
| A3 | TTS characters per call | ~800 blended (EN ~1,100 · zh/ja ~400) | ~90 s bot speech at 4–13 chars/s; range 400–1,200 → TTS ±$6–12 @1K |
| A4 | LLM tokens per call | ~7,800 in / ~750 out (incl. post-call summary+translate) | 5K prompt/KB + history + 1× summarize + 1× translate; `cost-novasonic.md` §5.3 text-input basis |
| A5 | Voice runtime replica | 1 vCPU / 2 GiB, `minReplicas=1` (warm — call-setup latency), active whenever a call runs | Warm floor is a *performance* choice; see §10 |
| A6 | Control API replica | 0.5 vCPU / 1 GiB, `minReplicas=1`, ~5% active (webhooks/admin) | |
| A7 | Post-call worker | 0.5 vCPU / 1 GiB, scale-to-zero, ~30 s/job | Matches Service Bus U-304 design |
| A8 | Telemetry volume | ~200 KB/call + platform logs, 30-day retention | **Highly log-level sensitive** — see trap §8.3 |
| A9 | Cosmos RU per call | ~80 RU + dashboard scans (~0.3–1M RU/mo total) | 5-container mapping (users/demos/voices/activity/history), TTL 30 d |
| A10 | Audio recordings | **Off** (spec: recording disabled until consent approved) | Enabling costs little in $ (≈$0.15/mo @1K) but not free in compliance |
| A11 | Web-voice sessions | Negligible vs phone volume | If web talk grows, it bills the same STT/TTS meters |
| A12 | LLM model | gpt-4o-mini baseline; gpt-4o as quality/flagship variant | LLM provider/commercial terms still open (G-2) |

---

## 3. Unit prices used (East US list, 2026-11-06)

| Service | Meter | Price | Source |
|---|---|---:|---|
| Container Apps (Consumption) | vCPU-s, active | $0.000024 | pricing page / Retail Prices API |
| | vCPU-s, idle | $0.000003 | |
| | GiB-s (active & idle) | $0.000003 | |
| | Requests | $0.40/1M (2M free/mo) | |
| | Free grant | 180K vCPU-s + 360K GiB-s/mo | |
| Azure Speech STT | real-time standard | **$1.00 / audio hour** | speech pricing page |
| Azure Speech TTS | neural standard / HD-flash | **$15 / 1M chars** | 0.5M chars free on F0 tier only |
| | neural HD (latest) | ~$19.5 / 1M chars | azure.cn ¥139.92/1M |
| | custom professional voice | $24/1M chars + **$4.04/model-hour hosting** | |
| | MAI-Transcribe models | promo from ~$0.36/audio hr (check 2026-12-31 expiry) | MS AI blog / speech page |
| Azure OpenAI (Global Standard) | gpt-4o-mini | $0.15 in / $0.60 out (cached in $0.075) | openai pricing page |
| | gpt-4o | $2.50 in / $10 out (cached in $1.25) | |
| | Global Batch (async) | ~50% of Standard | fits post-call worker |
| Cosmos DB **serverless** | 1M RU | $0.25 | storage $0.25/GB-mo; 50 GB & single-region limits |
| Service Bus Standard | base | $0.0135/hr ≈ **$9.86/mo** (first 13M ops incl.) | Basic is $0.05/1M ops but no topics/sessions |
| Log Analytics (Analytics) | ingestion | **$2.30/GB** (first 5 GB/mo free) | Basic Logs table: $0.50/GB |
| Blob Storage Hot LRS | storage | ~$0.0184/GB-mo | writes $0.05/10K, reads $0.0044/10K |
| ACR Basic | per day | $0.167/day ≈ **$5.08/mo** (10 GB incl.) | |
| Key Vault Standard | operations | $0.03/10K | cert renewal $3/renew — avoid, use managed TLS |
| Azure DNS | public zone | ~$0.50/mo + $0.40/M queries | |
| Egress | data out | ~$0.087/GB after monthly free grant | audio WS ≈ 3–15 GB/mo → $0 at these tiers |
| Azure Managed Redis (deferred) | B0 1 GB | $11.68/mo single-node / $23.36 HA | D-9 defers; legacy Cache for Redis retires 2028-09 |
| *(adjacent)* Twilio | inbound local | $0.0085/min + $1.15/mo/DID | |
| *(adjacent)* Twilio | Media Streams | $0.0044/min | billed on top of the call leg |

---

## 4. Per-service monthly breakdown — baseline 1,000 calls/month

| # | Service / resource | Monthly | Share | Kind |
|---|---|---:|---:|---|
| 1 | **Container Apps — voice runtime** (1 vCPU/2 GiB, warm) | $27.43 | 29% | $22.03 idle floor + $5.40 call-active |
| 2 | **Azure Speech — STT** (25 audio hrs) | $25.00 | 27% | variable |
| 3 | **Azure Speech — TTS** (0.8M chars) | $12.00 | 13% | variable |
| 4 | **Container Apps — control API** (0.5 vCPU/1 GiB, warm) | $13.21 | 14% | mostly fixed |
| 5 | **Service Bus Standard** | $9.86 | 10% | fixed |
| 6 | **ACR Basic** | $5.08 | 5% | fixed |
| 7 | **LLM (gpt-4o-mini)** | $1.62 | 2% | variable |
| 8 | Container Apps — worker (scale-to-zero) | $0.45 | <1% | variable |
| 9 | Cosmos DB serverless (RU + storage) | $0.08 | <1% | variable |
| 10 | Blob Storage (artifacts, summaries) | $0.10 | <1% | fixed |
| 11 | Key Vault + Azure DNS | $0.70 | <1% | fixed |
| 12 | Log Analytics / App Insights | $0.00 | — | within 5 GB free grant |
| | **Total** | **~$93.9** | 100% | |
| | *(swap mini → gpt-4o)* | *+$25.4* → **~$119** | | |

**Fixed vs variable split at 1K calls:** fixed ≈ **$50/mo** (warm compute floor,
Service Bus, ACR, DNS, storage) · variable ≈ **$44/mo** (speech, LLM,
call-active compute). Variable cost ≈ **$0.044/call** on gpt-4o-mini,
**$0.071/call** on gpt-4o.

---

## 5. Per-service comparison across monthly volumes

(USD/month; gpt-4o-mini baseline. This is the "billing period" comparison to
use once live — replace each column with actual Cost Analysis exports.)

| Service | 500 | 1,000 | 3,000 | 5,000 | Scaling |
|---|---:|---:|---:|---:|---|
| Speech STT | 12.50 | 25.00 | 75.00 | 125.00 | linear |
| Speech TTS | 6.00 | 12.00 | 36.00 | 60.00 | linear |
| LLM (mini) | 0.81 | 1.62 | 4.86 | 8.10 | linear |
| LLM (gpt-4o variant) | 13.50 | 27.00 | 81.00 | 135.00 | linear |
| Container Apps (all 3 apps) | 37.37 | 39.49 | 47.95 | 56.41 | floor + linear |
| Service Bus Standard | 9.86 | 9.86 | 9.86 | 9.86 | flat |
| ACR Basic | 5.08 | 5.08 | 5.08 | 5.08 | flat |
| Cosmos serverless | 0.05 | 0.08 | 0.18 | 0.25 | ~linear |
| Blob + KV + DNS | 0.80 | 0.80 | 0.85 | 0.85 | flat |
| Log Analytics | 0.00 | 0.00 | 1.00 | 3.00 | see §8.3 |
| **Azure total (mini)** | **~$72** | **~$94** | **~$181** | **~$269** | |
| **Azure total (gpt-4o)** | **~$85** | **~$119** | **~$257** | **~$395** | |
| *(adjacent)* Twilio telephony | 20.50 | 39.85 | 117.25 | 194.65 | linear |
| **All-in estimate (mini)** | **~$93** | **~$134** | **~$298** | **~$463** | |

Per-call all-in (mini) falls from ~$0.19 (500) to ~$0.09 (5,000) as the fixed
floor amortizes — the same shape as the AWS doc's R6 finding.

---

## 6. Main cost drivers

At the 1,000-call baseline the Pareto is:

1. **Container Apps warm compute (42%, ~$39.5).** ~$35 of this is the 24/7
   floor of the two `minReplicas=1` apps (memory is billed at the same
   $0.000003/GiB-s whether active or idle). This is the price of call-setup
   latency predictability.
2. **Azure Speech (40%, ~$37 combined).** STT $1/audio-hour × caller minutes
   is the #1 line item that scales with traffic; TTS $15/1M chars is driven by
   how much the bot talks (per `cost-novasonic.md`, "the bot talking one extra
   sentence costs money").
3. **LLM (2–23%, $1.6–$27).** Model choice is the biggest single lever on the
   variable half: gpt-4o-mini vs gpt-4o is a 17× spread on this line.
4. **Platform floor (Service Bus + ACR + DNS, ~$15.6, 17%).** Unavoidable at
   this design; small and stable.

At 3,000+ calls the ranking flips: STT becomes #1 and the speech pair exceeds
50% of the bill — that is where commitment tiers and model choice pay off.

---

## 7. Comparison with the AWS baseline (context only)

| | AWS (nova-sonic end-to-end, `cost-novasonic.md`) | Azure (cascade, this model) |
|---|---:|---:|
| 1,000 calls/mo, all-in | $131.62 (incl. Chime $1.95) | ~$134 (incl. Twilio $39.85) |
| Fixed infra | $32.97 | ~$50 Azure + $1.15 Twilio DID |
| Per-call AI cost | $0.0967 | $0.038 (mini) – $0.065 (4o) |

**Do not read this as "Azure is the same price."** The engines differ
(end-to-end speech model vs STT→LLM→TTS cascade) and the telephony providers
differ (Chime VC inbound $0.00065/min vs Twilio $0.0085/min + $0.0044/min Media
Streams — Twilio is ~20× the Chime leg and 30% of the all-in bill). Spec §15
requires **identical call scripts on both platforms** (R-T-013) for any real
comparison; this table is orientation only.

---

## 8. Cost traps — where "unexpected / unusually high" charges would come from

Since there is no live bill to flag anomalies in, these are the modeled
equivalents: known pricing behaviors that would make the actual invoice
overshoot this projection. Ranked by blast radius.

1. **Cosmos DB provisioned/autoscale instead of serverless — up to +$117/mo.**
   The portal default is provisioned; autoscale's floor is 400 RU/s *per
   container*. With 5 containers (users/demos/voices/activity/history) that is
   2,000 RU/s ≈ $117/mo at near-zero traffic vs **~$0.10** serverless. (Serverless
   limits — 50 GB storage, single region — fit the 30-day-TTL design fine.
   Note serverless AZ option bills RU ×1.25.)
2. **Container Apps idle bill & sizing mistakes — +$10 to +$120/mo.**
   `minReplicas=2` "to be safe" doubles the floor; 2→4 GiB memory doubles the
   memory component ($15.5→$31/mo on the runtime); putting the environment on a
   **Dedicated workload profile** adds $0.10/hr management (~$73/mo) *plus*
   full node-hours regardless of use. Most "serverless surprise bills" are
   idle replicas, not traffic.
3. **Verbose logging — +$2.30 per GB.** Emitting full transcripts, frame
   events, or OTel spans per audio chunk at debug level can easily push 20–50
   GB/mo = **+$46 to +$115/mo**. App Insights free retention is 90 days but
   *ingestion* is always billed; extended interactive retention is +$0.10/GB-mo.
4. **Speech custom-voice / HD traps.** Custom professional voice hosting is
   **$4.04 per model-hour** — a forgotten deployed endpoint is ~$2,900/mo.
   Neural-HD "latest" voices are ~30% above standard neural ($15 → ~$19.5/1M).
   And STT bills **all audio seconds sent** — silence included; VAD does not
   halve it (spec §13).
5. **Azure OpenAI provisioned (PTU) at low volume.** PTU bills per hour
   regardless of usage; at ≤5K calls/mo pay-as-you-go Global Standard is
   clearly cheaper. Also note Regional/Data-Zone deployments price ~+10% vs
   Global Standard.
6. **Service Bus Premium ($0.928/hr per MU ≈ $677/mo per MU)** — nothing in
   this workload needs it; Standard includes DLQ/topics/transactions at ~$10.
7. **Redis provisioned "just in case" — +$12–24/mo** for zero benefit while
   the runtime is single-replica (D-9 already defers it).
8. **Blob recordings with GRS + no lifecycle** — 2× storage and per-op creep;
   recording is currently disabled (A10) and should stay gated on consent.
9. **Zone/region redundancy add-ons** (Cosmos AZ ×1.25 RU, ZRS storage,
   cross-region egress) quietly opting in during Bicep review.
10. **Promo-priced meters expiring** (e.g. MAI-Transcribe discount noted
    through 2026-12-31) — a step-up on the STT line if adopted on promo terms.

---

## 9. Adjacent (non-Azure) costs — for the total picture

Kept separate because the ask is Azure hosting, but these are part of the
product's real run rate (spec §13 cost structure includes telephony):

| Item | 500 | 1,000 | 3,000 | 5,000 |
|---|---:|---:|---:|---:|
| Twilio inbound voice ($0.0085/min) | 12.75 | 25.50 | 76.50 | 127.50 |
| Twilio Media Streams ($0.0044/min) | 6.60 | 13.20 | 39.60 | 66.00 |
| Twilio DID (local number) | 1.15 | 1.15 | 1.15 | 1.15 |
| **Twilio subtotal** | **20.50** | **39.85** | **117.25** | **194.65** |

Plus, if gates go the other way: MiniMax TTS (G-2) bills outside Azure by
characters (replacement of the Azure TTS line, roughly comparable magnitude),
and Azure Voice Live (G-6) would replace both STT+TTS+LLM lines with
audio-token billing ($15–17/1M audio tokens on Voice Live Standard) — re-model
before promotion.

---

## 10. Savings opportunities (no impact on performance or availability)

Ordered by expected annual value. "No perf/availability impact" means the
runtime stays warm where latency matters and nothing loses redundancy.

| # | Action | Est. saving | Notes |
|---|---|---|---|
| S1 | **Cosmos serverless + TTL-based retention** (the U-302 design) and day-partitioned activity queries instead of table scans | up to **~$100/mo** avoided | The single biggest protection; also keeps RUs proportional to calls |
| S2 | **Log hygiene**: debug transcripts → Basic Logs tables ($0.50/GB), 30-day retention, App Insights sampling, never log raw audio | **$10–100/mo** risk reduction | Directly defuses trap §8.3 |
| S3 | **Right-size the runtime to 0.5 vCPU / 1 GiB** once the Phase E load test confirms ≤4 concurrent calls/replica (R-T-008) | **~$13/mo** | Latency-neutral if capacity holds; keep `minReplicas=1` |
| S4 | **LLM tiering**: gpt-4o-mini as default brain, gpt-4o only for complex demos; enable prompt caching on the 5K prompt/KB (cached input 50% off); **move post-call summary/translate to Azure OpenAI Batch (~50% off)** — it's already async via the Service Bus worker; collapse the summary→translate→re-summary chain into one call | **$10–60/mo** at 3–5K calls | Latency-neutral (post-call path) and mostly latency-neutral in-call |
| S5 | **Evaluate MAI-Transcribe for STT** (promo from ~$0.36/audio-hr vs $1.00) with a zh-HK/zh-CN/ja accuracy benchmark on real calls; fall back to standard if quality fails | up to **~64% of the STT line** | Quality gate required — not a blind swap |
| S6 | **TTS discipline**: standard neural voices only (no HD-latest/custom); pre-synthesize and cache static greetings (greetings are ~10% of TTS chars); consider TTS commitment tier (~20% off) only after 3 months of stable volume | **$2–15/mo** | Commitment tier is a lock-in — buy late, not early |
| S7 | **Non-prod auto-shutdown**: dev/staging scale to zero outside business hours (runtime+control+worker), F0 Speech tier for dev (5 audio-hrs + 0.5M chars free) | **~60–80% of non-prod spend** | Zero prod impact |
| S8 | **Keep Service Bus Standard** (never Premium), ACR Basic with an image-retention rule, managed TLS (no Key Vault cert renewals at $3 each) | trap avoidance | |
| S9 | **1-year Azure Savings Plan (~15%) on Container Apps active usage** — only after traffic is steady; at this scale it's a few $/mo, so do it last | ~$2–5/mo | Cancel/adjust risk is low but savings are small |
| S10 | **Budgets & anomaly alerts from day one** (R-O-003): budget at 120% of the chosen tier's forecast, action group to the ops channel; weekly Cost Analysis accumulated-cost check | risk control | Makes any trap in §8 visible within days, not at invoice |

With S1–S6 applied, the 1,000-call month lands around **~$80 (mini)** and the
5,000-call month around **~$225**, versus ~$94 / ~$269 modeled — without
touching a single warm replica or redundancy setting.

---

## 11. Validation plan (turns this model into a real bill analysis)

1. **Phase C staging slice**: record per-call usage rows (provider, unit, model,
   tenant, completed-call count — the spec §13 `usage` entity, R-A-020) and
   compare the metered totals for a known call script against §4–§5.
2. **First invoice reconciliation**: export Cost Analysis by service/resource
   for the first full billing month; replace assumptions A2 (STT seconds), A3
   (TTS chars), A8 (log GB), A9 (RU) with measured values — these four drive
   ~95% of the variance.
3. **Record approved budget** in `docs/azure/acceptance.md` (G-4/R-T-016) —
   nothing here is a budget until the business owner signs it.
4. **Open decisions that move these numbers**: A-3 (language scope → TTS/STT
   mix), G-2 (LLM/TTS provider & commercial terms), D-3 (region — Asia regions
   price ~20% higher on some meters), D-6 (concurrency → replica count),
   G-6 (Voice Live promotion — full re-model).

---

## Appendix — arithmetic anchors

- Month = 730 h = 2,628,000 s. Container Apps runtime (1 vCPU/2 GiB):
  `cost = active_s × $0.000030 + idle_s × $0.000009`, active_s = calls × 180 s.
- STT: `calls × 1.5 min ÷ 60 × $1.00`. TTS: `calls × 800 chars ÷ 1M × $15`.
- LLM per call: `7,800 × in-rate + 750 × out-rate` (÷ 1M).
- Service Bus Standard: `$0.0135/hr × 730 = $9.855`. ACR Basic: `$0.167 × 30.4`.
- Free grants applied: Container Apps 180K vCPU-s + 360K GiB-s (≈$1.6),
  Log Analytics 5 GB, Cosmos/Key Vault/Blob amounts are negligible at scale.
- FX (rough, per `cost-novasonic.md` §9): RMB ≈ USD × 7.2, HKD ≈ USD × 7.8.
