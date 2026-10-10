# -*- coding: utf-8 -*-
"""翻译引擎抽象与语言表。"""

from __future__ import annotations

from typing import Dict, List, Optional

import httpx


class EngineError(RuntimeError):
    """引擎调用失败，message 面向用户，retryable 决定是否重试。"""

    def __init__(self, message: str, retryable: bool = True, status: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class LangSpec:
    __slots__ = ("label", "google", "baidu")

    def __init__(self, label: str, google: str, baidu: str):
        self.label = label
        self.google = google
        self.baidu = baidu

    def code_for(self, engine_name: str) -> str:
        return self.google if engine_name == "google" else self.baidu


# 统一语言表：一次选择，Google / 百度各自取自己的语言码
LANGUAGES: List[LangSpec] = [
    LangSpec("自动检测", "auto", "auto"),
    LangSpec("中文（简体）", "zh-CN", "zh"),
    LangSpec("中文（繁体）", "zh-TW", "cht"),
    LangSpec("英语", "en", "en"),
    LangSpec("日语", "ja", "jp"),
    LangSpec("韩语", "ko", "kor"),
    LangSpec("法语", "fr", "fra"),
    LangSpec("德语", "de", "de"),
    LangSpec("西班牙语", "es", "spa"),
    LangSpec("俄语", "ru", "ru"),
    LangSpec("葡萄牙语", "pt", "pt"),
    LangSpec("意大利语", "it", "it"),
    LangSpec("阿拉伯语", "ar", "ara"),
    LangSpec("泰语", "th", "th"),
    LangSpec("越南语", "vi", "vie"),
    LangSpec("印度尼西亚语", "id", "id"),
    LangSpec("荷兰语", "nl", "nl"),
    LangSpec("波兰语", "pl", "pl"),
    LangSpec("土耳其语", "tr", "tr"),
    LangSpec("瑞典语", "sv", "sv"),
]

LANG_BY_KEY: Dict[str, LangSpec] = {spec.label: spec for spec in LANGUAGES}

# 展示给前端的下拉项
def language_options() -> List[Dict[str, str]]:
    return [{"label": s.label, "key": s.label} for s in LANGUAGES]


class Engine:
    """引擎基类。"""

    name: str = "base"
    display_name: str = "Base"
    limit_chars: int = 4500          # 单请求字符上限
    limit_bytes: int = 4500          # 单请求字节上限
    measure_is_bytes: bool = False
    needs_credentials: bool = False

    async def translate(
        self,
        client: httpx.AsyncClient,
        text: str,
        src: str,
        dst: str,
        creds: Optional[Dict[str, str]] = None,
    ) -> str:
        raise NotImplementedError

    def resolve_lang(self, label: str, role: str) -> str:
        spec = LANG_BY_KEY.get(label)
        if spec is None:
            raise EngineError(f"未知语言：{label}", retryable=False)
        return spec.code_for(self.name)

    def health(self, creds: Optional[Dict[str, str]] = None) -> str:
        """返回该引擎是否可用及原因，用于前端提示。"""
        if self.needs_credentials:
            appid = (creds or {}).get("baidu_appid", "").strip()
            secret = (creds or {}).get("baidu_secret", "").strip()
            if not appid or not secret:
                return "未配置百度 appid/secret"
            return "ok"
        return "ok"