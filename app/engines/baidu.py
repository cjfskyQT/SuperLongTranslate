# -*- coding: utf-8 -*-
"""百度翻译引擎（官方开放平台 API）。

背景
----
新版百度翻译网页已改为 SPA，``window.gtk`` token 机制消失，
``/basetrans`` 在无 token 时返回空包——网页内部接口已不可用。
因此这里对接**官方开放平台 API**：

    https://fanyi-api.baidu.com/api/trans/vip/translate

免费注册地址：https://fanyi-api.baidu.com/  （通用文本翻译 → 领取 appid/secret）

官方限制：``q`` 单次最多 **6000 字节**（不是字符），所以中文段落要在
6000/3 = 2000 字符处切。本引擎 ``measure_is_bytes = True``，
由 :mod:`app.chunker` 按 UTF-8 字节计量切分，正好卡住这个限制。
"""

from __future__ import annotations

import hashlib
import json
import random
from typing import Dict, Optional

import httpx

from .base import Engine, EngineError

ENDPOINT = "https://fanyi-api.baidu.com/api/trans/vip/translate"
# 官方文档：高级版/尊享版 q 上限 6000 字节，留余量后取 5800
BAIDU_Q_MAX_BYTES = 5800

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://fanyi-api.baidu.com/",
}

ERROR_HINTS = {
    "54001": "签名错误，请检查 secret 是否填错",
    "54000": "参数不合法（多半是语言码或文本为空）",
    "54003": "IP 不在白名单，请在百度控制台把当前出口 IP 加入白名单",
    "55000000": "appid 不存在或已失效",
    "55000001": "请求内容不能为空",
    "55000003": "访问频率超出限制，请降低并发",
    "55000004": "账户余额不足",
    "55000005": "长文本请求过于频繁",
    "55000006": "文本长度超过限制",
}


class BaiduEngine(Engine):
    name = "baidu"
    display_name = "百度翻译（官方 API，需 appid/secret）"
    limit_chars = 5800
    limit_bytes = BAIDU_Q_MAX_BYTES
    measure_is_bytes = True
    needs_credentials = True

    async def translate(
        self,
        client: httpx.AsyncClient,
        text: str,
        src: str,
        dst: str,
        creds: Optional[Dict[str, str]] = None,
    ) -> str:
        creds = creds or {}
        appid = (creds.get("baidu_appid") or "").strip()
        secret = (creds.get("baidu_secret") or "").strip()
        if not appid or not secret:
            raise EngineError("未配置百度 appid/secret", retryable=False)

        if not text.strip():
            return ""

        salt = str(random.randint(32768, 65536))
        sign = hashlib.md5(f"{appid}{text}{salt}{secret}".encode("utf-8")).hexdigest()

        payload = {
            "q": text,
            "from": src,
            "to": dst,
            "appid": appid,
            "salt": salt,
            "sign": sign,
        }

        try:
            resp = await client.post(
                ENDPOINT, data=payload, headers=DEFAULT_HEADERS,
                timeout=httpx.Timeout(40.0, connect=15.0),
            )
        except httpx.TimeoutException as e:
            raise EngineError(f"请求超时：{e}", retryable=True) from e
        except httpx.HTTPError as e:
            raise EngineError(f"网络错误：{e}", retryable=True) from e

        if resp.status_code == 429:
            raise EngineError("触发限流（429），请降低并发", retryable=True, status=429)
        if resp.status_code != 200:
            raise EngineError(f"请求失败 {resp.status_code}：{resp.text[:200]}",
                              retryable=False, status=resp.status_code)

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise EngineError(f"返回内容无法解析：{resp.text[:200]}",
                              retryable=True) from e

        if "error_code" in data:
            code = str(data.get("error_code"))
            hint = ERROR_HINTS.get(code, data.get("error_msg", "未知错误"))
            retryable = code in {"55000003", "55000005", "54001"}
            raise EngineError(f"百度接口错误 {code}：{hint}",
                              retryable=retryable, status=int(code)
                              if code.isdigit() else None)

        results = data.get("trans_result") or []
        if not results:
            raise EngineError(f"返回结果为空：{resp.text[:200]}", retryable=True)

        # 官方在多段时按 \n 拆分下发，原样按行还原
        parts = [r.get("dst", "") for r in results if isinstance(r, dict)]
        out = "\n".join(p for p in parts if p)
        if not out.strip() and text.strip():
            raise EngineError("翻译结果为空", retryable=True)
        return out