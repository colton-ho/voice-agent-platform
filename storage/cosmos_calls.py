"""Cosmos CallRepository（FC-6）+ X1 折疊的 legacy 歷史文檔層。

容器（pipeline-infra-spec §1.1）：
  calls        PK /caller_id（匿名哨兵 "_none_"；id=call_id）
  call_events  PK /call_id   id=event_id（冪等去重鍵 R-A-007）
  transcripts  PK /call_id   id=segment_id（音頻禁止入 Cosmos）
  usage        PK /call_id   （F9 綠野，U-502 消費；本模組不寫）

X1 折疊（§1.1 註記）：legacy history item 的 summary/outcome/ttl 語義保留在
calls 文檔上；turns 內容遷 transcripts（文本輪次）/call_events（tool_call 事件），
外部 API 經 get_history_doc 重新拼回 turns[]（形狀不動）。

TTL（§1.3）：expires_at = started_at + ttl_days（業務邊界，epoch 秒）；每次寫入
重算剩餘 ttl = expires_at - now（Cosmos TTL=最後修改起算，直接搬字段會無限續期）；
讀側強制過期 `expires_at = null OR > now`。讀出時 ttl 欄曝光為 expires_at
（legacy ttl 字段=刪除點 epoch，語義 1:1 保留）。

查詢（§1.2）：列表 ORDER BY c.started_at DESC, c.id ASC（CI-1）；caller 路徑
單分區（WHERE c.caller_id=@x），全局/CSV 跨分區殘餘過濾；游標=keyset
(started_at, call_id)（同秒並列由 id 決定性排序）。按 call_id 點讀/更新降級為
id 等值跨分區查詢（§1.1 落定代價，TTL 30 天量級可接受）。
"""
import time
from datetime import datetime, timezone

from storage import cosmos
from storage.base import (
    CallEvent,
    CallFinalizedError,
    CallRecord,
    CallRepository,
    CallState,
    HistoryPage,
    StaleVersionError,
)

ANONYMOUS_CALLER_ID = "_none_"

_ORDER_BY = " ORDER BY c.started_at DESC, c.id ASC"
# §1.3-4 讀側強制過期（spec 原文 `(expires_at = null OR > @now)` 的等價寫法）
_EXPIRY = "(NOT IS_DEFINED(c.expires_at) OR IS_NULL(c.expires_at) OR c.expires_at > @now)"

# finalize 的 outcome 合併時不允許覆蓋的鍵（身份/TTL/終態標記）
_RESERVED = {"id", "call_id", "caller_id", "_etag", "ttl", "expires_at", "finalized"}


def _epoch(dt: datetime | None) -> int | None:
    return int(dt.timestamp()) if dt else None


def _dt(epoch) -> datetime | None:
    return datetime.fromtimestamp(epoch, tz=timezone.utc) if epoch else None


def _encode_cursor(anchor: dict | None) -> str | None:
    if not anchor:
        return None
    import base64
    import json

    payload = json.dumps(anchor, ensure_ascii=False)
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str | None) -> dict | None:
    if not cursor:
        return None
    import base64
    import json

    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
    except Exception as e:
        raise ValueError(f"invalid cursor: {e}") from e


class CosmosCallRepository(CallRepository):
    """FC-6 實現 + legacy 歷史讀寫面（bot.py 舊接口形狀不動）。"""

    def __init__(self, ttl_days: int = 30):
        self._ttl_days = ttl_days

    # -- container helpers ------------------------------------------------
    def _calls(self):
        return cosmos.get_container(cosmos.CALLS)

    def _events(self):
        return cosmos.get_container(cosmos.CALL_EVENTS)

    def _transcripts(self):
        return cosmos.get_container(cosmos.TRANSCRIPTS)

    def _now(self) -> int:
        return int(time.time())

    def _expiry_fields(self, started_at, expires_at=None) -> dict:
        """§1.3-2/3：expires_at=業務邊界；ttl=剩餘秒（每次寫入重算，clamp ≥1）。"""
        if expires_at is None:
            expires_at = int(started_at or self._now()) + self._ttl_days * 86400
        return {"expires_at": int(expires_at), "ttl": max(1, int(expires_at) - self._now())}

    def _read_row(self, doc: dict) -> dict:
        """讀出整形：ttl 欄曝光為刪除點 epoch（=expires_at，§1.3-5 語義 1:1）。"""
        out = dict(doc)
        if out.get("expires_at") is not None:
            out["ttl"] = out["expires_at"]
        return out

    def _find_by_call_id(self, call_id: str) -> dict | None:
        sql = f"SELECT * FROM c WHERE c.id = @id AND {_EXPIRY}"
        rows = self._calls().query_items(
            sql,
            [{"name": "@id", "value": call_id}, {"name": "@now", "value": self._now()}],
        )
        return rows[0] if rows else None

    # -- doc <-> CallRecord ------------------------------------------------
    def _record_from_doc(self, doc: dict) -> CallRecord:
        return CallRecord(
            call_id=doc.get("call_id") or doc["id"],
            # legacy 行不寫 state（X1 折疊；U-306 狀態機落地前缺省視為終態）
            state=CallState(doc.get("state") or CallState.ENDED.value),
            started_at=_dt(doc.get("started_at")),
            tenant_id=doc.get("tenant_id"),
            provider_ids=doc.get("provider_ids"),
            ended_at=_dt(doc.get("ended_at")),
            expires_at=_dt(doc.get("expires_at")),
            owner=doc.get("owner"),
            handoff_outcome=doc.get("handoff_outcome"),
            version=doc.get("_etag", 0),
        )

    # -- FC-6 -------------------------------------------------------------
    async def create(self, record: CallRecord) -> CallRecord:
        body = {
            "id": record.call_id,
            "call_id": record.call_id,
            # FC-6 CallRecord 無 caller 欄（契約凍結）；匿名哨兵 per §1.1。
            # 歸屬 caller 的寫入走 legacy 面（write_outcome_row 显式写 caller_id）。
            "caller_id": ANONYMOUS_CALLER_ID,
            "state": record.state.value if isinstance(record.state, CallState) else str(record.state),
            "started_at": _epoch(record.started_at),
            "tenant_id": record.tenant_id,
            "provider_ids": dict(record.provider_ids) if record.provider_ids else None,
            "ended_at": _epoch(record.ended_at),
            "owner": record.owner,
            "handoff_outcome": record.handoff_outcome,
        }
        body.update(self._expiry_fields(body["started_at"], _epoch(record.expires_at)))
        try:
            created = self._calls().create_item(body)
        except cosmos.ContainerConflict:
            return self._record_from_doc((await self._get_doc(record.call_id)) or body)
        return self._record_from_doc(created)

    async def _get_doc(self, call_id: str) -> dict | None:
        return self._find_by_call_id(call_id)

    async def get(self, call_id: str) -> CallRecord | None:
        doc = self._find_by_call_id(call_id)
        return self._record_from_doc(doc) if doc else None

    async def update(self, call_id: str, changes, *, expected_version) -> CallRecord:
        doc = self._require(call_id, expected_version)
        ops = self._set_ops(changes, doc)
        new = self._calls().patch_item(call_id, doc["caller_id"], ops, etag=doc.get("_etag"))
        return self._record_from_doc(new)

    async def append_events(self, call_id: str, events) -> int:
        doc = self._find_by_call_id(call_id)
        if doc is None:
            raise KeyError(call_id)
        if doc.get("finalized"):
            raise CallFinalizedError(call_id)
        expiry = self._expiry_fields(doc.get("started_at"), doc.get("expires_at"))
        appended = 0
        for ev in events:
            body = {
                "id": ev.event_id,
                "call_id": call_id,
                "event_type": ev.event_type,
                "occurred_at": _epoch(ev.occurred_at),
                "payload": dict(ev.payload),
                "expires_at": expiry["expires_at"],
                "ttl": expiry["ttl"],
            }
            try:
                self._events().create_item(body)
                appended += 1
            except cosmos.ContainerConflict:
                pass  # event_id 去重（R-A-007）
        return appended

    async def finalize(self, call_id: str, *, outcome, expected_version) -> CallRecord:
        doc = self._require(call_id, expected_version)
        updates = {k: v for k, v in outcome.items() if k not in _RESERVED}
        state = updates.get("state")
        if isinstance(state, CallState):
            updates["state"] = state.value
        updates.setdefault("state", CallState.ENDED.value)
        updates.setdefault("ended_at", self._now())
        updates["finalized"] = True
        ops = self._set_ops(updates, doc)
        new = self._calls().patch_item(call_id, doc["caller_id"], ops, etag=doc.get("_etag"))
        return self._record_from_doc(new)

    def _require(self, call_id: str, expected_version) -> dict:
        doc = self._find_by_call_id(call_id)
        if doc is None:
            raise KeyError(call_id)
        if doc.get("finalized"):
            raise CallFinalizedError(call_id)
        if str(doc.get("_etag")) != str(expected_version):
            raise StaleVersionError(f"{call_id}: expected {expected_version}, got {doc.get('_etag')}")
        return doc

    def _set_ops(self, changes, doc: dict) -> list:
        ops = [{"op": "set", "path": f"/{k}", "value": v} for k, v in changes.items()]
        # §1.3-2 任何寫入都重算剩餘 TTL（刪除時點恆等 expires_at）
        expiry = self._expiry_fields(doc.get("started_at"), doc.get("expires_at"))
        ops.append({"op": "set", "path": "/ttl", "value": expiry["ttl"]})
        return ops

    async def query_history(self, *, caller=None, tenant_id=None, cursor=None, limit=50) -> HistoryPage:
        rows, next_cursor = self.query_history_docs(
            caller=caller, tenant_id=tenant_id, cursor=cursor, limit=limit
        )
        return HistoryPage(items=[self._record_from_doc(r) for r in rows], next_cursor=next_cursor)

    # -- legacy 歷史文檔面（bot.py 舊語義） --------------------------------
    def upsert_outcome_doc(self, item: dict) -> None:
        """write_outcome_row 的整行寫入（等價舊 put_item upsert）。

        §1.3-5：legacy ttl=刪除點 epoch → expires_at 1:1 反推；系統 ttl 改剩餘秒。
        """
        body = dict(item)
        body["id"] = body["call_id"]
        body["caller_id"] = body.get("caller_id") or ANONYMOUS_CALLER_ID
        body.update(self._expiry_fields(body.get("started_at"), body.get("expires_at") or body.get("ttl")))
        self._calls().upsert_item(body)

    def patch_fields(self, call_id: str, fields: dict, remove: tuple = (), *, etag: str | None = None) -> None:
        """SET 風格局部更新（等價舊 update_item SET），含剩餘 TTL 重算。

        etag：B-3 worker 的 optimistic concurrency 縫——帶值時 patch 憑 etag
        守護，並發寫衝突拋 ContainerPreconditionFailed（呼叫方按暫時性重試）。
        """
        doc = self._find_by_call_id(call_id)
        if doc is None:
            raise KeyError(call_id)
        ops = self._set_ops(fields, doc)
        ops += [{"op": "remove", "path": f"/{k}"} for k in remove]
        self._calls().patch_item(call_id, doc["caller_id"], ops, etag=etag)

    def write_turns(self, call_id: str, turns: list) -> None:
        """X1：文本輪次→transcripts，tool_call 事件→call_events。

        確定性 id（call_id#t{seq}/#e{seq}）⇒ 重刷同列表=冪等（撞 id 跳過）。
        """
        doc = self._find_by_call_id(call_id)
        expiry = self._expiry_fields(
            (doc or {}).get("started_at"), (doc or {}).get("expires_at")
        )
        for idx, turn in enumerate(turns):
            if isinstance(turn, dict) and "kind" in turn:
                body = {
                    "id": f"{call_id}#e{idx}",
                    "call_id": call_id,
                    "kind": turn.get("kind"),
                    "data": turn.get("data"),
                    "t": turn.get("t"),
                }
                container = self._events()
            else:
                body = {
                    "id": f"{call_id}#t{idx}",
                    "call_id": call_id,
                    "who": (turn or {}).get("who"),
                    "text": (turn or {}).get("text"),
                    "t": (turn or {}).get("t"),
                }
                container = self._transcripts()
            body.update(expiry)
            try:
                container.create_item(body)
            except cosmos.ContainerConflict:
                pass  # 同 seq 重刷跳過（冪等）

    def get_history_doc(self, call_id: str) -> dict | None:
        """點讀全行（含重拼 turns[]）；讀側強制過期 ⇒ 過期行視同不存在。"""
        doc = self._find_by_call_id(call_id)
        if doc is None:
            return None
        out = self._read_row(doc)
        out["turns"] = self._merged_turns(call_id)
        return out

    def _merged_turns(self, call_id: str) -> list:
        params = [{"name": "@id", "value": call_id}]
        merged: list = []
        for row in self._transcripts().query_items(
            "SELECT * FROM t WHERE t.call_id = @id", params, partition_key=call_id
        ):
            merged.append({"who": row.get("who"), "text": row.get("text"), "t": row.get("t")})
        for row in self._events().query_items(
            "SELECT * FROM e WHERE e.call_id = @id", params, partition_key=call_id
        ):
            if "kind" in row:  # legacy 事件行（append_event）
                merged.append({"kind": row.get("kind"), "data": row.get("data"), "t": row.get("t")})
            else:  # FC-6 CallEvent 行 → 外部 turns 形狀
                merged.append(
                    {"kind": row.get("event_type"), "data": row.get("payload"), "t": row.get("occurred_at")}
                )
        merged.sort(key=lambda r: r.get("t") or 0)
        return merged

    def query_history_docs(
        self, *, caller=None, web_user=None, tenant_id=None,
        outcome=None, engine=None, scenario=None,
        start_after=None, start_before=None,
        columns=None, cursor=None, limit=50,
    ) -> tuple[list, str | None]:
        """單頁列表查詢（LIMIT limit+1 偷看末頁）。回 (rows, next_cursor)。"""
        sql, params, pk = self._list_query(
            caller=caller, web_user=web_user, tenant_id=tenant_id,
            outcome=outcome, engine=engine, scenario=scenario,
            start_after=start_after, start_before=start_before,
            columns=columns, cursor=cursor,
        )
        sql += " OFFSET 0 LIMIT @limit"
        params.append({"name": "@limit", "value": limit + 1})
        rows = self._calls().query_items(sql, params, partition_key=pk)
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = _encode_cursor(
                {"started_at": last.get("started_at"), "call_id": last.get("call_id") or last.get("id")}
            )
        return [self._read_row(r) for r in rows], next_cursor

    def iter_history_docs(self, *, caller=None, web_user=None, tenant_id=None,
                          outcome=None, engine=None, scenario=None,
                          start_after=None, start_before=None,
                          columns=None, page_size=500):
        """keyset 翻頁生成器（CSV/指標等需全量遍歷的路徑）。"""
        cursor = None
        while True:
            sql, params, pk = self._list_query(
                caller=caller, web_user=web_user, tenant_id=tenant_id,
                outcome=outcome, engine=engine, scenario=scenario,
                start_after=start_after, start_before=start_before,
                columns=columns, cursor=cursor,
            )
            sql += " OFFSET 0 LIMIT @limit"
            params.append({"name": "@limit", "value": page_size})
            rows = self._calls().query_items(sql, params, partition_key=pk)
            for r in rows:
                yield self._read_row(r)
            if len(rows) < page_size:
                return
            last = rows[-1]
            cursor = _encode_cursor(
                {"started_at": last.get("started_at"), "call_id": last.get("call_id") or last.get("id")}
            )

    def _list_query(self, *, caller, web_user, tenant_id, outcome, engine, scenario,
                    start_after, start_before, columns, cursor):
        now = self._now()
        params = [{"name": "@now", "value": now}]
        clauses = [_EXPIRY]
        pk = None
        if caller is not None:
            # §1.2 caller 路徑 = 單分區查詢（PK /caller_id）
            clauses.append("c.caller_id = @caller")
            params.append({"name": "@caller", "value": caller})
            pk = caller
        if web_user is not None:
            clauses.append("c.web_user = @web_user")
            params.append({"name": "@web_user", "value": web_user})
        if tenant_id is not None:
            clauses.append("c.tenant_id = @tenant_id")
            params.append({"name": "@tenant_id", "value": tenant_id})
        for field, val in (("outcome", outcome), ("engine", engine), ("scenario", scenario)):
            if val is not None:
                clauses.append(f"c.{field} = @{field}")
                params.append({"name": f"@{field}", "value": val})
        if start_after is not None:
            clauses.append("c.started_at >= @start_after")
            params.append({"name": "@start_after", "value": int(start_after)})
        if start_before is not None:
            clauses.append("c.started_at <= @start_before")
            params.append({"name": "@start_before", "value": int(start_before)})
        anchor = _decode_cursor(cursor)
        if anchor:
            clauses.append(
                "(c.started_at < @ks_started OR (c.started_at = @ks_started AND c.id > @ks_id))"
            )
            params.append({"name": "@ks_started", "value": int(anchor["started_at"])})
            params.append({"name": "@ks_id", "value": anchor["call_id"]})
        cols = "*" if not columns else ", ".join(f"c.{c}" for c in columns)
        sql = f"SELECT {cols} FROM c WHERE " + " AND ".join(clauses) + _ORDER_BY
        return sql, params, pk
