# Runbook — Storage failure (Cosmos unavailable / degraded)

Author: team-lead · Date: 2026-11-20 · Status: **runbook**
Scope: `docs/azure/implementation-plan.md:427` U-406 runbook #5 · closes **R-O-001** runbook side; failure-row evidence for **R-T-009**.
Ground truth: `storage/cosmos.py` (auth + exception translation), `storage/cosmos_calls.py` (FC-6 semantics), `storage/ownership.py` (FC-7 OCC), `tests/test_status_callbacks.py` (persist-before-ack), `bot.py:7034-7137` (status callbacks).
Affected surfaces: call records/events (Cosmos `calls` family + `ownership` container), status callbacks, post-call summaries, lease bookkeeping. **Live audio does not traverse Cosmos** — an ongoing call keeps talking; its *bookkeeping* degrades.

---

## 1. Symptom / trigger

- `alert-cosmos-429-<env>` firing (`ThrottledRequests`, `infra/bicep/modules/monitor.bicep:153`) — **degraded** (429/throttling or high latency);
- or `alert-app-failed-<env>` rising with 5xx from `/twilio/status-callback` — **persist-before-ack doing its job** (we refuse to ack unpersisted events so Twilio retries);
- worker logs: `read failed: ...` / `write failed: ...` (`workers/postcall_worker.py`);
- runtime logs: Cosmos exceptions from `CosmosCallRepository` / `CosmosSessionDirectory` paths.

**Design posture under storage failure (what the code guarantees):**

- **Persist-before-ack** (`bot.py:7094-7137`, `tests/test_status_callbacks.py`): a Twilio status event is persisted **before** the 200 ack. On persistence failure the endpoint returns **5xx so Twilio retries**; the deterministic `event_id` (`CallSid:CallStatus:SequenceNumber|Timestamp`) dedups redeliveries to a **single append**. We never ack an unpersisted event.
- Deliberate 200-drops (a retry can never help): event for a **finalized** call (`CallFinalizedError` → dropped, never revived), payload **without CallSid** (nothing to key on).
- **Unknown-CallSid → orphan persist** (U-205 decision): the event creates a minimal `RECEIVED`-state call record first (busy/no-answer/failed calls never open a stream and must not lose their trail).
- **Idempotent retry surfaces everywhere else**: `create()` idempotent on conflict; `append_events` dedups by `event_id`; FC-7 writes are etag-OCC (a losing writer retries within `_CLAIM_ATTEMPTS`, or surfaces `StaleVersionError` for a clean re-read); post-call worker treats write failures as transient (abandon/redeliver) and is idempotent at `summary_status=='ok'`.
- Lease consequence: if renewal writes fail long enough, leases expire within `lease_ttl_seconds` (30 s) and **owner-loss semantics apply to those calls** (`owner-loss.md`) — storage failure can cascade into call drops. This is the honest failure chain, not a separate bug.

## 2. Immediate operator checks (in this order)

```bash
# 1. Is Cosmos up? (account-level)
az cosmosdb show --name cosmos-voicebot-<env> --resource-group rg-voicebot-<env> \
  --query "{state: provisioningState, endpoints: documentEndpoint}" -o json

# 2. Degraded (429) or down? Check throttling first — 429 = capacity, not outage
#    Azure portal Metrics on cosmos-voicebot-<env>: ThrottledRequests (StatusCode 429), normalized RU consumption per container.
#    Alert context: alert-cosmos-429-<env> (monitor.bicep) tells you which dimension tripped.

# 3. What is failing right now — acks or background?
#    - 5xx on /twilio/status-callback  -> persist-before-ack leg (expected during outage; Twilio is retrying)
#    - worker "read failed"/"write failed" -> summary pipeline (abandon/retry loop is holding)
#    - FC-7 StaleVersionError spikes     -> lease contention/retries under latency

# 4. Call impact census — are calls being lost to lease expiry?
PYTHONPATH=. python3 scripts/deploy/drain_status.py census --owners <replica1,...> --format text
#    (drain_status.py authenticates like storage/cosmos.py:56-68 — same account, read-only)
```

Order matters: **availability → throttling → which surface is acking 5xx → call impact**. Do not restart apps before step 1-2: restarts do not fix 429s and lose in-flight retries.

## 3. Mitigation steps

1. **Throttled (429) but up:** this is capacity. The `calls` family runs autoscale 400–4000 RU/s (u-401-deployment-plan.md §6); sustained 429s under real load mean the ceiling is too low → raise the autoscale max (portal/CLI, needs authorization per R-O-004) or reduce write bursts. Application-side, the OCC/retry + abandon loops absorb brief 429s — no code action.
2. **Unavailable (hard down / regional):** Azure-side incident response. Meanwhile:
   - Twilio keeps retrying status callbacks (5xx leg) — nothing to do, do not ack anything manually;
   - calls in flight continue on media but their events/leases are unrecorded; **expect owner-loss fallout** (`owner-loss.md`) once leases lapse;
   - summaries fail transiently → the worker abandons until `maxDeliveryCount`; I-4 lands them `failed` if the outage outlasts the retries — replay later (§4).
3. **After recovery — reconcile (in this order):**
   1. Watch 5xx on `/twilio/status-callback` drain to zero as Twilio's retries land (dedup keeps one append per event — duplicates are normal and correct).
   2. Replay failed/pending summaries: `POST /api/admin/history/{call_id}/summarize` per call (`broker-outage.md` §3.4) — or sweep stale `pending` rows with the U-406 sweep note (`broker-outage.md` §3.3) if replay is not wanted.
   3. Orphan records created during the outage (RECEIVED-state, `provider_ids.twilio_call_sid`) are expected artifacts of the orphan-persist policy — leave them; they are the durable trail, not corruption.
   4. Check `ownership` rows settled: live leases only for actually-live calls (`drain_status.py census`).

## 4. Verification that the system recovered

1. `alert-cosmos-429-<env>` resolved; normalized RU consumption back under the autoscale ceiling.
2. `/twilio/status-callback` returns 200s again (spot-check with a signed probe or wait for organic Twilio traffic); no sustained 5xx in `alert-app-failed-<env>`.
3. Worker logs show successful completes; rows move `pending → ok` (replays included). No `pending` older than 15 min remains (sweep rule).
4. Lease census healthy: renewals succeeding (no `StaleVersionError` storm), no phantom owner-losses.
5. Twilio-side: no unexplained missing status events for calls placed during the window (dedup + retries should have converged); any gap limited to calls that hit owner-loss.

## 5. Escalation notes

- 429s persist after raising autoscale → data-model hot-partition suspect (sentinel `"_none_"` for anonymous callers is the known hot-PK risk, pipeline-infra-spec §1.4 note) — escalate to U-301/U-502 owners with per-PK metrics; do not shard ad hoc.
- Events genuinely lost (Twilio retry window expired during a long outage) → record the gap honestly in the incident; there is no replay from Twilio's side after its retry policy gives up. Do not fabricate rows.
- Storage failure combined with owner-loss across many calls → follow `owner-loss.md` for caller communication; **never** promise callers their in-flight call survived.
- If `drain_status.py census` itself cannot authenticate: operator principal needs Cosmos DB Built-in Data Contributor (u-401-deployment-plan.md RBAC row 4) — this is access, not an outage.
