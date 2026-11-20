# Runbook — Overload (admission rejection at MAX_CONCURRENT_CALLS_PER_REPLICA)

Author: team-lead · Date: 2026-11-20 · Status: **runbook**
Scope: `docs/azure/implementation-plan.md:427` U-406 runbook #7 · closes **R-O-001** runbook side.
Ground truth: `storage/ownership.py:202-234` (`reserve_capacity`), `storage/base.py:40-41` (`CapacityExceededError`), `bot.py:6938-6954` (`_twilio_reserve_stream`) / `bot.py:7014-7019` (fallback TwiML at refusal), `config_contract.py:37` (`MAX_CONCURRENT_CALLS_PER_REPLICA`), `tests/test_ownership.py` (admission cases).

---

## 1. Symptom / trigger

A replica reaches its admission cap — `MAX_CONCURRENT_CALLS_PER_REPLICA` concurrent calls (provisional value 3, `docs/azure/u-401-deployment-runbook.md` §1.2; the contract value is whatever `config_contract.py` validated at startup — it is a required integer, **no compiled-in default**, D-4).

- `reserve_capacity` raises **`CapacityExceededError`** ("replica <owner> at capacity (<cap>)", `storage/ownership.py:212-215`).
- When FC-7 admission is wired behind the webhook seam (`bot.py:6938-6954`), a refused reservation makes the voice webhook serve the **§6.1.9 fallback TwiML (`<Say>` + `<Hangup>`)** instead of `<Connect><Stream>` — the caller hears "Sorry, we are unable to connect your call right now. Please try again later." and the call ends. (Today the seam's default `StreamSessionDirectory.reserve()` is permissive; refusal is enforced at WS accept. The FC-7 `CapacityExceededError` path is the designed admission implementation behind `_twilio_reserve_stream` — `bot.py:6943-6947`.)
- Symptom signals: elevated fallback-TwiML responses in logs (`twilio incoming-call: stream cannot start, serving fallback TwiML`), admission warnings, and (if fleet capacity is exhausted) callers retrying into repeated fallback messages.

**Critical retry semantics (do not "fix" these away):**

- **Retried reservations for the same call are idempotent and MUST NOT be rejected at cap.** `reserve_capacity` checks same-call idempotency **before** the capacity check (`storage/ownership.py:202-209`): a webhook retry/replay for a call that already holds a reservation returns the existing reservation — it consumes no new capacity and is not mis-rejected even when the replica is at cap (`tests/test_ownership.py::test_reserve_same_call_is_idempotent`, run with `max_calls=1`). Twilio webhook retries of an in-flight reservation must keep succeeding.
- Expired rows do not occupy capacity (`test_admission_ignores_expired_rows`) and other replicas' rows do not occupy mine (`test_admission_ignores_other_replica_rows`) — the cap is genuinely **per replica**.

## 2. Immediate operator checks (concrete commands)

```bash
# 1. Per-replica load: live leases per owner (= calls + reservations in handshake)
az containerapp replica list --name ca-voicebot-<env>-runtime --resource-group rg-voicebot-<env> \
  --revision <rev> --query "[].name" -o tsv
PYTHONPATH=. python3 scripts/deploy/drain_status.py census --owners <replica1,...> --format text
#    per-owner counts near the cap = the hot replicas. (census counts leases; the cap
#    counts the same rows per storage/ownership.py:210-211.)

# 2. Fleet shape: is the app at max replicas already?
az containerapp show --name ca-voicebot-<env>-runtime --resource-group rg-voicebot-<env> \
  --query "properties.template.scale" -o json
#    template default: runtimeMinReplicas=1 / runtimeMaxReplicas=3 (containerApps.bicep:100-104)

# 3. Confirm it is overload, not a stall: healthy replicas should show renewing leases
#    (census values churn every ~30s TTL); a frozen census with high counts suggests a
#    different failure (storage — see storage-failure.md).
```

## 3. Mitigation steps

1. **Scale out first (fastest, no code):** raise the replica ceiling so KEDA's HTTP-concurrency rule can add replicas:
   ```bash
   az containerapp update --name ca-voicebot-<env>-runtime --resource-group rg-voicebot-<env> \
     --max-replicas <n> --min-replicas <m>
   ```
   Fleet capacity ≈ replicas × `MAX_CONCURRENT_CALLS_PER_REPLICA`. `--min-replicas` matters: cold replicas take time to start (provisioning latency = callers getting fallback TwiML meanwhile).
2. **Raise the per-replica cap only with evidence:** `MAX_CONCURRENT_CALLS_PER_REPLICA` is defined as the **load-tested** concurrency cap (`config_contract.py:37`). Raising it without a load test trades fallback rejections for possible OOM/latency collapse (which becomes `owner-loss.md`). If raising: config change + restart via the normal deploy path, then load test (U-501/D-6).
3. **Shed honestly:** the fallback TwiML (Say+Hangup) **is** the designed shedding behavior — callers get a clear message and can retry. There is no queue/hold music and **no fallback transfer target** (`twilio-outage-fallback.md` §5 gap). Do not improvise routing.
4. **During sustained overload:** keep the release/drain posture sane — do not blue/green-switch mid-overload (a switch pins old calls on the draining revision and stacks pressure on green; see `release-drain.md` P1 rule about deploy-azure.sh as well).

## 4. Verification that the system recovered

1. Census per-owner counts drop below the cap; new calls bind without `CapacityExceededError`.
2. Fallback-TwiML responses disappear from the webhook logs (the `stream cannot start` warning stops).
3. Callers can connect organically; if you can place test calls, one lands on each new replica (census owner set grows).
4. No side effects from the spike: no orphaned reservations (unbound rows self-expire within their reservation TTL), no owner-loss cascade.

## 5. Escalation notes

- At `runtimeMaxReplicas` with all replicas at cap → true fleet exhaustion. Options in order: raise `--max-replicas` (capacity headroom decision — record authorization per R-O-004), raise the per-replica cap with load-test sign-off (U-501/D-6), or accept shedding (the Say+Hangup message is the approved caller experience).
- Same-call webhook retries getting rejected at cap = **bug** (idempotency is checked before admission — `storage/ownership.py:204-209`): capture CallSid + census + logs and escalate to the U-305 owner. Never respond by disabling admission.
- Callers reporting "call drops when it connects" instead of the fallback message → that is not overload shedding; that is `owner-loss.md` (crash under memory pressure) — check replica restart counts.
- Honest capacity notes: the per-replica cap is *admission only* — it does not reserve CPU/memory per call; real capacity is load-test-defined (U-501 acceptance, D-6 values). Until a real load test exists (none has been run in a real environment), treat every capacity number here as provisional (`u-401-deployment-runbook.md` §1.2).
