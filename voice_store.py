"""Voice store: Cosmos-backed editable voice registry (Part A of the
voice-registry + activity-log proposal).

Mirrors :class:`demo_store.DemoStore` — sync container verbs wrapped in
``asyncio.to_thread`` so the event loop never blocks, an in-memory ``_cache``,
a **lazy** container accessor (resolved on first use, like DemoStore, so
constructing the singleton at import time never touches Azure), and graceful
degradation when the container is missing. Two additions over DemoStore, both
new here:

  * ``delete(provider, voice_id)`` — removes one document (idempotent).
  * ``seed_if_empty(constants)`` — batch-writes the three in-code constant
    dicts (MINIMAX_VOICES / POLLY_VOICES / NOVA_SONIC_VOICES, incl. the
    ``DEFAULT_*`` voice ids) ONCE, when the live container is reachable AND
    empty.

Container shape
---------------
- Container: ``voices`` (§1.1 frozen). Partition key ``provider`` ∈ {minimax,
  polly, nova-sonic}; id = ``voice_id``. ``(provider, voice_id)`` is unique.
- Document: ``id`` + ``provider`` + ``voice_id`` + the provider-specific attrs
  (``label``/``gender``/``language``|``locale``/``boost``/``engine``/
  ``polyglot``/``lang_label`` …).

Liveness / fallback
-------------------
``rescan()`` sets ``live=True`` when the query succeeds and ``live=False`` when
the container is missing. ``bot.voices_for(provider)`` consults ``live`` to
choose between the live registry and the in-code constant dict, so shipping
this code is **zero-impact** until the container exists: with no container,
``live`` stays False and every resolver reads the same constants as before
(byte-identical).

Serialization
-------------
Cosmos JSON stores plain numbers natively (the DDB-era ``bot._to_ddb`` write
converter retired with F-16).
``demo_store._from_ddb`` stays as the read-path number normalizer so e.g. a
MiniMax ``boost`` that was stored numeric comes back as int/float.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from demo_store import _from_ddb
from storage import cosmos
from storage.cosmos import ContainerNotFound

logger = logging.getLogger(__name__)

# The three providers that share the container. Used to validate the partition
# key and to iterate the constant dicts during seeding.
VOICE_PROVIDERS = ("minimax", "polly", "nova-sonic")


class VoiceStore:
    """Cosmos-backed voice registry mirroring DemoStore's shape.

    Containers resolve through ``storage.cosmos`` on first use so constructing
    a ``VoiceStore`` never touches Azure (bot.py builds the singleton at
    import time). The cache starts empty and ``live`` starts False; the FastAPI
    lifespan startup hook ``await``s :meth:`rescan` (then :meth:`seed_if_empty`)
    to prewarm it.
    """

    def __init__(self, table_name: str | None = None, region: str | None = None):
        self._table_name = table_name or cosmos.VOICES
        # provider -> {voice_id -> attrs}. attrs DOES NOT include the key
        # columns / document id (provider/voice_id/id) — they are stripped on
        # read so the cached dict matches the in-code constant dict shape
        # exactly.
        self._cache: dict[str, dict[str, dict[str, Any]]] = {
            p: {} for p in VOICE_PROVIDERS
        }
        # Drives bot.voices_for(): True once a scan succeeds, False when the
        # container is missing (so callers fall back to the in-code constants).
        self.live: bool = False
        self.last_skipped: list[dict[str, str]] = []

    def _voices(self):
        return cosmos.get_container(cosmos.VOICES)

    @staticmethod
    def _is_missing(exc: Exception) -> bool:
        return isinstance(exc, ContainerNotFound)

    # ---- deserialization --------------------------------------------------

    @staticmethod
    def _split_item(item: dict[str, Any]) -> tuple[str | None, str | None, dict[str, Any]]:
        """Container document -> (provider, voice_id, attrs).

        Numbers are normalized first; the key columns and the document id are
        stripped from ``attrs`` (``id`` == voice_id) so the cached attr dict
        matches the shape of the in-code constant dict (which has NO
        provider/voice_id/id keys)."""
        clean = {k: v for k, v in _from_ddb(item).items() if not k.startswith("_")}
        provider = clean.pop("provider", None)
        voice_id = clean.pop("voice_id", None)
        clean.pop("id", None)
        return provider, voice_id, clean

    # ---- public API (sync reads — read the in-memory cache) ---------------

    def list(self, provider: str | None = None) -> dict[str, dict[str, Any]]:
        """Return ``{voice_id: attrs}`` for one provider (the shape of the
        in-code constant dict). ``provider=None`` -> the full nested
        ``{provider: {voice_id: attrs}}`` map. Synchronous, reads the cache;
        unknown provider / empty cache -> ``{}`` (never raises)."""
        if provider is None:
            return {p: dict(v) for p, v in self._cache.items()}
        return dict(self._cache.get(provider, {}))

    def get(self, provider: str, voice_id: str) -> dict[str, Any] | None:
        """Return one voice's attr dict or None. Synchronous, reads the cache."""
        if not provider or not voice_id:
            return None
        return self._cache.get(provider, {}).get(voice_id)

    # ---- prewarm / refresh (async) ----------------------------------------

    async def rescan(self) -> int:
        """Cross-partition full read -> rebuild the cache (§1.1 allows the
        cross-partition read here — seeding/rescan is a rare admin-path op).
        Returns the number of voices loaded.

        On success sets ``live=True``. Container-missing -> log a warning, set
        ``live=False``, record ``last_skipped``, KEEP the prior cache, and
        return ``-1`` so callers fall back to the in-code constants (matches
        the tech-design A.3 contract). Any other query error degrades the same
        way but is recorded as a scan failure. Never raises."""
        self.last_skipped = []

        def _query() -> list[dict[str, Any]]:
            return self._voices().query_items("SELECT * FROM c", [])

        try:
            items = await asyncio.to_thread(_query)
        except Exception as e:
            if self._is_missing(e):
                reason = f"table {self._table_name!r} missing"
                # Container absent -> NOT live -> voices_for() falls back to the
                # in-code constants. Return -1 as the explicit not-live signal.
                self.live = False
                logger.warning(
                    f"voice_store: {reason}; falling back to in-code constants"
                )
                self.last_skipped.append({"id": "*", "reason": reason})
                return -1
            reason = f"scan failed: {type(e).__name__}: {e}"
            # A transient query error keeps the prior cache + prior liveness so
            # a blip doesn't flip a healthy registry to constants.
            logger.warning(
                f"voice_store: {reason}; keeping prior cache "
                f"({sum(len(v) for v in self._cache.values())} voices, live={self.live})"
            )
            self.last_skipped.append({"id": "*", "reason": reason})
            return sum(len(v) for v in self._cache.values())

        new_cache: dict[str, dict[str, dict[str, Any]]] = {p: {} for p in VOICE_PROVIDERS}
        for item in items:
            provider, voice_id, attrs = self._split_item(item)
            if not provider or not voice_id:
                self.last_skipped.append({"id": "?", "reason": "item missing provider/voice_id"})
                continue
            if provider not in new_cache:
                # Unknown provider partition — keep it in the cache anyway so a
                # future provider doesn't silently vanish, but it won't be read
                # by voices_for (which only asks for the three known providers).
                new_cache[provider] = {}
            new_cache[provider][voice_id] = attrs

        self._cache = new_cache
        self.live = True
        total = sum(len(v) for v in new_cache.values())
        logger.info(
            f"voice_store: scanned {self._table_name}, loaded {total} voices "
            f"({ {p: len(v) for p, v in new_cache.items()} })"
        )
        return total

    # ---- writes -----------------------------------------------------------

    async def put(self, provider: str, voice_id: str, attrs: dict[str, Any]) -> None:
        """Persist one voice as a single document (upsert).

        ``attrs`` is the validated, provider-specific attr dict (NO provider /
        voice_id keys — they are added here as the key columns). Numbers stay
        native JSON so a numeric MiniMax ``boost`` round-trips. Raises ValueError
        on an empty provider / voice_id. Does NOT refresh the cache; the caller
        (admin endpoint / seeding) calls :meth:`rescan` afterwards."""
        if not provider or not voice_id:
            raise ValueError("put requires non-empty provider and voice_id")

        item: dict[str, Any] = {"provider": provider, "voice_id": voice_id}
        # attrs may legitimately carry a "provider"/"voice_id" key only if a
        # caller copied a full row; the explicit key columns win.
        for k, v in (attrs or {}).items():
            if k in ("provider", "voice_id"):
                continue
            item[k] = v
        item["id"] = voice_id  # Cosmos id == voice_id（显式赋值压过输入）

        def _call():
            return self._voices().upsert_item(item)

        await asyncio.to_thread(_call)

    async def delete(self, provider: str, voice_id: str) -> None:
        """Delete one voice document by (provider, voice_id). Idempotent: a
        missing row (or container) is a no-op. Does NOT refresh the cache; the
        caller calls :meth:`rescan` afterwards."""
        if not provider or not voice_id:
            raise ValueError("delete requires non-empty provider and voice_id")

        def _call():
            return self._voices().delete_item(item_id=voice_id, partition_key=provider)

        try:
            await asyncio.to_thread(_call)
        except ContainerNotFound:
            pass

    # ---- one-time seed ----------------------------------------------------

    async def seed_if_empty(self, constants: dict[str, dict[str, dict[str, Any]]]) -> int:
        """Batch-write the in-code constant dicts ONCE, iff the live container
        is reachable AND empty.

        ``constants`` is ``{provider: {voice_id: attrs}}`` (the three in-code
        dicts). Returns the number of voices written (0 if the container
        already had rows or is missing). Container missing -> no-op (returns
        0). Refreshes the cache (sets ``live``) via :meth:`rescan` first to
        decide emptiness, and again after writing so the cache reflects the
        seeded rows.

        Idempotent: a non-empty live container is never re-seeded, so an admin
        who deletes every voice will NOT get the constants re-injected on the
        next boot (deletion is intentional)."""
        # rescan establishes liveness + current contents.
        await self.rescan()
        if not self.live:
            # Container missing -> seeding is a no-op (constants are the fallback).
            return 0
        if any(self._cache.get(p) for p in VOICE_PROVIDERS):
            # Already has rows for at least one provider -> never re-seed.
            return 0

        written = 0
        for provider, voices in (constants or {}).items():
            for voice_id, attrs in (voices or {}).items():
                await self.put(provider, voice_id, attrs)
                written += 1
        if written:
            await self.rescan()
            logger.info(f"voice_store: seeded {written} voices from in-code constants")
        return written
