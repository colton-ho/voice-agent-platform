"""Demo store: Cosmos-backed demo configs preserving DemoLoader's interface.

Drop-in replacement for :class:`demo_loader.DemoLoader` as the ``DEMO_LOADER``
singleton in bot.py — the public surface is byte-identical so no call site
changes:

    list()  -> [{id, label, lang, kb_chars}]   (sorted by id, sync, non-blocking)
    get(id) -> full demo dict | None            (sync, non-blocking)
    last_skipped : list[{id, reason}]

Two differences from DemoLoader, both additive:

  * ``rescan()`` is **async** (it queries the container), whereas
    DemoLoader.rescan is sync. The only caller of DemoStore.rescan is the
    FastAPI ``lifespan`` startup hook, which awaits it. ``get``/``list`` stay
    synchronous and read the in-memory cache, so every per-call site in bot.py
    (which only ever calls ``get``/``list``) is unchanged.
  * ``put(demo_dict)`` is new (DemoLoader had no writer — it scanned disk). It
    persists one demo as a single document.

Storage goes through the ``storage.cosmos`` seam (container ``demos``,
pipeline-infra-spec §1.1): PK /type = constant ``"demo"`` (X3 ruling — a
single-partition list, the table is small), id=demo_id. Reads/writes wrap the
sync container verbs in ``asyncio.to_thread`` and degrade gracefully (log +
keep prior/empty cache) when the container is missing. The ``last_skipped``
attribute records the degrade reason for admin diagnostics, paralleling
DemoLoader.

Serialization
-------------
Cosmos JSON stores plain numbers natively — no Decimal conversion on write
(the DDB-era ``bot._to_ddb`` converter retired with F-16). On read, ``_from_ddb`` is kept as the
number-normalizing passthrough (pinned by tests) and ``_deserialize`` strips
storage-only keys (PK ``type`` + ``_``-prefixed system props) before the
SHARED normalizer so the cached dict is byte-identical to the manifest path.

Container shape
---------------
- Container: ``demos`` (§1.1 frozen). Partition key ``type`` (constant
  ``"demo"``), id = demo id.
- Document: the full canonical demo dict (system/greeting/kb_* per-lang maps,
  kb_body str|map, tool_ids list, mcp_servers list, filler map, tags list).
"""

from __future__ import annotations

import asyncio
import logging
import os
from decimal import Decimal
from typing import Any

from demo_loader import kb_chars_of, normalize_demo_dict
from storage import cosmos
from storage.cosmos import ContainerNotFound

logger = logging.getLogger(__name__)

# X3：demos 容器單分區常量（PK /type）
DEMO_PARTITION_VALUE = "demo"


def _from_ddb(value: Any) -> Any:
    """Recursively convert DynamoDB Decimals back to plain Python numbers.

    Integral Decimals -> int, fractional -> float. Lists / dicts are walked;
    other scalars pass through unchanged. Kept as the read-path number
    normalizer (pinned by tests); Cosmos JSON carries native numbers so this
    is a passthrough there."""
    if isinstance(value, Decimal):
        # Integral values (e.g. timeout_ms) -> int; fractional -> float.
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_from_ddb(v) for v in value]
    if isinstance(value, dict):
        return {k: _from_ddb(v) for k, v in value.items()}
    return value


class DemoStore:
    """Cosmos-backed demo store with the DemoLoader public interface.

    Containers resolve through ``storage.cosmos`` on first use so constructing
    a ``DemoStore`` never touches Azure (cheap to import — bot.py builds the
    singleton at import time). The cache starts empty; the FastAPI lifespan
    startup hook ``await``s :meth:`rescan` to prewarm it.
    """

    def __init__(self, table_name: str | None = None, region: str | None = None):
        self._table_name = table_name or cosmos.DEMOS
        self._cache: dict[str, dict[str, Any]] = {}
        self.last_skipped: list[dict[str, str]] = []

    def _demos(self):
        return cosmos.get_container(cosmos.DEMOS)

    @staticmethod
    def _is_missing(exc: Exception) -> bool:
        return isinstance(exc, ContainerNotFound)

    # ---- deserialization --------------------------------------------------

    @staticmethod
    def _deserialize(item: dict[str, Any]) -> dict[str, Any]:
        """Container document -> canonical demo dict (same key shape as
        DemoLoader.get).

        Storage-only keys (PK ``type``, ``_``-prefixed system props) are
        stripped first so they can never leak into the canonical shape; then
        through the SHARED normalizer so the ``tool_ids`` key (NOT ``tools``)
        and all other keys are byte-identical to the manifest path.
        ``validate_tools=False``: stored ids were already validated when
        written, and a registry change must not silently drop them on read.
        """
        clean = {k: v for k, v in item.items() if not k.startswith("_") and k != "type"}
        return normalize_demo_dict(_from_ddb(clean), validate_tools=False)

    # ---- public API (sync reads — same signatures as DemoLoader) ----------

    def list(self) -> list[dict[str, Any]]:
        """Return demo summaries {id, label, lang, kb_chars}, sorted by id.

        Reads the in-memory cache synchronously (non-blocking). Empty cache
        (before prewarm, or after a degrade) -> []."""
        out = [
            {
                "id": demo["id"],
                "label": demo["label"],
                "lang": demo["lang"],
                "kb_chars": kb_chars_of(demo.get("kb_body")),
            }
            for demo in self._cache.values()
        ]
        out.sort(key=lambda x: x["id"] or "")
        return out

    def get(self, demo_id: str) -> dict[str, Any] | None:
        """Return the full demo dict for ``demo_id`` or None. Synchronous,
        reads the cache. Empty cache / unknown id -> None (never raises)."""
        if not demo_id:
            return None
        return self._cache.get(demo_id)

    # ---- prewarm / refresh (async) ----------------------------------------

    async def rescan(self) -> int:
        """Query the ``demos`` container -> rebuild the in-memory cache.
        Returns the number of demos loaded.

        Container-missing / query error -> record ``last_skipped`` and DEGRADE:
        the existing cache (prior contents, or empty at startup) is KEPT, and
        the previous count is returned. Never raises, so a storage outage at
        startup does not block the app (mirrors user_store's degrade
        philosophy)."""
        self.last_skipped = []

        def _query() -> list[dict[str, Any]]:
            # X3 單分區：全部 demo 都在 PK 值 "demo" 分區裡
            return self._demos().query_items(
                "SELECT * FROM c WHERE c.type = @type",
                [{"name": "@type", "value": DEMO_PARTITION_VALUE}],
                partition_key=DEMO_PARTITION_VALUE,
            )

        try:
            items = await asyncio.to_thread(_query)
        except Exception as e:
            reason = (
                f"table {self._table_name!r} missing"
                if self._is_missing(e)
                else f"query failed: {type(e).__name__}: {e}"
            )
            logger.warning(
                f"demo_store: {reason}; keeping prior cache "
                f"({len(self._cache)} demos)"
            )
            self.last_skipped.append({"id": "*", "reason": reason})
            return len(self._cache)

        new_cache: dict[str, dict[str, Any]] = {}
        for item in items:
            try:
                demo = self._deserialize(item)
            except Exception as e:  # pragma: no cover — defensive
                bad_id = item.get("id", "?")
                reason = f"deserialize failed: {type(e).__name__}: {e}"
                logger.warning(f"demo_store: demo {bad_id!r}: {reason}; skipping")
                self.last_skipped.append({"id": str(bad_id), "reason": reason})
                continue
            demo_id = demo.get("id")
            if not demo_id:
                self.last_skipped.append({"id": "?", "reason": "item missing id"})
                continue
            if demo_id in new_cache:
                reason = f"duplicate id {demo_id}"
                logger.warning(f"demo_store: {reason}; skipping")
                self.last_skipped.append({"id": demo_id, "reason": reason})
                continue
            new_cache[demo_id] = demo

        self._cache = new_cache
        logger.info(
            f"demo_store: scanned {self._table_name}, loaded {len(new_cache)} demos, "
            f"skipped {len(self.last_skipped)}"
        )
        return len(new_cache)

    # ---- write ------------------------------------------------------------

    async def put(self, demo_dict: dict[str, Any]) -> None:
        """Persist one demo as a single document (upsert).

        The dict is normalized through the SHARED helper (guaranteeing the
        ``tool_ids`` key shape on disk too); numbers are native JSON (no
        Decimal conversion — Cosmos serializes plain JSON). Raises ValueError
        if the demo has no ``id`` (the document id). Does NOT refresh the
        cache; the caller (PATCH endpoint / migration) calls ``rescan``
        afterwards."""
        if not isinstance(demo_dict, dict) or not demo_dict.get("id"):
            raise ValueError("demo_dict must be a dict with a non-empty 'id'")

        canonical = normalize_demo_dict(demo_dict, validate_tools=False)
        item = dict(canonical)
        # X3 分區鍵常量；显式赋值压过任何同名输入键
        item["type"] = DEMO_PARTITION_VALUE
        item["id"] = canonical["id"]

        def _call():
            return self._demos().upsert_item(item)

        await asyncio.to_thread(_call)
