# -*- coding: utf-8 -*-
"""语种探测与目标语言兜底测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.langdetect import (AUTO_LABEL, TARGET_EN, TARGET_ZH,  # noqa: E402
                            detect_language, resolve_target, suggest_target)


def test_detect_chinese():
    assert detect_language("这是一段中文文本，用来验证语种探测是否正确。") == "zh"


def test_detect_english():
    assert detect_language("This is an English sentence used for language detection.") \
        == "latin"


def test_detect_japanese():
    # 日语含汉字，必须靠假名区分，否则会被误判成中文
    assert detect_language("これは日本語のテキストです。漢字も含みます。") == "ja"


def test_detect_korean():
    assert detect_language("이것은 한국어 텍스트입니다. 한자도 포함됩니다.") == "ko"


def test_detect_unknown_on_short_text():
    assert detect_language("") == "unknown"
    assert detect_language("   ") == "unknown"
    assert detect_language("12345 !!! ???") == "unknown"


def test_suggest_target_rule():
    """需求：原文中文 -> 目标英语；原文其它 -> 目标中文。"""
    assert suggest_target("这是一段中文文本，用来验证目标语言的自动兜底。") == TARGET_EN
    assert suggest_target("This is an English paragraph for the fallback rule.") == TARGET_ZH
    assert suggest_target("これは日本語のテキストです。漢字も含みます。") == TARGET_ZH
    assert suggest_target("이것은 한국어 텍스트입니다.") == TARGET_ZH


def test_suggest_target_unknown_returns_none():
    assert suggest_target("###") is None


def test_resolve_target_not_auto_is_untouched():
    r = resolve_target("日语", "这是一段中文。")
    assert r["label"] == "日语"
    assert r["auto_picked"] is False


def test_resolve_target_auto_chinese_source():
    r = resolve_target(AUTO_LABEL, "这是一段中文文本，需要自动选择目标语言。")
    assert r["label"] == TARGET_EN
    assert r["auto_picked"] is True
    assert r["detected"] == "zh"


def test_resolve_target_auto_english_source():
    r = resolve_target(AUTO_LABEL, "This is an English paragraph of text.")
    assert r["label"] == TARGET_ZH
    assert r["auto_picked"] is True
    assert r["detected"] == "latin"


def test_resolve_target_auto_undetectable_falls_back_safely():
    """判不出语种时也不能把 auto 传给引擎，用中文兜底。"""
    r = resolve_target(AUTO_LABEL, "###")
    assert r["label"] == TARGET_ZH
    assert r["auto_picked"] is True
    assert r["detected"] == "unknown"


def test_detect_handles_long_text_sampling():
    long_zh = "中文测试。" * 20000
    assert detect_language(long_zh) == "zh"
    long_en = "The quick brown fox. " * 20000
    assert detect_language(long_en) == "latin"


def test_pure_numbers_not_misdetected():
    assert detect_language("2024 2025 2026 12345 67890 000") == "unknown"


if __name__ == "__main__":
    import pytest  # noqa: F401
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))