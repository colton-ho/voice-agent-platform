"""Stage 2 of spec §9.3: pure record-in/record-out transforms.

Maps each discovery §7 DynamoDB source record to the Cosmos adapter shapes in
storage/ (pipeline-infra-spec §1.1 containers). No I/O, no network — every
function is deterministic given ``(item, now)`` (``now`` is injectable so the
remaining-TTL fields are reproducible in tests).

Target shapes (single source of truth in the app layer, mirrored here):

- users     -> ``users``   container: id=username + username/password_hash/
              role/created_at/disabled (user_store.py container shape).
- demos     -> ``demos``   container: canonical demo dict from
              ``demo_loader.normalize_demo_dict`` (17 keys incl.
              engine/provider/voice/model/asr_filter) + PK ``type="demo"``
              (demo_store.py X3 single-partition shape).
- voices    -> ``voices``  container: id=voice_id + provider/voice_id + attrs
              (voice_store.py put() shape).
- activity  -> ``activity`` container: id=ts_id + PK ``day``; legacy DDB
              ``ttl`` (delete-point epoch, §1.3-5) -> ``expires_at`` 1:1 and
              ``ttl`` recomputed as remaining seconds (§1.3-2/3).
- history   -> X1 fold (cosmos_calls.py): the call row into ``calls``
              (id=call_id, PK caller_id, ``caller`` -> caller_id with the
              ``_none_`` anonymous sentinel), text turns into ``transcripts``
              and tool_call turns into ``call_events`` with the deterministic
              ``<call_id>#t<i>`` / ``<call_id>#e<i>`` ids used by
              ``CosmosCallRepository.write_turns``.

Change markers (delta stage) and their limits are documented on
:func:`change_marker`.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

if __package__ in (None, ""):  # plain `python scripts/migration/transform.py`
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )

from demo_loader import normalize_demo_dict  # noqa: E402
from demo_store import DEMO_PARTITION_VALUE, _from_ddb  # noqa: E402

from scripts.migration.common import checksum, read_jsonl, write_jsonl  # noqa: E402

# §1.1 frozen partition keys — must match tests/fake_cosmos.py _PK exactly.
CONTAINER_PK: dict[str, str] = {
    "users": "username",
    "demos": "type",
    "voices": "provider",
    "activity": "day",
    "calls": "caller_id",
    "call_events": "call_id",
    "transcripts": "call_id",
}

ANONYMOUS_CALLER_ID = "_none_"  # cosmos_calls.ANONYMOUS_CALLER_ID


class TransformError(ValueError):
    """A source record that cannot be mapped to a target shape (reject)."""


@dataclass(frozen=True)
class SourceSpec:
    name: str
    table_env: str
    default_table: str | None  # None = env-required (history)
    key_fields: tuple[str, ...]
    containers: tuple[str, ...]


SOURCES: dict[str, SourceSpec] = {
    "users": SourceSpec("users", "USERS_TABLE", "genaiic-voicebot-users",
                        ("username",), ("users",)),
    "demos": SourceSpec("demos", "DEMOS_TABLE", "genaiic-voicebot-demos",
                        ("id",), ("demos",)),
    "voices": SourceSpec("voices", "VOICES_TABLE", "genaiic-voicebot-voices",
                         ("provider", "voice_id"), ("voices",)),
    "activity": SourceSpec("activity", "ACTIVITY_TABLE", "genaiic-voicebot-activity",
                           ("ts_id",), ("activity",)),
    "history": SourceSpec("history", "HISTORY_TABLE", None,
                          ("call_id",), ("calls", "transcripts", "call_events")),
}
SOURCE_ORDER: tuple[str, ...] = ("users", "demos", "voices", "activity", "history")


def get_source(name: str) -> SourceSpec:
    try:
        return SOURCES[name]
    except KeyError:
        raise TransformError(
            f"unknown source {name!r} (expected one of {list(SOURCE_ORDER)})"
        ) from None


def table_name(spec: SourceSpec, override: str | None = None) -> str:
    """Resolve the DynamoDB table name: CLI override -> env -> default.

    ``history`` has no default (bot.py:1229: env-required) and raises when
    neither the env var nor an override is present.
    """
    import os

    name = (override or "").strip() or (os.environ.get(spec.table_env) or "").strip()
    if not name:
        name = spec.default_table or ""
    if not name:
        raise TransformError(
            f"source {spec.name!r}: table name required "
            f"(set {spec.table_env} or pass --table)"
        )
    return name


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else int(now)


# ---------------------------------------------------------------------------
# Business keys / change markers (delta stage)
# ---------------------------------------------------------------------------

def business_key(source: str, item: dict) -> str:
    """Deterministic business key of a source record (sort key for snapshots)."""
    spec = get_source(source)
    for f in spec.key_fields:
        if not item.get(f):
            raise TransformError(f"{source}: record missing key field {f!r}")
    if source == "voices":
        return f"{item['provider']}#{item['voice_id']}"
    return str(item[spec.key_fields[0]])


def row_key(source: str, item: dict) -> str:
    """Business key with a checksum fallback for unkeyable rows (still stable)."""
    try:
        return business_key(source, item)
    except TransformError:
        return f"raw:{checksum(item)}"


def change_marker(source: str, item: dict) -> str:
    """Per-source change marker driving the delta stage.

    - users / demos / voices / history: content checksum. These rows have no
      updated_at attribute in DynamoDB, so change detection is scan-and-diff.
      Limits: deletions are invisible to delta (reconcile catches them); each
      delta is a full re-scan of the (small / TTL-bounded) table. For history
      this also captures the late summary/turns patches, which land after the
      outcome row: a checksum change is re-emitted on the next delta run.
    - activity: the ``ts_id`` marker (``<epoch_ms>#<uuid>``) — the audit log is
      append-only, so the row identity IS the change marker. Limits: an
      in-place edit would be invisible (rows are never mutated in practice),
      and TTL expiry removes rows silently (delta never reports deletions).
    """
    get_source(source)  # validate
    if source == "activity":
        ts_id = item.get("ts_id")
        return f"ts:{ts_id}" if ts_id else f"raw:{checksum(item)}"
    return checksum(item)


# ---------------------------------------------------------------------------
# Per-source transforms (pure)
# ---------------------------------------------------------------------------

def _clean(item: dict) -> dict:
    """Decimal-normalized copy without storage/underscore keys."""
    return {k: v for k, v in _from_ddb(item).items() if not k.startswith("_")}


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def transform_users(item: dict) -> dict:
    """DDB users row -> ``users`` container document (user_store shape)."""
    username = item.get("username")
    if not username:
        raise TransformError("users: record missing 'username'")
    if not item.get("password_hash"):
        raise TransformError(f"users: {username!r} missing 'password_hash'")
    return {
        "id": username,
        "username": username,
        "password_hash": item.get("password_hash"),
        "role": item.get("role") or "user",
        "created_at": _as_int(item.get("created_at")),
        "disabled": bool(item.get("disabled")),
    }


def transform_demos(item: dict) -> dict:
    """DDB demos row -> ``demos`` container document.

    Routes through the SHARED ``normalize_demo_dict`` so the canonical
    17-key shape (incl. engine/provider/voice/model/asr_filter) can never
    drift from the manifest path (demo_store._deserialize contract).
    """
    demo_id = item.get("id")
    if not demo_id:
        raise TransformError("demos: record missing 'id'")
    clean = {k: v for k, v in _clean(item).items() if k != "type"}
    canonical = normalize_demo_dict(clean, validate_tools=False)
    canonical["id"] = demo_id
    canonical["type"] = DEMO_PARTITION_VALUE  # X3 PK constant (wins over input)
    return canonical


def transform_voices(item: dict) -> dict:
    """DDB voices row -> ``voices`` container document (voice_store.put shape)."""
    provider, voice_id = item.get("provider"), item.get("voice_id")
    if not provider or not voice_id:
        raise TransformError("voices: record missing 'provider'/'voice_id'")
    attrs = {
        k: v for k, v in _clean(item).items()
        if k not in ("provider", "voice_id", "id")
    }
    out = dict(attrs)
    out["provider"] = provider
    out["voice_id"] = voice_id
    out["id"] = voice_id  # Cosmos id == voice_id (voice_store.put)
    return out


def transform_activity(item: dict, *, now: int | None = None,
                       ttl_days: int = 90) -> dict:
    """DDB activity row -> ``activity`` container document.

    Legacy ``ttl`` is the delete-point epoch (§1.3-5 semantics 1:1) and maps
    to ``expires_at``; the system ``ttl`` becomes the remaining seconds
    recomputed at write time (§1.3-2/3). Rows without a legacy ttl derive
    ``expires_at`` from ``ts`` + ``ttl_days``.
    """
    ts_id, day = item.get("ts_id"), item.get("day")
    if not ts_id:
        raise TransformError("activity: record missing 'ts_id'")
    if not day:
        raise TransformError("activity: record missing 'day'")
    now = _now(now)
    clean = _clean(item)
    out = {k: v for k, v in clean.items() if k not in ("id", "ttl", "expires_at")}
    expires_at = _as_int(clean.get("expires_at"))
    if expires_at is None:
        expires_at = _as_int(clean.get("ttl"))
    if expires_at is None:
        ts = _as_int(clean.get("ts"))
        expires_at = (ts // 1000 if ts is not None else now) + ttl_days * 86400
    out["id"] = ts_id
    out["expires_at"] = expires_at
    out["ttl"] = max(1, expires_at - now)
    return out


def transform_history(item: dict, *, now: int | None = None,
                      ttl_days: int = 30) -> dict:
    """DDB history row -> X1 fold: (calls doc, transcripts rows, call_events rows).

    - ``turns`` moves out of the call row (the external API re-assembles it
      via ``CosmosCallRepository.get_history_doc``, shape unchanged);
    - legacy ``ttl`` (delete-point epoch) -> ``expires_at`` 1:1 and the system
      ``ttl`` recomputed as remaining seconds, both propagated to every
      folded turn row (write_turns parity);
    - ``caller`` -> ``caller_id`` PK with the ``_none_`` sentinel.
    """
    call_id = item.get("call_id")
    if not call_id:
        raise TransformError("history: record missing 'call_id'")
    now = _now(now)
    clean = _clean(item)
    turns = clean.pop("turns", None) or []
    clean.pop("id", None)
    clean.pop("ttl", None)
    clean.pop("expires_at", None)
    caller = clean.get("caller")
    expires_at = _as_int(item.get("ttl")) or _as_int(clean.get("started_at") or now) \
        + ttl_days * 86400
    expiry = {"expires_at": int(expires_at), "ttl": max(1, int(expires_at) - now)}

    doc = dict(clean)
    doc["id"] = call_id
    doc["call_id"] = call_id
    doc["caller_id"] = caller or ANONYMOUS_CALLER_ID
    doc.update(expiry)

    transcripts: list[dict] = []
    events: list[dict] = []
    for idx, turn in enumerate(turns):
        if isinstance(turn, dict) and "kind" in turn:
            row = {
                "id": f"{call_id}#e{idx}",
                "call_id": call_id,
                "kind": turn.get("kind"),
                "data": turn.get("data"),
                "t": turn.get("t"),
            }
            row.update(expiry)
            events.append(row)
        else:
            t = turn if isinstance(turn, dict) else {}
            row = {
                "id": f"{call_id}#t{idx}",
                "call_id": call_id,
                "who": t.get("who"),
                "text": t.get("text"),
                "t": t.get("t"),
            }
            row.update(expiry)
            transcripts.append(row)
    return {"calls": doc, "transcripts": transcripts, "call_events": events}


# ---------------------------------------------------------------------------
# Envelope assembly + validation
# ---------------------------------------------------------------------------

def make_envelope(source: str, key: str, container: str, item: dict) -> dict:
    return {"source": source, "key": key, "container": container, "item": item}


def transform_record(source: str, item: dict, *, now: int | None = None) -> list[dict]:
    """One source record -> list of target envelopes (may be >1 for history)."""
    get_source(source)
    key = business_key(source, item)
    now = _now(now)
    if source == "users":
        rows = [("users", transform_users(item))]
    elif source == "demos":
        rows = [("demos", transform_demos(item))]
    elif source == "voices":
        rows = [("voices", transform_voices(item))]
    elif source == "activity":
        rows = [("activity", transform_activity(item, now=now))]
    else:  # history
        fold = transform_history(item, now=now)
        rows = [("calls", fold["calls"])]
        rows += [("transcripts", r) for r in fold["transcripts"]]
        rows += [("call_events", r) for r in fold["call_events"]]
    return [make_envelope(source, key, container, row) for container, row in rows]


def validate_envelope(env: dict) -> str | None:
    """Shape validation for one transformed envelope. None = accept.

    Mirrors what the Cosmos containers require (fake_cosmos keying is
    ``(body[pk_field], body["id"])``): the item must carry a non-empty
    ``id`` and the container's §1.1 partition key.
    """
    if not isinstance(env, dict):
        return "envelope is not a dict"
    container = env.get("container")
    pk_field = CONTAINER_PK.get(container)
    if pk_field is None:
        return f"unknown container {container!r}"
    item = env.get("item")
    if not isinstance(item, dict):
        return "envelope 'item' is not a dict"
    if not item.get("id"):
        return "item missing non-empty 'id'"
    if item.get(pk_field) in (None, ""):
        return f"item missing partition key {pk_field!r}"
    return None


# ---------------------------------------------------------------------------
# CLI: file-to-file (pure stage)
# ---------------------------------------------------------------------------

def transform_file(source: str, input_path: str, out_path: str,
                   *, now: int | None = None) -> dict:
    """Raw snapshot JSONL -> transformed envelope JSONL. Deterministic order."""
    rows = read_jsonl(input_path)
    envelopes: list[dict] = []
    rejects: list[dict] = []
    for line_no, raw in enumerate(rows, start=1):
        try:
            envelopes.extend(transform_record(source, raw, now=now))
        except TransformError as e:
            rejects.append({"line": line_no, "reason": str(e)})
    envelopes.sort(key=lambda e: (e["key"], e["container"], e["item"].get("id") or ""))
    write_jsonl(out_path, envelopes)
    return {
        "source": source,
        "records": len(rows),
        "transformed_rows": len(envelopes),
        "rejects": rejects,
        "out": out_path,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="U-404 §9.3 stage 2: transform raw source JSONL to Cosmos-shaped "
        "envelope JSONL (pure, record-in/record-out).",
    )
    parser.add_argument("--source", required=True, choices=list(SOURCE_ORDER))
    parser.add_argument("--input", required=True, help="raw snapshot JSONL (export stage output)")
    parser.add_argument("--out", required=True, help="transformed envelope JSONL")
    parser.add_argument("--now", type=int, default=None,
                        help="epoch seconds used for the remaining-ttl fields (default: wall clock)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = transform_file(args.source, args.input, args.out, now=args.now)
    if args.json:
        import json

        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    else:
        for k in sorted(result):
            print(f"{k}: {result[k]}")
    return 0 if not result["rejects"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
