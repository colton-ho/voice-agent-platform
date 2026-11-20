"""Stage 1 of spec §9.3: export a consistent source dataset from DynamoDB.

Reads one discovery §7 table through an INJECTED client seam
(:class:`DynamoClient`) and writes a JSONL snapshot with:

- pagination (scan pages via ``LastEvaluatedKey``),
- resumability (checkpoint file records the scan cursor after every page; a
  killed run resumes where it stopped),
- deterministic ordering (rows are deduplicated by business key and sorted
  before the final snapshot is written).

The real client (:class:`Boto3DynamoClient`) imports boto3 lazily — the whole
suite is importable and testable with zero AWS access. The AWS side is frozen
for RUNTIME; this stage legitimately READS AWS data at cutover time only
(D-8 / §9.3 path, user authorization required — see the runbook).

Usage::

    python -m scripts.migration.export --source users --workdir runs/cutover
    python -m scripts.migration.export --source history --workdir runs/cutover \\
        --table genaiic-voicebot-history   # history is env/table-required
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Protocol

if __package__ in (None, ""):  # plain `python scripts/migration/export.py`
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )

from scripts.migration.common import (  # noqa: E402
    canonical_json,
    checkpoint_path,
    load_checkpoint,
    new_checkpoint,
    read_jsonl,
    save_checkpoint,
    write_jsonl,
)
from scripts.migration.transform import (  # noqa: E402
    SOURCE_ORDER,
    change_marker,
    get_source,
    row_key,
    table_name,
)

DEFAULT_PAGE_SIZE = 250


class DynamoClient(Protocol):
    """Injectable seam over the DynamoDB scan API (tests never touch AWS)."""

    def scan_page(
        self, table: str, limit: int, exclusive_start_key: dict | None
    ) -> tuple[list[dict], dict | None]:
        """Return (items, last_evaluated_key or None)."""
        ...


class Boto3DynamoClient:
    """Real client: boto3 DynamoDB resource table scan with Decimal cleanup.

    Region resolution mirrors the legacy script: DDB_REGION -> AWS_REGION ->
    us-east-1 (discovery §9 us-east-1 defaults).
    """

    def __init__(self, region: str | None = None):
        self._region = (
            (region or "").strip()
            or (os.environ.get("DDB_REGION") or "").strip()
            or (os.environ.get("AWS_REGION") or "").strip()
            or "us-east-1"
        )
        self._tables: dict[str, Any] = {}

    def _table(self, name: str):
        if name not in self._tables:
            import boto3  # lazy: offline tooling must not require boto3

            self._tables[name] = boto3.resource(
                "dynamodb", region_name=self._region
            ).Table(name)
        return self._tables[name]

    def scan_page(self, table, limit, exclusive_start_key):
        from demo_store import _from_ddb  # Decimal -> int/float normalization

        kwargs: dict[str, Any] = {"Limit": int(limit)}
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        resp = self._table(table).scan(**kwargs)
        return [_from_ddb(it) for it in resp.get("Items", [])], resp.get("LastEvaluatedKey")


def scan_all(client: DynamoClient, table: str, page_size: int = DEFAULT_PAGE_SIZE) -> list[dict]:
    items: list[dict] = []
    start_key = None
    while True:
        page, start_key = client.scan_page(table, page_size, start_key)
        items.extend(page)
        if not start_key:
            return items


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def export_source(
    client: DynamoClient,
    source: str,
    workdir: str,
    *,
    table: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    resume: bool = True,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    """Export one source into ``<workdir>/snapshots/<source>.jsonl``.

    - Idempotent: a completed snapshot is a no-op unless ``force``.
    - Resumable: on interruption the checkpoint keeps the scan cursor and the
      partial file keeps the scanned lines; ``resume=True`` continues.
    - Deterministic: finalize dedupes by business key (last scan wins) and
      sorts rows by business key before writing the final snapshot.
    """
    spec = get_source(source)
    table = table_name(spec, table)
    snap_dir = os.path.join(workdir, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    final = os.path.join(snap_dir, f"{spec.name}.jsonl")
    partial = final + ".partial"
    ckpt_file = checkpoint_path(snap_dir, spec.name)

    if force:
        for path in (final, partial, ckpt_file):
            if os.path.exists(path):
                os.remove(path)

    if dry_run:
        items = scan_all(client, table, page_size)
        return {
            "source": spec.name, "table": table, "exported": len(items),
            "dry_run": True, "snapshot": None, "checkpoint": None,
        }

    ckpt = load_checkpoint(ckpt_file)
    if ckpt and ckpt.get("scan_complete") and os.path.exists(final) and not force:
        return {
            "source": spec.name, "table": table, "exported": len(read_jsonl(final)),
            "skipped": "snapshot already complete (use --force to re-export)",
            "snapshot": final, "checkpoint": ckpt_file,
        }

    resuming = bool(resume and ckpt and not ckpt.get("scan_complete")
                    and os.path.exists(partial))
    if resuming:
        start_key = ckpt.get("last_evaluated_key")
    else:
        ckpt = new_checkpoint(spec.name, table, _now_iso())
        start_key = None
    if os.path.exists(partial) and not resuming:
        os.remove(partial)

    scanned = 0
    with open(partial, "a" if resuming else "w", encoding="utf-8") as f:
        while True:
            page, last_key = client.scan_page(table, page_size, start_key)
            for item in page:
                f.write(canonical_json(item) + "\n")
                scanned += 1
            f.flush()
            start_key = last_key
            ckpt["last_evaluated_key"] = start_key
            ckpt["updated_at"] = _now_iso()
            save_checkpoint(ckpt_file, ckpt)  # cursor durable BEFORE next page
            if not start_key:
                break

    # Finalize: dedupe by business key (a resumed page may re-read a row),
    # sort deterministically, then publish the snapshot atomically-ish.
    keyed: dict[str, dict] = {}
    for row in read_jsonl(partial):
        keyed[row_key(spec.name, row)] = row
    ordered = [keyed[k] for k in sorted(keyed)]
    write_jsonl(final, ordered)
    os.remove(partial)

    ckpt["scan_complete"] = True
    ckpt["last_evaluated_key"] = None
    ckpt["count"] = len(ordered)
    ckpt["markers"] = {k: change_marker(spec.name, v) for k, v in keyed.items()}
    ckpt["updated_at"] = _now_iso()
    save_checkpoint(ckpt_file, ckpt)

    return {
        "source": spec.name, "table": table, "exported": len(ordered),
        "scanned": scanned, "resumed": resuming, "snapshot": final,
        "checkpoint": ckpt_file,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="U-404 §9.3 stage 1: export one DynamoDB source to a JSONL snapshot.",
    )
    parser.add_argument("--source", required=True, choices=list(SOURCE_ORDER))
    parser.add_argument("--workdir", required=True, help="run directory (snapshots/ is created inside)")
    parser.add_argument("--table", default=None, help="table-name override (default: env <TABLE>_TABLE or discovery §7 default)")
    parser.add_argument("--region", default=None, help="AWS region (default: DDB_REGION -> AWS_REGION -> us-east-1)")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--no-resume", action="store_true", help="ignore an interrupted checkpoint and restart the scan")
    parser.add_argument("--force", action="store_true", help="re-export even if a completed snapshot exists")
    parser.add_argument("--dry-run", action="store_true", help="scan and count only — write nothing")
    parser.add_argument("--json", action="store_true", help="machine-readable result")
    args = parser.parse_args(argv)

    result = export_source(
        Boto3DynamoClient(region=args.region),
        args.source,
        args.workdir,
        table=args.table,
        page_size=args.page_size,
        resume=not args.no_resume,
        force=args.force,
        dry_run=args.dry_run,
    )
    if args.json:
        import json

        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    else:
        for k in sorted(result):
            print(f"{k}: {result[k]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
