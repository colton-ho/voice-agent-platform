# Runbook — Broker outage (Service Bus down / degraded)

Author: team-lead · Date: 2026-11-20 · Status: **runbook**
Scope: `docs/azure/implementation-plan.md:427` U-406 runbook #4 · closes **R-O-001** runbook side; failure-row evidence for **R-T-009**.
Ground truth: `workers/postcall_worker.py`, `storage/servicebus.py`, `bot.py:1203-1216` (`_servicebus_sink`) / `bot.py:1357-1370` (`_dispatch_summary`), `docs/azure/pipeline-infra-spec.md` §2.1/§2.2, `docs/azure/product-impact.md` §7.1 (I-4).
Affected surface: **post-call summary jobs only** (`postcall-jobs` queue). Live calls/media do not traverse Service Bus.

---

## 1. Symptom / trigger

- `alert-dlq-<env>` firing (DeadLetterMessageCount on `postcall-jobs`, `infra/bicep/modules/monitor.bicep:76`);
- worker logs showing `transient failure ... abandoning`, `read failed`, `write failed`, or enqueue failures (`summary job enqueue failed for <call_id>` from `bot.py:1368`);
- Azure portal / `az servicebus queue show` shows growing active or dead-letter counts;
- or the namespace `sb-voicebot-<env>` is unreachable (region/service incident, auth expiry, mis-config).

**What happens while the broker is down (semantics, not speculation):**

- **Producer side (`bot.py:1357-1370`)**: end-of-call summary dispatch is **at-most-once fire-and-forget** (§2.2; no outbox). If `send` fails, it is logged and **does not block teardown**; the call's history row stays `summary_status=pending` ("行留 pending 由 U-406 清掃" — the sweep is §5 below). The caller is unaffected.
- **Consumer side (`workers/postcall_worker.py`)**: peek-lock consumption (`storage/servicebus.py` `_RealQueue`, lockDuration PT1M):
  - transient failure (LLM / network / 429 / 5xx / 30s summary timeout / read-write failure) → **abandon** → redelivery, up to `maxDeliveryCount=5`;
  - permanent failure (`ValueError`/`TypeError`/`KeyError`/`AttributeError`, explicit `PermanentJobError`) → write `summary_status='failed'` + `summary_error` → **complete** (terminal, no retry);
  - **I-4 hard invariant** (`product-impact.md:236`): on the **last delivery** a still-transient failure settles to the permanent branch (`failed` + complete) — no row is ever left `pending` forever by the worker;
  - crash residue (process dies holding a lock, never completes/abandons) → lock expires → redelivery → that is what ends in the **DLQ** after 5 deliveries. DLQ holds crash residue/poison only (`postcall_worker.py:12`).
- **Degradation leg (§2.2, `bot.py:1203-1216`)**: when `SERVICEBUS_NAMESPACE` is **unset**, dispatch falls back to an **in-process** `asyncio.create_task(self._summarize_and_update(...))` with the same accept contract (same invoke shape, same 30s budget) — dev/test parity. This is a *startup* configuration state, not an automatic failover: a **runtime** broker outage does not switch legs.

## 2. Immediate operator checks (concrete commands)

```bash
# 1. Queue health + the six explicit config values (pipeline-infra-spec §2.1, V6)
az servicebus queue show --name postcall-jobs --namespace-name sb-voicebot-<env> \
  --resource-group rg-voicebot-<env> \
  --query "{ttl: defaultMessageTimeToLive, deadLetterOnExpiry: enableDeadLetteringOnMessageExpiration, lock: lockDuration, maxDelivery: maxDeliveryCount, dupDetection: enableDuplicateDetection, counts: countDetails}" -o json
#    counts.activeMessageCount       = queued, waiting
#    counts.deadLetterMessageCount   = DLQ depth (the poison/crash residue)

# 2. Is it the broker or us? Check the worker app + alert state
az containerapp show --name ca-voicebot-<env>-worker --resource-group rg-voicebot-<env> --query "properties.provisioningState"
#    then App Insights / Log Analytics: worker exceptions, "summary job enqueue failed" from the runtime

# 3. How many summaries are stuck (the real user-facing impact)
#    Rows with summary_status='pending' older than the A-3 limit (15 min) are the backlog to sweep (§5).
#    Inspect via Cosmos Data Explorer (cosmos-voicebot-<env>, voicebot db, calls family) or the repo reader:
#      PYTHONPATH=. python3 -c "from storage.cosmos_calls import CosmosCallRepository; ..."
```

## 3. Mitigation steps

1. **Broker reachable but degraded (throttling / transient 5xx):** do nothing aggressive — the worker's abandon/redeliver cycle (≤5 deliveries, lock-duration backoff) is the designed retry. Let `maxDeliveryCount` and I-4 do their job; watch DLQ depth.
2. **Broker down (namespace unreachable):**
   - Summaries accumulate as `pending` rows; live calls are unaffected. No action on the runtime.
   - Restore Service Bus (Azure-side incident response) or wait it out. Dispatch is at-most-once: calls that ended **while sends were failing** did not get jobs enqueued — their rows stay `pending` (see §5 sweep + §4 replay).
   - Optional short-term mitigation if the outage is long and summaries are urgent: run the bot with `SERVICEBUS_NAMESPACE` unset to use the in-process leg (restarts required; same accept contract). Weigh a restart against the outage; usually waiting is right.
3. **DLQ sweep (the U-406 DLQ sweep note — I-4 / pipeline-infra-spec §2.2):**
   - The DLQ (`postcall-jobs/$DeadLetterQueue`, built-in) only holds crash residue/poison after I-4 did its job.
   - Sweep rule (product-impact.md:236): **any residual `summary_status='pending'` row older than the A-3 limit (15 minutes) is flipped to `failed`** (error-as-data: set `summary_error`, e.g. "swept: pending > 15 min after broker outage"). Use the repo's own patch path so TTL recompute semantics hold:
     ```bash
     PYTHONPATH=. python3 - <<'PY'
     from storage.cosmos_calls import CosmosCallRepository
     repo = CosmosCallRepository(ttl_days=30)
     # call_ids from the backlog query above; flip each stale pending row to failed:
     for call_id in [<stale-call-ids>]:
         repo.patch_fields(call_id, {"summary_status": "failed",
                                     "summary_error": "swept by U-406 DLQ sweep: pending > 15 min"})
     PY
     ```
   - Inspect DLQ messages before discarding them (each carries the `call_id` of a crashed job): Azure portal Service Bus Explorer / `az servicebus queue show` counts. There is **no in-repo DLQ reader** (the narrow queue interface `storage/servicebus.py` covers `postcall-jobs` only) — portal/Service Bus Explorer is the manual drain entry point (pipeline-infra-spec §2.1 DLQ row).
4. **Replay (re-summarize) for calls that should have summaries:** use the admin endpoint — it re-enqueues to the same queue with the same idempotency (C-9, `bot.py:3941-3953`):
   ```bash
   curl -X POST "https://<host>/api/admin/history/<call_id>/summarize" -H "Cookie: <admin-session>"
   # 200 {"call_id": ..., "summary_status": "pending"} = accepted (row flips pending, job queued)
   # 500 {"error"} = enqueue/persist failed (writes failed — never leaves the row wedged)
   ```
   The worker is idempotent (`summary_status=='ok'` → complete, zero side effects), so replaying an already-summarized call is safe. The admin SPA's 重新生成 button hits the same endpoint.

## 4. Verification that the system recovered

1. `az servicebus queue show ... --query "countDetails"` — `activeMessageCount` drains toward 0 as the worker consumes; `deadLetterMessageCount` stops growing.
2. Rows reach terminal states: `summary_status` moves `pending → ok | failed` per I-1..I-4 (`product-impact.md` §7.1); no row stays `pending` past 15 min after the sweep.
3. Worker logs: `transient failure ... abandoning` stops; `retry exhausted ... settling failed` should appear at most once per poison message, then silence.
4. `alert-dlq-<env>` resolves and stays quiet through one full maxDeliveryCount window (5 deliveries × lock backoff).
5. Spot-check one replayed call: `/api/admin/history/<call_id>` shows `summary_status: "ok"` with a rendered `summary` (or `failed` with `summary_error` — error-as-data is a valid terminal state).

## 5. Escalation notes

- DLQ keeps growing after the broker is healthy → a poison message class (malformed envelope is normally dropped + completed by the worker — `postcall_worker.py:73-78`; sustained growth implies repeated crashes mid-processing). Capture worker logs + DLQ messages and escalate to the worker owner (U-304). Do not silently dead-letter-clear without the §3.3 sweep record.
- Rows stuck `pending` while the queue is healthy → the dispatch side lost the job (at-most-once) or the worker is down; replay via §3.4, then sweep the rest.
- Never "fix" this by writing `summary_status='ok'` without a real summary — the wire enum and I-2 (`ok ⇒ summary present`) are frozen contract (product-impact §7.1).
- Secret note (adjacent, from `infra/bicep/modules/keyvault.bicep:42`): broker auth failures caused by expired credentials are a secret-rotation matter (manual Key Vault rotation process) — rotation drill itself is tracked outside the 8-card runbook set (see U-406 report).
