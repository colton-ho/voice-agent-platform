"""Service Bus postcall-jobs worker（pipeline-infra-spec §2.2，R-A-010）。

单队列消费 summary job：幂等（summary_status=='ok' 直接 complete）、
LLM 调用 30s 硬预算、重试/终态收口（I-4）。wire enum 严格
{pending, ok, failed}（product-impact §7.1 钉死，不加第四值）。

失败分类（误判代价两侧都有兜底——重试上限 5 + I-4 终态收口）：
- 永久性：ValueError/TypeError/KeyError/AttributeError（调用配置非法）或
  显式 PermanentJobError → 写 failed 終態 → complete（不重试）。
- 暫時性：其餘一切（LLM/網絡/429/5xx/30s 超時）→ abandon 重投；
  **I-4 硬不变式**：最后一次投递仍暂时失败 → 走永久性分支落 failed，
  任何行不得永卡 pending。DLQ 只留进程崩溃残余（U-406 清扫，Phase E）。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# §2.2 handler 预算（bot.py _summarize_and_update 原值移 worker 侧）
SUMMARY_TIMEOUT_S = 30.0
# §2.1 队列契约 maxDeliveryCount（超限 broker 自动死信）
MAX_DELIVERY_COUNT = 5
# wire enum（product-impact §7.1）——只允许这三个值
WIRE_STATUSES = ("pending", "ok", "failed")


class PermanentJobError(Exception):
    """显式标记的永久性失败（重试无意义）→ failed 終態。"""


def is_final_delivery(delivery_count: int, max_delivery_count: int = MAX_DELIVERY_COUNT) -> bool:
    """本次投递是否已无重投机会（I-4 收口判定）。

    delivery_count 基数存在双约定：AMQP「既往投递次数」（首次=0，末投=max-1）
    与 Azure 文档「times delivered」措辞（首次=1，末投=max）。取 >= max-1
    是双约定下都保证「末投必落 failed」的安全侧——0 基恰好末投收口，
    1 基提前一轮收口（宁早收口不永卡 pending，I-4 硬不变式优先于精确边界）。
    """
    return delivery_count >= max_delivery_count - 1


class PostcallWorker:
    """peek-lock 消费 postcall-jobs：repo/ invoke/ queue 全注入（可测）。

    repo: storage.cosms_calls 形状（get_history_doc / patch_fields）。
    invoke_summary: async (turns, lang) -> summary dict（生产=bot._invoke_summary_bedrock，
    空转写短路语义在其内部保留：不发 LLM 请求写空结构）。
    queue: storage.servicebus 4 动词队列。
    """

    def __init__(
        self,
        repo,
        invoke_summary,
        queue,
        *,
        timeout_s: float = SUMMARY_TIMEOUT_S,
        max_delivery_count: int = MAX_DELIVERY_COUNT,
    ):
        self._repo = repo
        self._invoke = invoke_summary
        self._queue = queue
        self._timeout_s = timeout_s
        self._max_delivery_count = max_delivery_count

    # ---- 单消息处理（返回 "completed"/"abandoned" 供测试观察） ------------

    async def handle(self, msg) -> str:
        job = msg.body or {}
        if job.get("job_type") != "summary" or not job.get("call_id"):
            # 非 summary / 畸形信封：不投毒不重投，记日志后结算（未来
            # job_type 同队列判别，§2.1 F8 最小集）
            logger.warning(f"postcall_worker: dropping non-summary job {job!r}")
            self._queue.complete(msg)
            return "completed"

        call_id = job["call_id"]
        try:
            doc = await asyncio.to_thread(self._repo.get_history_doc, call_id)
        except Exception as e:  # noqa: BLE001 — 读失败按暂时性重试
            return await self._settle_transient(msg, call_id, f"read failed: {e}")

        if doc is None:
            # 行不存在/已过期：无处写终态，complete 防空转重投
            logger.warning(f"postcall_worker: no row for {call_id}; completing")
            self._queue.complete(msg)
            return "completed"

        if doc.get("summary_status") == "ok":
            # R-A-010 幂等：重复投递无副作用
            self._queue.complete(msg)
            return "completed"

        turns = doc.get("turns") or []
        lang = doc.get("lang") or ""
        try:
            struct = await asyncio.wait_for(
                self._invoke(turns, lang), timeout=self._timeout_s
            )
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            return await self._settle_failed(msg, call_id, e)
        except PermanentJobError as e:
            # 显式标记分支必须先于 except Exception——PermanentJobError 是
            # Exception 子类，落进泛型分支会被误判暂时性（BUG-01：白付重投）。
            return await self._settle_failed(msg, call_id, e)
        except Exception as e:  # noqa: BLE001 — 含 30s 超時/网络/429/5xx
            return await self._settle_transient(msg, call_id, e)

        # 成功（_coerce_summary_struct 永不拋：解析失败已落诊断结构=成功形态）
        try:
            await asyncio.to_thread(
                self._repo.patch_fields,
                call_id,
                {"summary": struct, "summary_status": "ok"},
                ("summary_error",),
                etag=doc.get("_etag"),  # optimistic concurrency：并发写冲突下轮重判
            )
        except Exception as e:  # noqa: BLE001 — 含 etag 冲突 → 下轮重读重试
            return await self._settle_transient(msg, call_id, f"write failed: {e}")
        self._queue.complete(msg)
        return "completed"

    # ---- 终态/重试收口 ----------------------------------------------------

    async def _settle_transient(self, msg, call_id, err) -> str:
        """暂时性失败：abandon 重投；最后一投走 I-4 永久性分支（不再投递）。"""
        if is_final_delivery(msg.delivery_count, self._max_delivery_count):
            logger.warning(
                f"postcall_worker: retry exhausted for {call_id} "
                f"(delivery_count={msg.delivery_count}); settling failed"
            )
            return await self._settle_failed(msg, call_id, err)
        logger.warning(
            f"postcall_worker: transient failure for {call_id} "
            f"(delivery_count={msg.delivery_count}): {err}; abandoning"
        )
        self._queue.abandon(msg)
        return "abandoned"

    async def _settle_failed(self, msg, call_id, err) -> str:
        """永久性/收口失败：写 failed 終態（error-as-data）→ complete。
        终态写入自身失败 → abandon（保下轮重试写入的机会，I-4 优先）。"""
        try:
            await asyncio.to_thread(
                self._repo.patch_fields,
                call_id,
                {"summary_status": "failed", "summary_error": str(err)[:500]},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"postcall_worker: failed-state write failed for {call_id}: {e}; abandoning"
            )
            self._queue.abandon(msg)
            return "abandoned"
        self._queue.complete(msg)
        return "completed"

    # ---- 进程入口（部署面 U-401 接管；此处只留可运行骨架） -----------------

    async def run_forever(self, *, poll_wait: float = 5.0) -> None:
        while True:
            msgs = await asyncio.to_thread(
                self._queue.receive, max_messages=1, max_wait=poll_wait
            )
            for msg in msgs:
                await self.handle(msg)


def main() -> None:
    import os

    from storage import servicebus
    from storage.cosmos_calls import CosmosCallRepository

    from bot import _invoke_summary_bedrock  # 惰性：worker 本体不绑 bot 重导入

    ttl_days = int(os.environ.get("HISTORY_TTL_DAYS", "30"))
    worker = PostcallWorker(
        repo=CosmosCallRepository(ttl_days=ttl_days),
        invoke_summary=_invoke_summary_bedrock,
        queue=servicebus.get_queue(servicebus.POSTCALL_QUEUE),
    )
    asyncio.run(worker.run_forever())


if __name__ == "__main__":
    main()
