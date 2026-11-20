# Runbook — Release drain (blue → green, normal path)

Author: team-lead · Date: 2026-11-20 · Status: **runbook (drain rehearsal simulated — see `rehearsals/drain-rehearsal.md`)**
Scope: `docs/azure/implementation-plan.md:427` U-406 runbook #1 · closes **R-O-001** runbook side (`docs/azure/requirements.md:89`); drain evidence serves **R-A-016**/**R-T-010**.
Ground truth: `docs/azure/blue-green-release.md` §2–§5, §8 (scheme, drain-complete criterion, tooling). Topology: `docs/azure/u-401-deployment-plan.md` §1–§2.
Tools: `scripts/deploy/drain-runtime.sh`, `scripts/deploy/drain_status.py` (all flags below verified against the scripts; az flags verified against Azure CLI help).

---

## 1. Symptom / trigger

This is a **planned operation**, not an incident. You are in the middle of a blue/green release of `ca-voicebot-<env>-runtime`:

- green (new revision) is deployed and passed readiness;
- new-call routing has been switched to green (`az containerapp ingress traffic set --revision-weight <green>=100 <blue>=0`);
- blue is at **0% traffic but still ACTIVE**, hosting in-flight calls whose media WebSockets stay pinned to their replicas (blue-green-release.md §2.1);
- your job: watch blue's FC-7 lease census until it reaches **zero**, then retire blue.

**Drain-complete criterion (blue-green-release.md §4):** zero live FC-7 lease rows (`kind == "lease"` ∧ `lease_expires_at > now`) attributed to any current replica of the blue revision — bound leases by `owner_id`, unbound reservations by `reserved_by`. Owner IDs are Container Apps **replica names**.

## 2. Preconditions — check these FIRST (blue-green-release.md §5)

> These are honest gaps in the current tree. Do not trust a drain on them.

1. **P1 — revision mode must be `Multiple`.** `infra/bicep/modules/containerApps.bicep:498` still sets `activeRevisionsMode: 'Single'`. Until the Bicep delta lands, flip per window:
   ```
   az containerapp revision set-mode --mode multiple --name ca-voicebot-<env>-runtime --resource-group rg-voicebot-<env>
   ```
   **Never run `scripts/deploy/deploy-azure.sh <env> --apply` during a blue/green window** — it reverts the mode to Single and resets traffic to `latestRevision: 100`, collapsing the scheme (blue-green-release.md §5 P1).
2. **P2 — the runtime must be writing FC-7 leases.** `bot.py` does not yet wire `CosmosSessionDirectory` into the call path; until it lands with the owner convention (`owner_id` = replica name), the census is **trivially zero** and the criterion is untrusted. **Proof required: `drain_status.py census` reports N > 0 with real calls on blue** (blue-green-release.md §5 P2 / §9 step 3) before this runbook's outcome is believed in production.
3. **P3 — operator access.** `drain_status.py` authenticates like `storage/cosmos.py:56-68` (`DefaultAzureCredential` + `COSMOS_ENDPOINT`/`COSMOS_DATABASE`): you need **Cosmos DB Built-in Data Contributor** on `cosmos-voicebot-<env>` (RBAC row 4, u-401-deployment-plan.md §3) plus `az login` + subscription selected (`az account set --subscription <id>`).

## 3. Immediate operator checks (concrete commands)

```bash
# 0. Identity / tooling (read-only)
az account show
command -v az && command -v python3          # both required by drain-runtime.sh preflight

# 1. What-if first — read-only census + plan, mutates nothing (default mode)
scripts/deploy/drain-runtime.sh <env> <blue>
#    exit 0 = report printed (nothing changed); exit 1 = usage/preflight problem

# 2. Who are blue's replicas right now (owner set = replica names)
az containerapp replica list --name ca-voicebot-<env>-runtime --resource-group rg-voicebot-<env> \
  --revision <blue> --query "[].name" -o tsv

# 3. Direct census (same tool the loop polls)
PYTHONPATH=. python3 scripts/deploy/drain_status.py census --owners <replica1,replica2,...> --format text
#    --format count  -> just the number;  --format json  -> machine-readable
```

How to read the census: `live leases attributed to N owner(s): <total>`, per-owner counts, then one line per call (`holder=`, `expires_in=`). `reserved:<replica>` holders are unbound reservations — a call in handshake; it counts against drain-complete.

## 4. Mitigation / operation steps (normal path)

```bash
# The drain wait loop: polls the census every 30s (default = FC-7 lease TTL),
# deadline 60 min (default >= MAX_CALL_DURATION_SECONDS 3600s), exits 0 on drain-complete.
scripts/deploy/drain-runtime.sh <env> <blue> --apply

# Same, and retire blue the moment it is drained (the ONLY mutation the tool ever
# makes: `az containerapp revision deactivate`, and only at zero leases):
scripts/deploy/drain-runtime.sh <env> <blue> --apply --retire-when-drained
```

Flags (verified in `scripts/deploy/drain-runtime.sh:30-38`): `--apply` · `--deadline <minutes>` (default 60) · `--poll-seconds <n>` (default 30) · `--retire-when-drained` · `--force` + `--confirm-forced-drain=<rev>` (forced-drain path only → `forced-drain.md`).
Exit codes: `0` drained / what-if ok · `1` usage or preflight · `2` deadline reached with live leases (no `--force`) · `3` forced-drain incomplete.

While the loop runs:
- **Do nothing to blue.** Retention (ACTIVE + `runtimeMinReplicas=1` + connection-driven scale, blue-green-release.md §3) is what keeps in-flight calls alive. There is no `az` command to edit an existing revision's scale; `az containerapp revision copy` is **forbidden** mid-drain (new replicas ⇒ new owner IDs ⇒ lease attribution breaks).
- If a poll prints `warning: no replicas for '<blue>'` — blue's replicas are gone (scaled in or replaced). Any calls they owned died with them (owner-loss, `owner-loss.md`); their leases self-reap within 30 s, so the count heals. Verify before trusting a zero.
- On `exit 2` (deadline, calls remain) → go to `forced-drain.md`. Options at that point: extend `--deadline`, announced forced-BYE (default), carrier-side completion, or manual hard teardown (last resort).

## 5. Verification that the system recovered / completed

1. The tool prints `==> drain-complete: zero live leases attributed to '<blue>'` and exits `0`.
2. Confirm independently:
   ```bash
   az containerapp replica list --name ca-voicebot-<env>-runtime --resource-group rg-voicebot-<env> \
     --revision <blue> --query "[].name" -o tsv
   PYTHONPATH=. python3 scripts/deploy/drain_status.py census --owners <replicas> --format count   # -> 0
   ```
3. If `--retire-when-drained` was used: `az containerapp revision show --name ca-voicebot-<env>-runtime --resource-group rg-voicebot-<env> --revision <blue> --query "{name:name,active:properties.active,traffic:properties.trafficWeight}"` shows `active: false`.
4. New calls still land on green (place a test call; correlate to a green replica via the census owner set / logs — blue-green-release.md §9 step 5).
5. Watch window after drain: monitor `alert-app-failed-<env>`, `alert-dlq-<env>`, `alert-cosmos-429-<env>` (`infra/bicep/modules/monitor.bicep`) before declaring the release clean — the watch window is the rollback runway (`rollback.md`).

## 6. Escalation notes

- Drain cannot be trusted (census always 0 with live calls present) → P2 wiring is missing; escalate to the runtime owner (U-305 wiring) before retiring anything. Retiring blue without a trusted census risks dropping live calls.
- Deadline reached twice with calls that will not end → treat as forced-drain decision (`forced-drain.md`); get explicit approval per options table; never "just deactivate".
- Tool exits 1 at preflight → `az` missing / not logged in / `COSMOS_ENDPOINT`/`COSMOS_DATABASE` unset / helper missing; fix access (P3) — nothing was mutated (default mode is what-if).
- Rehearsal status: drain rehearsal is **simulated — real-env rehearsal pending** (`rehearsals/drain-rehearsal.md`); the deployment test of R-A-016 (calls completing on blue while new calls land on green) has **not** yet been performed against a real environment.
