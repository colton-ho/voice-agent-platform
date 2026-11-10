"""HistoryRecorder integration tests.

Uses FakeCosmos (storage.cosmos provider seam) in place of the old moto
DynamoDB table; the real bot.py module is loaded with history enabled.

Verifies:
  - finalize() writes a row with ttl, turn_count, summary_status='pending'
    and the buffered turns.
  - The follow-up _summarize_and_update task overwrites summary_status to
    'ok' and stores summary.model + summary.generated_at.
  - On Bedrock failure the row's summary_status flips to 'failed'.
  - HISTORY_DISABLED=1 short-circuits to _history is None and creates no
    AWS clients.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import time

import pytest

from fake_cosmos import FakeCosmos
from storage import cosmos
from storage.cosmos_calls import CosmosCallRepository


def _import_bot_with_env(env: dict) -> object:
    """Drop bot module state then re-import so module-level _history sees env."""
    for mod in list(sys.modules):
        if mod in ("bot", "runtime_config", "demo_loader"):
            del sys.modules[mod]
    for k, v in env.items():
        os.environ[k] = v
    return importlib.import_module("bot")


@pytest.fixture
def aws_creds(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("MINIMAX_API_KEY", "x")
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    yield


def test_history_enabled_by_default(aws_creds, monkeypatch):
    # B10（BC-c1）：HISTORY_TABLE 退役后默认启用，HISTORY_DISABLED=1 才禁用。
    monkeypatch.delenv("HISTORY_DISABLED", raising=False)
    bot = _import_bot_with_env({})
    assert bot._history is not None
    assert bot.HISTORY_DISABLED is False


def test_history_disabled_via_flag(aws_creds, monkeypatch):
    monkeypatch.setenv("HISTORY_DISABLED", "1")
    bot = _import_bot_with_env({"HISTORY_DISABLED": "1"})
    assert bot._history is None
    assert bot.HISTORY_DISABLED is True


def test_finalize_writes_pending_then_ok(aws_creds, monkeypatch):
    monkeypatch.setenv("HISTORY_TTL_DAYS", "7")
    monkeypatch.delenv("HISTORY_DISABLED", raising=False)

    fake = FakeCosmos()
    cosmos.set_container_provider(fake)
    try:
        bot = _import_bot_with_env({
            "HISTORY_TTL_DAYS": "7",
        })
        assert bot._history is not None

        # Patch out the Bedrock call so the test is hermetic.
        async def fake_summary(turns, lang_key, **_):
            return {
                "intent": "Stub intent",
                "key_questions": ["q1"],
                "action_items": [],
                "sentiment": "positive",
                "model": "stub-model-id",
                "generated_at": int(time.time()),
            }
        monkeypatch.setattr(bot, "_invoke_summary_bedrock", fake_summary)

        call_id = "test-call-001"
        started = time.time() - 10  # call lasted ~10s
        bot._history.attach(call_id, {
            "caller": "+15551234567",
            "started_at": started,
            "engine": "nova-sonic",
            "lang": "en-US",
            "scenario": "default",
            "provider": "polly",
            "voice": "tiffany",
            # A-4 裁定(a)：model 欄=選擇 key（鏡像運行時寫入 bot.py:6079/:7111），動態防朽
            "model": bot.DEFAULT_MODEL,
            "minimax_model": "speech-2.8-turbo",
        })
        bot._history.append(call_id, {"who": "user", "text": "hello", "t": 0.5})
        bot._history.append(call_id, {"who": "bot", "text": "hi there", "t": 1.2})

        async def run():
            await bot._history.finalize(call_id)
            # finalize() spawned the summary task; let it run to completion.
            for t in asyncio.all_tasks() - {asyncio.current_task()}:
                await t

        asyncio.run(run())

        # 读回走独立 repo 实例（等价旧 table.get_item）；_read_row 把 ttl
        # 曝光为删除点 epoch（§1.3-5 语义 1:1），断言形状不动。
        item = CosmosCallRepository(ttl_days=30).get_history_doc(call_id)
    finally:
        cosmos.reset_container_provider()

    assert item is not None, "row not written"
    assert item["call_id"] == call_id
    assert item["caller"] == "+15551234567"
    assert int(item["turn_count"]) == 2
    # ttl = started + 7 days
    expected_ttl = int(started) + 7 * 86400
    assert abs(int(item["ttl"]) - expected_ttl) <= 2
    # summary_status flipped from pending → ok by the spawned task
    assert item["summary_status"] == "ok"
    summary = item["summary"]
    assert summary["intent"] == "Stub intent"
    assert summary["model"] == "stub-model-id"
    assert "generated_at" in summary
    # turns persisted
    assert len(item["turns"]) == 2
    assert item["turns"][0]["who"] == "user"


def test_finalize_failed_summary_marks_status(aws_creds, monkeypatch):
    monkeypatch.delenv("HISTORY_DISABLED", raising=False)

    fake = FakeCosmos()
    cosmos.set_container_provider(fake)
    try:
        bot = _import_bot_with_env({})

        async def boom(turns, lang_key, **_):
            raise RuntimeError("bedrock unreachable")
        monkeypatch.setattr(bot, "_invoke_summary_bedrock", boom)

        call_id = "test-call-fail"
        bot._history.attach(call_id, {
            "caller": "+15550000000",
            "started_at": time.time(),
            "engine": "nova-sonic",
            "lang": "en-US",
            "scenario": "default",
            "provider": "polly",
            "voice": "tiffany",
            # A-4 裁定(a)：model 欄=選擇 key（鏡像運行時寫入 bot.py:6079/:7111），動態防朽
            "model": bot.DEFAULT_MODEL,
            "minimax_model": "",
        })
        bot._history.append(call_id, {"who": "user", "text": "hi", "t": 0.1})

        async def run():
            await bot._history.finalize(call_id)
            for t in asyncio.all_tasks() - {asyncio.current_task()}:
                await t

        asyncio.run(run())

        item = CosmosCallRepository(ttl_days=30).get_history_doc(call_id)
    finally:
        cosmos.reset_container_provider()

    assert item is not None
    assert item["summary_status"] == "failed"
    assert "summary_error" in item
    assert "bedrock" in item["summary_error"].lower()


def test_coerce_summary_struct_handles_fenced_json(aws_creds, monkeypatch):
    bot = _import_bot_with_env({})
    fenced = '```json\n{"intent":"x","key_questions":["q"],"action_items":[],"sentiment":"neutral"}\n```'
    out = bot._coerce_summary_struct(fenced)
    assert out["intent"] == "x"
    assert out["key_questions"] == ["q"]
    assert out["sentiment"] == "neutral"


def test_coerce_summary_struct_degrades_on_garbage(aws_creds):
    bot = _import_bot_with_env({})
    out = bot._coerce_summary_struct("not json at all")
    assert out["intent"].startswith("not json")
    assert out["key_questions"] == []
    assert out["sentiment"] == "neutral"
