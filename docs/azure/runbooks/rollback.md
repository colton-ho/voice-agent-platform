# Runbook — Rollback (flip traffic back to blue-previous)

Author: team-lead · Date: 2026-11-20 · Status: **runbook (rollback rehearsal simulated — see `rehearsals/rollback-rehearsal.md`)**
Scope: `docs/azure/implementation-plan.md:427` U-406 runbook #3 · closes **R-O-001** runbook side; rehearsal evidence for R-O-002's "routing restored via runbook" path.
Ground truth: `docs/azure/blue-green-release.md` §7 (rollback interplay), §2.3 decision D5, §3 (retention = rollback runway), §5 P1.

---

## 1. Symptom / trigger

Regression observed during the watch window after a blue/green switch (`blue-green-release.md` §2.3 D5): elevated app failures (`alert-app-failed-<env>`), DLQ growth (`alert-dlq-<env>`), Cosmos throttling (`alert-cosmos-429-<env>`), or smoke/real-call failures attributable to the green revision. Decision: **route new calls back to blue-previous**.

Rollback here means exactly one thing: **flip the ingress traffic weights back** (`blue-green-release.md` §7). It is:
- **NOT** the RG-level rebuild (`docs/azure/u-401-deployment-runbook.md` §4, infra disaster path);
- **NOT** the old-world "re-apply `containerImage` at the previous tag" (`u-401-deployment-runbook.md` §4 row 3 — which defers the media-endpoint story to U-405/blue-green-release.md).

## 2. Immediate operator checks (concrete commands)

```bash
# 1. Confirm both revisions and current weights before touching anything
az containerapp revision show --name ca-voicebot-<env>-runtime --resource-group rg-voicebot-<env> \
  --revision <blue> --query "{name:name,active:properties.active,traffic:properties.trafficWeight}" -o json
az containerapp revision show --name ca-voicebot-<env>-runtime --resource-group rg-voicebot-<env> \
  --revision <green> --query "{name:name,active:properties.active,traffic:properties.trafficWeight}" -o json

# 2. Blue must still be ACTIVE (it was retained for exactly this moment — blue-green-release.md §3)
#    If blue is gone/inactive: rollback by traffic flip is impossible; escalate (§5).

# 3. What is in flight on green (these calls keep draining on green after the flip)
az containerapp replica list --name ca-voicebot-<env>-runtime --resource-group rg-voicebot-<env> \
  --revision <green> --query "[].name" -o tsv
PYTHONPATH=. python3 scripts/deploy/drain_status.py census --owners <green-replicas> --format text

# 4. P1 check: revision mode must be Multiple (blue-green-release.md §5 P1)
#    traffic-set silently behaves differently in Single mode — verify before flipping.
```

## 3. Mitigation steps

```bash
# 1. Flip the weights — the only traffic mutation of the rollback
az containerapp ingress traffic set --name ca-voicebot-<env>-runtime \
  --resource-group rg-voicebot-<env> --revision-weight <blue>=100 <green>=0
```
Effects (blue-green-release.md §2.1):
- new calls / new media WebSockets land on blue immediately;
- **established WebSockets do not move** — green's in-flight calls continue to completion on green;
- zero DNS operations: `PUBLIC_MEDIA_BASE_URL` / the Twilio-side media URL stay valid (hostname binds at app level).

```bash
# 2. Drain green with the SAME tool, roles swapped (green is now the draining revision)
scripts/deploy/drain-runtime.sh <env> <green> --apply            # add --retire-when-drained to retire at zero
```
Green is only retired once drained (or via `forced-drain.md` at an approved deadline — same options table, roles swapped).

**Interactions with green draining:**
- Retention logic is symmetric: green stays ACTIVE at weight 0 while its calls run; its leases count only against green's replicas.
- Never retire **blue** during/after rollback until the new watch window closes clean — blue is the live revision again and the runway for a *second* rollback (back to green) if the regression was misdiagnosed.
- **Do not run `scripts/deploy/deploy-azure.sh <env> --apply` during any of this** (P1: reverts mode to Single + `latestRevision: 100`, collapsing the scheme).

**Data / schema compatibility considerations:**
- The media path is **stateless**; call records live in Cosmos (`calls` family, U-301) and are unaffected by which revision served a call. **No data migration is involved in a release or its rollback** (blue-green-release.md §7).
- Compatibility risk is therefore only in what green **wrote** during the window: stay within frozen wire contracts (summary wire enum `{pending, ok, failed}` — product-impact §7.1, no fourth value; CallEvent dedup by deterministic `event_id`; finalized calls never revived). A green build that respected FC-0..FC-8 + the wire contracts is safe to roll back against.
- If green wrote rows with fields blue's code does not understand, that is a contract breach — treat as a release-process failure (escalate to solution-architect per spec §16), not something to patch in this runbook.

## 4. Verification that the system recovered

1. New calls land on blue: place a test call and correlate it to a **blue** replica (`drain_status.py census` owner set / logs).
2. The regression signal clears: `alert-app-failed-<env>` / smoke failures return to baseline within the observation window; `alert-dlq-<env>` and `alert-cosmos-429-<env>` quiet.
3. Green drains to zero (its calls finish normally — callers hear no difference):
   ```bash
   PYTHONPATH=. python3 scripts/deploy/drain_status.py census --owners <green-replicas> --format count   # -> 0
   scripts/deploy/drain-runtime.sh <env> <green> --apply   # prints drain-complete, exits 0
   ```
4. Record the incident: trigger, watch-window evidence, flip timestamp, drain results, and the retained-blue state — this doubles as the R-O-002 rollback-rehearsal evidence trail.

## 5. Escalation notes

- Blue inactive/missing when the regression hits → traffic-flip rollback is unavailable; options: redeploy the previous image as a new revision and flip to it once ready, or the RG-level rebuild (`u-401-deployment-runbook.md` §4). Escalate before choosing — the latter is an infra disaster path, not a release rollback.
- Rollback repeatedly needed for the same release → stop flipping; the release is defective. Hold traffic on blue-previous, keep green at 0%, and route root-cause to the release owner (U-503 cutover/rollback governance; R-O-004: changes need recorded authorization).
- Green's calls refuse to drain (census wedged past `MAX_CALL_DURATION_SECONDS`) → `forced-drain.md` options table with green in the `<revision>` slot; the same confirm-gate applies.
- Rehearsal status: rollback rehearsal is **simulated — real-env rehearsal pending** (`rehearsals/rollback-rehearsal.md`).
