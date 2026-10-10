# -*- coding: utf-8 -*-
"""配置持久化。

百度 appid/secret 明文存在 ``data/settings.json``（本地文件，不外传）。
也支持环境变量覆盖：``BAIDU_APPID`` / ``BAIDU_SECRET``。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SETTINGS_PATH = DATA_DIR / "settings.json"

# 必须是可重入锁：save_settings() 持锁期间会调用 load_settings()，
# 若用普通 Lock 会自死锁（保存请求永久挂起，前端表现为"保存不了"）。
_LOCK = threading.RLock()

DEFAULTS: Dict[str, Any] = {
    "engine": "google",
    "source_lang": "自动检测",
    "target_lang": "中文（简体）",
    "concurrency": 4,
    "max_retries": 4,
    "chunk_limit": 0,          # 0 = 用引擎默认值
    "baidu_appid": "",
    "baidu_secret": "",
}

SECRET_FIELDS = {"baidu_secret"}


def _mask(value: str) -> str:
    if not value:
        return ""
    return value[:4] + "*" * 6 + value[-2:] if len(value) > 8 else "***"


def load_settings() -> Dict[str, Any]:
    with _LOCK:
        data = dict(DEFAULTS)
        if SETTINGS_PATH.exists():
            try:
                raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data.update({k: v for k, v in raw.items() if k in DEFAULTS})
            except (json.JSONDecodeError, OSError):
                pass
        # 环境变量优先
        if os.environ.get("BAIDU_APPID"):
            data["baidu_appid"] = os.environ["BAIDU_APPID"]
        if os.environ.get("BAIDU_SECRET"):
            data["baidu_secret"] = os.environ["BAIDU_SECRET"]
        return data


def save_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    """合并保存，返回脱敏后的完整配置（供前端回显）。"""
    with _LOCK:
        current = load_settings()
        for k, v in (patch or {}).items():
            if k in DEFAULTS:
                current[k] = v
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, SETTINGS_PATH)
        return redact(current)


def redact(data: Dict[str, Any]) -> Dict[str, Any]:
    """脱敏副本，用于返回给前端展示。"""
    out = dict(data)
    for f in SECRET_FIELDS:
        out[f] = _mask(out.get(f, ""))
    out["baidu_secret_set"] = bool(data.get("baidu_secret"))
    return out