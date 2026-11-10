"""U-104 — MCP sigv4 降级行为测试（decision (c) FINAL 2026-11-06）。

冻结卡（implementation-plan §5 U-104）：auth.type=sigv4 在 config/UI 保持合法，
连接期经现有 MissingCredentialsError 路径（mcp_sigv4.py:41-46）降级为
{ok:false, error}，绝不崩呼叫；botocore/boto3 import 结构不动（cutover 前照跑）。

退役注（cutover/F）——本文件原为 SigV4 签名单测，以下旧断言在 U-104 退役：
- test_sigv4_auth_adds_authorization_and_date
- test_sigv4_auth_adds_security_token_for_temp_creds
- test_sigv4_signature_covers_body
- test_params_is_streamable_subclass_with_auth_in_dump
- test_params_accepted_by_pipecat_mcpclient
退役依据：decision (c) 裁定运行态 mcp_sigv4.py 与 botocore 在 cutover（U-501/F）
整体退役，签名不再是 Azure 路径验收面。
原 test_signed_headers_excludes_volatile_httpx_headers（403 SignedHeaders 回归钉）
不随批退役——评审 F-08 裁定回补（见文末「回补钉」）：mcp_sigv4.py cutover 前是
活码，其稳定头 allowlist 被「顺手重构」破坏会表现为线上 403 静默回归。回补与退役
机制自洽：cutover（U-501/F）mcp_sigv4.py 退役时此钉随之退役。

保留钉：MissingCredentialsError 类型化错误（mcp_sigv4.py:41-46 行为冻结）。
新增钉：(1) sigv4 配置面合法 + store 落库保留；(2) /test 降级包
{ok:false, tools:[], error}（bot.py:5199）；(3) 呼叫路径连接跳过（bot.py:1940-1945）。
"""
from __future__ import annotations

import importlib
import sys

import pytest

import mcp_sigv4


def _no_aws_creds(monkeypatch):
    import boto3

    class _NoCredSession:
        def get_credentials(self):
            return None

    monkeypatch.setattr(boto3, "Session", lambda *a, **k: _NoCredSession())


@pytest.fixture
def bot_mod(monkeypatch, tmp_path):
    monkeypatch.setenv("MINIMAX_API_KEY", "x")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pwd")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("MCP_CFG_PATH_OVERRIDE", str(tmp_path / "mcp_servers.json"))
    for mod in list(sys.modules):
        if mod in ("bot", "runtime_config", "demo_loader", "mcp_config", "user_store"):
            del sys.modules[mod]
    return importlib.import_module("bot")


def _sigv4_server_payload(**overrides):
    base = {
        "id": "agentcore-prod",
        "label": "AgentCore",
        "transport": "streamable_http",
        "url": "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/foo/invocations",
        "headers": {},
        "enabled": True,
        "auth": {"type": "sigv4", "service": "bedrock-agentcore", "region": "us-east-1"},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 降级路径之根：缺失凭据的类型化错误（mcp_sigv4.py:41-46 冻结行为）
# ---------------------------------------------------------------------------
def test_missing_credentials_raises_typed_error(monkeypatch):
    _no_aws_creds(monkeypatch)
    with pytest.raises(mcp_sigv4.MissingCredentialsError):
        mcp_sigv4.resolve_credentials()
    # 高层构造器透出同一类型错误——bot.py 两条降级路径都只认它
    with pytest.raises(mcp_sigv4.MissingCredentialsError):
        mcp_sigv4.make_sigv4_streamable_params("https://x.amazonaws.com/mcp")


# ---------------------------------------------------------------------------
# (1) auth.type=sigv4 配置面保持合法（decision (c)：config/UI 合法性冻结）
# ---------------------------------------------------------------------------
def test_sigv4_auth_stays_valid_in_config_surface(tmp_path):
    import mcp_config

    canonical = {"type": "sigv4", "service": "bedrock-agentcore", "region": "us-east-1"}
    assert mcp_config.validate_auth(dict(canonical)) == canonical
    # service/region 缺省沿用既有默认（非新增行为，钉住防漂移）；显式空串仍拒绝
    assert mcp_config.validate_auth({"type": "sigv4"}) == canonical
    with pytest.raises(ValueError, match="auth.service"):
        mcp_config.validate_auth({"type": "sigv4", "service": ""})
    # N2：auth 必须留在规范化字典里——upsert 不得剥掉 sigv4 配置
    server = mcp_config.validate_server(_sigv4_server_payload())
    assert server["auth"] == canonical

    # store 落库往返：upsert 后 get 原样保留 sigv4（config 面"保持合法"的落点）
    store = mcp_config.McpConfig(str(tmp_path / "mcp.json"))
    store.upsert(_sigv4_server_payload())
    assert store.get("agentcore-prod")["auth"] == canonical


# ---------------------------------------------------------------------------
# (2) /test 端点降级包：{ok:false, tools:[], error}（bot.py:5168-5199 冻结语义）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_test_endpoint_degrades_with_ok_false_envelope(bot_mod, monkeypatch):
    _no_aws_creds(monkeypatch)
    bot_mod.MCP_CONFIG.upsert(_sigv4_server_payload())
    result = await bot_mod.admin_mcp_servers_test("agentcore-prod")
    # 连接问题是数据不是传输错误：永远 200 + 这个信封（SPA 内联渲染依赖）
    assert result["ok"] is False
    assert result["tools"] == []
    assert "sigv4 setup failed" in result["error"]
    assert "no AWS credentials" in result["error"]  # MissingCredentialsError 文案透传


# ---------------------------------------------------------------------------
# (3) 呼叫路径连接降级：缺凭据跳过该 server，呼叫不中断（bot.py:1940-1945）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_call_connect_skips_sigv4_server_without_credentials(bot_mod, monkeypatch):
    _no_aws_creds(monkeypatch)
    bot_mod.MCP_CONFIG.upsert(_sigv4_server_payload())
    monkeypatch.setattr(bot_mod.DEMO_LOADER, "get", lambda k: {"mcp_servers": ["agentcore-prod"]})
    clients, schemas = await bot_mod._connect_mcp_clients("demo", "phone")
    assert clients == [] and schemas == []  # 降级为无 MCP 工具，呼叫照常


# ---------------------------------------------------------------------------
# 回补钉（评审 F-08）：SignedHeaders 稳定头 allowlist（403 回归类，沿用原测试名）
# ---------------------------------------------------------------------------
def test_signed_headers_excludes_volatile_httpx_headers():
    """httpx 发送前改写易变头（connection/accept-encoding/content-length/user-agent），
    任一被折进 SignedHeaders 即与线上字节不一致 → AgentCore 403 静默回归。
    静态 dummy 凭据，零 AWS 凭据链/网络依赖；cutover（U-501/F）随 mcp_sigv4.py 退役。
    """
    import httpx
    from botocore.credentials import Credentials

    req = httpx.Request(
        "POST",
        "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/foo/invocations",
        content=b'{"jsonrpc":"2.0","method":"tools/list"}',
        headers={
            "content-type": "application/json",
            "user-agent": "ua", "connection": "keep-alive",
            "accept-encoding": "gzip", "content-length": "44",
        },
    )
    auth = mcp_sigv4.sign_request_headers(
        Credentials("ak", "sk"), "bedrock-agentcore", "us-east-1", req
    )["Authorization"]
    signed = set(
        [p for p in auth.split(", ") if p.startswith("SignedHeaders=")][0]
        .split("=", 1)[1].split(";")
    )
    # 只含稳定头：allowlist（content-type/accept）+ botocore 从 URL/签名机制派生的 host/x-amz-date
    assert signed <= {"content-type", "accept", "host", "x-amz-date"}
    assert {"content-type", "host"} <= signed
    for volatile in ("user-agent", "connection", "accept-encoding", "content-length"):
        assert volatile not in signed, f"{volatile} must not be signed"
