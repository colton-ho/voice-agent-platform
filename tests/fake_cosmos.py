"""In-memory Cosmos 容器替身（U-303 契約測試）。

按 pipeline-infra-spec §1.1 分區鍵建容器；SQL 評估器只覆蓋 cosmos_calls /
activity_store 實際發出的形狀（等值/範圍/keyset/到期過濾 + ORDER BY
c.started_at DESC, c.id ASC 或 c.ts_id DESC + OFFSET 0 LIMIT），查詢原文記入
.queries 供形狀斷言。
"""
import itertools
import re

from storage.cosmos import (
    ContainerConflict,
    ContainerNotFound,
    ContainerPreconditionFailed,
)

# §1.1 分區鍵（frozen）
_PK = {
    "users": "username",
    "demos": "type",
    "voices": "provider",
    "activity": "day",
    "calls": "caller_id",
    "call_events": "call_id",
    "transcripts": "call_id",
    "usage": "call_id",
}

_RE_EXPIRY = re.compile(
    r"^\(NOT IS_DEFINED\((\w+)\.(\w+)\) OR IS_NULL\(\1\.\2\) OR \1\.\2 > @(\w+)\)$"
)
_RE_KEYSET = re.compile(
    r"^\((\w+)\.started_at < @(\w+) OR \(\1\.started_at = @(\w+) AND \1\.id > @(\w+)\)\)$"
)
_RE_SIMPLE = re.compile(r"^(\w+)\.(\w+)\s*(=|>=|<=|>|<)\s*@(\w+)$")


class FakeContainer:
    def __init__(self, name: str):
        self.name = name
        self.pk_field = _PK[name]
        self.docs: dict = {}
        self.queries: list = []
        self.writes: list = []  # (op, item_id) 紀錄
        self._etag = itertools.count(1)

    # -- write verbs ------------------------------------------------------
    def _stamp(self, body: dict) -> dict:
        doc = dict(body)
        doc["_etag"] = str(next(self._etag))
        return doc

    def create_item(self, body: dict) -> dict:
        key = (body[self.pk_field], body["id"])
        if key in self.docs:
            raise ContainerConflict(body["id"])
        self.writes.append(("create", body["id"]))
        doc = self._stamp(body)
        self.docs[key] = doc
        return dict(doc)

    def upsert_item(self, body: dict) -> dict:
        key = (body[self.pk_field], body["id"])
        self.writes.append(("upsert", body["id"]))
        doc = self._stamp(body)
        self.docs[key] = doc
        return dict(doc)

    def replace_item(self, item_id, partition_key, body, *, etag=None) -> dict:
        cur = self.docs.get((partition_key, item_id))
        if cur is None:
            raise ContainerNotFound(item_id)
        if etag is not None and str(cur.get("_etag")) != str(etag):
            raise ContainerPreconditionFailed(item_id)
        self.writes.append(("replace", item_id))
        doc = self._stamp(body)
        self.docs[(partition_key, item_id)] = doc
        return dict(doc)

    def patch_item(self, item_id, partition_key, ops, *, etag=None) -> dict:
        cur = self.docs.get((partition_key, item_id))
        if cur is None:
            raise ContainerNotFound(item_id)
        if etag is not None and str(cur.get("_etag")) != str(etag):
            raise ContainerPreconditionFailed(item_id)
        self.writes.append(("patch", item_id))
        doc = dict(cur)
        for op in ops:
            field = op["path"].lstrip("/")
            if op["op"] in ("set", "add", "replace"):
                doc[field] = op.get("value")
            elif op["op"] == "remove":
                doc.pop(field, None)
        doc = self._stamp(doc)
        self.docs[(partition_key, item_id)] = doc
        return dict(doc)

    def read_item(self, item_id, partition_key) -> dict:
        doc = self.docs.get((partition_key, item_id))
        if doc is None:
            raise ContainerNotFound(item_id)
        return dict(doc)

    def delete_item(self, item_id, partition_key) -> dict:
        cur = self.docs.pop((partition_key, item_id), None)
        if cur is None:
            raise ContainerNotFound(item_id)
        self.writes.append(("delete", item_id))
        return dict(cur)

    # -- query ------------------------------------------------------------
    def query_items(self, query, parameters, *, partition_key=None, max_count=None) -> list:
        self.queries.append({"query": query, "parameters": list(parameters),
                             "partition_key": partition_key})
        params = {p["name"]: p["value"] for p in parameters}
        rows = [dict(d) for d in self.docs.values()]
        if partition_key is not None:
            rows = [r for r in rows if r.get(self.pk_field) == partition_key]
        for pred in self._predicates(query):
            rows = [r for r in rows if pred(r, params)]
        if "ORDER BY c.ts_id DESC" in query:
            # ts_id = "<epoch_ms>#<uuid>" 定長 epoch 前綴，字典序 == 時間序
            rows.sort(key=lambda r: r.get("ts_id") or "", reverse=True)
        elif "ORDER BY c.started_at DESC, c.id ASC" in query:
            # DESC(started_at) + ASC(id) 同秒並列穩定排序（CI-1）
            rows.sort(key=lambda r: (-(r.get("started_at") or 0), r.get("id") or ""))
        m = re.search(r"OFFSET 0 LIMIT @(\w+)$", query.strip())
        if m:
            rows = rows[: params["@" + m.group(1)]]
        return rows

    def _predicates(self, query: str):
        where = query.split(" WHERE ", 1)
        if len(where) < 2:
            return []
        where = where[1].split(" ORDER BY ")[0].split(" OFFSET ")[0]
        preds = []
        for raw in _split_and(where):
            p = raw.strip()
            m = _RE_EXPIRY.match(p)
            if m:
                # (NOT IS_DEFINED(c.expires_at) OR IS_NULL(c.expires_at) OR c.expires_at > @now)
                field, pname = m.group(2), "@" + m.group(3)
                preds.append(lambda r, pa, f=field, n=pname: r.get(f) is None or r.get(f) > pa[n])
                continue
            m = _RE_KEYSET.match(p)
            if m:
                preds.append(lambda r, pa, g=m: (
                    (r.get("started_at") or 0) < pa["@" + g.group(2)]
                    or ((r.get("started_at") or 0) == pa["@" + g.group(3)]
                        and (r.get("id") or "") > pa["@" + g.group(4)])
                ))
                continue
            m = _RE_SIMPLE.match(p)
            if m:
                field, op, pname = m.group(2), m.group(3), "@" + m.group(4)
                if op == "=":
                    preds.append(lambda r, pa, f=field, n=pname: r.get(f) == pa[n])
                else:
                    cmp = {"<": lambda a, b: a < b, ">": lambda a, b: a > b,
                           "<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b}[op]
                    preds.append(lambda r, pa, f=field, n=pname, c=cmp: (
                        r.get(f) is not None and c(r.get(f), pa[n])))
                continue
            raise AssertionError(f"FakeCosmos: unsupported predicate: {p!r}")
        return preds


def _split_and(where: str) -> list:
    parts, depth, buf, i = [], 0, [], 0
    while i < len(where):
        ch = where[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and where.startswith(" AND ", i):
            parts.append("".join(buf))
            buf = []
            i += 5
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p for p in parts if p]


class FakeCosmos:
    """provider(name) -> FakeContainer；同名容器單例。"""

    def __init__(self):
        self._containers: dict = {}

    def __call__(self, name: str) -> FakeContainer:
        return self.container(name)

    def container(self, name: str) -> FakeContainer:
        if name not in self._containers:
            self._containers[name] = FakeContainer(name)
        return self._containers[name]

    def seed(self, container_name: str, *docs: dict) -> None:
        c = self.container(container_name)
        for d in docs:
            c.upsert_item(d)
