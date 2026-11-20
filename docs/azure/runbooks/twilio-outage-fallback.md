# Runbook — Twilio outage / degradation (fallback TwiML + status-callback behavior)

Author: team-lead · Date: 2026-11-20 · Status: **runbook**
Scope: `docs/azure/implementation-plan.md:427` U-406 runbook #8 · closes **R-O-001** runbook side.
Ground truth: `bot.py:6900-6929` (§6.1.9 fallback TwiML, `_twilio_fallback_twiml`), `bot.py:6975-7031` (voice webhook), `bot.py:7034-7175+` (§6.1.8 status callbacks), `providers/telephony/twilio_adapter.py` (`_FALLBACK_SAY`, FC-8 seam), `tests/test_status_callbacks.py` (all behaviors below are test-pinned).

---

## 1. Symptom / trigger

Twilio-side degradation or outage, or our Twilio integration misbehaving. Observable classes:

| Class | Caller-facing behavior | Where it shows |
|---|---|---|
| A. Stream cannot start (our side or setup failure) | **§6.1.9 fallback TwiML**: `<Response><Say>…</Say><Hangup/></Response>` — caller hears the approved message and the call ends cleanly | webhook logs: `twilio incoming-call: stream cannot start, serving fallback TwiML call_sid=…` |
| B. Twilio not configured on our side | **503** `Twilio integration not configured` on `/twilio/incoming-call` and `/twilio/status-callback` (pinned contract) | logs + response body |
| C. Signature problems | **403** `invalid signature` (missing/invalid/wrong-host `X-Twilio-Signature`) — nothing is persisted | logs: `twilio incoming-call rejected: bad signature` |
| D. Twilio outage / our endpoint unreachable | Calls fail at Twilio's side; **status callbacks retry/redeliver** when we return 5xx or are unreachable | `alert-app-failed-<env>`, Twilio console webhook failure counts |
| E. Post-connect stream death | Adapter's trailing `<Say>` (same copy) then hangup — same message as A for consistency (`providers/telephony/twilio_adapter.py:39`) | call experience |

Approved fallback copy (`bot.py:6910`, identical to `twilio_adapter._FALLBACK_SAY`):
> "Sorry, we are unable to connect your call right now. Please try again later."

Fallback triggers (`bot.py:6957-6972` `_twilio_stream_ready`): (1) missing stream config — no Twilio-visible public host, so no `wss://` Stream URL can be built; (2) stream reservation failure (`_twilio_reserve_stream` refusal/exception — includes FC-7 admission rejection, see `overload.md`).

## 2. Immediate operator checks (concrete commands)

```bash
# 1. Which class? Check our endpoint first (it distinguishes B/C/D from A)
curl -s -o /dev/null -w '%{http_code}\n' -X POST "https://<host>/twilio/incoming-call"
#    503 -> class B (Twilio not configured); 403 -> class C (signature); 200 with fallback XML -> class A

# 2. Config gate: TWILIO_AUTH_TOKEN / TWILIO_ACCOUNT_SID / TWILIO_PUBLIC_BASE_URL present?
#    (In Azure: Key Vault secret TWILIO-AUTH-TOKEN + env injection, u-401-deployment-plan.md §4.
#     Signature validation runs through the FC-8 seam providers/telephony/twilio_adapter —
#     decision A-1: official twilio SDK RequestValidator, stdlib twilio_sig is the test oracle.)

# 3. Class A trigger? Grep runtime logs for the two triggers:
#    "twilio stream setup: missing stream config"   -> PUBLIC_MEDIA_BASE_URL/public host problem
#    "twilio stream reservation failed for ..."     -> reservation/admission refusal (overload.md)
#    "stream cannot start, serving fallback TwiML"  -> confirm fallback serving

# 4. Twilio-side health: Twilio console/status page + webhook failure counts for
#    /twilio/incoming-call and /twilio/status-callback (class D).

# 5. Status-callback backlog: 5xx rate on /twilio/status-callback (alert-app-failed-<env>)
#    — high rate during class D is EXPECTED (persist-before-ack, §3) and self-heals on retry.
```

## 3. Mitigation steps + status-callback retry/redelivery behavior

**Class A (fallback serving):** the call is already handled per §6.1.9 — caller heard the message and the line closed. Fix the trigger:
- missing stream config → restore `PUBLIC_MEDIA_BASE_URL`/host config (u-401-deployment-runbook.md §2 step 6 two-pass backfill; custom domains: CNAME → `RUNTIME_FQDN`, runbook §2 step 8);
- reservation failure → `overload.md` (capacity) or reservation seam errors in logs.

**Class B:** restore Twilio configuration (Key Vault secret `TWILIO-AUTH-TOKEN` + `TWILIO_ACCOUNT_SID`/`TWILIO_PUBLIC_BASE_URL` env). Note the pinned contract: the 503 gate is intentional — the bot refuses rather than serving broken TwiML.

**Class C:** signatures validate against the **Twilio-visible external URL** (`TWILIO_PUBLIC_BASE_URL`), never the internal/CloudFront host (pinned pitfall, `tests/test_status_callbacks.py::test_status_callback_403_when_signature_for_wrong_host`). If 403s spike after a domain/routing change, check that `PUBLIC_CONTROL_BASE_URL` matches what Twilio actually signs. Query strings are part of the signed URL.

**Class D (Twilio outage / unreachable endpoints):**
- Calls fail at Twilio; nothing on our side recovers them. Keep endpoints up so **status callbacks redeliver**: Twilio retries while we return 5xx or time out; on success the deterministic `event_id` (`CallSid:CallStatus:SequenceNumber|Timestamp`) **dedups redeliveries to a single append** (`tests/test_status_callbacks.py::test_duplicate_delivery_dedups_to_single_append`). Persistence failures return **5xx — we never ack an unpersisted event** (persist-before-ack).
- Deliberate 200-drops (retry can never help): events for **finalized** calls (dropped, never revived) and payloads **without CallSid**. Unknown CallSid events are **persisted as orphans** (minimal RECEIVED-state record) — expected artifacts, not errors.
- When Twilio recovers, the backlog converges automatically; spot-check with `az cosmosdb`/Data Explorer that `call_events` appends for the window are singletons per `event_id`.

**Any class:** if the fallback message itself must change, it is **approved copy pinned by test** (`_TWILIO_FALLBACK_SAY`) — a copy change is a contract change (route through product/solution-architect), not an ops tweak.

## 4. Verification that the system recovered

1. A test call completes the happy path: 200 TwiML is `<Connect><Stream url="wss://<host>/twilio/media-stream">…` (stream-only — `test_happy_path_twiml_stays_stream_only`); the media WS connects.
2. `stream cannot start` warnings gone from logs; no fallback TwiML being served.
3. `/twilio/status-callback` 2xx rate restored; Twilio console webhook failure counts flat.
4. Event trail consistent: no duplicate `event_id` appends; orphan records only for genuinely stream-less calls (busy/no-answer/failed).

## 5. Escalation notes

- **Known gap — do not promise a transfer fallback:** the §6.1.9 fallback is **Say+Hangup only**. A `<Dial>` transfer target is **deliberately not wired** because `config_contract.py` (spec §11 — frozen) has **no fallback/transfer number config key** (`bot.py:6915-6922` docstring; `tests/test_status_callbacks.py::test_fallback_twiml_shape` asserts `Dial` is absent). An operator can only serve Say+Hangup today; adding a transfer target needs a config-contract addition (route through solution-architect + user, spec §16 — U-205 must not add config keys unilaterally).
- Persistent class C after host changes → the two-pass URL backfill or custom-domain binding is wrong (`u-401-deployment-runbook.md` §2 step 8); escalate with the exact `full_url` Twilio signed vs `TWILIO_PUBLIC_BASE_URL`.
- Twilio long outage → per R-O-004, telephone routing/number changes need recorded authorization; the fallback keeps callers informed meanwhile. If the incident outlasts Twilio's status-callback retry window, record the event-trail gap honestly (no replay exists from Twilio).
- Signature validation failures caused by an expired `TWILIO_AUTH_TOKEN` → Key Vault secret rotation (manual process per `infra/bicep/modules/keyvault.bicep:42`; rotation drill is tracked outside this 8-runbook set).
