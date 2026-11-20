# U-404 · Migration Runbook — DynamoDB → Cosmos (spec §9.3)

Operating manual for the `scripts/migration/` tool suite: export / transform /
test-import / reconcile / delta / full-run / rollback over the five DynamoDB
sources documented in discovery §7. Implements the spec §9.3 migration
procedure step by step (numbers below reference §9.3):

1. Export a consistent source dataset and record its cutover boundary.
2. Transform IDs, timestamps, expiry, encodings, and provider references explicitly.
3. Import a small test set with deterministic idempotent upserts.
4. Reconcile counts, business-key uniqueness, representative queries, timestamps, and expiry.
5. Choose a documented delta-capture or write-freeze strategy.
6. Run the full migration and final delta process.
7. Retain rollback exports under the approved retention policy.
8. Reconcile writes created on Azure before any rollback.

---

## 0. D-8 gate — nothing runs without user authorization

**This suite is built and tested in advance, but no stage RUNS a migration
until the user explicitly authorizes one (decision D-8 / implementation-plan
U-404 "D-8 gated").** Concretely:

- The export/delta stages **read** the frozen AWS side (boto3) at cutover
  time only. The AWS side is frozen for RUNTIME; migration reads are the
  explicitly sanctioned D-8/§9.3 exception.
- test-import / full-run / rollback write **nothing external** by default:
  the default target is the in-memory FakeCosmos and every write path is
  gated behind an explicit flag (`--apply` for stages 3/5/6, `--yes` for
  stage 7). `--target real` is the cutover path (storage.cosmos) and
  full-run refuses `--target real` without `--apply`.
- Getting the authorization is an operator action, out of band. This runbook
  assumes it has been obtained before any `--target real` command is run.

House style follows `scripts/deploy/deploy-azure.sh`: what-if/dry-run first,
explicit confirmation to act, no secrets echoed (credentials flow through the
standard SDK chains — env vars / managed identity — never through argv).

## 1. Source inventory (discovery §7 — exactly five tables)

| Source | Table (env / default) | Key | TTL / retention | Target containers (§1.1) |
|---|---|---|---|---|
| `users` | `USERS_TABLE` → `genaiic-voicebot-users` | PK `username` | none | `users` (PK `username`, id=username) |
| `demos` | `DEMOS_TABLE` → `genaiic-voicebot-demos` | PK `id` | none | `demos` (PK `type="demo"`, id=demo id) |
| `voices` | `VOICES_TABLE` → `genaiic-voicebot-voices` | PK `provider` + SK `voice_id` | none | `voices` (PK `provider`, id=voice_id) |
| `activity` | `ACTIVITY_TABLE` → `genaiic-voicebot-activity` | PK `day` + SK `ts_id` (`<epoch_ms>#<uuid>`) | per-row TTL (`ACTIVITY_LOG_TTL_DAYS`) | `activity` (PK `day`, id=ts_id) |
| `history` | `HISTORY_TABLE` (**env-required** — no default; empty ⇒ disabled, bot.py:1229) | PK `call_id` | TTL attr `ttl` = `started_at + HISTORY_TTL_DAYS*86400` (default 30 d) | X1 fold: `calls` (PK `caller_id`, id=call_id) + `transcripts` + `call_events` |

Region resolution for the AWS reads: `DDB_REGION` → `AWS_REGION` → `us-east-1`
(discovery §9 us-east-1 defaults). Table names resolve: `--table` override →
env var → discovery default (`history` raises without env/override).

### Transform notes (§9.3-2)

- **IDs**: business keys preserved; Cosmos `id` set per container shape
  (username / demo id / voice_id / ts_id / call_id). History turn rows get the
  deterministic `write_turns` ids `<call_id>#t<i>` / `<call_id>#e<i>`.
- **Timestamps**: unchanged (epoch seconds for history, epoch ms for
  activity `ts`); no timezone shifts applied.
- **Expiry** (§9.2): the legacy DDB `ttl` attribute is the **delete-point
  epoch** (§1.3-5 semantics 1:1). It maps to `expires_at`; the system `ttl`
  is recomputed as remaining seconds at write time (Cosmos TTL counts from
  last modification — carrying the raw field would renew forever).
  Expired records are **not imported**: the target stores suppress expired
  rows on read (`expires_at = null OR > now`), and reconcile reports them as
  present-or-absent consistently on both sides.
- **Encodings**: DynamoDB `Decimal` values are normalized to `int`/`float`
  on read (the pinned `_from_ddb` normalizer); JSONL snapshots are UTF-8
  canonical JSON (sorted keys) so files are byte-deterministic.
- **Provider references** (`provider`, `voice_id`, `engine`, `voice`,
  `model`, `minimax_model`): passed through verbatim — no name remapping.
- **Demo canonical shape**: routed through the shared
  `demo_loader.normalize_demo_dict` (17 keys incl.
  engine/provider/voice/model/asr_filter; tool list key is always
  `tool_ids`, never `tools`) + the X3 PK constant `type="demo"`. Method
  signatures in storage/ are untouched (discovery §10).
- **History X1 fold**: `caller` → `caller_id` (PK; `_none_` sentinel for
  anonymous), `turns[]` leaves the call row — text turns land in
  `transcripts`, tool_call turns in `call_events`; `get_history_doc`
  reassembles the identical external `turns[]` shape.

## 2. Stage-by-stage procedure

All commands are runnable as `scripts/migration/migrate.sh <stage> [args]` or
directly as `python -m scripts.migration.<module>` (module names:
`export`, `transform`, `test_import`, `reconcile`, `delta`, `full_run`,
`rollback`). Use a fresh run directory per attempt, e.g. `runs/<date>/`.

### 2.1 export (§9.3-1) — `scripts/migration/export.py`

```
scripts/migration/migrate.sh export --source users --workdir runs/cutover
scripts/migration/migrate.sh export --source history --workdir runs/cutover --table <HISTORY_TABLE>
```

- Reads one table through the injectable `DynamoClient` seam (production:
  `Boto3DynamoClient`, lazy boto3; tests never touch AWS).
- Writes `snapshots/<source>.jsonl` — one raw record per line, **sorted by
  business key** (users: username, demos: id, voices: `provider#voice_id`,
  activity: ts_id, history: call_id). Identical data ⇒ byte-identical files.
- **Checkpoint** `snapshots/<source>.checkpoint.json`: the scan cursor
  (`last_evaluated_key`) is saved after every page and the partial line log is
  kept in `snapshots/<source>.jsonl.partial`. A killed run **resumes** with a
  plain re-run (`--no-resume` restarts; `--force` re-exports from scratch).
  On completion the checkpoint records `scan_complete`, the row count and the
  per-row **change markers** used by delta.
- Idempotent: a completed snapshot is a no-op unless `--force`.
- `--dry-run` scans and counts only.

**Cutover boundary (§9.3-1)**: record the run directory + `manifest.json`
timestamps as the boundary. Data written to DynamoDB AFTER the scan is not in
the snapshot — that is what delta (§9.3-5) is for.

### 2.2 transform (§9.3-2) — `scripts/migration/transform.py`

```
scripts/migration/migrate.sh transform --source users \
    --input runs/cutover/snapshots/users.jsonl \
    --out runs/cutover/transformed/users.transformed.jsonl
```

Pure record-in/record-out (no I/O beyond the two files). Output is one
envelope per target row: `{"source", "key", "container", "item"}`, sorted by
`(key, container, id)`. Unmappable records (missing business key etc.) are
reported as rejects — **rejects are a gate**: do not proceed with a non-zero
reject count without investigating (§4).

### 2.3 test-import (§9.3-3) — `scripts/migration/test_import.py`

```
scripts/migration/migrate.sh test-import --input runs/cutover/transformed/users.transformed.jsonl
scripts/migration/migrate.sh test-import --input ... --apply
scripts/migration/migrate.sh test-import --input ... --target real --apply   # cutover only
```

- Shape validation per envelope (non-empty `id` + the §1.1 partition key) and
  **accept/reject counts** per container; upserts are the deterministic
  idempotent writes of §9.3-3 (re-running converges to the same state).
- Default target `--target fake` = FakeCosmos (in-memory) — verifiable
  offline with zero Azure access. `--target real` = storage.cosmos.
- Dry-run by default: without `--apply` it validates and counts only.

### 2.4 reconcile (§9.3-4) — `scripts/migration/reconcile.py`

```
scripts/migration/migrate.sh reconcile --snapshots runs/cutover/snapshots [--target real]
```

Compares the source snapshot (expected = per-record transform) against the
target state (read through the same storage-adapter container seam):

- counts per entity (per §1.1 container),
- key-level diffs: `missing_in_target` / `extra_in_target`,
- checksum-ish field comparison: per-row checksums over business fields, with
  per-field `diffs` / `missing_fields` on mismatch.

Both human-readable and `--json` output. Excluded from comparison: the system
`ttl` (remaining seconds, write-derived per §1.3-2/3) and Cosmos system props
(`_etag` etc.). `extra_fields` (fields only present in the target, e.g. added
by live traffic) are reported for visibility but are **not fatal**;
everything else is.

**Reconcile gates**: exit code 0 only when `ok=true`. Block the migration on:
any missing/extra row, any value drift, any `transform_rejects`/`rejected`
count, or a `diff_rows` count > 0. Representative queries and
business-key uniqueness are covered by the key-level diff (keys are unique by
construction on both sides); timestamps/expiry by the field comparison
(`started_at`/`ended_at`/`expires_at` compared, `ttl` excluded by design).

### 2.5 delta (§9.3-5) — `scripts/migration/delta.py`

```
scripts/migration/migrate.sh delta --source users --workdir runs/cutover
scripts/migration/migrate.sh delta --source users --workdir runs/cutover --apply
```

Re-exports the source and emits **only new/changed records since the export
checkpoint**, into `snapshots/<source>.delta.jsonl` (raw) and
`snapshots/<source>.delta.transformed.jsonl` (envelopes), then advances the
checkpoint markers. `--apply` chains test-import on the transformed delta.

**Per-source change markers and their limits:**

| Source | Marker | Limits |
|---|---|---|
| users / demos / voices | content checksum (sha256 of the canonical record) | No `updated_at` exists in these tables ⇒ change detection is scan-and-diff. Deletions are invisible to delta (reconcile catches them). Every delta re-scans the table (small; acceptable). |
| history | content checksum | Rows are mutated after insert (late `turns[]`/`summary` patches per `HistoryRecorder`). A late patch surfaces as a checksum change on the **next** delta run — run the final delta after the summary window, or use the §9.3-5 write-freeze alternative. Deletions/TTL expiry invisible. |
| activity | `ts_id` (`<epoch_ms>#<uuid>`) — the audit log is append-only | An in-place edit would be invisible (rows are never mutated in practice). TTL expiry deletes rows silently; delta never reports deletions. |

**Delta-capture vs write-freeze (§9.3-5)**: this suite implements delta
capture as above. If the operational window cannot tolerate the history
"late summary" gap, use the documented alternative: freeze writes to the
source (stop traffic / disable writes) before the final export — a frozen
snapshot is then complete by definition.

### 2.6 full-run (§9.3-6) — `scripts/migration/full_run.py`

```
scripts/migration/migrate.sh full-run --workdir runs/cutover                          # rehearsal (fake target)
scripts/migration/migrate.sh full-run --workdir runs/cutover --target real --apply    # cutover
```

Orchestrates export → transform → test-import → reconcile per source and
writes `manifest.json`: run id, timestamps, per-source file paths and counts,
the complete **row index** (`container`/`id`/`partition_key` of every row the
run produced — the rollback input) and the reconcile report. Exit code 0 only
when the manifest `ok` is true. The default (no flags) is a self-contained
offline rehearsal against FakeCosmos.

### 2.7 rollback (§9.3-7/8) — `scripts/migration/rollback.py`

**DESTRUCTIVE.** `remove` deletes exactly the rows recorded in a manifest's
row index — nothing else. `restore` re-upserts those rows from the run's
retained transformed JSONL (undoing a remove). Dry-run by default; execution
requires the explicit `--yes` flag:

```
scripts/migration/migrate.sh rollback --manifest runs/cutover/manifest.json              # dry-run listing
scripts/migration/migrate.sh rollback --manifest runs/cutover/manifest.json --yes        # DELETE the run's rows
scripts/migration/migrate.sh rollback --manifest ... --mode restore --yes                # re-add them
```

Procedure:

1. **First reconcile Azure-created writes (§9.3-8)**: rows written to Azure
   after the migration are NOT in the manifest and will NOT be removed — and
   routing calls back to AWS alone does not synchronize them. Reconcile those
   writes explicitly (reconcile against the pre-migration snapshots will show
   them as `extra_in_target`) and decide their disposition before rolling back.
2. Dry-run the rollback and review the listing against the manifest row count.
3. Execute with `--yes` (`--target real` at cutover). `remove` is idempotent:
   rows already gone are counted as `missing`, not errors.
4. **Retain the rollback exports** (the run directory: snapshots, transformed
   JSONL, manifest) under the approved retention policy (§9.3-7). `restore`
   depends on the transformed JSONL still being present.
5. Record the rollback in the cutover log (U-503 rehearsal/rollback records).

## 3. Recommended cutover sequence (§9.3-1..8)

1. **Rehearsal offline**: `full-run` against the fake target; reconcile green;
   tests green (`tests/test_migration_tooling.py`).
2. Obtain **user authorization (D-8)** for the migration run.
3. `export` all five sources; record the boundary (run dir + timestamps).
4. `transform` + `test-import --target real --apply` on a **small test set**
   first (§9.3-3: restrict with a filtered snapshot), reconcile it.
5. `full-run --target real --apply` for the full set; reconcile gate green.
6. Final `delta` per source (after the summary window for history, or under a
   write-freeze); `test-import` the deltas with `--target real --apply`;
   final reconcile.
7. Archive the run directory as the rollback export set.
8. Any rollback follows §2.7 — including the Azure-created-writes
   reconciliation first (§9.3-8).

## 4. Testing / offline verification

```
python -m pytest tests/test_migration_tooling.py -p no:cacheprovider
```

Deterministic and network-free: the export stage runs against an injected
in-memory `DynamoClient` (the boto3 seam is injectable), and
test-import/reconcile/rollback run against `tests/fake_cosmos.py`. Coverage:
per-source transform round-trips (record → transform → fake accepts → real
store deserializers read back), test-import accept/reject counts, reconcile
count + field diff detection, delta changed-record selection (both marker
families), rollback removing exactly the manifest's rows (and restore), and
checkpoint resume with deterministic re-export.

## 5. File map

```
scripts/migration/
  migrate.sh          thin dispatcher (house style: set -euo pipefail, dry-run defaults)
  __init__.py         suite overview + D-8 gate
  common.py           JSONL/checkpoint plumbing, checksums, target resolution
  transform.py        §9.3-2 pure transforms + change markers + envelope validation
  export.py           §9.3-1 DynamoClient seam + resumable, deterministic export
  test_import.py      §9.3-3 load/validate against FakeCosmos (or real, gated)
  reconcile.py        §9.3-4 source-vs-target comparison (human + JSON)
  delta.py            §9.3-5 marker-based change capture since a checkpoint
  full_run.py         §9.3-6 orchestration + manifest
  rollback.py         §9.3-7/8 destructive remove/restore (--yes gate)
tests/test_migration_tooling.py   offline test suite (FakeCosmos + fake DynamoDB)
docs/azure/migration-runbook.md   this document
```

## 6. Legacy disposition

`scripts/migrate_demos_to_ddb.py` (disk demos → DynamoDB seeder) and its
harness `tests/test_migrate_demos.py` are **REPLACE'd** by this suite
(discovery §8 group H: "test_migrate_demos + scripts/migrate_demos_to_ddb.py
REPLACE with §9.3 migration tooling"). The legacy script remains as a
read-only reference for the demo seeding path; do not run it at cutover — the
demo source of record for migration is the `demos` DynamoDB table
(discovery §7), exported/transformed here.
