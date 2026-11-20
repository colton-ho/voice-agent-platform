"""Stage 4 of spec §9.3: reconcile source snapshot vs target state.

Compares what the source snapshot SHOULD produce (per-record transform) with
what is actually stored in the target containers:

- counts per entity (per §1.1 container),
- key-level diffs (missing in target / extra in target),
- checksum-ish field comparison (per-row checksums over business fields, with
  per-field diffs on mismatch).

Excluded from comparison: the system ``ttl`` (remaining seconds, recomputed on
every store write per §1.3-2/3) and Cosmos system properties (``_``-prefixed).
``extra_fields`` (fields present only in the target) are reported for
visibility but are NOT fatal — live traffic legitimately adds fields to rows;
everything else (missing/extra rows, value drift, missing fields) is fatal.

Report: machine-readable dict + human rendering (spec §9.3-4 evidence).

Usage::

    python -m scripts.migration.reconcile --snapshots runs/cutover/snapshots
    python -m scripts.migration.reconcile --snapshots ... --sources users,demos --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Callable

if __package__ in (None, ""):  # plain `python scripts/migration/reconcile.py`
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )

from scripts.migration.common import (  # noqa: E402
    is_system_field,
    read_jsonl,
    resolve_container_provider,
    row_checksum,
)
from scripts.migration.transform import (  # noqa: E402
    CONTAINER_PK,
    SOURCE_ORDER,
    get_source,
    transform_record,
)


def _effective(item: dict) -> dict:
    """Business fields only: no system props, no None placeholders."""
    return {
        k: v for k, v in item.items()
        if not is_system_field(k) and v is not None
    }


def build_expected(source_rows: dict[str, list[dict]], *, now: int | None = None) -> dict:
    """{source: [raw rows]} -> {container: {(pk, id): expected item}}."""
    expected: dict[str, dict] = {}
    for name in SOURCE_ORDER:
        for raw in source_rows.get(name, []):
            for env in transform_record(name, raw, now=now):
                container = env["container"]
                item = env["item"]
                pk = item[CONTAINER_PK[container]]
                expected.setdefault(container, {})[(pk, item["id"])] = item
    return expected


def read_target(get_container: Callable, containers: list[str]) -> dict:
    """{container: {(pk, id): actual item}} via the container-verb seam."""
    from storage.cosmos import ContainerNotFound

    actual: dict[str, dict] = {}
    for container_name in containers:
        pk_field = CONTAINER_PK[container_name]
        try:
            rows = get_container(container_name).query_items("SELECT * FROM c", [])
        except ContainerNotFound:
            rows = []  # container absent == empty target side
        for item in rows:
            key = (item.get(pk_field), item.get("id"))
            actual.setdefault(container_name, {})[key] = item
    return actual


def _key_str(key: tuple) -> dict:
    return {"partition_key": key[0], "id": key[1]}


def compare_container(container: str, expected: dict, actual: dict) -> dict:
    missing = sorted(k for k in expected if k not in actual)
    extra = sorted(k for k in actual if k not in expected)
    field_diffs = []
    diff_rows = 0
    for key in sorted(set(expected) & set(actual)):
        exp, act = _effective(expected[key]), _effective(actual[key])
        diffs = {
            f: {"expected": exp.get(f), "actual": act.get(f)}
            for f in sorted(set(exp) & set(act))
            if exp[f] != act[f]
        }
        missing_fields = sorted(set(exp) - set(act))
        extra_fields = sorted(set(act) - set(exp))
        if diffs or missing_fields:
            diff_rows += 1
        if diffs or missing_fields or extra_fields:
            row = _key_str(key)
            row.update(
                {
                    "checksum_expected": row_checksum(expected[key]),
                    "checksum_actual": row_checksum(actual[key]),
                    "diffs": diffs,
                    "missing_fields": missing_fields,
                    "extra_fields": extra_fields,  # informational, non-fatal
                }
            )
            field_diffs.append(row)
    return {
        "container": container,
        "expected_count": len(expected),
        "actual_count": len(actual),
        "missing_in_target": [_key_str(k) for k in missing],
        "extra_in_target": [_key_str(k) for k in extra],
        "field_diffs": field_diffs,
        "diff_rows": diff_rows,
        "ok": not missing and not extra and diff_rows == 0,
    }


def reconcile(source_rows: dict[str, list[dict]], get_container: Callable,
              *, now: int | None = None) -> dict:
    """Full comparison. ``source_rows`` = {source name: raw snapshot rows}."""
    expected_by_container = build_expected(source_rows, now=now)
    actual_by_container = read_target(get_container, sorted(expected_by_container))

    sources: dict[str, dict] = {}
    totals = {"expected": 0, "actual": 0, "missing": 0, "extra": 0, "diff_rows": 0}
    ok = True
    for name in SOURCE_ORDER:
        if name not in source_rows:
            continue
        per_container = {}
        source_ok = True
        for container in get_source(name).containers:
            cmp_res = compare_container(
                container,
                expected_by_container.get(container, {}),
                actual_by_container.get(container, {}),
            )
            per_container[container] = cmp_res
            totals["expected"] += cmp_res["expected_count"]
            totals["actual"] += cmp_res["actual_count"]
            totals["missing"] += len(cmp_res["missing_in_target"])
            totals["extra"] += len(cmp_res["extra_in_target"])
            totals["diff_rows"] += cmp_res["diff_rows"]
            source_ok = source_ok and cmp_res["ok"]
        sources[name] = {"containers": per_container, "ok": source_ok}
        ok = ok and source_ok

    from datetime import datetime, timezone

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": ok,
        "totals": totals,
        "sources": sources,
    }


def render_report(report: dict) -> str:
    lines = [f"reconcile ok: {report['ok']}", f"totals: {report['totals']}"]
    for name in SOURCE_ORDER:
        src = report["sources"].get(name)
        if not src:
            continue
        lines.append(f"[{name}] ok={src['ok']}")
        for container, res in sorted(src["containers"].items()):
            lines.append(
                f"  {container}: expected={res['expected_count']} "
                f"actual={res['actual_count']} "
                f"missing={len(res['missing_in_target'])} "
                f"extra={len(res['extra_in_target'])} "
                f"diff_rows={res['diff_rows']} ok={res['ok']}"
            )
            for k in res["missing_in_target"]:
                lines.append(f"    missing: {k}")
            for k in res["extra_in_target"]:
                lines.append(f"    extra:   {k}")
            for fd in res["field_diffs"]:
                lines.append(
                    f"    field-diff {fd['partition_key']!r}/{fd['id']!r}: "
                    f"diffs={sorted(fd['diffs'])} missing_fields={fd['missing_fields']} "
                    f"extra_fields={fd['extra_fields']}"
                )
    return "\n".join(lines)


def load_snapshots(snapshots_dir: str, sources: list[str]) -> dict[str, list[dict]]:
    """Read ``<source>.jsonl`` raw snapshots from a directory."""
    out: dict[str, list[dict]] = {}
    for name in sources:
        path = os.path.join(snapshots_dir, f"{name}.jsonl")
        out[name] = read_jsonl(path) if os.path.exists(path) else []
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="U-404 §9.3 stage 4: reconcile source snapshots vs target state.",
    )
    parser.add_argument("--snapshots", required=True, help="directory with <source>.jsonl snapshots")
    parser.add_argument("--sources", default=",".join(SOURCE_ORDER),
                        help="comma-separated source subset (default: all five)")
    parser.add_argument("--target", choices=("fake", "real"), default="fake",
                        help="'fake' = in-memory FakeCosmos (default); 'real' = storage.cosmos")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(argv)

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in sources if s not in SOURCE_ORDER]
    if unknown:
        print(f"error: unknown sources {unknown} (expected {list(SOURCE_ORDER)})")
        return 1

    source_rows = load_snapshots(args.snapshots, sources)
    get_container, label = resolve_container_provider(args.target)
    report = reconcile(source_rows, get_container)
    report["target"] = label

    if args.json:
        print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    else:
        print(f"target: {label}")
        print(render_report(report))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
