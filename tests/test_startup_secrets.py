"""B-5 CR-C1 三件套契约测试：启动期取密钥（B-5①）+ B12 fail-fast（B-5②）。

- resolve_secret：env-only（secretRef 注入/测试缝）。deviation ④/U-401：KV 直读兜底
  （_kv_secret）已裁撤——缺 env 即空串，任何情况下不得转 Key Vault 直读。
- resolve_api_keys：CR-C1 注入缝取值形状（build_cascade_engine 三 api_key）。
- B12：AUTH_SECRET 缺值 fail-fast（ephemeral fallback 已退役；兜底裁撤后依旧 fail-fast）。
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


def _install_fake_kv_sdk(monkeypatch, value: str) -> list:
    """装假 azure KV SDK（identity + keyvault.secrets），get_secret 固定回 value。

    兜底已裁撤：钉的是「任何情况下 KV 直读都不得被触碰」——返回的 calls 必须为空。
    """
    calls: list = []
    fake_identity = types.ModuleType("azure.identity")
    fake_identity.DefaultAzureCredential = lambda: None
    fake_kv_pkg = types.ModuleType("azure.keyvault")
    fake_secrets = types.ModuleType("azure.keyvault.secrets")

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def get_secret(self, name):
            calls.append(name)
            return types.SimpleNamespace(value=value)

    fake_secrets.SecretClient = _FakeClient
    for name, mod in {
        "azure.identity": fake_identity,
        "azure.keyvault": fake_kv_pkg,
        "azure.keyvault.secrets": fake_secrets,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return calls


def test_resolve_secret_env_wins_without_kv(monkeypatch):
    import bot

    monkeypatch.setenv("MINIMAX_API_KEY", "env-val")
    calls = _install_fake_kv_sdk(monkeypatch, "kv-val")
    assert bot.resolve_secret("MINIMAX_API_KEY", "MINIMAX-API-KEY") == "env-val"
    assert calls == []


def test_resolve_secret_env_only_no_kv_fallback(monkeypatch):
    import bot

    # deviation ④/U-401 裁撤钉：_kv_secret 不复存在；缺 env 回空串（=MI 模式语义）
    assert not hasattr(bot, "_kv_secret")
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    calls = _install_fake_kv_sdk(monkeypatch, "kv-val")
    assert bot.resolve_secret("MINIMAX_API_KEY", "MINIMAX-API-KEY") == ""
    # 兜底若复活（lazy import KV 直读）：get_secret 被调或回 "kv-val" ≠ "" → 本钉必红
    assert calls == []


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


def test_auth_secret_env_only_fail_fast_even_with_kv_sdk(monkeypatch):
    # deviation ④/U-401：AUTH_SECRET 只认 env——KV SDK 在场也禁止直读，缺值仍 fail-fast。
    # 兜底若复活：假 SDK 会把 AUTH_SECRET 解析成 "kv-auth"、import 不再 raise → 本钉必红
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    monkeypatch.setenv("KEY_VAULT_URL", "https://kv.test")
    calls = _install_fake_kv_sdk(monkeypatch, "kv-auth")
    with pytest.raises(RuntimeError, match="AUTHSECRET"):
        _reimport_bot(monkeypatch, {})
    assert calls == []


def test_minimax_tts_missing_key_names_injection_channel(monkeypatch):
    import bot

    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("KEY_VAULT_URL", raising=False)
    with pytest.raises(RuntimeError, match="MINIMAX-API-KEY"):
        bot._build_tts("minimax", "tiffany", region="us-east-1", frozen=None, aio_session=None)
