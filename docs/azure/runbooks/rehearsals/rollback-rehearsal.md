# Rehearsal record — Rollback (traffic flip to blue-previous)

> # simulated — real-env rehearsal pending
> **This is a TABLETOP/SIMULATED rehearsal.** No cloud provisioning or cloud-touching command was executed. "Executed" items ran locally with recorded output; "Deferred" items require a real dev/staging environment and **have not happened**. This record must not be cited as evidence that rollback works in a real environment.

Author: team-lead · Date: 2026-11-20 · Scope: U-406 rehearsal requirement for `rollback.md` (R-O-001 rehearsal clause, simulated form; R-O-002's "routing restored via runbook" needs a real-env rehearsal before it can be claimed).

Environment (local): same as `drain-rehearsal.md` §1 (macOS checkout; az 2.92.0 with a session present but no ARM call made; Azure SDK not installed in the dev checkout).

## 1. Executed (offline — verbatim results)

### 1.1 The one traffic mutation: flag verification (help text only — no cloud calls)

The rollback command is exactly the release switch with weights reversed (`docs/azure/blue-green-release.md` §7):

```
az containerapp ingress traffic set --name ca-voicebot-<env>-runtime \
  --resource-group rg-voicebot-<env> --revision-weight <blue>=100 <green>=0
```

Verified against installed `az` 2.92.0 `--help`: `az containerapp ingress traffic set` accepts `--revision-weight` (list of revision weights, space-separated), `--name`, `--resource-group` — the flags above are real. (Also verified: `az containerapp revision show` / `replica list` flags used by `rollback.md` §2 checks.)

### 1.2 Post-rollback green drain: same tool, roles swapped

`rollback.md` §3 step 2 uses `scripts/deploy/drain-runtime.sh <env> <green> --apply` — the same script as the release drain (the tool is role-agnostic: `<revision>` = "the draining revision"). Executed: the identical gating battery recorded in `drain-rehearsal.md` §1.1–§1.2 (script is byte-identical; `bash -n` rc=0; all 8 mis-use cases exit 1 before any cloud path). What this proves: the confirm-gate cannot fire a forced-drain accidentally on the green drain either.

### 1.3 Test evidence underpinning the rollback accounting (re-run for this record)

```
$ .venv/bin/python -m pytest tests/test_drain.py tests/test_ownership.py -p no:cacheprovider --basetemp=/private/tmp/ut406
49 passed in 0.13s        (tests/test_drain.py: 12 passed · tests/test_ownership.py: 37 passed)
```
Relevant here: the drain-complete predicate (`is_drain_complete` / `census` attribution incl. unbound reservations) is what decides "green is drained, safe to retire" after the flip; `test_force_end_*` cover the forced path if green's calls outlive the post-rollback deadline; owner-loss tests document what happens if a green replica dies mid-drain (leases self-reap, census heals).

### 1.4 Data/schema compatibility review (document-level, not executed)

Per `blue-green-release.md` §7 and `rollback.md` §3: the media path is stateless, call records live in Cosmos and are unaffected by which revision served a call — **no data migration is involved in a release or its rollback**. Frozen wire contracts (summary enum `{pending, ok, failed}` — product-impact §7.1; event dedup by deterministic `event_id`; finalized calls never revived) are what make green-written data safe for blue-previous code. This was **reviewed against the contracts/tests** (`tests/test_status_callbacks.py` 30 passed incl. dedup/finalize cases, run in `drain-rehearsal.md` §1.5) — not exercised across two real revisions.

### 1.5 P1 hazard check (document-level)

`rollback.md` carries the P1 rule from `blue-green-release.md` §5: `scripts/deploy/deploy-azure.sh <env> --apply` during a blue/green window reverts `activeRevisionsMode` to Single and resets traffic to `latestRevision: 100`, collapsing the scheme. Verified the warning is sourced from `containerApps.bicep:498` / `:529-534` as cited. Not executable offline (would require a deployment — never performed).

## 2. Deferred — requires a real environment (NOT executed)

| # | Deferred action | Why deferred |
|---|---|---|
| 1 | Pre-flip checks on a live app: both revisions ACTIVE, current weights, P1 mode = Multiple | needs provisioned app (two real revisions) |
| 2 | The flip itself (`az containerapp ingress traffic set --revision-weight <blue>=100 <green>=0`) | cloud mutation — authorization-gated (R-O-004); none performed |
| 3 | Verify new calls land on blue (test call → correlate to a blue replica via census/logs) | needs real calls |
| 4 | Green drain end-to-end (`drain-runtime.sh <env> <green> --apply`) with real in-flight calls | needs real calls; P2 (runtime lease wiring) open |
| 5 | Watch-window verification: regression signal clears (`alert-app-failed-<env>` etc.), second-rollback runway intact | needs live monitoring |
| 6 | The full R-O-002 evidence chain ("routing restored via runbook in rollback rehearsal") | composed of steps 1–5 |

## 3. Honest conclusion

- The rollback **procedure and its safety rails** (single traffic mutation, role-swapped drain tool, forced-drain gate, drain-complete accounting) are verified at the level that is verifiable offline: flags exist in the real CLI, the script's gates reject mis-use, and the accounting logic passes 49 unit tests.
- **No rollback has been performed against any real deployment.** Until §2 runs on staging (blue-green-release.md §9 step 10 + step 12 archiving), rollback readiness is unproven and R-O-002/R-T-010 deployment evidence must not be claimed.
