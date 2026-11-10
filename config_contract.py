"""U-003 — spec §11 configuration contract: AppConfig + load_config + ConfigError.

单一定义点：此处冻结哪些 §11 占位符必填/条件必填/默认/可选（实现计划 U-003
validation matrix）。纯函数 loader，零 SDK 依赖（与 FC 契约同约束）。
失败哲学（R-C-001/002, D-4）：一次性列出全部违规，绝不静默回退；
除 RECORDING_ENABLED / VOICE_LIVE_ENABLED 外不存在任何默认值。
"""
from collections.abc import Mapping
from dataclasses import dataclass

# 占位符含义——随每条违规一起列出（R-C-001），来源 spec §11
_MEANINGS = {
    "APP_ENV": "deployment environment name",
    "AZURE_LOCATION": "validated Azure region",
    "PUBLIC_CONTROL_BASE_URL": "public control-plane base URL (https://)",
    "PUBLIC_MEDIA_BASE_URL": "public media WebSocket base URL (wss://)",
    "TELEPHONY_PROVIDER": "telephony provider name (e.g. twilio)",
    "VOICE_ENGINE": "voice engine selection (cascade resolves FC-2+FC-3+FC-4; any other value resolves FC-5)",
    "STT_PROVIDER": "speech-to-text provider name (e.g. azure_speech)",
    "TTS_PROVIDER": "text-to-speech provider name (e.g. azure_speech)",
    "LLM_PROVIDER": "approved LLM adapter name",
    "LLM_ENDPOINT": "verified LLM endpoint URL",
    "LLM_DEPLOYMENT": "exact approved LLM deployment name",
    "LLM_API_VERSION": "verified LLM API version (if the adapter requires it)",
    "SPEECH_REGION": "validated Azure Speech region",
    "SPEECH_ENDPOINT": "verified Speech endpoint URL (if required)",
    "STT_LOCALE": "validated recognition locale",
    "TTS_VOICE": "validated synthesis voice",
    "COSMOS_ENDPOINT": "Cosmos DB account endpoint",
    "COSMOS_DATABASE": "Cosmos database name",
    "BLOB_ACCOUNT_URL": "Blob storage account URL",
    "SERVICEBUS_NAMESPACE": "fully qualified Service Bus namespace",
    "REDIS_ENDPOINT": "Redis endpoint (only if coordination is enabled)",
    "KEY_VAULT_URL": "Key Vault URL",
    "TWILIO_ACCOUNT_SID": "Twilio account SID",
    "TWILIO_AUTH_TOKEN_SECRET_NAME": "Key Vault secret name holding the Twilio auth token",
    "MAX_CONCURRENT_CALLS_PER_REPLICA": "load-tested concurrency cap per replica (integer > 0)",
    "MAX_CALL_DURATION_SECONDS": "approved max call duration in seconds (integer > 0)",
    "RECORDING_ENABLED": "call recording toggle (strict true/false, default false)",
    "TRANSCRIPT_RETENTION_DAYS": "approved transcript retention in days (integer > 0)",
    "VOICE_LIVE_ENABLED": "Voice Live realtime toggle (strict true/false, default false)",
    "APPLICATIONINSIGHTS_CONNECTION_STRING": "Application Insights connection string",
}

# 冻结的验证矩阵（U-003 卡）——四类，不多不少：
_REQUIRED = (
    "APP_ENV", "AZURE_LOCATION", "PUBLIC_CONTROL_BASE_URL", "PUBLIC_MEDIA_BASE_URL",
    "TELEPHONY_PROVIDER", "VOICE_ENGINE", "STT_PROVIDER", "TTS_PROVIDER",
    "LLM_PROVIDER", "LLM_ENDPOINT", "LLM_DEPLOYMENT", "SPEECH_REGION",
    "STT_LOCALE", "TTS_VOICE", "COSMOS_ENDPOINT", "COSMOS_DATABASE",
    "BLOB_ACCOUNT_URL", "SERVICEBUS_NAMESPACE", "KEY_VAULT_URL",
    "MAX_CONCURRENT_CALLS_PER_REPLICA", "MAX_CALL_DURATION_SECONDS",
    "TRANSCRIPT_RETENTION_DAYS", "APPLICATIONINSIGHTS_CONNECTION_STRING",
)
_INT_REQUIRED = ("MAX_CONCURRENT_CALLS_PER_REPLICA", "MAX_CALL_DURATION_SECONDS", "TRANSCRIPT_RETENTION_DAYS")
_CONDITIONAL_TWILIO = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN_SECRET_NAME")
_DEFAULTED_FALSE = ("RECORDING_ENABLED", "VOICE_LIVE_ENABLED")
_OPTIONAL = ("LLM_API_VERSION", "SPEECH_ENDPOINT", "REDIS_ENDPOINT")


class ConfigError(Exception):
    """Carries ALL violations at once (R-C-001), each naming its placeholder + meaning."""

    def __init__(self, violations):
        self.violations = tuple(violations)
        super().__init__("configuration invalid: " + "; ".join(self.violations))


@dataclass(frozen=True)
class AppConfig:
    # 一个字段对应一个 §11 占位符（小写蛇形），共 30 个
    app_env: str
    azure_location: str
    public_control_base_url: str
    public_media_base_url: str
    telephony_provider: str
    voice_engine: str
    stt_provider: str
    tts_provider: str
    llm_provider: str
    llm_endpoint: str
    llm_deployment: str
    llm_api_version: str | None
    speech_region: str
    speech_endpoint: str | None
    stt_locale: str
    tts_voice: str
    cosmos_endpoint: str
    cosmos_database: str
    blob_account_url: str
    servicebus_namespace: str
    redis_endpoint: str | None
    key_vault_url: str
    twilio_account_sid: str | None
    twilio_auth_token_secret_name: str | None
    max_concurrent_calls_per_replica: int
    max_call_duration_seconds: int
    recording_enabled: bool
    transcript_retention_days: int
    voice_live_enabled: bool
    applicationinsights_connection_string: str


def _fmt(name: str, problem: str) -> str:
    return f"{name} ({_MEANINGS[name]}): {problem}"


def load_config(env: Mapping[str, str]) -> AppConfig:
    """Pure: reads ONLY the §11 names from env (never legacy AWS-era names).

    R-C-003 方向：旧变量（AWS_REGION/USERS_TABLE/…）根本不被读取——
    旧环境只会因 §11 必填缺失而整体失败并被指名。
    """
    violations: list[str] = []
    raw: dict[str, str] = {}

    for name in _REQUIRED:
        value = (env.get(name) or "").strip()
        if not value:
            violations.append(_fmt(name, "missing or empty"))
        else:
            raw[name] = value

    ints: dict[str, int] = {}
    for name in _INT_REQUIRED:
        value = raw.get(name)
        if value is None:
            continue  # 缺失已报，不重复计违规
        try:
            parsed = int(value)
        except ValueError:
            violations.append(_fmt(name, f"must be an integer > 0, got {value!r}"))
            continue
        if parsed <= 0:
            violations.append(_fmt(name, f"must be an integer > 0, got {value!r}"))
        else:
            ints[name] = parsed

    bools: dict[str, bool] = {}
    for name in _DEFAULTED_FALSE:
        value = (env.get(name) or "").strip()
        if not value:
            bools[name] = False                       # 仅有的两个默认值（R-C-002）
        elif value in ("true", "false"):
            bools[name] = value == "true"             # 严格字面量，其他值一律报错
        else:
            violations.append(_fmt(name, f"must be 'true' or 'false' (case-sensitive), got {value!r}"))  # CR-2：错误文案须指名变量+合法字面量+大小写敏感

    optionals = {name: (env.get(name) or "").strip() or None for name in _OPTIONAL}

    # 条件必填：仅当 TELEPHONY_PROVIDER=twilio（U-003 矩阵冻结）
    twilio = {}
    if raw.get("TELEPHONY_PROVIDER") == "twilio":
        for name in _CONDITIONAL_TWILIO:
            value = (env.get(name) or "").strip()
            if not value:
                violations.append(_fmt(name, "required when TELEPHONY_PROVIDER=twilio"))
            else:
                twilio[name] = value
    else:
        for name in _CONDITIONAL_TWILIO:
            twilio[name] = (env.get(name) or "").strip() or None

    if violations:
        raise ConfigError(violations)

    return AppConfig(
        app_env=raw["APP_ENV"],
        azure_location=raw["AZURE_LOCATION"],
        public_control_base_url=raw["PUBLIC_CONTROL_BASE_URL"],
        public_media_base_url=raw["PUBLIC_MEDIA_BASE_URL"],
        telephony_provider=raw["TELEPHONY_PROVIDER"],
        voice_engine=raw["VOICE_ENGINE"],
        stt_provider=raw["STT_PROVIDER"],
        tts_provider=raw["TTS_PROVIDER"],
        llm_provider=raw["LLM_PROVIDER"],
        llm_endpoint=raw["LLM_ENDPOINT"],
        llm_deployment=raw["LLM_DEPLOYMENT"],
        llm_api_version=optionals["LLM_API_VERSION"],
        speech_region=raw["SPEECH_REGION"],
        speech_endpoint=optionals["SPEECH_ENDPOINT"],
        stt_locale=raw["STT_LOCALE"],
        tts_voice=raw["TTS_VOICE"],
        cosmos_endpoint=raw["COSMOS_ENDPOINT"],
        cosmos_database=raw["COSMOS_DATABASE"],
        blob_account_url=raw["BLOB_ACCOUNT_URL"],
        servicebus_namespace=raw["SERVICEBUS_NAMESPACE"],
        redis_endpoint=optionals["REDIS_ENDPOINT"],
        key_vault_url=raw["KEY_VAULT_URL"],
        twilio_account_sid=twilio["TWILIO_ACCOUNT_SID"],
        twilio_auth_token_secret_name=twilio["TWILIO_AUTH_TOKEN_SECRET_NAME"],
        max_concurrent_calls_per_replica=ints["MAX_CONCURRENT_CALLS_PER_REPLICA"],
        max_call_duration_seconds=ints["MAX_CALL_DURATION_SECONDS"],
        recording_enabled=bools["RECORDING_ENABLED"],
        transcript_retention_days=ints["TRANSCRIPT_RETENTION_DAYS"],
        voice_live_enabled=bools["VOICE_LIVE_ENABLED"],
        applicationinsights_connection_string=raw["APPLICATIONINSIGHTS_CONNECTION_STRING"],
    )
