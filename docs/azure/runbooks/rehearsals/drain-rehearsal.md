# Rehearsal record — Release drain

> # simulated — real-env rehearsal pending
> **This is a TABLETOP/SIMULATED rehearsal.** No cloud provisioning or cloud-touching command was executed. Everything under "Executed" below actually ran in a local checkout and its output is recorded verbatim; everything under "Deferred" requires a real dev/staging environment and **has not happened**. This record must not be cited as evidence that a drain works in a real environment.

Author: team-lead · Date: 2026-11-20 · Scope: U-406 rehearsal requirement for `release-drain.md` + `forced-drain.md` (R-O-001: "at least the drain and rollback runbooks have been rehearsed (evidence recorded)" — **simulated form only**); drill list mapped to `docs/azure/blue-green-release.md` §9.

Environment (local): macOS checkout `/Users/colton/Developer/voice-agent-platform`; Azure CLI 2.92.0 installed, an `az` session exists (`az account show` rc=0) but **no ARM call was made** (tabletop boundary); `python3` 3.13.12; `.venv/bin/python` 3.13 (pytest); Azure SDK **not** installed in this checkout (`ModuleNotFoundError: No module named 'azure'`) — consistent with `docs/azure/verification-b19-acceptance.md:128` ("azure SDK 未裝").

## 1. Executed (offline — verbatim results)

### 1.1 Script syntax gate

```
$ bash -n scripts/deploy/drain-runtime.sh
rc=0 (syntax OK)
```

### 1.2 `drain-runtime.sh` argument/confirmation gating (the safety-critical paths — all run before any az/preflight call, `drain-runtime.sh:52-133`)

| # | Command | rc | First output line |
|---|---|---|---|
| 1 | `bash scripts/deploy/drain-runtime.sh` (no args) | 1 | `scripts/deploy/drain-runtime.sh — U-405 blue/green release drain tool` (usage) |
| 2 | `bash scripts/deploy/drain-runtime.sh sandbox ca-x` | 1 | `error: unknown environment 'sandbox' (expected dev\|staging\|prod)` |
| 3 | `bash scripts/deploy/drain-runtime.sh staging` (1 arg) | 1 | usage |
| 4 | `bash scripts/deploy/drain-runtime.sh staging ca-x --deadline abc` | 1 | `error: --deadline must be a whole number of minutes (got 'abc')` |
| 5 | `bash scripts/deploy/drain-runtime.sh staging ca-x --poll-seconds 0` | 1 | `error: --poll-seconds must be >= 1 (got '0')` |
| 6 | `bash scripts/deploy/drain-runtime.sh staging ca-x --force` | 1 | `error: --force requires --apply (what-if mode never forces)` |
| 7 | `bash scripts/deploy/drain-runtime.sh staging ca-x --apply --force --confirm-forced-drain=ca-y` | 1 | `error: --force requires --confirm-forced-drain=ca-x (explicit confirmation of the revision to force-drain)` |
| 8 | `bash scripts/deploy/drain-runtime.sh staging ca-x --confirm-forced-drain=ca-x` | 1 | `error: --confirm-forced-drain is only valid together with --force` |

**What this proves:** the forced-drain two-key gate (`--apply` AND `--confirm-forced-drain=<exact-revision>`) and all numeric/enum validation reject bad input with exit 1 **before any cloud or mutation path is reachable**. **What it does not prove:** anything about a real drain loop.

### 1.3 `drain_status.py` CLI validation (offline)

| # | Command | rc | Last output line |
|---|---|---|---|
| 1 | `python3 scripts/deploy/drain_status.py` (no subcommand) | 2 | `drain_status.py: error: the following arguments are required: command` |
| 2 | `python3 scripts/deploy/drain_status.py census` (no `--owners`) | 2 | `drain_status.py census: error: the following arguments are required: --owners` |
| 3 | `python3 scripts/deploy/drain_status.py census --owners a --format bogus` | 2 | `error: argument --format: invalid choice: 'bogus' (choose from text, json, count)` |
| 4 | `python3 scripts/deploy/drain_status.py force-end --reason x` (no `--owners`) | 2 | `drain_status.py force-end: error: the following arguments are required: --owners` |
| 5 | `python3 scripts/deploy/drain_status.py census --help` | 0 | usage printed |
| 6 | `PYTHONPATH=. python3 scripts/deploy/drain_status.py census --owners replica-a --format count` | 1 | `ModuleNotFoundError: No module named 'azure'` |

Case 6 is the live-run boundary in this checkout: `fetch_rows` → `storage.ownership.get_store()` → lazy `azure.cosmos` import stops **before any network call**. In a real environment the next boundaries are `COSMOS_ENDPOINT`/`COSMOS_DATABASE` presence (`storage/cosmos.py:60-63`) and `DefaultAzureCredential` (RBAC row 4).

### 1.4 Azure CLI flag verification (help text only — no cloud calls)

Verified against installed `az` 2.92.0 `--help` that the exact flags used in the runbooks exist: `az containerapp ingress traffic set --revision-weight/--name/--resource-group`; `az containerapp revision set-mode --mode/--name/--resource-group`; `az containerapp revision deactivate --revision/--name/--resource-group`; `az containerapp replica list --name/--resource-group/--revision/--query/--output`; `az containerapp revision show --revision/--name/--resource-group`; `az containerapp update --min-replicas/--max-replicas`; `az servicebus queue show --name/--namespace-name/--resource-group/--query`.

### 1.5 Test evidence (re-run for this record)

```
$ .venv/bin/python -m pytest tests/test_drain.py tests/test_ownership.py -p no:cacheprovider --basetemp=/private/tmp/ut406
49 passed in 0.13s        (tests/test_drain.py: 12 passed · tests/test_ownership.py: 37 passed)
```
Semantics demonstrated by these tests (used by the runbooks): drain-complete criterion + census attribution (`test_is_drain_complete_scenarios`, `test_census_totals_per_owner_and_holder_labels`); `hangup` command shape (`test_build_hangup_command_shape`); forced-BYE delivery/bookkeeping and the **unwired-transport honest refusal** (`test_force_end_unwired_transport_reports_not_accepted`, `test_force_end_delivers_hangup_and_records_bookkeeping`, `test_force_end_targets_only_attributed_live_calls`, `test_force_end_delivery_failure_reported_not_accepted`); admission idempotency at cap (`test_reserve_same_call_is_idempotent`, `test_admission_rejected_at_capacity`); owner-loss refusals (`test_route_owner_lost_no_live_lease`, `test_route_rejected_after_lease_expiry`, `test_route_owner_loss_is_nonterminal_retry_after_rebind`); exactly-one-bind races (`test_bind_race_exactly_one_wins`).

Supporting suites (also re-run): `tests/test_postcall_worker.py` + `tests/test_status_callbacks.py` → **30 passed** (broker/storage/Twilio runbook semantics).

## 2. Deferred — requires a real environment (NOT executed)

Mapped to `blue-green-release.md` §9 checklist:

| §9 step | Deferred action | Why deferred |
|---|---|---|
| 1 | P1: `az containerapp revision set-mode --mode multiple` + record mode output; P3 credential check against real Cosmos | needs provisioned app + Cosmos (`az` session exists but no ARM call made — tabletop) |
| 2 | Deploy green alongside blue; readiness probe + smoke | needs real app |
| 3 | Load blue with N≥2 real PSTN calls; **prove census N>0** (P2) | needs real calls + FC-7 runtime wiring (P2 is an open code gap — `bot.py` does not yet write leases; `blue-green-release.md` §5) |
| 4–6 | Weight switch; new call lands on green; blue calls complete uninterrupted (the R-A-016 deployment test) | needs real app + real calls |
| 7 | Natural drain: `drain-runtime.sh <env> <blue> --apply` end-to-end (first cloud-touching step is `az containerapp revision show` inside what-if; deliberately not executed) | needs real app |
| 8 | Forced-drain drill B (`--apply --force --confirm-forced-drain=<blue>`) with a real caller hearing an early-but-normal ending | needs real calls + the `DRAIN_SEND_FACTORY` owner-addressable transport wiring (currently an unwired seam — `force-end` would honestly exit 3, see §1.5 tests) |
| 9 | Failure-path drill (B unwired) against real leases | unit-level equivalent executed (`test_force_end_unwired_transport_reports_not_accepted`); live drill deferred |
| 10 | Rollback drill | see `rollback-rehearsal.md` |
| 11 | Hard-teardown D drill (manual, signed-off, documents the mid-audio drop honestly) | requires explicit sign-off; never automated |
| 12 | Archive real outputs as U-501 acceptance evidence | follows steps 1–11 |

## 3. Honest conclusion

- The **tooling-level** behaviors (gating, validation, exit codes 1/2/3 semantics, census predicate, forced-BYE delivery incl. its honest failure modes) are exercised and evidenced — by direct execution above and by the 49 unit tests.
- **No drain has ever been rehearsed against a live deployment**, and the drain-complete criterion cannot be trusted in production until P2 is proven (census > 0 with real calls). R-O-001's rehearsal clause is satisfied **only in simulated form** pending the real-env rehearsal of §2.
