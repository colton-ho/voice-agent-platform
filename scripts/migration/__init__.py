"""U-404 migration tool suite — spec §9.3 DynamoDB -> Cosmos data migration.

Stages (run in this order; see docs/azure/migration-runbook.md):

1. export      — read DynamoDB sources (discovery §7) into JSONL snapshots
                 (paginated, resumable via checkpoint, deterministic order).
2. transform   — pure record-in/record-out mapping to the Cosmos adapter
                 shapes in storage/ (canonical demo dict via
                 demo_loader.normalize_demo_dict, X1 history fold via the
                 cosmos_calls row shapes).
3. test-import — load transformed JSONL through the storage adapters against
                 FakeCosmos (tests/fake_cosmos.py); accept/reject counts and
                 shape validation without touching real Azure.
4. reconcile   — source snapshot vs target state: counts per entity, key-level
                 diffs, checksum-ish field comparison; human + JSON reports.
5. delta       — re-export and select only changed/new records since a
                 checkpoint (per-source change markers; limits documented).
6. full-run    — export -> transform -> test-import -> reconcile with a
                 manifest (rollback input).
7. rollback    — remove/restore the rows recorded in a manifest (DESTRUCTIVE;
                 requires --yes).

Sources covered (discovery §7, exactly five DynamoDB tables):
users, demos, voices, activity, history.

D-8 gate
--------
This is BUILD-TIME tooling. Nothing here runs against AWS or Azure without
explicit user authorization of a migration (D-8 / spec §9.3). The export and
delta stages read the frozen AWS side only at cutover time; test-import and
rollback default to the in-memory FakeCosmos target and dry-run modes so no
external write can happen by accident.

House style follows scripts/deploy/deploy-azure.sh: safe defaults
(what-if/dry-run first, explicit --apply/--yes to act), no secrets echoed.

This suite REPLACES the legacy one-shot scripts/migrate_demos_to_ddb.py and
the test_migrate_demos harness (discovery §8 group H: "REPLACE with §9.3
migration tooling").
"""

SOURCES_DOC = ("users", "demos", "voices", "activity", "history")
