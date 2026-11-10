"""B-5 CR-C1 三件套契约测试：启动期取密钥（B-5①）+ B12 fail-fast（B-5②）。

- resolve_secret：env（secretRef 注入/测试缝）优先，缺则 §3.1 冻结 secret 名 MI 直读。
- resolve_api_keys：CR-C1 注入缝取值形状（build_cascade_engine 三 api_key）。
- B12：AUTH_SECRET 缺值 fail-fast（ephemeral fallback 已退役）。
- MiniMax 消费点缺 key 时错误文案指名注入通道。
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest


def _reimport_bot(monkeypatch, env: dict):
    for mod in ("bot", "runtime_config", "demo_loader"):
        sys.modules.pop(mod, None)
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    return importlib.import_module("bot")


def test_resolve_secret_env_wins_without_kv(monkeypatch):
    import bot

    monkeypatch.setenv("MINIMAX_API_KEY", "env-val")
    monkeypatch.setattr(bot, "_kv_secret", lambda name: pytest.fail("KV must not be touched"))
    assert bot.resolve_secret("MINIMAX_API_KEY", "MINIMAX-API-KEY") == "env-val"


def test_resolve_secret_kv_fallback_uses_frozen_secret_name(monkeypatch):
    import bot

    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    seen = {}
    monkeypatch.setattr(bot, "_kv_secret", lambda name: seen.setdefault(name, "kv-val"))
    assert bot.resolve_secret("MINIMAX_API_KEY", "MINIMAX-API-KEY") == "kv-val"
    # §3.1 secret 名冻结：解析必须按名点读，不得自造名
    assert seen == {"MINIMAX-API-KEY": "kv-val"}


def test_resolve_api_keys_feeds_cascade_seam(monkeypatch):
    import bot

    monkeypatch.setenv("SPEECH_KEY", "k-speech")
    monkeypatch.setenv("LLM_API_KEY", "k-llm")
    monkeypatch.setenv("MINIMAX_API_KEY", "k-mm")
    assert bot.resolve_api_keys() == {
        "speech_api_key": "k-speech",
        "llm_api_key": "k-llm",
        "minimax_api_key": "k-mm",
    }


def test_auth_secret_fail_fast_when_missing(monkeypatch):
    # B12：ephemeral fallback 退役——缺值拒启，错误文案指名注入通道
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    monkeypatch.delenv("KEY_VAULT_URL", raising=False)
    with pytest.raises(RuntimeError, match="AUTHSECRET"):
        _reimport_bot(monkeypatch, {})


def test_auth_secret_resolved_from_key_vault(monkeypatch):
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    monkeypatch.setenv("KEY_VAULT_URL", "https://kv.test")
    fake_identity = types.ModuleType("azure.identity")
    fake_identity.DefaultAzureCredential = lambda: None
    fake_kv_pkg = types.ModuleType("azure.keyvault")
    fake_secrets = types.ModuleType("azure.keyvault.secrets")

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def get_secret(self, name):
            assert name == "AUTHSECRET"
            return types.SimpleNamespace(value="kv-auth")

    fake_secrets.SecretClient = _FakeClient
    for name, mod in {
        "azure.identity": fake_identity,
        "azure.keyvault": fake_kv_pkg,
        "azure.keyvault.secrets": fake_secrets,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    bot = _reimport_bot(monkeypatch, {})
    assert bot.AUTH_SECRET == "kv-auth"


def test_minimax_tts_missing_key_names_injection_channel(monkeypatch):
    import bot

    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("KEY_VAULT_URL", raising=False)
    with pytest.raises(RuntimeError, match="MINIMAX-API-KEY"):
        bot._build_tts("minimax", "tiffany", region="us-east-1", frozen=None, aio_session=None)
