"""U-101 — FC-4 合成驱动循环单点（azure_speech / minimax_tts 共用）。

is_final 单块前瞻 + cancel() 代际守卫只此一份实现：SynthesisChunk 契约要求
is_final 只能落在最后一个音频块上，而"最后"要到文本流结束才知道；cancel 收尾
竞态（cancel 后仍补发终块/收尾事件）是这段循环唯一出过 bug 的地方，合并为
单点避免两适配器各修一遍。
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable

from providers.base import SynthesisChunk


async def stream_synthesis(
    *,
    service,
    text_stream: AsyncIterator[str],
    context_id: str,
    output_format,
    is_current: Callable[[], bool],
    error_label: str,
) -> AsyncIterator[SynthesisChunk]:
    """驱动 service.run_tts(text, context_id)，产出带代际守卫的 SynthesisChunk 流。

    service 协议：run_tts(text, context_id) 产出带 .audio（非空才计数）或
    .error（非 None 即失败）的帧。is_current() 为 False 表示 cancel() 已中止
    本代合成——立即停止产出（含终块），幂等退出。
    """
    pending: SynthesisChunk | None = None
    async for text in text_stream:
        if not is_current():
            return  # cancel() 已中止本代合成
        if not (text or "").strip():
            continue
        async for frame in service.run_tts(text, context_id):
            if not is_current():
                return
            error = getattr(frame, "error", None)
            if error is not None:
                raise RuntimeError(f"{error_label}: {error}")
            audio = getattr(frame, "audio", None) or b""
            if not audio:
                continue
            chunk = SynthesisChunk(audio=audio, format=output_format, is_final=False)
            if pending is not None:
                yield pending
            pending = chunk
    if pending is not None and is_current():
        yield SynthesisChunk(audio=pending.audio, format=pending.format, is_final=True)
