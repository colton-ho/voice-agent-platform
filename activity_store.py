"""Activity audit store: Cosmos-backed operations audit log (Part B).

An *operations* audit trail — who did what, when — distinct from the
conversation transcripts kept by ``HistoryRecorder``. Records admin/auth
actions (login/logout, demo edits, mcp/voice/user/config mutations) plus
call start/stop events.

Design (tech_design Part B.2/B.3):

- **Write-path semantics are best-effort**: sync container verbs invoked via
  ``asyncio.to_thread``, TTL on every row, writes that NEVER raise into the
  caller.
- **Lazy container accessor** (like ``DemoStore``, not eager) — N3 in the tech
  design. Constructing an ``ActivityStore`` when the storage is absent / creds
  aren't ready must never touch Azure, so the bot.py singleton can be built at
  import time and the endpoints/lifespan stay zero-impact until the container
  exists.
- **Graceful degrade**: container missing → ``log`` is a silent no-op, ``query``
  returns empty. Shipping the code is zero-impact on prod until the container
  exists + env is set.

Container shape (pipeline-infra-spec §1.1: PK ``day`` = UTC ``YYYY-MM-DD``,
id = ``ts_id`` = ``<epoch_ms>#<uuid>``) so a time-range query touches a small
set of day partitions, time-sorted within a day (ts_id 字典序 == 時間序).

TTL (§1.3)
----------
``expires_at`` = ts + ACTIVITY_LOG_TTL_DAYS*86400 (业务删除点, epoch 秒); 每次
写入重算 ``ttl = expires_at - now`` (Cosmos TTL = 最后修改起算, 直接搬字段会
无限续期); 读侧强制过期过滤 ``expires_at = null OR > now``。读出时 ttl 欄
曝光为 expires_at (legacy ttl 字段=删除点 epoch, 语义 1:1 保留)。

Redaction — DEFAULT-DENY (the round-1 reviewer BLOCKER fix)
-----------------------------------------------------------
The old "redact keys matching ``password|token|...``" substring approach was
unsafe: a credential-bearing field whose JSON key doesn't match (an mcp
``url``/``headers``/``auth`` block, a ``verification_code``, a bearer value)
would persist a raw secret. The model here is **default-deny**:

1. Callers pass a CURATED, already-safe ``detail`` (changed field NAMES for
   mutating endpoints, never raw bodies) — enforced at the call site.
2. ``ACTIVITY_DETAIL_VALUE_ALLOWLIST`` names, per activity type, the ONLY
   fields whose *values* may be stored. ``_sanitize_detail`` is the store-side
   backstop: any field NOT in the allowlist is masked to its NAME ONLY (value
   → ``"***changed***"``). An UNKNOWN type masks all values.
3. A residual substring scrub runs LAST as belt-and-suspenders only —
   correctness rests on the default-deny allowlist, not the substring list.

Net: a credential-bearing field can only ever appear as its field NAME,
never its value — regardless of how the field is named.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from storage import cosmos
from storage.cosmos import ContainerNotFound

logger = logging.getLogger(__name__)

# §1.3-4 讀側強制過期（與 cosmos_calls 同形）
_EXPIRY = "(NOT IS_DEFINED(c.expires_at) OR IS_NULL(c.expires_at) OR c.expires_at > @now)"


def _ttl_days_default() -> int:
    raw = (os.environ.get("ACTIVITY_LOG_TTL_DAYS") or "").strip()
    try:
        v = int(raw)
        return v if v > 0 else 90
    except (TypeError, ValueError):
        return 90


# --- Redaction (DEFAULT-DENY) --------------------------------------------

# Per-type value allowlist: the ONLY fields whose *values* may be stored for a
# given activity type. Anything else → field-name only ("***changed***").
# NEVER list password/password_hash (user-*), url/headers/auth (mcp-*).
ACTIVITY_DETAIL_VALUE_ALLOWLIST: dict[str, set[str]] = {
    "config-web": {"engine", "lang", "scenario", "provider", "voice", "minimax_model", "model", "asr_filter"},
    "config-phone": {"engine", "lang", "scenario", "provider", "voice", "minimax_model", "model", "asr_filter"},
    "voice-create": {"provider", "voice_id", "label", "gender", "language", "locale", "polyglot", "engine", "boost"},
    "voice-update": {"provider", "voice_id", "label", "gender", "language", "locale", "polyglot", "engine", "boost"},
    "voice-delete": {"provider", "voice_id", "label", "gender", "language", "locale", "polyglot", "engine", "boost"},
    "user-create": {"role", "disabled"},
    "user-update": {"role", "disabled"},
    "user-delete": {"role", "disabled"},
    "mcp-upsert": {"id", "label", "transport", "enabled"},
    "mcp-delete": {"id", "label", "transport", "enabled"},
    # demo-edit stores changed FIELD NAMES only (the caller passes
    # {"changed": [..names..]}); no per-value fields are allowlisted, so any
    # stray value-bearing key is masked.
    "demo-edit": {"changed"},
    "login": {"username"},
    "logout": {"username"},
    # Guest experience links — the token is NEVER passed to detail (and would be
    # masked by default-deny regardless); only the link parameters are recorded.
    "guest-link-create": {"ttl_minutes", "scenario", "lang", "engine", "voice", "provider"},
    "guest-login": {"scenario"},
    # call-start / call-end carry only non-secret runtime descriptors.
    "call-start": {"engine", "lang", "scenario", "voice"},
    "call-end": {"engine", "lang", "scenario", "voice"},
}

_MASK = "***changed***"

# Belt-and-suspenders ONLY (runs last). Correctness rests on the default-deny
# allowlist above, not this substring list.
_SECRET_SUBSTR = re.compile(
    r"password|token|api[_-]?key|secret|authorization|key|credential|bearer",
    re.IGNORECASE,
)


def _scrub_residual(value: Any) -> Any:
    """Belt-and-suspenders: recursively mask any dict key matching a known
    secret substring. Runs LAST, after the allowlist has already masked
    non-allowlisted fields — it is NOT the primary defence."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_SUBSTR.search(k):
                out[k] = _MASK
            else:
                out[k] = _scrub_residual(v)
        return out
    if isinstance(value, list):
        return [_scrub_residual(v) for v in value]
    return value


def _sanitize_detail(activity_type: str, detail: dict[str, Any] | None) -> dict[str, Any]:
    """DEFAULT-DENY store-side backstop.

    Any key not in this type's ``ACTIVITY_DETAIL_VALUE_ALLOWLIST`` is masked to
    its NAME ONLY (value → ``"***changed***"``). An UNKNOWN type masks ALL
    values. A residual secret-substring scrub runs LAST as belt-and-suspenders.
    Even a careless caller that passes a raw credential value under an
    innocuous key (e.g. mcp ``url``/``headers``/``auth``) cannot leak it.
    """
    if not detail:
        return {}
    if not isinstance(detail, dict):
        # Non-dict detail can't be field-allowlisted; mask entirely.
        return {"detail": _MASK}
    allow = ACTIVITY_DETAIL_VALUE_ALLOWLIST.get(activity_type)
    out: dict[str, Any] = {}
    for k, v in detail.items():
        if allow is not None and k in allow:
            out[k] = v
        else:
            # Not allowlisted (or unknown type) → keep the field NAME, drop the
            # value. The audit trail still records THAT the field changed.
            out[k] = _MASK
    # Residual belt-and-suspenders scrub on the surviving (allowlisted) values.
    return _scrub_residual(out)


class ActivityStore:
    """Cosmos-backed operations audit store (best-effort write, lazy container).

    Constructing this never touches Azure — containers resolve through
    ``storage.cosmos`` on first use (N3). ``log`` swallows ALL failures (incl.
    container-missing) so it can never break the operation it audits; ``query``
    returns empty when the container is absent.
    """

    def __init__(
        self,
        table_name: str | None = None,
        region: str | None = None,
        ttl_days: int | None = None,
    ):
        # B10（BC-c1）：ACTIVITY_TABLE 退役——容器名 §1.1 冻结（cosmos.ACTIVITY）。
        self._table_name = table_name or cosmos.ACTIVITY
        self._ttl_days = ttl_days if ttl_days is not None else _ttl_days_default()

    def _activity(self):
        return cosmos.get_container(cosmos.ACTIVITY)

    @staticmethod
    def _is_missing(exc: Exception) -> bool:
        return isinstance(exc, ContainerNotFound)

    # ---- write (best-effort, never raises) --------------------------------

    async def log(
        self,
        actor: str,
        actor_role: str | None = None,
        type: str = "",
        target: str | None = None,
        detail: dict[str, Any] | None = None,
        status: str = "success",
        error: str | None = None,
    ) -> None:
        """Persist one audit row. BEST-EFFORT: any failure — including a
        missing container or unavailable credentials — is swallowed with a debug
        log and NEVER raised into the caller.

        Row: ``day`` (UTC YYYY-MM-DD) / ``ts_id`` (``<epoch_ms>#<uuid>``) /
        ``expires_at`` (now + ttl_days*86400, §1.3) with ``ttl`` recomputed as
        the remaining seconds. ``detail`` is passed through
        ``_sanitize_detail`` (default-deny) so the store can't persist a raw
        secret even if a caller is careless.
        """
        try:
            now = time.time()
            now_ms = int(now * 1000)
            dt = datetime.now(timezone.utc)
            ts_id = f"{now_ms}#{uuid.uuid4().hex}"
            expires_at = int(now) + self._ttl_days * 86400
            row: dict[str, Any] = {
                "id": ts_id,
                "day": dt.strftime("%Y-%m-%d"),
                "ts_id": ts_id,
                "ts": now_ms,
                "actor": actor or "anon",
                "actor_role": actor_role or "",
                "type": type or "",
                "status": status or "success",
                "detail": _sanitize_detail(type or "", detail),
                # §1.3-2/3：expires_at=业务删除点；ttl=剩余秒（每次写入重算，
                # Cosmos TTL 按最后修改起算，直接搬删除点会无限续期）
                "expires_at": expires_at,
                "ttl": max(1, expires_at - int(now)),
            }
            if target is not None:
                row["target"] = target
            if error:
                row["error"] = str(error)[:500]

            def _call():
                return self._activity().upsert_item(row)

            await asyncio.to_thread(_call)
        except Exception as e:  # noqa: BLE001 — best-effort, must never raise
            # NB: the `type` PARAMETER shadows the builtin in this scope, so use
            # the class attribute directly rather than type(e).
            logger.debug(
                f"activity_store: log dropped (type={type!r} actor={actor!r}): "
                f"{e.__class__.__name__}: {e}"
            )

    # ---- read (paginated, newest-first) -----------------------------------

    async def query(
        self,
        day_from: str | None = None,
        day_to: str | None = None,
        actor: str | None = None,
        type: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Query the day partitions in ``[day_from, day_to]`` (UTC dates),
        optionally filter by ``actor`` / ``type``, return newest-first and
        paginated.

        Returns ``{"items": [...], "cursor": <opaque str|None>}``. ``cursor`` is
        a ``"<day>|<ts_id>"`` marker identifying the last row returned; pass it
        back to fetch the next (older) page. Container missing / any error →
        ``{"items": [], "cursor": None}``.
        """
        try:
            limit = max(1, min(int(limit or 100), 1000))
        except (TypeError, ValueError):
            limit = 100

        # Default window: today back 90 days (TTL horizon) when unset.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        d_to = (day_to or today)
        d_from = day_from or _days_ago(d_to, 90)
        if d_from > d_to:
            d_from, d_to = d_to, d_from

        days = _day_range_desc(d_from, d_to)  # newest day first

        # Parse cursor: only start emitting once we pass it (newest-first).
        cur_day, cur_ts_id = None, None
        if cursor and "|" in cursor:
            cur_day, cur_ts_id = cursor.split("|", 1)

        try:
            collected: list[dict[str, Any]] = []
            next_cursor: str | None = None

            for day in days:
                # Skip day-partitions newer than the cursor's day (already sent).
                if cur_day is not None and day > cur_day:
                    continue

                def _query_day(d=day) -> list[dict[str, Any]]:
                    # 單分區（PK /day）+ §1.3-4 強制過期；ts_id DESC == 時間序。
                    # actor/type 過濾留在 Python 側（與 legacy 行為一致）。
                    return self._activity().query_items(
                        f"SELECT * FROM c WHERE c.day = @day AND {_EXPIRY} ORDER BY c.ts_id DESC",
                        [{"name": "@day", "value": d}, {"name": "@now", "value": int(time.time())}],
                        partition_key=d,
                    )

                rows = await asyncio.to_thread(_query_day)

                for r in rows:
                    ts_id = r.get("ts_id", "")
                    # Cursor: on the cursor's own day, skip rows >= the cursor
                    # ts_id (already returned). DESC order means "older" ==
                    # smaller ts_id.
                    if cur_day is not None and day == cur_day and cur_ts_id is not None:
                        if ts_id >= cur_ts_id:
                            continue
                    if actor and r.get("actor") != actor:
                        continue
                    if type and r.get("type") != type:
                        continue
                    collected.append(_read_row(r))
                    if len(collected) >= limit:
                        next_cursor = f"{day}|{ts_id}"
                        return {"items": collected, "cursor": next_cursor}

                # Once we've consumed the cursor's day, the cursor no longer
                # constrains older days.
                if cur_day is not None and day == cur_day:
                    cur_day, cur_ts_id = None, None

            return {"items": collected, "cursor": None}
        except Exception as e:  # noqa: BLE001 — degrade to empty
            if self._is_missing(e):
                logger.debug(f"activity_store: query container for {self._table_name!r} missing → empty")
            else:
                # `type` param shadows the builtin — use the class attribute.
                logger.warning(f"activity_store: query failed: {e.__class__.__name__}: {e}")
            return {"items": [], "cursor": None}


# --- helpers --------------------------------------------------------------

def _days_ago(day: str, n: int) -> str:
    from datetime import date, timedelta

    y, m, d = (int(x) for x in day.split("-"))
    return (date(y, m, d) - timedelta(days=n)).strftime("%Y-%m-%d")


def _day_range_desc(d_from: str, d_to: str) -> list[str]:
    """Inclusive list of UTC YYYY-MM-DD strings from d_to back to d_from
    (newest first). Capped at 366 partitions defensively."""
    from datetime import date, timedelta

    y1, m1, d1 = (int(x) for x in d_from.split("-"))
    y2, m2, d2 = (int(x) for x in d_to.split("-"))
    start = date(y1, m1, d1)
    cur = date(y2, m2, d2)
    out: list[str] = []
    guard = 0
    while cur >= start and guard < 366:
        out.append(cur.strftime("%Y-%m-%d"))
        cur = cur - timedelta(days=1)
        guard += 1
    return out


def _read_row(item: dict[str, Any]) -> dict[str, Any]:
    """Container doc -> external row shape（legacy DDB 行, 語義 1:1）。
    ttl 欄曝光為刪除點 epoch（=expires_at，§1.3-5）；存儲系統屬性（_ 前綴）
    不外泄。數字歸一化複用 demo_store._from_ddb。"""
    from demo_store import _from_ddb

    out = {k: v for k, v in _from_ddb(item).items() if not k.startswith("_")}
    if out.get("expires_at") is not None:
        out["ttl"] = out["expires_at"]
    return out
