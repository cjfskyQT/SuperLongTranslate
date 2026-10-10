# -*- coding: utf-8 -*-
"""轻量语种探测 + 目标语言兜底。

用途：目标语言被误选成「自动检测」时，翻译到"自动检测"是没有意义的
（Google 会返回异常结果）。这里按**原文实际语种**自动挑一个合理的目标：

* 原文是中文 -> 目标「英语」
* 原文是日语/韩语/拉丁字母等非中文 -> 目标「中文（简体）」

刻意不做联网识别：本地按 Unicode 区段统计即可，长文本场景下开销极小，
也不会因为外部接口抖动而影响主流程。对中日韩的区分足够用；对拉丁字母
语言不做细分（法语德语等同样适合译成中文）。
"""

from __future__ import annotations

from typing import Dict, Optional

AUTO_LABEL = "自动检测"
TARGET_ZH = "中文（简体）"
TARGET_EN = "英语"

# 探测阈值：低于这些数量视为"判不出来"，交回上层处理
_MIN_KANA = 5
_MIN_HANGUL = 5
_MIN_HAN = 4
_MIN_LATIN = 8
_SAMPLE = 20000


def detect_language(text: str) -> str:
    """返回 zh / ja / ko / latin / unknown。"""
    if not text:
        return "unknown"

    han = kana = hangul = latin = 0
    for ch in text[:_SAMPLE]:
        o = ord(ch)
        if (0x4E00 <= o <= 0x9FFF) or (0x3400 <= o <= 0x4DBF):
            han += 1
        elif 0x3040 <= o <= 0x30FF:
            kana += 1
        elif (0xAC00 <= o <= 0xD7AF) or (0x1100 <= o <= 0x11FF):
            hangul += 1
        elif (0x41 <= o <= 0x5A) or (0x61 <= o <= 0x7A):
            latin += 1

    # 假名 / 谚文是决定性特征：日语汉字再多也不能当成中文
    if kana >= _MIN_KANA:
        return "ja"
    if hangul >= _MIN_HANGUL:
        return "ko"
    if han >= _MIN_HAN:
        return "zh"
    if latin >= _MIN_LATIN:
        return "latin"
    return "unknown"


LANG_LABEL: Dict[str, str] = {
    "zh": "中文",
    "ja": "日语",
    "ko": "韩语",
    "latin": "拉丁字母语言（英语等）",
    "unknown": "无法判定",
}


def suggest_target(text: str) -> Optional[str]:
    """原文语种 -> 建议的目标语言标签。判不出时返回 None。"""
    lang = detect_language(text)
    if lang == "unknown":
        return None
    return TARGET_EN if lang == "zh" else TARGET_ZH


def resolve_target(dst_label: str, text: str) -> Dict[str, Optional[str]]:
    """解析最终目标语言；被误选为「自动检测」时按原文兜底。

    返回 {"label", "auto_picked", "detected"}：
      auto_picked=True 表示发生过兜底替换。
    """
    if dst_label != AUTO_LABEL:
        return {"label": dst_label, "auto_picked": False, "detected": None}

    detected = detect_language(text)
    label = suggest_target(text)
    if label is None:
        # 判不出来时给出安全默认（译成中文），比把 auto 传给引擎稳
        label = TARGET_ZH
    return {"label": label, "auto_picked": True, "detected": detected}