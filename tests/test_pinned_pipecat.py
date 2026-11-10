"""U-001 pin guard — installed pipecat must be the deployment pin, resolved
from site-packages, NOT from the vendored repo-root ./pipecat source tree.

易错点（discovery.md G-1 导入陷阱）：仓库根目录的 ./pipecat 是 1.1.x 时代
vendored 源码副本（仅参考，部署 tarball 同样排除）。未安装 pipecat-ai 时，
`import pipecat` 会命中它构成的空命名空间包——导入"成功"但 `__file__` 为
None、没有任何真实内容，会把缺失依赖伪装成正常。本文件同时钉版本与导入来源。
"""
import importlib.metadata
from pathlib import Path

import pipecat

# 与部署钉(deploy/cloudformation.yaml:427)和代码内守卫(bot.py:6090)一致
PINNED_PIPECAT = "1.4.0"

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDORED_PIPECAT_DIR = REPO_ROOT / "pipecat"


def test_installed_pipecat_version_matches_pin():
    # 版本守卫同款判定(bot.py:6143-6149)：部署钉的是发行版元数据版本
    assert importlib.metadata.version("pipecat-ai") == PINNED_PIPECAT
    assert pipecat.__version__ == PINNED_PIPECAT


def test_pipecat_imports_from_site_packages_not_vendored_tree():
    # 命名空间包陷阱的直接断言：__file__ 为 None 即落在 vendored 空包上
    assert pipecat.__file__ is not None, (
        "pipecat resolved to a namespace package (vendored ./pipecat trap); "
        "pipecat-ai is not properly installed"
    )
    resolved = Path(pipecat.__file__).resolve()
    assert "site-packages" in resolved.parts, resolved
    assert not resolved.is_relative_to(VENDORED_PIPECAT_DIR), resolved


def test_pipecat_mcp_service_import_smoke():
    # U-105（CR-3）：C5 互作闸门——pipecat/services/mcp_service.py:28 在 import 期绑定
    # mcp.client.streamable_http.streamablehttp_client，mcp 版本漂移（2.x 更名史）在此即炸
    from mcp.client.streamable_http import streamablehttp_client

    import pipecat.services.mcp_service as mcp_service

    assert streamablehttp_client is not None and mcp_service is not None
    # pipecat METADATA:115 声明 mcp[cli]<2,>=1.11.0——防 2.x 再度溜进来
    assert importlib.metadata.version("mcp").split(".")[0] == "1"
