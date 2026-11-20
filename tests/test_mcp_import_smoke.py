"""U-105 — pipecat↔mcp version-pairing smoke test.

背景（C5 互作闸门）：`pipecat/services/mcp_service.py:28` 在 import 期绑定
`mcp.client.streamable_http.streamablehttp_client`。mcp SDK 2.x 更名/挪动了
该符号，`mcp==2.3.0` 会在此直接 `ImportError: cannot import name
'streamablehttp_client' from 'mcp.client.streamable_http'`，进而拖垮 bot.py
的 live MCP tool 路径（`from pipecat.services.mcp_service import MCPClient`）。

pipecat-ai 1.4.0 的 METADATA 声明 `mcp[cli]<2,>=1.11.0`（`extra == "mcp"`），
故正确配对必须落在 1.x 区间内。本文件钉死两条 import 冒烟：
  1. `from mcp.client.streamable_http import streamablehttp_client`
  2. `from pipecat.services.mcp_service import MCPClient`

任何 mcp 版本漂移都会在此立即红灯，而不是等到 runtime 才炸。
"""
import importlib.metadata

import pytest


def test_mcp_version_within_pipecat_declared_range():
    """pipecat-ai 1.4.0 METADATA: `mcp[cli]<2,>=1.11.0`."""
    mcp_version = importlib.metadata.version("mcp")
    major, minor = (int(x) for x in mcp_version.split(".")[:2])
    assert major == 1, f"mcp {mcp_version} violates pipecat-ai 1.4.0's <2 upper bound"
    assert (major, minor) >= (1, 11), f"mcp {mcp_version} below pipecat's >=1.11.0 floor"


def test_streamablehttp_client_imports_from_mcp_client_streamable_http():
    from mcp.client.streamable_http import streamablehttp_client

    assert streamablehttp_client is not None


def test_pipecat_mcp_service_imports_mcp_client():
    from pipecat.services.mcp_service import MCPClient

    assert MCPClient is not None
