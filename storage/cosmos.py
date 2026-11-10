"""Cosmos DB 容器解析缝（pipeline-infra-spec §1.1）。

生产：DefaultAzureCredential + COSMOS_ENDPOINT/COSMOS_DATABASE（B5 憑據接管），
SDK 全程惰性导入——未配置存储时 bot 可照常 import（HISTORY_DISABLED 降级路径）。
测试：set_container_provider 注入 in-memory 容器，不触 SDK。

容器操作是本仓自定义的窄接口（7 个动词），真实适配层 _RealContainer 把
azure-cosmos 异常翻译成本模块的三个语义错误，业务层（cosmos_calls）不感知 SDK。
"""
import os

# §1.1 冻结的 8 容器（全小写 snake）
USERS = "users"
DEMOS = "demos"
VOICES = "voices"
ACTIVITY = "activity"
CALLS = "calls"
CALL_EVENTS = "call_events"
TRANSCRIPTS = "transcripts"
USAGE = "usage"


class ContainerNotFound(Exception):
    """Item does not exist (read/replace/patch)."""


class ContainerConflict(Exception):
    """Item already exists (create)."""


class ContainerPreconditionFailed(Exception):
    """Optimistic-concurrency etag mismatch (replace/patch)."""


_provider_override = None
_real_clients: dict = {}


def set_container_provider(provider) -> None:
    """测试缝：provider(container_name) -> 容器对象（7 动词）。"""
    global _provider_override
    _provider_override = provider


def reset_container_provider() -> None:
    global _provider_override
    _provider_override = None


def get_container(name: str):
    if _provider_override is not None:
        return _provider_override(name)
    return _real_container(name)


def _real_container(name: str):
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential

    endpoint = os.environ.get("COSMOS_ENDPOINT", "").strip()
    database = os.environ.get("COSMOS_DATABASE", "").strip()
    if not endpoint or not database:
        raise RuntimeError("COSMOS_ENDPOINT/COSMOS_DATABASE not configured")
    key = (endpoint, database)
    if key not in _real_clients:
        client = CosmosClient(endpoint, credential=DefaultAzureCredential())
        _real_clients[key] = client.get_database_client(database)
    return _RealContainer(_real_clients[key].get_container_client(name))


class _RealContainer:
    """azure-cosmos ContainerProxy → 窄接口 + 异常翻译。"""

    def __init__(self, container):
        self._c = container

    def _translate(self, fn, *args, **kwargs):
        from azure.cosmos.exceptions import (
            CosmosAccessConditionFailedError,
            CosmosHttpResponseError,
            CosmosResourceExistsError,
            CosmosResourceNotFoundError,
        )

        try:
            return fn(*args, **kwargs)
        except CosmosResourceNotFoundError as e:
            raise ContainerNotFound from e
        except CosmosResourceExistsError as e:
            raise ContainerConflict from e
        except CosmosAccessConditionFailedError as e:
            raise ContainerPreconditionFailed from e
        except CosmosHttpResponseError as e:
            # 412 = etag 条件不满足（部分网关只回通用错误）
            if getattr(e, "status_code", None) == 412:
                raise ContainerPreconditionFailed from e
            raise

    def create_item(self, body: dict) -> dict:
        return self._translate(self._c.create_item, body=body)

    def upsert_item(self, body: dict) -> dict:
        return self._translate(self._c.upsert_item, body=body)

    def read_item(self, item_id: str, partition_key: str) -> dict:
        return self._translate(self._c.read_item, item=item_id, partition_key=partition_key)

    def replace_item(self, item_id: str, partition_key: str, body: dict, *, etag: str | None = None) -> dict:
        from azure.core import MatchConditions

        kwargs = {"item": item_id, "partition_key": partition_key, "body": body}
        if etag is not None:
            kwargs["etag"] = etag
            kwargs["match_condition"] = MatchConditions.IfNotModified
        return self._translate(self._c.replace_item, **kwargs)

    def patch_item(self, item_id: str, partition_key: str, ops: list, *, etag: str | None = None) -> dict:
        from azure.core import MatchConditions

        kwargs = {"item": item_id, "partition_key": partition_key, "patch_operations": ops}
        if etag is not None:
            kwargs["etag"] = etag
            kwargs["match_condition"] = MatchConditions.IfNotModified
        return self._translate(self._c.patch_item, **kwargs)

    def delete_item(self, item_id: str, partition_key: str) -> dict:
        return self._translate(self._c.delete_item, item=item_id, partition_key=partition_key)

    def query_items(self, query: str, parameters: list, *, partition_key: str | None = None,
                    max_count: int | None = None) -> list:
        kwargs = {"query": query, "parameters": parameters,
                  "enable_cross_partition_query": partition_key is None}
        if partition_key is not None:
            kwargs["partition_key"] = partition_key
        if max_count is not None:
            kwargs["max_item_count"] = max_count
        return self._translate(lambda: list(self._c.query_items(**kwargs)))
