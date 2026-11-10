"""U-003 tests — spec §11 validation matrix (R-C-001/002/003, decision D-4).

矩阵行逐条参数化（U-003 自测要求）；名字清单在此独立成列以钉死矩阵，
config_contract.py 的清单若漂移会被这些用例直接抓住。
"""
from pathlib import Path

import pytest

from config_contract import AppConfig, ConfigError, load_config

REQUIRED_NAMES = (
    "APP_ENV", "AZURE_LOCATION", "PUBLIC_CONTROL_BASE_URL", "PUBLIC_MEDIA_BASE_URL",
    "TELEPHONY_PROVIDER", "VOICE_ENGINE", "STT_PROVIDER", "TTS_PROVIDER",
    "LLM_PROVIDER", "LLM_ENDPOINT", "LLM_DEPLOYMENT", "SPEECH_REGION",
    "STT_LOCALE", "TTS_VOICE", "COSMOS_ENDPOINT", "COSMOS_DATABASE",
    "BLOB_ACCOUNT_URL", "SERVICEBUS_NAMESPACE", "KEY_VAULT_URL",
    "MAX_CONCURRENT_CALLS_PER_REPLICA", "MAX_CALL_DURATION_SECONDS",
    "TRANSCRIPT_RETENTION_DAYS", "APPLICATIONINSIGHTS_CONNECTION_STRING",
)
INT_NAMES = ("MAX_CONCURRENT_CALLS_PER_REPLICA", "MAX_CALL_DURATION_SECONDS", "TRANSCRIPT_RETENTION_DAYS")
BOOL_NAMES = ("RECORDING_ENABLED", "VOICE_LIVE_ENABLED")
TWILIO_NAMES = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN_SECRET_NAME")
OPTIONAL_NAMES = ("LLM_API_VERSION", "SPEECH_ENDPOINT", "REDIS_ENDPOINT")

# 合法基线：23 必填 + twilio 条件必填，单用例只破坏一处
BASE_ENV = {
    "APP_ENV": "development",
    "AZURE_LOCATION": "eastus",
    "PUBLIC_CONTROL_BASE_URL": "https://control.example",
    "PUBLIC_MEDIA_BASE_URL": "wss://media.example",
    "TELEPHONY_PROVIDER": "twilio",
    "VOICE_ENGINE": "cascade",
    "STT_PROVIDER": "azure_speech",
    "TTS_PROVIDER": "azure_speech",
    "LLM_PROVIDER": "azure_openai",
    "LLM_ENDPOINT": "https://llm.example",
    "LLM_DEPLOYMENT": "gpt-deployment",
    "SPEECH_REGION": "eastus",
    "STT_LOCALE": "en-US",
    "TTS_VOICE": "en-US-JennyNeural",
    "COSMOS_ENDPOINT": "https://cosmos.example",
    "COSMOS_DATABASE": "voicebot",
    "BLOB_ACCOUNT_URL": "https://blob.example",
    "SERVICEBUS_NAMESPACE": "sb.example",
    "KEY_VAULT_URL": "https://vault.example",
    "MAX_CONCURRENT_CALLS_PER_REPLICA": "5",
    "MAX_CALL_DURATION_SECONDS": "3600",
    "TRANSCRIPT_RETENTION_DAYS": "30",
    "APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=test",
    "TWILIO_ACCOUNT_SID": "ACtest",
    "TWILIO_AUTH_TOKEN_SECRET_NAME": "twilio-auth-token",
}

# spec §11 块逐行核对值（.env.example 验收 5 的期望集）
SPEC_BLOCK = {
    "APP_ENV": "development",
    "AZURE_LOCATION": "<validated-region>",
    "PUBLIC_CONTROL_BASE_URL": "https://<control-host>",
    "PUBLIC_MEDIA_BASE_URL": "wss://<media-host>",
    "TELEPHONY_PROVIDER": "twilio",
    "VOICE_ENGINE": "cascade",
    "STT_PROVIDER": "azure_speech",
    "TTS_PROVIDER": "azure_speech",
    "LLM_PROVIDER": "<approved-adapter>",
    "LLM_ENDPOINT": "<verified-endpoint>",
    "LLM_DEPLOYMENT": "<exact-approved-deployment>",
    "LLM_API_VERSION": "<verified-if-required>",
    "SPEECH_REGION": "<validated-speech-region>",
    "SPEECH_ENDPOINT": "<verified-endpoint-if-required>",
    "STT_LOCALE": "<validated-locale>",
    "TTS_VOICE": "<validated-voice>",
    "COSMOS_ENDPOINT": "<account-endpoint>",
    "COSMOS_DATABASE": "voicebot",
    "BLOB_ACCOUNT_URL": "<account-url>",
    "SERVICEBUS_NAMESPACE": "<fully-qualified-namespace>",
    "REDIS_ENDPOINT": "<if-coordination-enabled>",
    "KEY_VAULT_URL": "<vault-url>",
    "TWILIO_ACCOUNT_SID": "<account-id>",
    "TWILIO_AUTH_TOKEN_SECRET_NAME": "<key-vault-secret-name>",
    "MAX_CONCURRENT_CALLS_PER_REPLICA": "<load-tested-value>",
    "MAX_CALL_DURATION_SECONDS": "<approved-limit>",
    "RECORDING_ENABLED": "false",
    "TRANSCRIPT_RETENTION_DAYS": "<approved-value>",
    "VOICE_LIVE_ENABLED": "false",
    "APPLICATIONINSIGHTS_CONNECTION_STRING": "<environment-specific-value>",
}


def _env(**overrides):
    env = dict(BASE_ENV)
    for key, value in overrides.items():
        if value is ...:
            env.pop(key, None)
        else:
            env[key] = value
    return env


# --- 矩阵行：必填非空（23 行 × 缺失/空白 两种破坏） -------------------------


@pytest.mark.parametrize("name", REQUIRED_NAMES)
def test_required_missing_reports_placeholder_with_meaning(name):
    with pytest.raises(ConfigError) as excinfo:
        load_config(_env(**{name: ...}))
    assert f"{name} (" in str(excinfo.value)   # 名称 + 含义 + 问题（R-C-001）


@pytest.mark.parametrize("name", REQUIRED_NAMES)
def test_required_blank_counts_as_missing(name):
    with pytest.raises(ConfigError) as excinfo:
        load_config(_env(**{name: "   "}))
    assert f"{name} (" in str(excinfo.value)


# --- 矩阵行：整数 > 0（3 行 × 4 种非法值） ---------------------------------


@pytest.mark.parametrize("name", INT_NAMES)
@pytest.mark.parametrize("bad", ["0", "-1", "abc", "1.5"])
def test_int_vars_must_parse_as_positive_int(name, bad):
    with pytest.raises(ConfigError) as excinfo:
        load_config(_env(**{name: bad}))
    assert f"{name} (" in str(excinfo.value)


# --- 矩阵行：默认布尔（2 行）：仅默认这两个，且严格字面量 -------------------


@pytest.mark.parametrize("name", BOOL_NAMES)
def test_defaulted_bool_unset_defaults_false(name):
    cfg = load_config(_env(**{name: ...}))
    assert getattr(cfg, name.lower()) is False


@pytest.mark.parametrize("name", BOOL_NAMES)
@pytest.mark.parametrize("bad", ["yes", "1", "True"])
def test_defaulted_bool_rejects_any_other_value(name, bad):
    with pytest.raises(ConfigError) as excinfo:
        load_config(_env(**{name: bad}))
    msg = str(excinfo.value)
    assert f"{name} (" in msg                                # 指名违规变量（CR-2）
    assert "must be 'true' or 'false' (case-sensitive)" in msg  # 明示合法字面量 + 大小写敏感（CR-2）
    assert repr(bad) in msg                                  # 指名收到的值，禁止裸 "invalid value"


@pytest.mark.parametrize("name", BOOL_NAMES)
def test_defaulted_bool_empty_string_treated_as_unset(name):
    # 空串视同未设：走默认 false，不报错
    cfg = load_config(_env(**{name: ""}))
    assert getattr(cfg, name.lower()) is False


@pytest.mark.parametrize("name", BOOL_NAMES)
def test_defaulted_bool_accepts_true_literal(name):
    cfg = load_config(_env(**{name: "true"}))
    assert getattr(cfg, name.lower()) is True


# --- 矩阵行：twilio 条件必填（2 行） ---------------------------------------


def test_twilio_vars_required_when_provider_is_twilio():
    with pytest.raises(ConfigError) as excinfo:
        load_config(_env(TWILIO_ACCOUNT_SID=..., TWILIO_AUTH_TOKEN_SECRET_NAME=...))
    for name in TWILIO_NAMES:
        assert f"{name} (" in str(excinfo.value)


def test_twilio_vars_optional_for_other_providers():
    cfg = load_config(_env(TELEPHONY_PROVIDER="other",
                           TWILIO_ACCOUNT_SID=..., TWILIO_AUTH_TOKEN_SECRET_NAME=...))
    assert cfg.twilio_account_sid is None
    assert cfg.twilio_auth_token_secret_name is None


# --- 矩阵行：可选（3 行） ---------------------------------------------------


@pytest.mark.parametrize("name", OPTIONAL_NAMES)
def test_optional_defaults_none(name):
    cfg = load_config(_env(**{name: ...}))
    assert getattr(cfg, name.lower()) is None


@pytest.mark.parametrize("name", OPTIONAL_NAMES)
def test_optional_accepts_value(name):
    cfg = load_config(_env(**{name: "value"}))
    assert getattr(cfg, name.lower()) == "value"


# --- 验收 1：一次列全（R-C-001） -------------------------------------------


def test_all_violations_reported_at_once():
    with pytest.raises(ConfigError) as excinfo:
        load_config(_env(AZURE_LOCATION=..., KEY_VAULT_URL=..., MAX_CALL_DURATION_SECONDS="abc"))
    names = [v.split(" (")[0] for v in excinfo.value.violations]
    assert sorted(names) == ["AZURE_LOCATION", "KEY_VAULT_URL", "MAX_CALL_DURATION_SECONDS"]


# --- 验收 2：不设选型 → 失败，绝不回退（R-C-002） --------------------------


def test_provider_selection_never_falls_back():
    dropped = ["LLM_PROVIDER", "LLM_ENDPOINT", "LLM_DEPLOYMENT", "STT_PROVIDER", "TTS_PROVIDER"]
    env = _env(**{name: ... for name in dropped})
    with pytest.raises(ConfigError) as excinfo:
        load_config(env)
    for name in dropped:
        assert f"{name} (" in str(excinfo.value)


# --- 验收 3：两个默认 false 仅解析并暴露（R-C-002） ------------------------


def test_flag_defaults_exposed_on_config():
    cfg = load_config(_env(RECORDING_ENABLED=..., VOICE_LIVE_ENABLED=...))
    assert cfg.recording_enabled is False
    assert cfg.voice_live_enabled is False
    assert isinstance(cfg, AppConfig)


# --- 验收 4：旧 AWS 变量环境失败并只列 §11 名（R-C-003 方向） --------------


def test_legacy_only_environment_lists_section11_names():
    legacy = {
        "AWS_REGION": "us-east-1",
        "USERS_TABLE": "users",
        "TWILIO_AUTH_TOKEN": "t",
        "BEDROCK_MODEL_ID": "us.x",
    }
    with pytest.raises(ConfigError) as excinfo:
        load_config(legacy)
    message = str(excinfo.value)
    assert "AZURE_LOCATION (" in message
    assert "LLM_ENDPOINT (" in message
    # 旧名从不被读取：不出现在任何违规里
    for name in legacy:
        assert name not in message
    assert len(excinfo.value.violations) == len(REQUIRED_NAMES)


# --- 验收 5：.env.example 仅占位符（等价于 secret 扫描） -------------------


def test_env_example_contains_only_spec_placeholders():
    example = Path(__file__).resolve().parent.parent / ".env.example"
    values = {}
    for line in example.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition("=")
        values[name] = value
    # 逐字核对 spec §11 块：任何真实值都会偏离占位符集而被抓住
    assert values == SPEC_BLOCK
