# -*- coding: utf-8 -*-
"""并发翻译流水线。

负责把切分好的片段安全地并发翻译完，并在失败时自救：

* **限并发**：信号量控制，避免把免费端点打挂（Google 429）。
* **退避重试**：指数退避 + 抖动，只重试可重试错误。
* **自动二分**：片段因长度相关失败时，一分为二递归重试——
  这样即使引擎实际限额比声明的更严，也能跑完而不是中断。
* **失败可续**：失败的片段单独汇总，不影响其余片段产出。
* **去重**：长文里重复片段只翻一次。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import httpx

from .chunker import Chunk, SplitResult
from .engines.base import Engine, EngineError


@dataclass
class Progress:
    total: int
    done: int = 0
    ok: int = 0
    failed: int = 0
    chars_in: int = 0
    chars_out: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return max(1e-6, time.monotonic() - self.started_at)

    @property
    def eta(self) -> float:
        if self.done < 2 or self.done >= self.total:
            return 0.0
        rate = self.done / self.elapsed
        return max(0.0, (self.total - self.done) / rate)

    def as_dict(self) -> Dict:
        return {
            "total": self.total,
            "done": self.done,
            "ok": self.ok,
            "failed": self.failed,
            "chars_in": self.chars_in,
            "chars_out": self.chars_out,
            "elapsed": round(self.elapsed, 2),
            "eta": round(self.eta, 1),
        }


@dataclass
class SegmentResult:
    text: str
    error: Optional[str] = None
    degraded: bool = False


ProgressCb = Callable[[Progress], None]


def build_client(timeout: float = 40.0, concurrency: int = 4) -> httpx.AsyncClient:
    """共享连接池的异步客户端——复用连接是长文吞吐的关键。"""
    limits = httpx.Limits(
        max_connections=max(4, concurrency * 2),
        max_keepalive_connections=max(2, concurrency),
    )
    return httpx.AsyncClient(
        limits=limits,
        follow_redirects=True,
        timeout=httpx.Timeout(timeout, connect=15.0),
        headers={"Accept-Encoding": "gzip, deflate"},
    )


async def translate_long_text(
    text: str,
    engine: Engine,
    split: SplitResult,
    src: str,
    dst: str,
    creds: Optional[Dict[str, str]] = None,
    concurrency: int = 4,
    max_retries: int = 4,
    progress_cb: Optional[ProgressCb] = None,
    client: Optional[httpx.AsyncClient] = None,
    cancel: Optional[asyncio.Event] = None,
) -> List[SegmentResult]:
    """并发翻译所有片段，按原顺序返回结果。

    ``text`` 只用于统计字数，片段本身来自 ``split``。
    """
    chunks: List[Chunk] = split.chunks
    results: List[SegmentResult] = [SegmentResult(text="") for _ in chunks]
    if not chunks:
        return results

    own_client = client is None
    client = client or build_client(concurrency=concurrency)
    sem = asyncio.Semaphore(max(1, concurrency))
    prog = Progress(total=len(chunks), chars_in=len(text))

    # 相同正文只翻一次，长文里重复段落很常见
    memo: Dict[str, str] = {}

    def bump(ok: bool, src_len: int, out_len: int) -> None:
        prog.done += 1
        if ok:
            prog.ok += 1
            prog.chars_out += out_len
        else:
            prog.failed += 1
        if progress_cb:
            progress_cb(prog)

    async def attempt(chunk: Chunk) -> str:
        """带退避重试 + 自动二分的单片段翻译。"""
        delay = 1.6
        for retry in range(max_retries + 1):
            if cancel is not None and cancel.is_set():
                raise EngineError("已取消", retryable=False)
            try:
                async with sem:
                    return await engine.translate(
                        client, chunk.text, src, dst, creds
                    )
            except EngineError as e:
                # 长度类失败 -> 立即二分重试，不浪费退避预算
                if e.retryable and _looks_like_size_error(e) and len(chunk.text) > 40:
                    half = len(chunk.text) // 2
                    left = await attempt(Chunk(chunk.index, chunk.text[:half], "", True))
                    right = await attempt(Chunk(chunk.index, chunk.text[half:], "", True))
                    return left + right
                if not e.retryable or retry >= max_retries:
                    raise
                await asyncio.sleep(delay + random.uniform(0, delay * 0.4))
                delay = min(delay * 1.9, 12.0)
            except Exception as e:  # noqa: BLE001 - 兜底成引擎错误
                if retry >= max_retries:
                    raise EngineError(f"未知错误：{type(e).__name__}: {e}") from e
                await asyncio.sleep(delay)
                delay = min(delay * 1.9, 12.0)
        raise EngineError("重试耗尽", retryable=True)

    async def worker(idx: int, chunk: Chunk) -> None:
        key = chunk.text
        # 纯空白片段无需请求引擎，原样保留即可（省一次调用，也避免被判"结果为空"）
        if not key.strip():
            results[idx] = SegmentResult(text=key, degraded=chunk.degraded)
            bump(True, 0, len(key))
            return
        try:
            if key in memo:
                results[idx] = SegmentResult(text=memo[key],
                                             degraded=chunk.degraded)
                bump(True, len(key), len(memo[key]))
                return
            out = await attempt(chunk)
            memo[key] = out
            results[idx] = SegmentResult(text=out, degraded=chunk.degraded)
            bump(True, len(key), len(out))
        except EngineError as e:
            results[idx] = SegmentResult(text=chunk.text,
                                         error=str(e),
                                         degraded=chunk.degraded)
            bump(False, len(key), 0)
        except asyncio.CancelledError:
            results[idx] = SegmentResult(text=chunk.text, error="已取消")
            bump(False, len(key), 0)
            raise

    try:
        # 先串行发起前若干片，让连接与令牌尽早建立
        tasks = [asyncio.create_task(worker(i, c)) for i, c in enumerate(chunks)]
        if progress_cb and chunks:
            progress_cb(prog)
        await asyncio.gather(*tasks)
    finally:
        if own_client:
            await client.aclose()

    return results


def _looks_like_size_error(e: EngineError) -> bool:
    msg = str(e)
    return any(k in msg for k in ("长度", "超过限制", "too long", "55000006"))


def join_results(split: SplitResult, results: List[SegmentResult]) -> str:
    """把片段译文拼回完整文本，保持原始段落结构。"""
    parts: List[str] = []
    for chunk, res in zip(split.chunks, results):
        text = res.text if res.text else chunk.text
        parts.append(text + chunk.joiner)
    return "".join(parts)