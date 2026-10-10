# -*- coding: utf-8 -*-
"""FastAPI 服务端：提供本地界面、翻译接口（流式进度）与 TTS 朗读代理。"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from typing import AsyncIterator, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .chunker import split_for_engine
from .engines import engine_info, get_engine
from .engines.base import EngineError, language_options
from .langdetect import LANG_LABEL, resolve_target
from .pipeline import (build_client, join_results, translate_long_text)

ROOT = config.ROOT
WEB_DIR = ROOT / "web"

MAX_INPUT_CHARS = 20_000_000      # 2000 万字符上限，防御性
MAX_BODY_BYTES = 64 * 1024 * 1024

app = FastAPI(title="超长文本翻译", docs_url="/api/docs", redoc_url=None)


# --------------------------------------------------------------------------
# 静态界面
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


# --------------------------------------------------------------------------
# 元信息与配置
# --------------------------------------------------------------------------

@app.get("/api/meta")
def meta() -> JSONResponse:
    return JSONResponse({
        "engines": engine_info(),
        "languages": language_options(),
        "settings": config.redact(config.load_settings()),
        "max_input_chars": MAX_INPUT_CHARS,
    })


@app.post("/api/settings")
def save_settings(payload: Dict) -> JSONResponse:
    merged = config.save_settings(payload or {})
    return JSONResponse({"ok": True, "settings": merged})


def _resolve_engine(name: str, chunk_limit: int):
    engine = get_engine(name)
    limit = int(chunk_limit or 0)
    if limit > 0:
        # 覆写切片上限（用户手动调优）
        if engine.measure_is_bytes:
            engine.limit_bytes = min(limit, 5800)
        else:
            engine.limit_chars = max(200, min(limit, 45000))
    return engine


def _parse_request(payload: Dict):
    text = (payload or {}).get("text", "")
    if not isinstance(text, str):
        raise EngineError("text 必须是字符串", retryable=False)
    if len(text) > MAX_INPUT_CHARS:
        raise EngineError(
            f"文本过长（{len(text):,} 字符），超过单次上限 {MAX_INPUT_CHARS:,}",
            retryable=False,
        )
    settings = config.load_settings()
    payload = payload or {}
    engine_name = payload.get("engine") or settings["engine"]
    src_label = payload.get("source_lang") or settings["source_lang"]
    dst_label = payload.get("target_lang") or settings["target_lang"]
    concurrency = int(payload.get("concurrency") or settings["concurrency"] or 4)
    max_retries = int(payload.get("max_retries") or settings["max_retries"] or 4)
    chunk_limit = int(payload.get("chunk_limit") or settings["chunk_limit"] or 0)
    # 是否允许在段落内部自动切分；默认不允许——先让用户自己分段
    allow_degrade = bool(payload.get("allow_degradation", False))

    engine = _resolve_engine(engine_name, chunk_limit)
    creds = {
        "baidu_appid": payload.get("baidu_appid") or settings["baidu_appid"],
        "baidu_secret": payload.get("baidu_secret") or settings["baidu_secret"],
    }

    src = engine.resolve_lang(src_label, "source")

    # 目标语言被误选成「自动检测」时，按原文语种兜底（译到 auto 没有意义）
    dst_resolved = resolve_target(dst_label, text)
    dst = engine.resolve_lang(dst_resolved["label"], "target")

    health = engine.health(creds)
    if health != "ok":
        raise EngineError(f"引擎不可用：{health}", retryable=False)

    return (text, engine, src, dst, creds, concurrency, max_retries,
            allow_degrade, dst_resolved)


def _degrade_payload(split) -> Dict:
    """超长段落清单（供前端提示用户自行分段）。"""
    return {
        "error": "存在超过单次上限的段落，需要你先分段或确认自动切分",
        "code": "needs_degradation",
        "degraded": split.degraded,
        "chunks": len(split.chunks),
        "paragraphs": split.paragraph_count,
    }


# --------------------------------------------------------------------------
# 预检：不请求任何翻译接口，只算切分结果
# --------------------------------------------------------------------------

@app.post("/api/preflight")
def preflight(payload: Dict) -> JSONResponse:
    """翻译前的切分预检——本地计算，不消耗任何翻译额度。

    刻意用 ``def`` 而非 ``async def``：切分是纯 CPU 计算，超长文档可能耗时。
    同步路由由 FastAPI 丢进线程池执行，不会阻塞事件循环。
    """
    try:
        text, engine, _src, _dst, _creds, *_rest, dst_res = _parse_request(payload)
    except EngineError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    if not text.strip():
        return JSONResponse({"ok": False, "error": "待翻译文本为空"}, status_code=400)

    split = split_for_engine(text, engine)
    return JSONResponse({
        "ok": True,
        "engine": engine.name,
        "chars": len(text),
        "paragraphs": split.paragraph_count,
        "chunks": len(split.chunks),
        "degraded": split.degraded,
        "limit": (engine.limit_bytes if engine.measure_is_bytes
                  else engine.limit_chars),
        "measure": "bytes" if engine.measure_is_bytes else "chars",
        "target": dst_res,
        "detected_label": LANG_LABEL.get(dst_res.get("detected")),
    })


# --------------------------------------------------------------------------
# 一次性翻译（CLI / 脚本用）
# --------------------------------------------------------------------------

@app.post("/api/translate")
async def translate_once(payload: Dict) -> JSONResponse:
    try:
        (text, engine, src, dst, creds, concurrency, max_retries,
         allow_degrade, _dst_res) = _parse_request(payload)
    except EngineError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    if not text.strip():
        return JSONResponse({"ok": False, "error": "待翻译文本为空"}, status_code=400)

    split = split_for_engine(text, engine)
    if split.degraded and not allow_degrade:
        return JSONResponse(_degrade_payload(split), status_code=409)
    if not split.chunks:
        return JSONResponse({"ok": False, "error": "未能切分出有效片段"},
                            status_code=400)

    t0 = time.monotonic()
    client = build_client(concurrency=concurrency)
    try:
        results = await translate_long_text(
            text, engine, split, src, dst, creds,
            concurrency=concurrency, max_retries=max_retries, client=client,
        )
    finally:
        await client.aclose()

    out = join_results(split, results)
    errors = [
        {"chunk": i, "error": r.error, "preview": split.chunks[i].text[:80]}
        for i, r in enumerate(results) if r.error
    ]
    # 全部片段都失败 -> 明确报错，绝不把原文当译文悄悄返回
    if errors and len(errors) == len(split.chunks):
        first = errors[0]["error"] or "未知原因"
        return JSONResponse({
            "ok": False,
            "error": f"全部 {len(split.chunks)} 片都翻译失败：{first}",
            "code": "all_chunks_failed",
            "errors": errors[:5],
        }, status_code=502)

    return JSONResponse({
        "ok": True,
        "text": out,
        "engine": engine.name,
        "stats": {
            "chars_in": len(text),
            "chars_out": len(out),
            "chunks": len(split.chunks),
            "paragraphs": split.paragraph_count,
            "elapsed": round(time.monotonic() - t0, 2),
            "errors": errors,
            "degraded": split.degraded,
        },
    })


# --------------------------------------------------------------------------
# 流式翻译（SSE 进度）
# --------------------------------------------------------------------------

def _sse(event: Dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/api/translate/stream")
async def translate_stream(payload: Dict, request: Request) -> StreamingResponse:
    try:
        (text, engine, src, dst, creds, concurrency, max_retries,
         allow_degrade, _dst_res) = _parse_request(payload)
    except EngineError as e:
        async def early() -> AsyncIterator[str]:
            yield _sse({"type": "error", "message": str(e)})
        return StreamingResponse(early(), media_type="text/event-stream")

    if not text.strip():
        async def empty() -> AsyncIterator[str]:
            yield _sse({"type": "error", "message": "待翻译文本为空"})
        return StreamingResponse(empty(), media_type="text/event-stream")

    split = split_for_engine(text, engine)

    # 有超长段落且用户未确认 -> 停下来让用户先分段
    if split.degraded and not allow_degrade:
        return JSONResponse(_degrade_payload(split), status_code=409)

    async def gen() -> AsyncIterator[str]:
        cancel = asyncio.Event()
        queue: "asyncio.Queue[Optional[Dict]]" = asyncio.Queue(maxsize=256)

        def on_progress(p) -> None:
            # 队列满只丢一次进度快照，绝不能因此中断翻译
            try:
                queue.put_nowait({"type": "progress", **p.as_dict()})
            except asyncio.QueueFull:
                pass

        async def run() -> None:
            client = build_client(concurrency=concurrency)
            try:
                results = await translate_long_text(
                    text, engine, split, src, dst, creds,
                    concurrency=concurrency, max_retries=max_retries,
                    progress_cb=on_progress, client=client, cancel=cancel,
                )
            except Exception as e:  # noqa: BLE001
                queue.put_nowait(
                    {"type": "error", "message": f"翻译中断：{e}"})
                queue.put_nowait(None)
                return
            finally:
                await client.aclose()

            out = join_results(split, results)
            errors = [
                {"chunk": i, "error": r.error, "preview": split.chunks[i].text[:80]}
                for i, r in enumerate(results) if r.error
            ]
            # 全部片段失败 -> 明确报错，不把原文当译文返回
            if errors and len(errors) == len(split.chunks):
                queue.put_nowait({
                    "type": "error",
                    "code": "all_chunks_failed",
                    "message": f"全部 {len(split.chunks)} 片都翻译失败："
                               f"{errors[0]['error'] or '未知原因'}",
                })
                queue.put_nowait(None)
                return
            queue.put_nowait({
                "type": "done",
                "text": out,
                "engine": engine.name,
                "errors": errors,
                "chunks": len(split.chunks),
                "paragraphs": split.paragraph_count,
                "degraded": split.degraded,
            })
            queue.put_nowait(None)

        task = asyncio.create_task(run())

        yield _sse({
            "type": "start",
            "engine": engine.name,
            "engine_name": engine.display_name,
            "chars": len(text),
            "chunks": len(split.chunks),
            "paragraphs": split.paragraph_count,
            "limit": (engine.limit_bytes if engine.measure_is_bytes
                      else engine.limit_chars),
            "measure": "bytes" if engine.measure_is_bytes else "chars",
            "degraded": split.degraded,
        })

        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if task.done():
                        break
                    yield ": keep-alive\n\n"   # 心跳，防代理断连
                    continue
                if item is None:
                    break
                yield _sse(item)
        finally:
            if not task.done():
                cancel.set()
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: B014
                    pass

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------
# 引擎连通性自检
# --------------------------------------------------------------------------

@app.post("/api/check")
async def check(payload: Dict) -> JSONResponse:
    try:
        text, engine, src, dst, creds, *_ = _parse_request(payload)
    except EngineError as e:
        return JSONResponse({"ok": False, "message": str(e)})

    probe = text.strip()[:200] or "Hello world"
    client = build_client(concurrency=1)
    try:
        out = await engine.translate(client, probe, src, dst, creds)
        return JSONResponse({"ok": True, "message": f"可用：{out[:60]}"})
    except EngineError as e:
        return JSONResponse({"ok": False, "message": str(e)})
    finally:
        await client.aclose()


# --------------------------------------------------------------------------
# 译文朗读（对齐网页版"听"的功能）
# --------------------------------------------------------------------------

TTS_LIMIT = 180


@app.get("/api/tts")
async def tts(text: str, lang: str = "en") -> StreamingResponse:
    chunks: List[str] = [text[i:i + TTS_LIMIT] for i in range(0, len(text), TTS_LIMIT)]
    if not chunks:
        return StreamingResponse(iter([]), media_type="audio/mpeg")

    url = ("https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob"
           f"&tl={urllib.parse.quote(lang)}&q=")

    async def stream() -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(timeout=30.0) as c:
            for part in chunks:
                try:
                    r = await c.get(url + urllib.parse.quote(part),
                                    headers={"User-Agent": "Mozilla/5.0"})
                    if r.status_code == 200 and r.content:
                        yield r.content
                except httpx.HTTPError:
                    break

    return StreamingResponse(stream(), media_type="audio/mpeg")