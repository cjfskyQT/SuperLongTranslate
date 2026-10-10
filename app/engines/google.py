# -*- coding: utf-8 -*-
"""Google 翻译引擎（免费端点，无需密钥）。

要点
----
* 网页版 Google 翻译的 5000 字符是**输入框的限制**，后端并没有真的卡；
  实测改用 POST 后，单次提交远比 5000 字符长得多的文本也能完整返回、不截断。
  这种不设防的厚道，让长文整段翻译成为可能。
* 必须使用 **POST**：GET 请求会被直接 429 限流。
* 留 4500 作为默认切片上限——远低于后端实际余量，换取稳定性与更快失败反馈。
  用户可在界面上调高/调低。
"""

from __future__ import annotations

import json
from typing import Dict, Optional

import httpx

from .base import Engine, EngineError

ENDPOINT = "https://translate.googleapis.com/translate_a/single"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://translate.google.com",
    "Referer": "https://translate.google.com/",
}


class GoogleEngine(Engine):
    name = "google"
    display_name = "Google（免费，无需密钥）"
    limit_chars = 4500
    limit_bytes = 4500
    measure_is_bytes = False
    needs_credentials = False

    async def translate(
        self,
        client: httpx.AsyncClient,
        text: str,
        src: str,
        dst: str,
        creds: Optional[Dict[str, str]] = None,
    ) -> str:
        payload = {"client": "gtx", "sl": src, "tl": dst, "dt": "t", "q": text}
        try:
            resp = await client.post(
                ENDPOINT,
                data=payload,
                headers=DEFAULT_HEADERS,
                timeout=httpx.Timeout(40.0, connect=15.0),
            )
        except httpx.TimeoutException as e:
            raise EngineError(f"请求超时：{e}", retryable=True) from e
        except httpx.HTTPError as e:
            raise EngineError(f"网络错误：{e}", retryable=True) from e

        if resp.status_code == 429:
            raise EngineError("触发限流（429），请降低并发或稍后再试",
                              retryable=True, status=429)
        if resp.status_code >= 500:
            raise EngineError(f"服务端错误 {resp.status_code}",
                              retryable=True, status=resp.status_code)
        if resp.status_code != 200:
            raise EngineError(f"请求失败 {resp.status_code}：{resp.text[:200]}",
                              retryable=False, status=resp.status_code)

        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError as e:
            raise EngineError(f"返回内容无法解析：{resp.text[:200]}",
                              retryable=True) from e

        if not isinstance(data, list) or not data:
            raise EngineError(f"返回结构异常：{resp.text[:200]}", retryable=True)

        segs = data[0]
        if not isinstance(segs, list):
            raise EngineError(f"返回结构异常：{resp.text[:200]}", retryable=True)

        out = []
        for seg in segs:
            if isinstance(seg, list) and seg and isinstance(seg[0], str):
                out.append(seg[0])
        result = "".join(out)
        if not result.strip() and text.strip():
            raise EngineError("翻译结果为空", retryable=True)
        return result