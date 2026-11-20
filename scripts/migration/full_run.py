"""Stage 6 of spec §9.3: full-run — export -> transform -> test-import -> reconcile.

Orchestrates the four stages per source and writes a MANIFEST that records
every row the run produced — the manifest is the input to the rollback stage
(spec §9.3-7 retains the snapshots as rollback exports under the approved
retention policy).

The default target is the in-memory FakeCosmos with ``--apply`` on: the full
pipeline runs as a self-contained rehearsal. ``--target real`` is the cutover
path (storage.cosmos) and requires ``--apply`` plus user authorization (D-8
gate) — nothing here runs against AWS/Azure without it.

Usage::

    python -m scripts.migration.full_run --workdir runs/cutover
    python -m scripts.migration.full_run --workdir runs/cutover --sources users,demos
    python -m scripts.migration.full_run --workdir runs/cutover --target real --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Callable

if __package__ in (None, ""):  # plain `python scripts/migration/full_run.py`
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )

from scripts.migration.common import (  # noqa: E402
    atomic_write_json,
    read_jsonl,
    resolve_container_provider,
    write_jsonl,
)
from scripts.migration.export import (  # noqa: E402
    DEFAULT_PAGE_SIZE,
    DynamoClient,
    _now_iso,
    export_source,
)
from scripts.migration.reconcile import reconcile  # noqa: E402
from scripts.migration.test_import import load_transformed  # noqa: E402
from scripts.migration.transform import (  # noqa: E402
    CONTAINER_PK,
    SOURCE_ORDER,
    TransformError,
    get_source,
    transform_record,
)


def run_full(
    client: DynamoClient,
    get_container: Callable,
    workdir: str,
    *,
    sources: list[str] | None = None,
    table_overrides: dict[str, str] | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    apply: bool = True,
    run_id: str | None = None,
    now: int | None = None,
    target_label: str = "injected",
) -> dict:
    """Run the four stages for every requested source; return the manifest."""
    sources = [s for s in SOURCE_ORDER if not sources or s in sources]
    table_overrides = table_overrides or {}
    started_at = _now_iso()
    run_id = run_id or started_at

    manifest: dict = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": None,
        "target": target_label,
        "applied": bool(apply),
        "sources": {},
        "rows": [],
        "reconcile": None,
        "ok": False,
    }
    source_rows: dict[str, list[dict]] = {}
    rows_index: dict[tuple, dict] = {}
    ok = True

    for name in sources:
        spec = get_source(name)
        export_res = export_source(
            client, name, workdir,
            table=table_overrides.get(name), page_size=page_size,
        )
        snapshot = export_res.get("snapshot") or os.path.join(
            workdir, "snapshots", f"{name}.jsonl"
        )
        raw_rows = read_jsonl(snapshot) if os.path.exists(snapshot) else []
        source_rows[name] = raw_rows

        # transform (pure) -> transformed JSONL
        envelopes: list[dict] = []
        rejects: list[dict] = []
        for raw in raw_rows:
            try:
                envelopes.extend(transform_record(name, raw, now=now))
            except TransformError as e:
                rejects.append({"record": str(e)})
        envelopes.sort(key=lambda e: (e["key"], e["container"], e["item"].get("id") or ""))
        transformed_path = os.path.join(
            workdir, "transformed", f"{name}.transformed.jsonl"
        )
        write_jsonl(transformed_path, envelopes)

        # test-import
        import_res = load_transformed(envelopes, get_container, apply=apply)
        for env in envelopes:
            if env["item"].get("id") and env["item"].get(CONTAINER_PK[env["container"]]):
                key = (env["container"], env["item"]["id"])
                rows_index[key] = {
                    "source": name,
                    "container": env["container"],
                    "id": env["item"]["id"],
                    "partition_key": env["item"][CONTAINER_PK[env["container"]]],
                }

        manifest["sources"][name] = {
            "table": export_res.get("table"),
            "snapshot": export_res.get("snapshot") or snapshot,
            "checkpoint": export_res.get("checkpoint"),
            "transformed": transformed_path,
            "counts": {
                "exported": export_res.get("exported", 0),
                "transformed_rows": len(envelopes),
                "transform_rejects": len(rejects),
                "accepted": import_res["accepted"],
                "rejected": import_res["rejected"],
                "wrote": import_res["wrote"],
            },
            "rejections": rejects + import_res["rejections"],
        }
        ok = ok and not rejects and import_res["rejected"] == 0

    manifest["rows"] = [rows_index[k] for k in sorted(rows_index)]

    # reconcile (meaningful once rows were applied; skipped otherwise)
    if apply:
        manifest["reconcile"] = reconcile(source_rows, get_container, now=now)
        ok = ok and manifest["reconcile"]["ok"]
    else:
        manifest["reconcile"] = {"skipped": "test-import not applied (--apply off)"}
        ok = ok and True

    manifest["ok"] = bool(ok)
    manifest["finished_at"] = _now_iso()
    manifest_path = os.path.join(workdir, "manifest.json")
    atomic_write_json(manifest_path, manifest)
    manifest["manifest_path"] = manifest_path
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="U-404 §9.3 stage 6: full-run — export/transform/test-import/reconcile "
        "with a manifest (rollback input).",
    )
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--sources", default=",".join(SOURCE_ORDER),
                        help="comma-separated subset (default: all five)")
    parser.add_argument("--table", action="append", default=[],
                        metavar="SOURCE=NAME", help="table override, repeatable")
    parser.add_argument("--region", default=None)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--target", choices=("fake", "real"), default="fake",
                        help="'fake' = in-memory FakeCosmos rehearsal (default); "
                             "'real' = storage.cosmos cutover path")
    parser.add_argument("--apply", action="store_true",
                        help="write rows into the target (required for --target real)")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in sources if s not in SOURCE_ORDER]
    if unknown:
        print(f"error: unknown sources {unknown} (expected {list(SOURCE_ORDER)})")
        return 1
    table_overrides = {}
    for raw in args.table:
        if "=" not in raw:
            print(f"error: --table expects SOURCE=NAME, got {raw!r}")
            return 1
        src, _, name = raw.partition("=")
        table_overrides[src.strip()] = name.strip()

    if args.target == "real" and not args.apply:
        print("error: --target real requires --apply "
              "(D-8 gate: real writes need explicit user authorization)")
        return 1

    from scripts.migration.export import Boto3DynamoClient  # lazy: boto3 optional

    get_container, label = resolve_container_provider(args.target)
    manifest = run_full(
        Boto3DynamoClient(region=args.region),
        get_container,
        args.workdir,
        sources=sources,
        table_overrides=table_overrides,
        page_size=args.page_size,
        apply=args.apply or args.target == "fake",  # fake rehearsal always applies
        run_id=args.run_id,
        target_label=label,
    )

    if args.json:
        print(json.dumps(manifest, sort_keys=True, ensure_ascii=False))
    else:
        print(f"run_id:   {manifest['run_id']}")
        print(f"target:   {manifest['target']}")
        print(f"manifest: {manifest['manifest_path']}")
        for name, res in manifest["sources"].items():
            print(f"  {name}: {res['counts']}")
        print(f"rows:     {len(manifest['rows'])}")
        print(f"ok:       {manifest['ok']}")
    return 0 if manifest["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
