# -*- coding: utf-8 -*-
"""引擎注册表。"""

from __future__ import annotations

from typing import Dict

from .base import Engine, EngineError
from .baidu import BaiduEngine
from .google import GoogleEngine

_REGISTRY: Dict[str, Engine] = {
    "google": GoogleEngine(),
    "baidu": BaiduEngine(),
}


def get_engine(name: str) -> Engine:
    """按名字取引擎实例；每次返回独立实例，便于按请求覆写切片上限。"""
    key = (name or "").strip().lower()
    if key not in _REGISTRY:
        raise EngineError(f"未知引擎：{name}", retryable=False)
    return type(_REGISTRY[key])()


def engine_info() -> list:
    return [
        {
            "name": e.name,
            "display_name": e.display_name,
            "limit_chars": e.limit_chars,
            "limit_bytes": e.limit_bytes,
            "measure_is_bytes": e.measure_is_bytes,
            "needs_credentials": e.needs_credentials,
        }
        for e in _REGISTRY.values()
    ]


__all__ = ["Engine", "EngineError", "get_engine", "engine_info",
           "GoogleEngine", "BaiduEngine"]