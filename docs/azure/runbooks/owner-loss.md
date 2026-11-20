# Runbook — Owner loss (replica dies mid-call)

Author: team-lead · Date: 2026-11-20 · Status: **runbook**
Scope: `docs/azure/implementation-plan.md:427` U-406 runbook #6 · closes **R-O-001** runbook side; failure-row evidence for **R-T-009**.
Ground truth: `storage/ownership.py:18-26` (module docstring — the **documented caller experience**, spec §8.2), `:283-355` (`route_command`), `:390-405` (`drain`); `tests/test_ownership.py` (owner-loss cases); `docs/azure/blue-green-release.md` §3 (drain accounting self-heals).

---

## 1. Symptom / trigger

A runtime replica of `ca-voicebot-<env>-runtime` dies, restarts, or is replaced while it hosts live calls: node loss, OOM/crash, SIGKILL, scale-in race, platform-initiated replacement. **What the system does (documented semantics — `storage/ownership.py:18-26`):**

- The established calls/media WebSockets **die with the process**. Shared state does not migrate live connections. **There is NO "another replica seamlessly takes over this live call" path.**
- Caller experience: **the call drops** — the caller goes through the carrier/front-end's ordinary disconnect handling; they can redial, or the control plane can re-initiate.
- The dead replica stops renewing; its leases **self-reap within `lease_ttl_seconds` (30 s)**. After that, `route_command` for those calls returns `accepted=False, detail="owner lost: no live lease"` — it refuses honestly and never blind-delivers to some other replica.
- Another replica may `bind_owner` the call_id **after** the lease expires — that restores **only future command routing** (a new lease). It does **not** and cannot restore the dropped media plane.

This is the failure mode `blue-green-release.md` §3 also relies on: drain accounting self-heals because dead replicas' leases expire.

## 2. Immediate operator checks (concrete commands)

```bash
# 1. Which replicas exist now vs before (a dead owner simply vanishes from this list)
az containerapp replica list --name ca-voicebot-<env>-runtime --resource-group rg-voicebot-<env> \
  --revision <rev> --query "[].{name:name, state:properties.runningState, started:properties.startTime}" -o table

# 2. Lease census — what the dead replica still "holds" (expires within 30s of its death)
PYTHONPATH=. python3 scripts/deploy/drain_status.py census --owners <current-replicas> --format text
#    Note: after ~30s the dead replica's leases are gone from the census entirely.

# 3. Telemetry — correlate the gap. With U-403 correlation fields (call ID, owner,
#    replica, trace) in App Insights: find calls whose traces STOP at the death timestamp
#    with no end-of-call/teardown events and no summary dispatch.

# 4. Call-side: Twilio console for the affected CallSids (from step 3)
#    — the legs terminated when the WS died (carrier-side disconnect).
```

**What the operator sees (honest telemetry-gap description):**
- A **hard gap**: media/WS traces and any in-call events for the affected calls simply stop at the death timestamp. There is no clean end-of-call sequence (no outcome row write, no summary job, no graceful WS close).
- The lease census drops those calls automatically within ≤30 s — **do not read "census 0" as "all calls ended normally"**; cross-check against call traces.
- Twilio status callbacks (`completed` etc.) may still arrive for the legs (persisted like any event; possibly as **orphan persists** if no call row was created yet — `storage-failure.md` §1).
- History rows for the dead calls may remain `summary_status=pending` forever (their teardown never ran) → they fall under the U-406 sweep note (`broker-outage.md` §3.3): stale `pending` > 15 min flips to `failed`.

## 3. Mitigation steps

1. **Nothing can save the in-flight calls** — do not attempt failover gymnastics for them. The honest product statement is documented (call drops; redial). Any "transparent recovery" claim would be false (`storage/ownership.py:18-26` explicitly forbids it).
2. **Stabilize the fleet**: ensure Container Apps restores the replica count (scale floor `runtimeMinReplicas=1`, KEDA HTTP-concurrency rules; live-WS replicas are not normally scaled in). If the replica died from OOM/crash-loop, capture `az containerapp logs` / App Insights for the crash before it recycles.
3. **Clean up the bookkeeping**:
   - Wait the 30 s TTL — leases self-reap; **do not hand-delete lease rows** (a manual delete races with concurrent bind and can eat a new owner's live lease — `storage/ownership.py:374-376` documents why release is done via expiry, not delete).
   - Sweep stale `pending` summary rows for calls that died with the replica (§2, `broker-outage.md` §3.3 sweep rule).
4. **Optional, per business need**: re-initiate outbound calls to the affected callers from the control plane (the documented recovery route is "redial or control-plane re-initiation", not resume).

## 4. Verification that the system recovered

1. Replica list is back at the expected count; no crash-loop in logs.
2. New calls bind to healthy replicas: `drain_status.py census` shows fresh leases with `owner_id` = live replica names; `route_command` targets work again (a `hangup` command to a healthy call returns `accepted=True` — e.g. via `drain_status.py force-end` in a controlled test).
3. No lease rows survive >30 s past their owner's death (self-reap confirmed).
4. Swept rows show `failed` + `summary_error`; incident record lists affected CallSids and the telemetry gap window.

## 5. Escalation notes

- **What you must NOT promise callers**: that the call can be resumed, continued, or transparently recovered on another replica — the platform does not do this (spec §8.2, `storage/ownership.py:18-26`). Approved wording: the call disconnected, please redial (or we can call you back).
- Repeated replica deaths → platform capacity/stability incident (node health, memory limits vs `MAX_CONCURRENT_CALLS_PER_REPLICA` × per-call footprint): escalate to the platform owner with the crash logs; consider lowering the admission cap temporarily (`overload.md`).
- If a dead owner's leases do NOT self-reap within ~30 s, that is a Cosmos/TTL problem (§1.3 TTL-on-write semantics) — escalate to the U-301/305 owners; the drain tool's zero-census cannot be trusted meanwhile.
- Command delivery to a live call returns "owner lost" though its lease looks live → storage inconsistency or clock skew; re-run the census, then escalate with both snapshots.
