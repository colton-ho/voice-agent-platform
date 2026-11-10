"""T1 acceptance tests — engine-aware language model + validation.

Covers the per-engine LANGUAGES model added to bot.py:
- ``_languages_for_engine`` partitions languages correctly per engine.
- Nova-only languages exist with ``stt=None``; post-C-3 (decision (a), ruling B
  2026-11-09) they carry ``engines=[]`` — Voice-Live-gated out of every engine
  (assertions below re-pointed accordingly).
- Both options payloads (``_admin_options_payload`` and ``/api/config``)
  expose ``engines`` on each ``languages`` item.
- ``_validate_segment`` enforces engine↔lang compatibility on the MERGED
  effective state (existing stored segment overlaid with the partial update),
  including the partial-update case (only ``lang`` changes).
- Zero regression: the original 5 languages remain valid under ``pipeline``.

These import ``bot.py`` directly (no DB / WS / Bedrock) for the unit checks,
and use a TestClient with the fake Cosmos users container for the end-to-end
PUT validation, mirroring tests/test_admin_api.py.
"""

from __future__ import annotations

import importlib
import sys

import pytest
from fastapi import HTTPException

from fake_cosmos import FakeCosmos
from storage import cosmos


_ADMIN_PWD = "test-pwd"

# Languages the design says Nova Sonic supports (and only Nova).
NOVA_ONLY_LANGS = ["en-GB", "en-AU", "en-IN", "fr-FR", "it-IT", "de-DE", "es-US", "pt-BR"]
ORIGINAL_PIPELINE_LANGS = ["zh-HK", "zh-CN", "en-US", "ja-JP", "en-SG"]


@pytest.fixture(autouse=True)
def _common_env(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", _ADMIN_PWD)
    monkeypatch.setenv("AUTH_SECRET", "test-secret")
    monkeypatch.setenv("MINIMAX_API_KEY", "x")
    monkeypatch.setenv("SITE_PASSWORD", "")
    f = FakeCosmos()
    cosmos.set_container_provider(f)
    yield f
    cosmos.reset_container_provider()


def _import_bot():
    for mod in list(sys.modules):
        if mod in ("bot", "runtime_config", "demo_loader", "user_store"):
            del sys.modules[mod]
    return importlib.import_module("bot")


# --------------------------------------------------------------------------
# Unit: LANGUAGES model + _languages_for_engine
# --------------------------------------------------------------------------

def test_original_five_langs_engines_annotated():
    bot = _import_bot()
    L = bot.LANGUAGES
    assert L["zh-HK"]["engines"] == ["pipeline"]
    assert L["zh-CN"]["engines"] == ["pipeline"]
    assert L["ja-JP"]["engines"] == ["pipeline"]
    assert L["en-SG"]["engines"] == ["pipeline"]
    assert set(L["en-US"]["engines"]) == {"pipeline", "nova-sonic"}
    # original 5 keep a real STT enum
    for k in ORIGINAL_PIPELINE_LANGS:
        assert L[k]["stt"] is not None, k


def test_nova_only_langs_present_with_none_stt():
    bot = _import_bot()
    L = bot.LANGUAGES
    for k in NOVA_ONLY_LANGS:
        assert k in L, f"missing nova-only lang {k}"
        assert L[k]["stt"] is None, f"{k} should have stt=None"
        # Re-pointed (ruling B 2026-11-09): engines=[] gates them out of every
        # engine until Azure Voice Live claims them.
        assert L[k]["engines"] == [], k
        # in-language label/prompt/greeting must be present and non-empty
        assert L[k]["label"]
        assert L[k]["prompt"]
        assert L[k]["greeting"]


def test_languages_for_engine_nova():
    bot = _import_bot()
    nova = bot._languages_for_engine("nova-sonic")
    # en-US keeps its historical dual engines list (untouched per A-3); the 8
    # Nova-only langs are gated to engines=[] -> excluded (ruling B 2026-11-09).
    assert "en-US" in nova
    for k in NOVA_ONLY_LANGS:
        assert k not in nova
    # does NOT contain pipeline-only langs
    for k in ("zh-HK", "zh-CN", "ja-JP", "en-SG"):
        assert k not in nova
    # C-3 re-point: the built-in Nova roster is emptied, so the old
    # "engine langs == nova voice locales" invariant becomes the empty table.
    assert bot.NOVA_SONIC_VOICES == {}
    nova_voice_locales = {v["locale"] for v in bot.NOVA_SONIC_VOICES.values()}
    assert nova_voice_locales == set()


def test_languages_for_engine_pipeline():
    bot = _import_bot()
    pipe = bot._languages_for_engine("pipeline")
    assert set(pipe) == set(ORIGINAL_PIPELINE_LANGS)
    for k in NOVA_ONLY_LANGS:
        assert k not in pipe


# --------------------------------------------------------------------------
# Unit: _validate_segment merged-state engine↔lang cross-check
# --------------------------------------------------------------------------

def _raises_400(fn):
    with pytest.raises(HTTPException) as exc:
        fn()
    assert exc.value.status_code == 400
    return exc.value


def test_validate_rejects_nova_with_chinese():
    bot = _import_bot()
    _raises_400(lambda: bot._validate_segment({"engine": "nova-sonic", "lang": "zh-CN"}))


def test_validate_rejects_pipeline_with_nova_only_lang():
    bot = _import_bot()
    # fr-FR has stt=None -> not available on pipeline
    _raises_400(lambda: bot._validate_segment({"engine": "pipeline", "lang": "fr-FR"}))


def test_validate_accepts_valid_combos():
    bot = _import_bot()
    # should NOT raise
    bot._validate_segment({"engine": "pipeline", "lang": "zh-HK"})
    # Re-pointed (ruling B 2026-11-09): engine nova-sonic left ENGINES, so
    # every nova combo is rejected at the engine check.
    _raises_400(lambda: bot._validate_segment({"engine": "nova-sonic", "lang": "en-US"}))
    _raises_400(lambda: bot._validate_segment({"engine": "nova-sonic", "lang": "fr-FR"}))


def test_validate_original_five_under_pipeline_all_valid():
    """Zero-regression: every original lang is valid under pipeline."""
    bot = _import_bot()
    for k in ORIGINAL_PIPELINE_LANGS:
        bot._validate_segment({"engine": "pipeline", "lang": k})  # must not raise


def test_validate_partial_update_lang_only_uses_stored_engine():
    """Only `lang` changes; stored engine drives the MERGED-state cross-check.
    Re-pointed data (ruling B 2026-11-09) to the selectable engine: switching
    to a Nova-only lang (fr-FR, engines=[]) must be rejected based on the
    MERGED state."""
    bot = _import_bot()
    current = {"engine": "pipeline", "lang": "en-US"}
    _raises_400(lambda: bot._validate_segment({"lang": "fr-FR"}, current))
    # ...and a pipeline-compatible lang switch passes
    bot._validate_segment({"lang": "zh-CN"}, current)


def test_validate_partial_update_engine_only_uses_stored_lang():
    """Only `engine` changes; stored lang is fr-FR (nova-only). Switching engine
    to pipeline must be rejected (fr-FR has no STT)."""
    bot = _import_bot()
    current = {"engine": "nova-sonic", "lang": "fr-FR"}
    _raises_400(lambda: bot._validate_segment({"engine": "pipeline"}, current))


def test_validate_no_current_back_compat():
    """Called with only updates (no current) still works for full-segment use."""
    bot = _import_bot()
    bot._validate_segment({"engine": "pipeline", "lang": "en-US"})


# --------------------------------------------------------------------------
# Unit: options payloads expose engines
# --------------------------------------------------------------------------

def test_admin_options_payload_has_engines_on_languages():
    bot = _import_bot()
    payload = bot._admin_options_payload()
    langs = {l["id"]: l for l in payload["languages"]}
    assert "engines" in langs["en-US"]
    assert set(langs["en-US"]["engines"]) == {"pipeline", "nova-sonic"}
    assert langs["zh-HK"]["engines"] == ["pipeline"]
    for k in NOVA_ONLY_LANGS:
        # Re-pointed (ruling B 2026-11-09): Voice-Live-gated -> engines=[]
        assert langs[k]["engines"] == []


def test_admin_options_payload_exposes_default_nova_sonic_voice():
    """T3 parity fix: the admin options payload must expose
    ``default_nova_sonic_voice`` (it already exists on /api/config) so the
    DefaultsForm normalizes the voice-on-engine-switch to the true default
    instead of falling back to the first nova voice id."""
    bot = _import_bot()
    payload = bot._admin_options_payload()
    assert payload["default_nova_sonic_voice"] == bot.DEFAULT_NOVA_SONIC_VOICE
    # Re-pointed 默認值一致性 (ruling B 2026-11-09): C-3 empties the roster and
    # invalidates the default — the key survives (shape frozen) with content
    # consistent with the empty table (nothing resolves to a ghost id).
    assert payload["default_nova_sonic_voice"] == ""
    assert payload["default_nova_sonic_voice"] not in bot.NOVA_SONIC_VOICES


# --------------------------------------------------------------------------
# End-to-end: /api/config + PUT validation via TestClient
# --------------------------------------------------------------------------

def _admin_client(bot):
    # ADMIN_PASSWORD first-boot seed removed: create the bootstrap admin
    # explicitly, then start the app and log in.
    import asyncio
    from fastapi.testclient import TestClient
    asyncio.run(bot.USER_STORE.create("admin", _ADMIN_PWD, role="admin"))
    client = TestClient(bot.app, base_url="https://testserver")
    client.__enter__()
    r = client.post("/api/auth/login", json={"username": "admin", "password": _ADMIN_PWD})
    assert r.status_code == 200, r.text
    return client


def _isolate_runtime(bot, tmp_path):
    """Belt-and-braces: repoint bot.RUNTIME_CONFIG at a temp-file-backed
    instance so PUTs in these tests don't mutate the repo's config/runtime.json.
    (Since the R-C-003 seam bot.py honors RUNTIME_CFG_PATH_OVERRIDE — set by
    each test below — so this re-point is redundant but kept defensive.)"""
    import runtime_config as rc
    bot.RUNTIME_CONFIG = rc.RuntimeConfig(
        path=str(tmp_path / "runtime.json"),
        fallback=bot._RUNTIME_FALLBACK,
    )


def test_api_config_languages_have_engines(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_CFG_PATH_OVERRIDE", str(tmp_path / "runtime.json"))
    bot = _import_bot()
    _isolate_runtime(bot, tmp_path)
    client = _admin_client(bot)
    r = client.get("/api/config")
    assert r.status_code == 200, r.text
    langs = {l["id"]: l for l in r.json()["languages"]}
    assert set(langs["en-US"]["engines"]) == {"pipeline", "nova-sonic"}
    # Re-pointed (ruling B 2026-11-09): Voice-Live-gated -> engines=[]
    assert langs["fr-FR"]["engines"] == []


def test_put_web_rejects_incompatible_engine_lang(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_CFG_PATH_OVERRIDE", str(tmp_path / "runtime.json"))
    bot = _import_bot()
    _isolate_runtime(bot, tmp_path)
    client = _admin_client(bot)
    # Establish a known pipeline baseline (don't rely on on-disk default).
    # Re-pointed data (ruling B 2026-11-09): nova-sonic left ENGINES.
    r = client.put("/api/admin/config/web", json={"engine": "pipeline", "lang": "en-US"})
    assert r.status_code == 200, r.text
    # Partial update: change ONLY lang to a Nova-only lang. Stored engine
    # stays pipeline; merged state (pipeline + fr-FR) is incompatible -> 400.
    r = client.put("/api/admin/config/web", json={"lang": "fr-FR"})
    assert r.status_code == 400, r.text

    # full valid switch to pipeline+zh-HK passes
    r = client.put("/api/admin/config/web", json={"engine": "pipeline", "lang": "zh-HK"})
    assert r.status_code == 200, r.text


def test_put_phone_rejects_pipeline_with_nova_only_lang(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_CFG_PATH_OVERRIDE", str(tmp_path / "runtime.json"))
    bot = _import_bot()
    _isolate_runtime(bot, tmp_path)
    client = _admin_client(bot)
    # pipeline + fr-FR (stt None) -> 400
    r = client.put("/api/admin/config/phone", json={"engine": "pipeline", "lang": "fr-FR"})
    assert r.status_code == 400, r.text
    # Re-pointed (ruling B 2026-11-09): nova-sonic + fr-FR is also rejected
    # — engine nova-sonic left ENGINES (was accepted pre-C-3).
    r = client.put("/api/admin/config/phone", json={"engine": "nova-sonic", "lang": "fr-FR"})
    assert r.status_code == 400, r.text
