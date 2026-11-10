"""T1 backend: demo one-click translate + extended PATCH (localized write-back)
+ whitelist-anchored localized-field discovery + detail missing_langs.

These mirror the patterns in test_admin_rest_tools.py: bypass auth via
dependency_overrides, build isolated tmp_path manifest dirs, point DEMO_LOADER
at them, and hit the FastAPI app via TestClient. Bedrock is ALWAYS mocked — no
real LLM calls.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def admin_password(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pwd")
    monkeypatch.setenv("MINIMAX_API_KEY", "x")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    yield


@pytest.fixture
def fresh_runtime_json(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    monkeypatch.setenv("RUNTIME_CFG_PATH_OVERRIDE", str(cfg_dir / "runtime.json"))
    yield


def _import_app():
    for mod in list(sys.modules):
        if mod in ("bot", "runtime_config", "demo_loader", "user_store"):
            del sys.modules[mod]
    bot = importlib.import_module("bot")
    _admin = {"username": "admin", "role": "admin", "disabled": False, "created_at": 0}
    bot.app.dependency_overrides[bot.require_user] = lambda: _admin
    bot.app.dependency_overrides[bot.require_admin] = lambda: _admin
    return bot


def _make_demo(data_root: Path, demo_id: str, manifest_text: str, *, kb: str | None = "kb body") -> Path:
    demo_dir = data_root / demo_id
    demo_dir.mkdir(parents=True)
    if kb is not None:
        (demo_dir / "kb.md").write_text(kb)
    (demo_dir / "manifest.yaml").write_text(manifest_text)
    return demo_dir


class _MemStore:
    """In-memory stand-in for the DDB-backed DemoStore exposing the interface
    the PATCH/translate endpoints use (get/list/rescan + async put). DDB-as-truth
    means PATCH no longer writes manifest.yaml, so the legacy disk DemoLoader (no
    ``put``) can't back the PATCH path; this shim exercises the real
    merge → put → rescan flow without a moto round-trip. ``put`` normalizes
    through the SAME shared helper as DemoStore (canonical ``tool_ids`` key)."""

    def __init__(self, data_root):
        from demo_loader import DemoLoader, normalize_demo_dict

        self._normalize = normalize_demo_dict
        self._cache = {
            demo_id: normalize_demo_dict(demo, validate_tools=False)
            for demo_id, demo in DemoLoader(str(data_root))._cache.items()
        }
        self.last_skipped = []

    def get(self, demo_id):
        return self._cache.get(demo_id)

    def list(self):
        from demo_loader import kb_chars_of

        out = [
            {"id": d["id"], "label": d["label"], "lang": d["lang"],
             "kb_chars": kb_chars_of(d.get("kb_body"))}
            for d in self._cache.values()
        ]
        out.sort(key=lambda x: x["id"] or "")
        return out

    async def put(self, demo_dict):
        canonical = self._normalize(demo_dict, validate_tools=False)
        self._cache[canonical["id"]] = canonical

    async def rescan(self):
        return len(self._cache)


def _bot_with_demo(tmp_path, manifest_text, demo_id="trans-demo", kb="kb body"):
    data_root = tmp_path / "data"
    data_root.mkdir()
    _make_demo(data_root, demo_id, manifest_text, kb=kb)
    bot = _import_app()

    # DDB-as-truth: the PATCH path writes via DEMO_LOADER.put (no manifest.yaml),
    # so back DEMO_LOADER with an in-memory store seeded from the disk manifest
    # rather than a put-less disk DemoLoader.
    bot.DEMO_LOADER = _MemStore(data_root)
    assert bot.DEMO_LOADER.get(demo_id) is not None
    return bot, data_root


_MANIFEST_KBPATH = """\
# top comment — must survive PATCH
id: trans-demo
label: Trans Demo
lang: zh-CN
tools: []
system:
  zh-CN: |
    你係客服。调用 verifyCustomer 然后 requestRepair。错误码 CUSTOMER_NOT_FOUND。
  en-US: |
    You are support. Call verifyCustomer.
greeting:
  zh-CN: 你好
  en-US: hi
kb_intro:
  zh-CN: 知识库介绍
kb_path: kb.md
"""


# ---------------------------------------------------------------------------
# AC1 — _demo_localized_fields whitelist-anchored, EXCLUDES kb_body
# ---------------------------------------------------------------------------
def test_localized_fields_whitelist_and_excludes_kb_body(fresh_runtime_json):
    bot = _import_app()
    demo = {
        "id": "x",
        "label": "X",
        "lang": "zh-CN",
        "tags": ["a"],
        "tools": ["end_call"],
        "mcp_servers": ["srv"],
        "system": {"zh-CN": "sys", "en-US": "sys-en"},
        "greeting": {"zh-CN": "hi"},
        "kb_intro": {"zh-CN": "intro"},
        "kb_ack": None,  # present-but-None optional → skipped
        # kb_body injected as a {lang: text} dict (kb_path demo) — MUST be excluded
        "kb_body": {"zh-CN": "KB body text", "en-US": "kb en"},
    }
    out = bot._demo_localized_fields(demo)
    assert "system" in out
    assert "greeting" in out
    assert "kb_intro" in out
    # kb_ack was None → not returned
    assert "kb_ack" not in out
    # BLOCKER: kb_body must NOT be returned even though it's a {lang:text} dict
    assert "kb_body" not in out
    # non-localized keys must never appear
    for k in ("id", "label", "lang", "tags", "tools", "mcp_servers"):
        assert k not in out


def test_localized_fields_empty_when_no_dicts(fresh_runtime_json):
    bot = _import_app()
    demo = {"id": "x", "system": {}, "greeting": {"zh-CN": "   "}, "kb_body": {"zh-CN": "x"}}
    # empty system dict and whitespace-only greeting → nothing localized
    assert bot._demo_localized_fields(demo) == []


# ---------------------------------------------------------------------------
# AC4 — detail missing_langs / present_langs
# ---------------------------------------------------------------------------
def test_detail_missing_and_present_langs(tmp_path, fresh_runtime_json):
    from fastapi.testclient import TestClient

    bot, _ = _bot_with_demo(tmp_path, _MANIFEST_KBPATH)
    client = TestClient(bot.app)
    r = client.get("/api/admin/demos/trans-demo")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["present_langs"]) == {"zh-CN", "en-US"}
    # ordering follows LANGUAGES declaration order
    assert body["present_langs"] == [k for k in bot.LANGUAGES if k in {"zh-CN", "en-US"}]
    # every LANGUAGES key not in system map is missing
    for lk in bot.LANGUAGES:
        if lk in {"zh-CN", "en-US"}:
            assert lk not in body["missing_langs"]
        else:
            assert lk in body["missing_langs"]
    # present + missing == all LANGUAGES, disjoint
    assert set(body["present_langs"]) | set(body["missing_langs"]) == set(bot.LANGUAGES)
    assert not (set(body["present_langs"]) & set(body["missing_langs"]))


# ---------------------------------------------------------------------------
# AC2 — POST /translate
# ---------------------------------------------------------------------------
def test_translate_invalid_target_lang_400(tmp_path, fresh_runtime_json):
    from fastapi.testclient import TestClient

    bot, _ = _bot_with_demo(tmp_path, _MANIFEST_KBPATH)
    client = TestClient(bot.app)
    r = client.post("/api/admin/demos/trans-demo/translate", json={"target_lang": "xx-YY"})
    assert r.status_code == 400, r.text
    assert "target_lang" in r.json()["detail"]


def test_translate_no_source_demo_400(tmp_path, fresh_runtime_json):
    from fastapi.testclient import TestClient

    # A valid demo with empty localized fields → no source → 400.
    manifest = """\
id: empty-demo
label: Empty
lang: en-US
tools: []
system:
  en-US: ""
greeting:
  en-US: ""
kb_path: kb.md
"""
    bot, _ = _bot_with_demo(tmp_path, manifest, demo_id="empty-demo")
    client = TestClient(bot.app)
    r = client.post("/api/admin/demos/empty-demo/translate", json={"target_lang": "fr-FR"})
    assert r.status_code == 400, r.text
    assert "no localized content" in r.json()["detail"] or "no usable source" in r.json()["detail"]


def test_translate_success_and_system_instruction(tmp_path, fresh_runtime_json, monkeypatch):
    from fastapi.testclient import TestClient

    bot, _ = _bot_with_demo(tmp_path, _MANIFEST_KBPATH)

    captured = {}

    def _fake_make_managed_llm(*, model=None, env=None):
        captured["model"] = model

        class _FakeLLM:
            async def complete(self, *, messages, max_tokens=None):
                captured["messages"] = messages
                captured["max_tokens"] = max_tokens
                return (
                    '{"system": "FR system verifyCustomer", '
                    '"greeting": "Bonjour", "kb_intro": "intro fr"}'
                )

        return _FakeLLM()

    monkeypatch.setattr(bot, "make_managed_llm", _fake_make_managed_llm)

    client = TestClient(bot.app)
    r = client.post(
        "/api/admin/demos/trans-demo/translate",
        json={"target_lang": "fr-FR", "source_lang": "zh-CN"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target_lang"] == "fr-FR"
    assert set(body["fields"]) == {"system", "greeting", "kb_intro"}
    assert body["source_used"]["system"] == "zh-CN"
    # already_exists false for fr-FR (not present yet)
    assert body["already_exists"]["system"] is False

    # system_instruction must carry the identifier-preservation directive.
    si = next(m.content for m in captured["messages"] if m.role == "system")
    assert "verifyCustomer" in si
    assert "requestRepair" in si
    assert "woNumber" in si
    assert "customerId" in si
    assert "smart version" in si and "premium version" in si and "elite version" in si
    assert "CUSTOMER_NOT_FOUND" in si and "IDENTITY_EXPIRED" in si
    assert "【" in si  # section-header structure mention

    # Disk untouched — no fr-FR written.
    refreshed = bot.DEMO_LOADER.get("trans-demo")
    assert "fr-FR" not in refreshed["system"]

    # When the caller does not specify a model, the translate path must default
    # to the strong TRANSLATE_MODEL — NOT the conversational DEFAULT_MODEL
    # (nova-2-lite echoes long prompts back untranslated).
    assert captured["model"] == bot.MODELS[bot.TRANSLATE_MODEL]
    assert bot.MODELS[bot.TRANSLATE_MODEL] != bot.MODELS[bot.DEFAULT_MODEL]
    # A-7/F6：翻譯 4096 原值透傳到 FC-3 調用面
    assert captured["max_tokens"] == 4096


def test_translate_per_field_source_fallback(tmp_path, fresh_runtime_json, monkeypatch):
    from fastapi.testclient import TestClient

    # greeting only has en-US; system has zh-CN. With source_lang=zh-CN,
    # greeting must independently fall back to en-US.
    manifest = """\
id: fb-demo
label: FB
lang: zh-CN
tools: []
system:
  zh-CN: 系统提示
greeting:
  en-US: hello only english
kb_path: kb.md
"""
    bot, _ = _bot_with_demo(tmp_path, manifest, demo_id="fb-demo")

    class _FakeLLM:
        async def complete(self, *, messages, max_tokens=None):
            return '{"system": "sys-fr", "greeting": "greet-fr"}'

    monkeypatch.setattr(bot, "make_managed_llm", lambda **kw: _FakeLLM())
    client = TestClient(bot.app)
    r = client.post(
        "/api/admin/demos/fb-demo/translate",
        json={"target_lang": "fr-FR", "source_lang": "zh-CN"},
    )
    assert r.status_code == 200, r.text
    su = r.json()["source_used"]
    assert su["system"] == "zh-CN"
    assert su["greeting"] == "en-US"  # independent per-field fallback


def test_translate_parse_failure_502_no_write(tmp_path, fresh_runtime_json, monkeypatch):
    from fastapi.testclient import TestClient

    bot, _ = _bot_with_demo(tmp_path, _MANIFEST_KBPATH)
    class _FakeLLM:
        async def complete(self, *, messages, max_tokens=None):
            return "I'm sorry, I cannot produce JSON for this."  # unparseable

    monkeypatch.setattr(bot, "make_managed_llm", lambda **kw: _FakeLLM())
    client = TestClient(bot.app)
    r = client.post(
        "/api/admin/demos/trans-demo/translate",
        json={"target_lang": "fr-FR", "source_lang": "zh-CN"},
    )
    assert r.status_code == 502, r.text
    assert "translation failed" in r.json()["detail"]
    # No disk write on failure.
    refreshed = bot.DEMO_LOADER.get("trans-demo")
    assert "fr-FR" not in refreshed["system"]


# ---------------------------------------------------------------------------
# AC3 — PATCH localized write-back
# ---------------------------------------------------------------------------
def test_patch_localized_rejects_kb_body(tmp_path, fresh_runtime_json):
    from fastapi.testclient import TestClient

    bot, _ = _bot_with_demo(tmp_path, _MANIFEST_KBPATH)
    client = TestClient(bot.app)
    r = client.patch(
        "/api/admin/demos/trans-demo",
        json={"localized": {"kb_body": {"fr-FR": "phantom"}}},
    )
    assert r.status_code == 400, r.text
    assert "kb_body" in r.json()["detail"]


def test_patch_localized_rejects_unknown_field(tmp_path, fresh_runtime_json):
    from fastapi.testclient import TestClient

    bot, _ = _bot_with_demo(tmp_path, _MANIFEST_KBPATH)
    client = TestClient(bot.app)
    r = client.patch(
        "/api/admin/demos/trans-demo",
        json={"localized": {"label": {"fr-FR": "x"}}},
    )
    assert r.status_code == 400, r.text


def test_patch_localized_rejects_unknown_lang(tmp_path, fresh_runtime_json):
    from fastapi.testclient import TestClient

    bot, _ = _bot_with_demo(tmp_path, _MANIFEST_KBPATH)
    client = TestClient(bot.app)
    r = client.patch(
        "/api/admin/demos/trans-demo",
        json={"localized": {"system": {"xx-YY": "x"}}},
    )
    assert r.status_code == 400, r.text
    assert "xx-YY" in r.json()["detail"]


def test_patch_localized_existing_lang_requires_overwrite(tmp_path, fresh_runtime_json):
    from fastapi.testclient import TestClient

    bot, data_root = _bot_with_demo(tmp_path, _MANIFEST_KBPATH)
    client = TestClient(bot.app)
    # zh-CN already exists in system → 400 without overwrite.
    r = client.patch(
        "/api/admin/demos/trans-demo",
        json={"localized": {"system": {"zh-CN": "覆盖?"}}},
    )
    assert r.status_code == 400, r.text
    assert "overwrite" in r.json()["detail"]
    # Stored zh-CN unchanged (no partial write before the 400).
    assert bot.DEMO_LOADER.get("trans-demo")["system"]["zh-CN"] != "覆盖?"


def test_patch_localized_writes_new_lang_preserves_others(
    tmp_path, fresh_runtime_json
):
    from fastapi.testclient import TestClient

    bot, data_root = _bot_with_demo(tmp_path, _MANIFEST_KBPATH)
    client = TestClient(bot.app)
    r = client.patch(
        "/api/admin/demos/trans-demo",
        json={
            "localized": {
                "system": {"fr-FR": "Système FR verifyCustomer"},
                "greeting": {"fr-FR": "Bonjour"},
            }
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "fr-FR" in body["present_langs"]

    # Store reflects the new lang AND preserves sibling langs (fine-grained
    # localized merge). DDB-as-truth: read back via DEMO_LOADER.get, not disk.
    refreshed = bot.DEMO_LOADER.get("trans-demo")
    assert refreshed["system"]["fr-FR"] == "Système FR verifyCustomer"
    assert refreshed["system"]["zh-CN"]  # sibling lang still there
    assert refreshed["system"]["en-US"]
    assert refreshed["greeting"]["fr-FR"] == "Bonjour"


def test_patch_localized_overwrite_replaces(tmp_path, fresh_runtime_json):
    from fastapi.testclient import TestClient

    bot, data_root = _bot_with_demo(tmp_path, _MANIFEST_KBPATH)
    client = TestClient(bot.app)
    r = client.patch(
        "/api/admin/demos/trans-demo",
        json={"localized": {"greeting": {"zh-CN": "新的问候"}}, "overwrite": True},
    )
    assert r.status_code == 200, r.text
    refreshed = bot.DEMO_LOADER.get("trans-demo")
    assert refreshed["greeting"]["zh-CN"] == "新的问候"


def test_patch_localized_creates_missing_optional_field(tmp_path, fresh_runtime_json):
    from fastapi.testclient import TestClient

    # Manifest has no kb_ack at all; writing kb_ack should create the map.
    bot, data_root = _bot_with_demo(tmp_path, _MANIFEST_KBPATH)
    client = TestClient(bot.app)
    r = client.patch(
        "/api/admin/demos/trans-demo",
        json={"localized": {"kb_ack": {"zh-CN": "确认收到"}}},
    )
    assert r.status_code == 200, r.text
    refreshed = bot.DEMO_LOADER.get("trans-demo")
    assert refreshed["kb_ack"]["zh-CN"] == "确认收到"


# ---------------------------------------------------------------------------
# AC5 — zero regression: omitting localized leaves the tools path working
# ---------------------------------------------------------------------------
def test_patch_without_localized_tools_path_unchanged(tmp_path, fresh_runtime_json):
    from fastapi.testclient import TestClient

    manifest = """\
id: zr-demo
label: ZR
lang: en-US
tools: []
mcp_servers: []
system:
  en-US: |
    you are a bot
greeting:
  en-US: hi
kb_path: kb.md
"""
    bot, data_root = _bot_with_demo(tmp_path, manifest, demo_id="zr-demo")
    client = TestClient(bot.app)
    r = client.patch("/api/admin/demos/zr-demo", json={"tools": ["end_call"]})
    assert r.status_code == 200, r.text
    refreshed = bot.DEMO_LOADER.get("zr-demo")
    # Tools land under the canonical tool_ids key (BLOCKER #1).
    assert refreshed["tool_ids"] == ["end_call"]
    # kb_body still a string (from kb_path), not a phantom per-lang dict.
    assert isinstance(refreshed["kb_body"], str)


def test_patch_empty_body_400(tmp_path, fresh_runtime_json):
    from fastapi.testclient import TestClient

    bot, _ = _bot_with_demo(tmp_path, _MANIFEST_KBPATH)
    client = TestClient(bot.app)
    r = client.patch("/api/admin/demos/trans-demo", json={})
    assert r.status_code == 400, r.text
    # overwrite-only body is still "empty" content-wise.
    r2 = client.patch("/api/admin/demos/trans-demo", json={"overwrite": True})
    assert r2.status_code == 400, r2.text
