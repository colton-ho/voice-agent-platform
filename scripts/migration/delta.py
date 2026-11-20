"""Stage 5 of spec §9.3: delta — capture changes since a checkpoint.

Re-exports the source (full re-scan through the injectable DynamoClient seam)
and emits ONLY the records that are new or changed since the export
checkpoint, identified by the per-source change markers
(:func:`scripts.migration.transform.change_marker`):

- users / demos / voices / history — content checksum (no updated_at exists in
  these tables). Limits: deletions are invisible (reconcile catches them);
  every delta re-scans the table (small / TTL-bounded). History's late
  summary/turns patches surface as checksum changes on the NEXT delta run —
  run the final delta after the summary window, or use a write-freeze
  (spec §9.3-5).
- activity — the ``ts_id`` marker (append-only audit log). Limits: in-place
  edits would be invisible; TTL expiry deletes rows silently.

Outputs (into ``<workdir>/snapshots/``):

- ``<source>.delta.jsonl``            — raw changed/new records (sorted)
- ``<source>.delta.transformed.jsonl`` — transformed envelopes for those records

and advances the checkpoint markers. ``--apply`` additionally runs the
test-import stage on the transformed delta (default target FakeCosmos;
writes need ``--apply`` — D-8 gate).

Usage::

    python -m scripts.migration.delta --source users --workdir runs/cutover
    python -m scripts.migration.delta --source history --workdir runs/cutover --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys

if __package__ in (None, ""):  # plain `python scripts/migration/delta.py`
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )

from scripts.migration.common import (  # noqa: E402
    checkpoint_path,
    load_checkpoint,
    read_jsonl,
    resolve_container_provider,
    save_checkpoint,
    write_jsonl,
)
from scripts.migration.export import (  # noqa: E402
    DEFAULT_PAGE_SIZE,
    DynamoClient,
    _now_iso,
    scan_all,
)
from scripts.migration.transform import (  # noqa: E402
    SOURCE_ORDER,
    TransformError,
    change_marker,
    get_source,
    row_key,
    table_name,
    transform_record,
)
from scripts.migration.test_import import load_transformed  # noqa: E402


def delta_source(
    client: DynamoClient,
    source: str,
    workdir: str,
    *,
    table: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    now: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Compute + write the changed/new records since the export checkpoint."""
    spec = get_source(source)
    table = table_name(spec, table)
    snap_dir = os.path.join(workdir, "snapshots")
    ckpt_file = checkpoint_path(snap_dir, spec.name)
    ckpt = load_checkpoint(ckpt_file)
    if not ckpt or not ckpt.get("scan_complete"):
        raise SystemExit(
            f"delta: no completed export checkpoint for {spec.name!r} at {ckpt_file} "
            f"— run the export stage first"
        )
    old_markers: dict = ckpt.get("markers") or {}

    items = scan_all(client, table, page_size)
    keyed: dict[str, dict] = {}
    for item in items:
        keyed[row_key(spec.name, item)] = item
    new_markers = {k: change_marker(spec.name, v) for k, v in keyed.items()}

    changed_keys = sorted(k for k in keyed if old_markers.get(k) != new_markers[k])
    new_keys = sorted(k for k in changed_keys if k not in old_markers)
    modified_keys = [k for k in changed_keys if k in old_markers]
    unchanged = sum(1 for k in keyed if old_markers.get(k) == new_markers[k])
    changed = [keyed[k] for k in changed_keys]

    result: dict = {
        "source": spec.name,
        "table": table,
        "total": len(keyed),
        "changed": len(changed),
        "new": len(new_keys),
        "modified": len(modified_keys),
        "unchanged": unchanged,
        "changed_keys": changed_keys,
        "transform_rejects": [],
    }

    # Transform the changed rows; collect rejects (unkeyable/unmappable rows).
    envelopes: list[dict] = []
    kept: list[dict] = []
    for item in changed:
        try:
            envelopes.extend(transform_record(spec.name, item, now=now))
            kept.append(item)
        except TransformError as e:
            result["transform_rejects"].append({"key": row_key(spec.name, item), "reason": str(e)})
    envelopes.sort(key=lambda e: (e["key"], e["container"], e["item"].get("id") or ""))

    if not dry_run:
        delta_raw = os.path.join(snap_dir, f"{spec.name}.delta.jsonl")
        delta_transformed = os.path.join(snap_dir, f"{spec.name}.delta.transformed.jsonl")
        write_jsonl(delta_raw, [keyed[k] for k in changed_keys])
        write_jsonl(delta_transformed, envelopes)
        result["delta"] = delta_raw
        result["delta_transformed"] = delta_transformed

        # Advance the checkpoint markers only after the delta is materialized.
        ckpt["markers"] = new_markers
        ckpt["updated_at"] = _now_iso()
        ckpt["last_delta_at"] = _now_iso()
        ckpt["last_delta_count"] = len(changed)
        save_checkpoint(ckpt_file, ckpt)
    result["dry_run"] = bool(dry_run)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="U-404 §9.3 stage 5: delta — changed/new records since a checkpoint.",
    )
    parser.add_argument("--source", required=True, choices=list(SOURCE_ORDER))
    parser.add_argument("--workdir", required=True, help="run directory (uses snapshots/<source>.checkpoint.json)")
    parser.add_argument("--table", default=None)
    parser.add_argument("--region", default=None)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--apply", action="store_true",
                        help="also test-import the transformed delta (needs --target; writes need --apply)")
    parser.add_argument("--target", choices=("fake", "real"), default="fake")
    parser.add_argument("--dry-run", action="store_true", help="compute counts only — write nothing")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from scripts.migration.export import Boto3DynamoClient  # lazy: boto3 optional

    result = delta_source(
        Boto3DynamoClient(region=args.region),
        args.source,
        args.workdir,
        table=args.table,
        page_size=args.page_size,
        dry_run=args.dry_run,
    )

    if args.apply and not args.dry_run and result.get("delta_transformed"):
        get_container, label = resolve_container_provider(args.target)
        envelopes = read_jsonl(result["delta_transformed"])
        report = load_transformed(envelopes, get_container, apply=True)
        result["import"] = {"target": label, **report}

    if args.json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    else:
        for k in sorted(result):
            if k == "changed_keys":
                continue
            print(f"{k}: {result[k]}")
        if result["changed_keys"]:
            print(f"changed_keys: {result['changed_keys']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
