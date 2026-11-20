# Runbook — Forced drain (approved deadline reached, calls remain)

Author: team-lead · Date: 2026-11-20 · Status: **runbook (forced-BYE transport seam unwired — see §3)**
Scope: `docs/azure/implementation-plan.md:427` U-406 runbook #2 · closes **R-O-001** runbook side.
Ground truth: `docs/azure/blue-green-release.md` §6 (forced-drain table), §2.3 decision D3/D4; spec `:245-246` ("shutdown grace does not guarantee every call completes"; a release is **not** automatically non-disruptive).
Tools: `scripts/deploy/drain-runtime.sh` (`--force --confirm-forced-drain=<rev>` gating), `scripts/deploy/drain_status.py force-end`.

---

## 1. Symptom / trigger

`release-drain.md`'s wait loop exited **2**: the approved drain deadline (default 60 min, `--deadline`) passed and the census still shows live leases attributed to the draining (blue) revision. A forced-drain decision is now **authorized in principle** and needs an explicit choice among the four options below.

**Default posture (blue-green-release.md §6): B — announced forced-BYE, with C as fallback. D (hard teardown) is NEVER automatic and never something a tool does quietly.**

| Option | Mechanism | Caller experience | Use when |
|---|---|---|---|
| **A. Let them finish** | rerun with a longer `--deadline`, keep polling | unaffected — calls complete normally | default **before** the deadline; still preferred after it if business allows |
| **B. Announced forced-BYE (DEFAULT at deadline)** | one FC-7 `hangup` command per live call → owner ends the call through its normal end-of-call path → Twilio BYE | **early but normal ending** — the caller hears a normal close/hangup. **No silent drop.** | deadline approved, forcing authorized |
| **C. Carrier-side completion** | complete the call via Twilio console/REST per CallSid | normal carrier hangup (possibly mid-conversation, but a clean disconnect) | when B is unavailable (e.g. transport unwired) |
| **D. Hard teardown** | `az containerapp revision deactivate` → replicas die | **mid-audio drop = silent drop** | **never automatic**; manual last resort, explicit sign-off only |

## 2. Immediate operator checks (concrete commands)

```bash
# 1. Who is still live on the draining revision (read-only)
az containerapp replica list --name ca-voicebot-<env>-runtime --resource-group rg-voicebot-<env> \
  --revision <blue> --query "[].name" -o tsv
PYTHONPATH=. python3 scripts/deploy/drain_status.py census --owners <replica1,replica2,...> --format text
#    Each line = one call that option B would end early. Record call_ids (for Twilio console cross-check + audit).

# 2. What-if of the forced run (mutates nothing; proves gating + prints the plan)
scripts/deploy/drain-runtime.sh <env> <blue>
#    prints: "What --force would add at the deadline ..." and the required confirmation flag

# 3. Transport sanity — is the owner-addressable command path wired? (see §3)
#    If DRAIN_SEND_FACTORY is unset, force-end will honestly report
#    "no owner-addressable transport wired" and exit 3 (kills nothing).
```

## 3. Mitigation steps

### Option B — announced forced-BYE (default)

```bash
scripts/deploy/drain-runtime.sh <env> <blue> --apply --force --confirm-forced-drain=<blue>
```

Gating (verified in `scripts/deploy/drain-runtime.sh:121-133`):
- `--force` **requires** `--apply` (what-if never forces) — else exit 1;
- `--force` **requires** `--confirm-forced-drain=<revision>` equal to the `<revision>` positional argument — else exit 1. This two-key gate is the explicit confirmation that you are ending live calls early.

What the run does: at the deadline it calls `drain_status.py force-end --owners <replicas> --reason "forced-drain U-405 deadline"` — one `Command(name="hangup", payload={"reason", "call_id"})` per live call via `route_command` (vocabulary per `tests/test_ownership.py`; command IDs are idempotent) — then settles 2 polls (2 × `--poll-seconds`, default 30 s each) and recounts.

**Honesty about B's wiring (blue-green-release.md §6):** delivery needs the FC-7 owner-addressable transport (`send`, `storage/ownership.py:136-137`). While it is unwired (the `DRAIN_SEND_FACTORY` seam in `scripts/deploy/drain_status.py` is unset), `route_command` refuses truthfully (`accepted=False`, "no owner-addressable transport wired", `storage/ownership.py:288-291`), `force-end` reports that per call and **exits 3, killing nothing**. In that state use option C.

Standalone helper form (same thing the script runs):
```bash
PYTHONPATH=. python3 scripts/deploy/drain_status.py force-end --owners <replica1,replica2,...> --reason "forced-drain U-405 deadline"
#    exit 0 = all hangup commands accepted; exit 3 = at least one NOT accepted (transport
#             unwired/unreachable, or no live owner for that call) — per drain_status.py exit codes
```
Unbound reservations (`reserved:<replica>` holders in the census) report `NOT accepted (owner lost: no live lease)` — there is no owner to deliver to; they expire on their own within the 30 s lease TTL.

### Option C — carrier-side completion (fallback)

Complete each remaining call via Twilio console / REST per CallSid (the census `call_id` **is** the Twilio CallSid). Caller hears a normal carrier hangup. Use when `force-end` exits 3.

### Option D — hard teardown (last resort, MANUAL)

```bash
az containerapp revision deactivate --name ca-voicebot-<env>-runtime --resource-group rg-voicebot-<env> --revision <blue>
```
**This drops the listed calls mid-audio (silent drop).** It requires explicit sign-off and must only be run by a human who has just re-read the census list. `drain-runtime.sh` will never do this for you while leases are live — even under `--force`, residual leases exit 3 with this command printed as a manual option only (blue-green-release.md §6; `drain-runtime.sh:310-319`).

## 4. Verification that the system recovered

1. After B: the tool prints `==> drain-complete after forced-drain` and exits 0 (optionally `--retire-when-drained` already deactivated blue). Re-run:
   ```bash
   PYTHONPATH=. python3 scripts/deploy/drain_status.py census --owners <replicas> --format count   # -> 0
   ```
2. Caller experience check (evidence for R-A-016): the affected callers heard an early but normal ending — verify via Twilio console call logs (normal `completed` termination, not `failed`) for the recorded CallSids.
3. The calls' end-of-call path ran on the owner: history rows reach a terminal summary state (`ok`/`failed`) via the post-call worker — if rows stay `pending` beyond the 15-min A-3 limit, apply the sweep note in `broker-outage.md` §5.
4. If the run exited 3: **nothing was killed and nothing dropped** — that is by design. Re-check the census; residual leases either belong to calls still finishing (retry A) or to lost owners (self-reap ≤ 30 s).

## 5. Escalation notes

- `--force` refuses with an exit-1 gate message → you omitted `--apply` or `--confirm-forced-drain=<blue>` does not match the revision argument exactly. Never work around the gate; re-read the census before retrying.
- `force-end` exits 3 with "no owner-addressable transport wired" → the U-305 transport seam is not wired in this deployment. Wire `DRAIN_SEND_FACTORY` (module:function returning the async `(owner_id, Command) -> bool` callable) and rerun, or proceed to option C. Do not escalate straight to D.
- Deadline extension is a business decision (calls may run up to `MAX_CALL_DURATION_SECONDS`, provisional 3600 s) — record the extension with the same audit trail as the original deadline approval (R-O-004: production actions need recorded authorization).
- Option D requires explicit human sign-off per the forced-drain table. If nobody is authorized to sign off, the honest outcome is to hold (option A) — a silent drop is never the default.
