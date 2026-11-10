# 自测记录 · U-104 MCP sigv4 降级测试（决策 (c) FINAL 2026-11-06）

批次：任务 #9（U-101/U-102/U-104）· 实现：fullstack-dev · 日期：2026-11-09
冻结卡：`docs/azure/implementation-plan.md` §5 Phase B U-104；决策 (c)（§7 ADR）：**保持 `auth.type=sigv4` 在 config/UI 合法，连接时经既有 `MissingCredentialsError` 路径降级为 `{ok:false, error}`；cutover 时 botocore 出 Azure 镜像**。

## 一、验收标准逐条自测

| # | 验收标准 | 自测方法 | 结果 | 证据 |
|---|---|---|---|---|
| 1 | `auth.type=sigv4` 在 config/UI 保持合法 | `mcp_config.validate_auth` 接受 sigv4 形状（默认 service/region），`validate_server` 保留 `auth` 键（N2 注记），`McpConfig.upsert/get` 往返 | ✅ | `test_sigv4_auth_stays_valid_in_config_surface` |
| 2 | 连接时经既有 `MissingCredentialsError` 路径降级 `{ok:false, error}` | 直调 `bot_mod.admin_mcp_servers_test("agentcore-prod")` → `{"ok": False, "tools": [], "error": "sigv4 setup failed: ... no AWS credentials ..."}`（bot.py:5168-5199 信封逐字） | ✅ | `test_test_endpoint_degrades_with_ok_false_envelope` |
| 3 | 通话路径连接同样降级（skip + 警告，不炸） | `_connect_mcp_clients("demo", "phone")` 无凭据 → `([], [])`（bot.py:1940-1945 skip-with-warning） | ✅ | `test_call_connect_skips_sigv4_server_without_credentials` |
| 4 | `mcp_sigv4.py:41-46` `MissingCredentialsError` 作为钉（既有路径的类型化错误） | `_no_aws_creds` monkeypatch `boto3.Session` → 构造签名器抛 `MissingCredentialsError`（botocore `NoCredentialsError` 转译） | ✅ | `test_missing_credentials_raises_typed_error` |
| 5 | `test_mcp_sigv4.py` 转为降级测试（旧断言退役，F/cutover 注记） | 文件重写为 4 条降级测试；模块 docstring 记退役台账 | ✅ | 本文件 §三 |
| 6 | 不动 import 结构（botocore 留到 cutover） | 零运行时代码改动：`mcp_sigv4.py`/`mcp_config.py`/`bot.py` 均未动 | ✅ | git diff 面见 §四 |

## 二、异常场景自测

| 场景 | 期望 | 结果 |
|---|---|---|
| 无 AWS 凭据 + `auth.type=sigv4` server 进 /api/admin 测试端点 | `{ok:false, tools:[], error}` 信封，error 含 "sigv4 setup failed" 与 "no AWS credentials" | ✅ |
| 无 AWS 凭据 + 通话路径连接 sigv4 server | 跳过该 server 并告警，其余照常（返回 `([], [])`） | ✅ |
| sigv4 auth 形状非法（如空 service） | `validate_auth` 拒绝（config 面校验照旧，非降级） | ✅ |
| 有凭据时（未在本环境模拟） | 既有签名路径照旧——降级只在缺凭据时触发（`MissingCredentialsError` 路径是既有代码，未改） | ✅（代码走查：mcp_sigv4.py:41-46 分支原样） |

## 三、旧测试退役台账（decision (c) + U-501/F）

旧 `tests/test_mcp_sigv4.py` 共 6 条（4 签名 + 2 参数）按决策 (c) 退役，重写为 4 条降级测试：

| 旧测试 | 内容 | 处置 |
|---|---|---|
| 签名 ×3（sigv4 规范向量类） | 签名串/头构造 | 退役——签名逻辑随 `mcp_sigv4.py` + botocore 在 cutover（U-501/F）整体退役 |
| `test_signed_headers_excludes_volatile_httpx_headers` | **403 回归钉**（SignedHeaders 排除易变头） | 退役——随签名实现退役；**cutover 期间覆盖缺口**（见 §六.1）提请评审决定是否留替代钉 |
| 参数 ×2（`validate_auth`/`validate_server` 形状） | 配置面校验 | **保留语义**并入 `test_sigv4_auth_stays_valid_in_config_surface`（sigv4 保持合法 = 决策 (c) 的一半） |
| （新增）×3 | 降级行为三面（config 合法 / admin 端点 `{ok:false}` / 通话路径 skip） | 决策 (c) 的另一半 |

## 四、涉及文件清单

**修改（重写）**
- `tests/test_mcp_sigv4.py` — 6 条旧测试退役 → 4 条降级测试（模块 docstring 记台账与出处）

**复用（钉住的既有面，零改动）**
- `mcp_sigv4.py:41-46` `MissingCredentialsError`（typed error 转译）
- `bot.py:1940-1945` 连接路径 skip-with-warning；`bot.py:5168-5199` `admin_mcp_servers_test` `{ok:false, tools:[], error}` 信封
- `mcp_config.validate_auth`/`validate_server`（auth 在 canonical dict——N2）· `McpConfig.upsert/get`
- 测试 `bot_mod` fixture 模式（env + sys.modules 清理 + fresh import），`MCP_CFG_PATH_OVERRIDE` 隔离

**未动**：`mcp_sigv4.py`、`mcp_config.py`、`bot.py`、import 结构（botocore 留到 cutover）。

## 五、自测命令与结果

```
.venv/bin/python -m pytest tests/test_mcp_sigv4.py -q --capture=sys   → 4 passed（1 warning：bot.py 导入的预存弃用告警）
批次自测（4 新/重写文件 + 5 Phase A + test_mcp_runtime）              → 234 passed, 1 warning
全量 tests/ -q --capture=sys                                          → 6 failed（全预存，见 u-101-report §七）, 966 passed
```

## 六、未覆盖项 / 遗留问题

1. **cutover 期间签名覆盖缺口**：403 回归钉（SignedHeaders 排除易变 httpx 头）随旧测试退役。签名实现在 cutover 前仍在生产（AWS 回滚路径），是否保留一条替代钉由评审决定（我不擅自留新钉——退役是决策 (c) 明示）。
2. 有凭据正路径（真实 sigv4 签名→200）未在本环境模拟（需 AWS 凭据）；既有签名测试的退役不影响该路径代码。
3. `test_admin_mcp_servers.py:402` FastMCP 子进程 skip 为环境性 flaky（phase-a-report §1.5 已录），与本卡无关。
