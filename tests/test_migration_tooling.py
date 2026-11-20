"""U-404 migration tooling tests (spec §9.3 stages).

Deterministic, FakeCosmos-based (tests/fake_cosmos.py), NO NETWORK:

- the export stage runs against an injected in-memory DynamoClient
  (scripts/migration/export.py seam) — boto3/AWS is never touched;
- test-import / reconcile / rollback run against FakeCosmos through the
  storage-adapter container verbs — Azure is never touched.

Coverage: transform round-trips per source (source record -> transform ->
accepted by the Cosmos fake and readable back through the real store
deserializers), test-import accept/reject counts, reconcile count + field
diffs, delta changed-record selection (per-source change markers), rollback
removing exactly the manifest's rows, and checkpoint resume logic.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from fake_cosmos import FakeCosmos  # noqa: E402
from storage import cosmos  # noqa: E402
from storage.cosmos_calls import CosmosCallRepository  # noqa: E402

from scripts.migration.common import checkpoint_path, load_checkpoint, read_jsonl  # noqa: E402
from scripts.migration.delta import delta_source  # noqa: E402
from scripts.migration.export import export_source  # noqa: E402
from scripts.migration.full_run import run_full  # noqa: E402
from scripts.migration.reconcile import build_expected, reconcile, render_report  # noqa: E402
from scripts.migration.rollback import rollback_manifest  # noqa: E402
from scripts.migration.test_import import load_transformed  # noqa: E402
from scripts.migration.transform import (  # noqa: E402
    SOURCES,
    SOURCE_ORDER,
    TransformError,
    change_marker,
    table_name,
    transform_activity,
    transform_history,
    transform_record,
    validate_envelope,
)

NOW = 1_700_000_000  # injected "now" for deterministic ttl math
FUTURE_TTL = 2_000_000_000  # delete-point epoch safely ahead of wall clock

ALL_TABLES = {
    "users": "genaiic-voicebot-users",
    "demos": "genaiic-voicebot-demos",
    "voices": "genaiic-voicebot-voices",
    "activity": "genaiic-voicebot-activity",
    "history": "genaiic-voicebot-history",
}


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

class FakeDynamoClient:
    """In-memory DynamoDB scan stand-in with page cursors (never touches AWS).

    ``fail_on_page=N`` simulates a mid-export crash: the Nth scan_page call
    raises, so the export stage must resume from its checkpoint.
    """

    def __init__(self, tables: dict[str, list[dict]], fail_on_page: int | None = None):
        self.tables = {t: list(rows) for t, rows in tables.items()}
        self.fail_on_page = fail_on_page
        self.calls = 0

    def scan_page(self, table, limit, exclusive_start_key):
        self.calls += 1
        if self.fail_on_page is not None and self.calls >= self.fail_on_page:
            raise RuntimeError("simulated scan interruption")
        rows = self.tables.get(table, [])
        start = int((exclusive_start_key or {}).get("pos", 0))
        page = rows[start:start + limit]
        end = start + len(page)
        cursor = {"pos": end} if end < len(rows) else None
        return [dict(r) for r in page], cursor


@pytest.fixture
def fake():
    return FakeCosmos()


@pytest.fixture
def cosmos_provider(fake):
    """Route storage.cosmos to FakeCosmos (for CosmosCallRepository reads)."""
    cosmos.set_container_provider(fake)
    yield fake
    cosmos.reset_container_provider()


def user_row(**over) -> dict:
    row = {
        "username": "alice",
        "password_hash": "$2b$12$abcdefghijklmnopqrstuv",
        "role": "admin",
        "created_at": 1_600_000_000,
        "disabled": False,
    }
    row.update(over)
    return row


def demo_row(**over) -> dict:
    row = {
        "id": "demo-1",
        "label": "Demo 1",
        "lang": "zh-HK",
        "engine": "pipeline",
        "provider": "minimax",
        "voice": "voice-x",
        "model": "model-1",
        "system": {"zh-HK": "sys"},
        "greeting": {"zh-HK": "hi"},
        "kb_intro": {"zh-HK": "intro"},
        "kb_ack": {"zh-HK": "ack"},
        "kb_body": {"zh-HK": "kb"},
        "tool_ids": ["lookup"],
        "mcp_servers": ["srv-1"],
        "filler": {"timeout_ms": 5000},
        "asr_filter": {"mode": "strict"},
        "tags": ["a", "b"],
    }
    row.update(over)
    return row


def voice_row(**over) -> dict:
    row = {
        "provider": "minimax",
        "voice_id": "voice-x",
        "label": "Voice X",
        "gender": "female",
        "language": "zh",
        "boost": 3,
    }
    row.update(over)
    return row


def activity_row(**over) -> dict:
    row = {
        "day": "2026-11-01",
        "ts_id": "1730000000000#aaa",
        "ts": 1_730_000_000_000,
        "actor": "admin",
        "actor_role": "admin",
        "type": "login",
        "status": "success",
        "detail": {"username": "admin"},
        "ttl": FUTURE_TTL,
    }
    row.update(over)
    return row


def history_row(**over) -> dict:
    row = {
        "call_id": "call-1",
        "caller": "+15551234",
        "started_at": 1_699_999_000,
        "ended_at": 1_699_999_300,
        "duration_s": 300,
        "summary_status": "ok",
        "ttl": FUTURE_TTL,
        "transfer_requested": False,
        "transfer_topic": "",
        "web_user": "",
        "turn_count": 3,
        "lang": "zh-HK",
        "engine": "pipeline",
        "scenario": "it-support",
        "provider": "minimax",
        "voice": "voice-x",
        "model": "model-1",
        "minimax_model": "mm-1",
        "summary": {"headline": "fixed"},
        "turns": [
            {"who": "user", "text": "hi", "t": 10},
            {"kind": "tool_call", "data": {"name": "lookup"}, "t": 20},
            {"who": "bot", "text": "hello", "t": 30},
        ],
    }
    row.update(over)
    return row


SOURCE_ROWS = {
    "users": user_row,
    "demos": demo_row,
    "voices": voice_row,
    "activity": activity_row,
    "history": history_row,
}


def make_client(**overrides) -> FakeDynamoClient:
    tables = {ALL_TABLES[name]: [row()] for name, row in SOURCE_ROWS.items()}
    for name, rows in overrides.items():
        tables[ALL_TABLES[name]] = rows
    return FakeDynamoClient(tables)


# ---------------------------------------------------------------------------
# Source inventory + key/marker semantics (discovery §7)
# ---------------------------------------------------------------------------

def test_source_inventory_matches_discovery_s7():
    assert set(SOURCES) == {"users", "demos", "voices", "activity", "history"}
    assert SOURCE_ORDER == ("users", "demos", "voices", "activity", "history")
    assert SOURCES["users"].default_table == "genaiic-voicebot-users"
    assert SOURCES["demos"].default_table == "genaiic-voicebot-demos"
    assert SOURCES["voices"].default_table == "genaiic-voicebot-voices"
    assert SOURCES["activity"].default_table == "genaiic-voicebot-activity"
    # history is env/table-required (bot.py:1229: empty HISTORY_TABLE => disabled)
    assert SOURCES["history"].default_table is None
    # X1 fold: history lands in three §1.1 containers
    assert SOURCES["history"].containers == ("calls", "transcripts", "call_events")


def test_history_table_name_requires_env_or_override(monkeypatch):
    monkeypatch.delenv("HISTORY_TABLE", raising=False)
    with pytest.raises(TransformError):
        table_name(SOURCES["history"])
    assert table_name(SOURCES["history"], "genaiic-voicebot-history") == "genaiic-voicebot-history"
    monkeypatch.setenv("HISTORY_TABLE", "env-table")
    assert table_name(SOURCES["history"]) == "env-table"


def test_change_markers_per_source():
    # users/demos/voices/history: content checksum — change detection is scan-and-diff
    assert change_marker("users", user_row()) == change_marker("users", user_row())
    assert change_marker("users", user_row()) != change_marker("users", user_row(role="user"))
    assert change_marker("history", history_row()) != change_marker(
        "history", history_row(summary={"headline": "changed"})
    )
    # activity: ts_id marker (append-only audit log) — content edits are invisible
    assert change_marker("activity", activity_row()) == "ts:1730000000000#aaa"
    assert change_marker("activity", activity_row(status="failed")) == \
        change_marker("activity", activity_row())


# ---------------------------------------------------------------------------
# Transform round-trips per source (record -> transform -> fake accepts)
# ---------------------------------------------------------------------------

def test_transform_users_round_trip(fake):
    envs = transform_record("users", user_row(), now=NOW)
    assert len(envs) == 1
    env = envs[0]
    assert env["container"] == "users"
    assert validate_envelope(env) is None
    item = env["item"]
    assert item["id"] == "alice" and item["username"] == "alice"
    assert item["role"] == "admin" and item["disabled"] is False
    fake("users").upsert_item(item)  # fake accepts (pk username + id present)
    stored = fake("users").read_item("alice", "alice")
    assert stored["password_hash"] == user_row()["password_hash"]
    assert set(stored) >= {"id", "username", "password_hash", "role", "created_at", "disabled"}

    with pytest.raises(TransformError):
        transform_record("users", user_row(password_hash=""), now=NOW)
    with pytest.raises(TransformError):
        transform_record("users", {"password_hash": "x"}, now=NOW)


def test_transform_demos_round_trip(fake):
    # legacy DDB shape: raw `tools` key + stray extra field + numeric filler
    raw = demo_row()
    raw["tools"] = raw.pop("tool_ids")
    raw["stray_extra"] = "dropped"
    envs = transform_record("demos", raw, now=NOW)
    env = envs[0]
    assert env["container"] == "demos"
    assert validate_envelope(env) is None
    item = env["item"]
    # canonical demo-dict shape (normalize_demo_dict): 17 keys + storage keys
    assert item["tool_ids"] == ["lookup"] and "tools" not in item
    assert "stray_extra" not in item
    assert item["type"] == "demo"  # X3 single-partition constant
    assert set(item) == {
        "id", "label", "lang", "engine", "provider", "voice", "model",
        "system", "greeting", "kb_intro", "kb_ack", "kb_body", "tool_ids",
        "mcp_servers", "filler", "asr_filter", "tags", "type",
    }
    fake("demos").upsert_item(item)
    from demo_store import DemoStore

    stored = fake("demos").read_item("demo-1", "demo")
    canonical = DemoStore._deserialize(stored)  # real read-path deserializer
    assert canonical["id"] == "demo-1"
    assert canonical["engine"] == "pipeline" and canonical["provider"] == "minimax"
    assert canonical["voice"] == "voice-x" and canonical["model"] == "model-1"
    assert canonical["asr_filter"] == {"mode": "strict"}
    assert canonical["filler"] == {"timeout_ms": 5000}
    assert canonical["tool_ids"] == ["lookup"]

    with pytest.raises(TransformError):
        transform_record("demos", {"label": "no id"}, now=NOW)


def test_transform_voices_round_trip(fake):
    envs = transform_record("voices", voice_row(), now=NOW)
    env = envs[0]
    assert env["container"] == "voices"
    assert validate_envelope(env) is None
    item = env["item"]
    assert item["id"] == "voice-x" and item["voice_id"] == "voice-x"
    fake("voices").upsert_item(item)
    from voice_store import VoiceStore

    provider, voice_id, attrs = VoiceStore._split_item(fake("voices").read_item("voice-x", "minimax"))
    assert (provider, voice_id) == ("minimax", "voice-x")
    assert attrs == {"label": "Voice X", "gender": "female", "language": "zh", "boost": 3}

    with pytest.raises(TransformError):
        transform_record("voices", {"provider": "minimax"}, now=NOW)


def test_transform_activity_round_trip(fake):
    envs = transform_record("activity", activity_row(), now=NOW)
    env = envs[0]
    assert env["container"] == "activity"
    assert validate_envelope(env) is None
    item = env["item"]
    # legacy ttl (delete-point epoch) -> expires_at 1:1; ttl = remaining (§1.3)
    assert item["id"] == "1730000000000#aaa" and item["day"] == "2026-11-01"
    assert item["expires_at"] == FUTURE_TTL
    assert item["ttl"] == FUTURE_TTL - NOW
    fake("activity").upsert_item(item)
    stored = fake("activity").read_item("1730000000000#aaa", "2026-11-01")
    assert stored["actor"] == "admin" and stored["type"] == "login"
    assert stored["detail"] == {"username": "admin"}

    with pytest.raises(TransformError):
        transform_record("activity", {"day": "2026-11-01"}, now=NOW)
    with pytest.raises(TransformError):
        transform_record("activity", {"ts_id": "1#x"}, now=NOW)

    # a row without a legacy ttl derives expires_at from ts + ttl_days
    no_ttl = activity_row()
    no_ttl.pop("ttl")
    derived = transform_activity(no_ttl, now=NOW)
    assert derived["expires_at"] == 1_730_000_000 + 90 * 86400


def test_transform_history_round_trip(fake, cosmos_provider):
    envs = transform_record("history", history_row(), now=NOW)
    containers = [e["container"] for e in envs]
    assert containers == ["calls", "transcripts", "transcripts", "call_events"]
    for env in envs:
        assert validate_envelope(env) is None

    calls_doc = envs[0]["item"]
    assert calls_doc["id"] == "call-1" and calls_doc["call_id"] == "call-1"
    assert calls_doc["caller_id"] == "+15551234"  # caller -> caller_id PK mapping
    assert calls_doc["expires_at"] == FUTURE_TTL and calls_doc["ttl"] == FUTURE_TTL - NOW
    assert "turns" not in calls_doc  # X1 fold: turns live in transcripts/call_events

    for env in envs:
        fake(env["container"]).upsert_item(env["item"])

    # Real reader re-assembles the legacy turns[] shape (get_history_doc X1)
    repo = CosmosCallRepository(ttl_days=30)
    doc = repo.get_history_doc("call-1")
    assert doc is not None
    assert doc["turn_count"] == 3 and doc["summary"] == {"headline": "fixed"}
    assert doc["turns"] == [
        {"who": "user", "text": "hi", "t": 10},
        {"kind": "tool_call", "data": {"name": "lookup"}, "t": 20},
        {"who": "bot", "text": "hello", "t": 30},
    ]

    # anonymous caller -> "_none_" sentinel
    anon = transform_history(history_row(caller=""), now=NOW)
    assert anon["calls"]["caller_id"] == "_none_"

    with pytest.raises(TransformError):
        transform_record("history", {"caller": "+1"}, now=NOW)


# ---------------------------------------------------------------------------
# test-import: accept/reject counts + shape validation
# ---------------------------------------------------------------------------

def test_test_import_accept_reject_counts(fake):
    good = transform_record("users", user_row(), now=NOW)
    no_id = transform_record("users", user_row(), now=NOW)
    no_id[0] = {**no_id[0], "item": {**no_id[0]["item"], "id": ""}}
    bad_container = [{"source": "users", "key": "x", "container": "nope", "item": {"id": "1"}}]
    envelopes = good + no_id + bad_container

    report = load_transformed(envelopes, fake, apply=True)
    assert report["accepted"] == 1
    assert report["rejected"] == 2
    assert report["wrote"] == 1
    assert report["by_container"]["users"] == {"accepted": 1, "wrote": 1}
    assert [r["reason"] for r in report["rejections"]] == [
        "item missing non-empty 'id'",
        "unknown container 'nope'",
    ]
    assert fake("users").read_item("alice", "alice")["username"] == "alice"

    # dry-run: validated + counted, but nothing written
    empty = FakeCosmos()
    dry = load_transformed(envelopes, empty, apply=False)
    assert dry["accepted"] == 1 and dry["wrote"] == 0
    assert empty("users").docs == {}


# ---------------------------------------------------------------------------
# Reconcile: clean run + count/field diff detection
# ---------------------------------------------------------------------------

def test_reconcile_clean_match(fake):
    source_rows = {name: [row()] for name, row in SOURCE_ROWS.items()}
    envelopes = [
        env
        for name in SOURCE_ORDER
        for env in transform_record(name, source_rows[name][0], now=NOW)
    ]
    load_transformed(envelopes, fake, apply=True)

    report = reconcile(source_rows, fake, now=NOW)
    assert report["ok"] is True
    assert report["totals"]["expected"] == len(envelopes) == report["totals"]["actual"]
    assert report["totals"]["missing"] == 0 and report["totals"]["extra"] == 0
    assert "reconcile ok: True" in render_report(report)


def test_reconcile_detects_count_and_field_diffs(fake):
    source_rows = {
        "users": [user_row(), user_row(username="bob", password_hash="$2b$12$zz")],
        "voices": [voice_row()],
    }
    envelopes = [
        env
        for name, rows in source_rows.items()
        for raw in rows
        for env in transform_record(name, raw, now=NOW)
    ]
    load_transformed(envelopes, fake, apply=True)

    # 1) missing row in target  2) extra row in target  3) field drift
    fake("users").delete_item("bob", "bob")
    fake("voices").upsert_item(
        {"id": "ghost", "provider": "minimax", "voice_id": "ghost"}
    )
    drifted = dict(fake("users").read_item("alice", "alice"))
    drifted["role"] = "user"
    fake("users").upsert_item(drifted)

    report = reconcile(source_rows, fake, now=NOW)
    assert report["ok"] is False

    users = report["sources"]["users"]["containers"]["users"]
    assert users["expected_count"] == 2 and users["actual_count"] == 1
    assert users["missing_in_target"] == [{"partition_key": "bob", "id": "bob"}]
    assert users["diff_rows"] == 1
    assert users["field_diffs"][0]["diffs"]["role"] == {"expected": "admin", "actual": "user"}
    assert users["field_diffs"][0]["checksum_expected"] != users["field_diffs"][0]["checksum_actual"]

    voices = report["sources"]["voices"]["containers"]["voices"]
    assert voices["extra_in_target"] == [{"partition_key": "minimax", "id": "ghost"}]

    text = render_report(report)
    assert "missing: {'partition_key': 'bob', 'id': 'bob'}" in text
    assert "field-diff" in text


def test_reconcile_detects_field_diffs_only():
    expected = build_expected({"users": [user_row()]}, now=NOW)
    actual_row = dict(expected["users"][("alice", "alice")])
    actual_row["disabled"] = True
    actual_row["_etag"] = "7"  # system prop — excluded from comparison
    report = reconcile({"users": [user_row()]}, lambda name: FakeCosmos()(name), now=NOW)
    assert report["ok"] is False  # everything missing from the empty target

    class OneRow:
        def __call__(self, name):
            f = FakeCosmos()
            f.seed(name, actual_row)
            return f(name)

    report2 = reconcile({"users": [user_row()]}, OneRow(), now=NOW)
    users = report2["sources"]["users"]["containers"]["users"]
    assert users["field_diffs"][0]["diffs"] == {"disabled": {"expected": False, "actual": True}}
    assert "_etag" not in users["field_diffs"][0]["diffs"]
    assert users["field_diffs"][0]["extra_fields"] == []  # _etag is a system prop


# ---------------------------------------------------------------------------
# Delta: only changed/new records are picked
# ---------------------------------------------------------------------------

def test_delta_picks_only_changed_records(tmp_path):
    rows = [
        user_row(username="alice"),
        user_row(username="bob", password_hash="$2b$12$bbbb"),
        user_row(username="carol", password_hash="$2b$12$cccc"),
    ]
    client = FakeDynamoClient({ALL_TABLES["users"]: rows})
    workdir = str(tmp_path)
    export_source(client, "users", workdir, table=ALL_TABLES["users"])
    ckpt = load_checkpoint(checkpoint_path(os.path.join(workdir, "snapshots"), "users"))
    assert ckpt["scan_complete"] is True and len(ckpt["markers"]) == 3

    # bob unchanged; alice modified; dave new; carol unchanged
    client.tables[ALL_TABLES["users"]] = [
        user_row(username="alice", role="user"),  # modified
        user_row(username="bob", password_hash="$2b$12$bbbb"),  # unchanged
        user_row(username="carol", password_hash="$2b$12$cccc"),  # unchanged
        user_row(username="dave", password_hash="$2b$12$dddd"),  # new
    ]
    result = delta_source(client, "users", workdir, table=ALL_TABLES["users"], now=NOW)
    assert result["changed_keys"] == ["alice", "dave"]
    assert result["new"] == 1 and result["modified"] == 1 and result["unchanged"] == 2

    delta_rows = read_jsonl(result["delta"])
    assert [r["username"] for r in delta_rows] == ["alice", "dave"]
    delta_envs = read_jsonl(result["delta_transformed"])
    assert [e["key"] for e in delta_envs] == ["alice", "dave"]

    # markers advanced: a second delta right after sees nothing
    again = delta_source(client, "users", workdir, table=ALL_TABLES["users"], now=NOW)
    assert again["changed"] == 0 and again["changed_keys"] == []


def test_delta_activity_append_only_marker(tmp_path):
    client = FakeDynamoClient({ALL_TABLES["activity"]: [activity_row()]})
    workdir = str(tmp_path)
    export_source(client, "activity", workdir, table=ALL_TABLES["activity"])

    # an in-place edit is INVISIBLE to the ts_id marker (documented limit);
    # an appended row IS picked up
    client.tables[ALL_TABLES["activity"]] = [
        activity_row(status="failed"),  # edited in place -> not picked
        activity_row(ts_id="1730000001000#bbb", day="2026-11-02"),  # new
    ]
    result = delta_source(client, "activity", workdir, table=ALL_TABLES["activity"], now=NOW)
    assert result["changed_keys"] == ["1730000001000#bbb"]
    assert result["new"] == 1 and result["modified"] == 0


def test_delta_requires_completed_export(tmp_path):
    client = FakeDynamoClient({ALL_TABLES["users"]: [user_row()]})
    with pytest.raises(SystemExit):
        delta_source(client, "users", str(tmp_path), table=ALL_TABLES["users"])


# ---------------------------------------------------------------------------
# Checkpoint resume + deterministic ordering
# ---------------------------------------------------------------------------

def test_export_resume_after_interruption(tmp_path):
    rows = [user_row(username=n) for n in ("alice", "bob", "carol", "dave")]
    failing = FakeDynamoClient({ALL_TABLES["users"]: rows}, fail_on_page=2)
    workdir = str(tmp_path / "run")
    with pytest.raises(RuntimeError):
        export_source(failing, "users", workdir, table=ALL_TABLES["users"], page_size=2)

    snap_dir = os.path.join(workdir, "snapshots")
    ckpt = load_checkpoint(checkpoint_path(snap_dir, "users"))
    assert ckpt["scan_complete"] is False
    assert ckpt["last_evaluated_key"] == {"pos": 2}  # resume cursor after page 1
    assert len(read_jsonl(os.path.join(snap_dir, "users.jsonl.partial"))) == 2

    healthy = FakeDynamoClient({ALL_TABLES["users"]: rows})
    result = export_source(healthy, "users", workdir, table=ALL_TABLES["users"], page_size=2)
    assert result["resumed"] is True and result["exported"] == 4

    snapshot = read_jsonl(os.path.join(snap_dir, "users.jsonl"))
    assert [r["username"] for r in snapshot] == ["alice", "bob", "carol", "dave"]
    assert not os.path.exists(os.path.join(snap_dir, "users.jsonl.partial"))
    ckpt = load_checkpoint(checkpoint_path(snap_dir, "users"))
    assert ckpt["scan_complete"] is True and ckpt["last_evaluated_key"] is None
    assert set(ckpt["markers"]) == {"alice", "bob", "carol", "dave"}

    # a fresh export of the same data is byte-identical (deterministic)
    fresh = str(tmp_path / "fresh")
    export_source(FakeDynamoClient({ALL_TABLES["users"]: rows}), "users", fresh,
                  table=ALL_TABLES["users"], page_size=3)
    with open(os.path.join(fresh, "snapshots", "users.jsonl"), encoding="utf-8") as f:
        fresh_bytes = f.read()
    with open(os.path.join(snap_dir, "users.jsonl"), encoding="utf-8") as f:
        assert f.read() == fresh_bytes


def test_export_deterministic_order_regardless_of_scan_order(tmp_path):
    rows = [user_row(username=n) for n in ("bob", "alice", "carol")]
    a = str(tmp_path / "a")
    b = str(tmp_path / "b")
    export_source(FakeDynamoClient({ALL_TABLES["users"]: rows}), "users", a,
                  table=ALL_TABLES["users"])
    export_source(FakeDynamoClient({ALL_TABLES["users"]: list(reversed(rows))}), "users", b,
                  table=ALL_TABLES["users"])
    with open(os.path.join(a, "snapshots", "users.jsonl"), encoding="utf-8") as fa:
        with open(os.path.join(b, "snapshots", "users.jsonl"), encoding="utf-8") as fb:
            assert fa.read() == fb.read()
    snap = read_jsonl(os.path.join(a, "snapshots", "users.jsonl"))
    assert [r["username"] for r in snap] == ["alice", "bob", "carol"]


def test_export_idempotent_and_dry_run(tmp_path):
    client = make_client()
    workdir = str(tmp_path)
    first = export_source(client, "users", workdir, table=ALL_TABLES["users"])
    assert first["exported"] == 1
    second = export_source(client, "users", workdir, table=ALL_TABLES["users"])
    assert "already complete" in second["skipped"]

    dry = export_source(client, "users", workdir, table=ALL_TABLES["users"], dry_run=True)
    assert dry["dry_run"] is True and dry["snapshot"] is None


# ---------------------------------------------------------------------------
# full-run manifest + rollback (remove exactly the manifest's rows / restore)
# ---------------------------------------------------------------------------

def _run_full(tmp_path, **kw):
    client = make_client()
    fake = FakeCosmos()
    manifest = run_full(
        client, fake, str(tmp_path),
        table_overrides=dict(ALL_TABLES),
        run_id="run-test", now=NOW,
        **kw,
    )
    return manifest, fake


def test_full_run_manifest_and_counts(tmp_path):
    manifest, fake = _run_full(tmp_path)
    assert manifest["ok"] is True
    assert manifest["run_id"] == "run-test"
    # users/demos/voices/activity: 1 row each; history: 1 calls + 2 transcripts + 1 event
    counts = {name: res["counts"] for name, res in manifest["sources"].items()}
    assert counts["users"]["transformed_rows"] == 1
    assert counts["history"]["transformed_rows"] == 4
    assert counts["history"]["exported"] == 1
    assert all(c["transform_rejects"] == 0 and c["rejected"] == 0 for c in counts.values())
    assert len(manifest["rows"]) == 8
    assert manifest["reconcile"]["ok"] is True

    import json
    with open(os.path.join(str(tmp_path), "manifest.json"), encoding="utf-8") as f:
        assert json.load(f)["run_id"] == "run-test"


def test_rollback_removes_exactly_manifest_rows(tmp_path):
    manifest, fake = _run_full(tmp_path)
    planned = {(r["container"], r["id"]) for r in manifest["rows"]}
    assert len(planned) == 8

    # non-manifest row + a manifest row already gone
    fake("users").upsert_item({"id": "eve", "username": "eve", "password_hash": "x"})
    fake("users").delete_item("alice", "alice")

    report = rollback_manifest(manifest, fake, mode="remove", apply=True)
    assert report["planned"] == 8
    assert report["missing"] == 1  # alice was already gone — idempotent, not an error
    assert report["done"] == 7 and report["ok"] is True

    # exactly the manifest's rows are gone; eve survives
    assert fake("users").read_item("eve", "eve")["username"] == "eve"
    for container, item_id in planned - {("users", "alice")}:
        assert all(k[1] != item_id for k in fake(container).docs), (container, item_id)


def test_rollback_dry_run_touches_nothing(tmp_path):
    manifest, fake = _run_full(tmp_path)
    before = {name: dict(fake(name).docs) for name in
              ("users", "demos", "voices", "activity", "calls", "transcripts", "call_events")}
    report = rollback_manifest(manifest, fake, mode="remove", apply=False)
    assert report["done"] == 8 and report["applied"] is False
    after = {name: dict(fake(name).docs) for name in before}
    assert after == before


def test_rollback_restore_readds_rows(tmp_path):
    manifest, fake = _run_full(tmp_path)
    rollback_manifest(manifest, fake, mode="remove", apply=True)
    assert fake("calls").docs == {}

    report = rollback_manifest(
        manifest, fake, mode="restore", apply=True, base_dir=str(tmp_path)
    )
    assert report["done"] == 8 and report["ok"] is True
    assert fake("users").read_item("alice", "alice")["username"] == "alice"
    assert fake("calls").read_item("call-1", "+15551234")["call_id"] == "call-1"
    assert len(fake("transcripts").docs) == 2 and len(fake("call_events").docs) == 1


def test_rollback_requires_explicit_yes_for_writes(tmp_path):
    # The CLI gate (--yes) maps to apply=False by default; the library default
    # is also non-destructive — a plain call must not delete anything.
    manifest, fake = _run_full(tmp_path)
    rollback_manifest(manifest, fake, mode="remove")
    assert len(fake("users").docs) == 1
