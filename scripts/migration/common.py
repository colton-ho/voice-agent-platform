"""Shared plumbing for the U-404 §9.3 migration tool suite.

Run-directory layout (see docs/azure/migration-runbook.md)::

    <workdir>/
      snapshots/<source>.jsonl              # raw source snapshot, sorted by business key
      snapshots/<source>.jsonl.partial      # in-progress scan (removed at finalize)
      snapshots/<source>.checkpoint.json    # resumable scan cursor + per-row change markers
      transformed/<source>.transformed.jsonl
      manifest.json                         # full-run manifest (rollback input)

Everything here is deterministic and network-free: JSONL lines use canonical
key-sorted JSON so identical inputs produce byte-identical files.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

# Fields excluded from checksum / reconcile field comparison:
# - ``ttl``        write-time-derived remaining seconds (§1.3; recomputed on
#                  every store write, so it legitimately differs per write);
# - ``_*``         Cosmos system properties (_etag/_rid/_ts/_self/...).
COMPARE_EXCLUDE = frozenset({"ttl"})
SYSTEM_PREFIX = "_"


def is_system_field(name: str) -> bool:
    return name.startswith(SYSTEM_PREFIX) or name in COMPARE_EXCLUDE


def canonical_json(value: Any) -> str:
    """Deterministic JSON encoding (sorted keys, no spaces, UTF-8 kept)."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )


def checksum(value: Any) -> str:
    """Checksum-ish identity of a record/row (spec §9.3-4 comparison aid)."""
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def row_checksum(item: dict) -> str:
    """Row checksum ignoring write-derived/system fields (see COMPARE_EXCLUDE)."""
    return checksum({k: v for k, v in item.items() if not is_system_field(k)})


def read_jsonl(path: str) -> list[dict]:
    """Read a JSONL file into a list of dicts. Blank lines ignored."""
    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def write_jsonl(path: str, rows: list) -> None:
    """Write rows as canonical JSONL (caller supplies the deterministic order)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(canonical_json(row) + "\n")


def read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def atomic_write_json(path: str, obj: Any) -> None:
    """Write JSON via tmp+rename so a crash never leaves a torn file."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, sort_keys=True, ensure_ascii=False, indent=2, default=str)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Checkpoint (per source): resumable scan cursor + change-marker map for delta
# ---------------------------------------------------------------------------

def checkpoint_path(out_dir: str, source: str) -> str:
    return os.path.join(out_dir, f"{source}.checkpoint.json")


def load_checkpoint(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    return read_json(path)


def save_checkpoint(path: str, data: dict) -> None:
    atomic_write_json(path, data)


def new_checkpoint(source: str, table: str, now_iso: str) -> dict:
    return {
        "source": source,
        "table": table,
        "created_at": now_iso,
        "updated_at": now_iso,
        "scan_complete": False,
        "last_evaluated_key": None,
        "count": 0,
        "markers": {},
    }


# ---------------------------------------------------------------------------
# Target resolution (test-import / reconcile / full-run / rollback)
# ---------------------------------------------------------------------------

def resolve_container_provider(target: str):
    """Return ``(get_container, label)`` for a named target.

    - ``fake`` (safe default): the in-memory FakeCosmos from
      tests/fake_cosmos.py — nothing external is ever touched.
    - ``real``: the storage.cosmos production containers (requires
      COSMOS_ENDPOINT/COSMOS_DATABASE + DefaultAzureCredential). Only used at
      cutover with explicit user authorization (D-8 gate).
    """
    if target == "fake":
        from tests.fake_cosmos import FakeCosmos

        fake = FakeCosmos()
        return fake, "fake (tests/fake_cosmos.py, in-memory)"
    if target == "real":
        from storage import cosmos

        return cosmos.get_container, "real (storage.cosmos)"
    raise ValueError(f"unknown target {target!r} (expected 'fake' or 'real')")
