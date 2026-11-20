"""Stage 7 of spec §9.3: rollback — remove/restore the rows of a migration run.

*** DESTRUCTIVE — READ BEFORE USE ***

``remove`` deletes exactly the target rows recorded in the full-run manifest's
``rows`` index (container + id + partition key) — nothing else. ``restore``
re-upserts those rows from the run's retained transformed JSONL (undoing a
remove). Both modes default to a DRY-RUN listing; execution requires the
explicit ``--yes`` confirmation flag (house style: what-if first).

Spec §9.3-8: BEFORE rolling back, reconcile writes created on Azure since the
migration — routing calls back to AWS alone does not synchronize data. The
rollback exports (snapshots/transformed JSONL) are retained under the approved
retention policy (§9.3-7); ``restore`` depends on them still being present.

Usage::

    python -m scripts.migration.rollback --manifest runs/cutover/manifest.json
    python -m scripts.migration.rollback --manifest runs/cutover/manifest.json --yes
    python -m scripts.migration.rollback --manifest ... --mode restore --yes
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Callable

if __package__ in (None, ""):  # plain `python scripts/migration/rollback.py`
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )

from scripts.migration.common import read_json, read_jsonl, resolve_container_provider  # noqa: E402
from scripts.migration.transform import validate_envelope  # noqa: E402


def rollback_manifest(
    manifest: dict,
    get_container: Callable,
    *,
    mode: str = "remove",
    apply: bool = False,
    base_dir: str | None = None,
) -> dict:
    """Execute (or dry-run) a rollback for one manifest.

    Returns ``{"mode", "planned", "done", "missing", "rejections", "ok"}``.
    ``remove`` is idempotent: rows already gone count as ``missing`` (not an
    error). ``restore`` upserts every transformed row of the run.
    """
    if mode not in ("remove", "restore"):
        raise ValueError(f"unknown rollback mode {mode!r}")
    rows = manifest.get("rows") or []
    report: dict = {
        "mode": mode,
        "run_id": manifest.get("run_id"),
        "planned": len(rows),
        "done": 0,
        "missing": 0,
        "rejections": [],
        "applied": bool(apply),
        "ok": True,
    }

    if mode == "remove":
        from storage.cosmos import ContainerNotFound

        for row in rows:
            container = row.get("container")
            item_id = row.get("id")
            pk = row.get("partition_key")
            if not (container and item_id and pk):
                report["rejections"].append({"row": row, "reason": "malformed manifest row"})
                continue
            if not apply:
                report["done"] += 1  # planned deletions
                continue
            try:
                get_container(container).delete_item(item_id, pk)
                report["done"] += 1
            except ContainerNotFound:
                report["missing"] += 1
    else:  # restore
        seen: set = set()
        for name, src in sorted((manifest.get("sources") or {}).items()):
            path = src.get("transformed")
            if not path:
                continue
            if not os.path.isabs(path) and base_dir:
                path = os.path.join(base_dir, path)
            if not os.path.exists(path):
                report["rejections"].append({"source": name, "reason": f"missing {path}"})
                continue
            for env in read_jsonl(path):
                reason = validate_envelope(env)
                if reason:
                    report["rejections"].append(
                        {"source": name, "key": env.get("key"), "reason": reason}
                    )
                    continue
                key = (env["container"], env["item"]["id"])
                if key in seen:
                    continue
                seen.add(key)
                if not apply:
                    report["done"] += 1
                    continue
                get_container(env["container"]).upsert_item(env["item"])
                report["done"] += 1

    report["ok"] = not report["rejections"]
    return report


def render_report(report: dict) -> str:
    verb = "deleted" if report["mode"] == "remove" else "restored"
    if not report["applied"]:
        verb = f"would {verb}"
    lines = [
        f"mode:     {report['mode']}",
        f"run_id:   {report['run_id']}",
        f"applied:  {report['applied']}",
        f"planned:  {report['planned']}",
        f"{verb}:   {report['done']}",
        f"missing:  {report['missing']}",
    ]
    for rej in report["rejections"]:
        lines.append(f"  reject: {rej}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="U-404 §9.3 stage 7: rollback — DESTRUCTIVE; removes the rows a "
        "migration run wrote (or restores them). Dry-run by default; execution "
        "requires --yes. Reconcile Azure-created writes first (spec §9.3-8).",
    )
    parser.add_argument("--manifest", required=True, help="full-run manifest.json")
    parser.add_argument("--mode", choices=("remove", "restore"), default="remove")
    parser.add_argument("--yes", action="store_true",
                        help="EXPLICIT confirmation: actually delete/restore rows")
    parser.add_argument("--target", choices=("fake", "real"), default="fake",
                        help="'fake' = in-memory FakeCosmos (default); 'real' = storage.cosmos")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    manifest = read_json(args.manifest)
    base_dir = os.path.dirname(os.path.abspath(args.manifest))
    get_container, label = resolve_container_provider(args.target)

    if not args.yes:
        print("DRY-RUN (no changes written). This operation is DESTRUCTIVE — "
              "re-run with --yes to execute.")
    report = rollback_manifest(
        manifest, get_container, mode=args.mode, apply=args.yes, base_dir=base_dir
    )
    report["target"] = label

    if args.json:
        print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    else:
        print(f"target:   {label}")
        print(render_report(report))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
