#!/usr/bin/env python3
"""One-shot, idempotent migration: disk demos (data/) -> DynamoDB DemosTable.

Purpose (tech design M3 / T4)
-----------------------------
Part 2 of the "Demo 入库 DynamoDB" effort makes the ``DemosTable`` the single
source of truth for demo configs, served via :class:`demo_store.DemoStore`.
Because the table starts empty, the on-disk ``data/<id>/manifest.yaml`` demos
must be seeded into it exactly once after the table is created. This script is
that seeder. **Run it once, after the table exists and the bot is restarted,
but BEFORE taking real calls** (an empty table degrades the demo list /
falls back at call time — see the tech-design "迁移顺序" risk).

What it does
~~~~~~~~~~~~
1. ``DemoLoader(<path>)`` scans ``data/`` (or ``--path``) and produces the
   canonical demo dicts (already run through the SHARED
   ``demo_loader.normalize_demo_dict`` helper, so the tool-list key is
   ``tool_ids`` — **never** ``tools``).
2. For each demo, ``DemoStore.put(demo)`` writes one DDB item.
   ``put`` re-normalizes through the SAME shared helper and stores natively
   (Cosmos JSON; the DDB-era ``bot._to_ddb`` Decimal serialization retired
   with F-16), so the written item's key shape is
   byte-identical to what ``DemoStore.get`` reads back — ``tool_ids`` and
   all of ``system / greeting / kb_body / kb_intro / kb_ack / mcp_servers /
   filler / tags`` are preserved. We do NOT touch the runtime put/get logic;
   we only call it.
3. Idempotency: by default an ``id`` already present in the table is
   **skipped** (printed ``skip``). ``--overwrite`` force-replaces it
   (printed ``overwrite``). New ids are always written (``migrate``).
4. A ``migrated / skipped`` listing plus totals are printed at the end.

CLI
~~~
::

    python scripts/migrate_demos_to_ddb.py             # migrate, skip existing
    python scripts/migrate_demos_to_ddb.py --overwrite # force-replace existing
    python scripts/migrate_demos_to_ddb.py --path /some/data   # custom data root
    python scripts/migrate_demos_to_ddb.py --table my-demos    # custom table name

The target is the Cosmos ``demos`` container (§1.1; B10 retired the
``DEMOS_TABLE`` table-name env),
overridable with ``--table``. AWS region follows the same resolution as
:mod:`demo_store` (``DDB_REGION`` -> ``AWS_REGION`` -> ``us-east-1``).

Runtime note
~~~~~~~~~~~~~
This script does NOT modify ``bot.py`` / ``demo_store.py`` runtime behavior —
it only *imports* :class:`demo_loader.DemoLoader` and :class:`demo_store.DemoStore`
and calls their public surface. It is not invoked at runtime.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

# Make the project root importable so ``import demo_loader`` / ``demo_store``
# resolve regardless of cwd (mirrors scripts/migrate_demo_tools.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from demo_loader import DemoLoader  # noqa: E402  (after sys.path tweak)
from demo_store import DemoStore  # noqa: E402

logger = logging.getLogger("migrate_demos_to_ddb")

DEFAULT_DATA_ROOT = os.path.join(_PROJECT_ROOT, "data")


async def migrate(
    *,
    data_root: str,
    store: DemoStore,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Migrate all demos under ``data_root`` into ``store`` (DemosTable).

    Returns a summary dict::

        {
          "migrated": [ids written for the first time],
          "overwritten": [ids force-replaced because of --overwrite],
          "skipped": [{"id": ..., "reason": "exists"}],
          "total": <demos discovered on disk>,
        }

    Idempotent: an id already present in the table is skipped unless
    ``overwrite`` is True. The store's cache is (re)scanned once up front to
    learn which ids already exist; we do NOT mutate runtime put/get logic.
    """
    loader = DemoLoader(data_root)
    demos = [loader.get(s["id"]) for s in loader.list()]
    demos = [d for d in demos if d is not None]

    if loader.last_skipped:
        for s in loader.last_skipped:
            logger.warning(
                "loader skipped demo %s: %s", s.get("id"), s.get("reason")
            )

    # Learn which ids already live in the table (degrades to empty on a
    # missing/unreachable table — every demo is then a fresh migrate).
    await store.rescan()
    existing_ids = {s["id"] for s in store.list()}

    migrated: list[str] = []
    overwritten: list[str] = []
    skipped: list[dict[str, str]] = []

    for demo in demos:
        demo_id = demo.get("id")
        if not demo_id:
            skipped.append({"id": "?", "reason": "demo missing id"})
            continue
        if demo_id in existing_ids and not overwrite:
            print(f"  [{'skip':>10}] {demo_id} (already in table)")
            skipped.append({"id": demo_id, "reason": "exists"})
            continue
        await store.put(demo)
        if demo_id in existing_ids:
            print(f"  [{'overwrite':>10}] {demo_id}")
            overwritten.append(demo_id)
        else:
            print(f"  [{'migrate':>10}] {demo_id}")
            migrated.append(demo_id)

    return {
        "migrated": migrated,
        "overwritten": overwritten,
        "skipped": skipped,
        "total": len(demos),
    }


def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    data_root = os.path.abspath(args.path)
    store = DemoStore(table_name=args.table)
    print(f"[migrate_demos_to_ddb] data root : {data_root}")
    print(f"[migrate_demos_to_ddb] table     : {store._table_name}")
    print(f"[migrate_demos_to_ddb] overwrite : {args.overwrite}")

    summary = asyncio.run(
        migrate(data_root=data_root, store=store, overwrite=args.overwrite)
    )

    print(
        f"[migrate_demos_to_ddb] summary: "
        f"{len(summary['migrated'])} migrated, "
        f"{len(summary['overwritten'])} overwritten, "
        f"{len(summary['skipped'])} skipped "
        f"(of {summary['total']} on disk)"
    )
    if summary["migrated"]:
        print(f"  migrated   : {', '.join(summary['migrated'])}")
    if summary["overwritten"]:
        print(f"  overwritten: {', '.join(summary['overwritten'])}")
    if summary["skipped"]:
        print(
            "  skipped    : "
            + ", ".join(f"{s['id']}({s['reason']})" for s in summary["skipped"])
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "One-shot, idempotent migration of disk demos (data/) into the "
            "DynamoDB DemosTable via DemoStore.put. Skips existing ids unless "
            "--overwrite. Preserves the tool_ids key shape."
        )
    )
    parser.add_argument(
        "--path",
        default=DEFAULT_DATA_ROOT,
        help="Demo data root to read from (default: <project>/data).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Force-replace demos whose id already exists in the table "
        "(default: skip existing ids).",
    )
    parser.add_argument(
        "--table",
        default=None,
        help="Demos container name (default: Cosmos container 'demos', §1.1 — B10 retired the DEMOS_TABLE env).",
    )
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except Exception as e:  # pragma: no cover — surfaces traceback to operator
        logger.exception("migrate_demos_to_ddb failed")
        print(f"[migrate_demos_to_ddb] ERROR: {type(e).__name__}: {e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
