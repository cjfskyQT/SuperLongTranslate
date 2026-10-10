# -*- coding: utf-8 -*-
"""切分器属性测试 —— 守护"零丢失 + 段落优先"两条不变量。

这些是整个工具的地基：切错一次，长文翻译的结果就会错位或丢内容。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chunker import (  # noqa: E402
    build_split, measure_bytes, measure_chars, reconstruct,
)

LIMIT = 400

SAMPLES = {
    "空文本": "",
    "纯空格": "   \n\n  \t ",
    "单段英文": "The quick brown fox jumps over the lazy dog. " * 5,
    "多段中文": "第一段内容。\n\n第二段内容。\n\n第三段内容。",
    "换行不分段": "行一\n行二\n行三\n行四",
    "结尾带换行": "第一段。\n\n第二段。\n\n",
    "CRLF": "第一段。\r\n\r\n第二段。\r\n\r\n",
    "无标点超长串": "啊" * 2500,
    "超长英文单词": "x" * 2000 + " end.",
    "混合中英": "Hello world. " * 80 + "\n\n你好，世界。这是一个测试。" * 80,
    "带引号收尾": "他说：“这是一段测试。”然后离开了。" * 60 + "\n\n下一段。",
    "版本号小数": "Version 3.14 released. See docs at www.example.com for more. " * 40,
    "超多段落": "\n\n".join(f"第 {i} 段的内容。" for i in range(200)),
}


def check_invariants(text: str, limit: int, measure, name: str):
    res = build_split(text, limit, measure)

    # 1. 零丢失
    assert reconstruct(res.chunks) == text, f"[{name}] 重组后与原文不一致"

    if not text:
        assert res.chunks == []
        return res

    # 2. 每个片段都是原文的连续子串（只切不插值）
    pos = 0
    for c in res.chunks:
        seg = c.text + c.joiner
        assert text[pos:pos + len(seg)] == seg, f"[{name}] 片段非连续子串"
        pos += len(seg)

    # 3. 不超限
    for c in res.chunks:
        assert measure(c.text) <= limit, f"[{name}] 片段超限 {measure(c.text)}>{limit}"
        # 允许纯空白片段（忠实保留原文结构），但不允许长度为 0 的空片段
        assert len(c.text) > 0, f"[{name}] 出现空片段"

    # 4. 段落优先：非降级片段必须结束在段落边界（即紧跟一个换行）
    pos = 0
    for i, c in enumerate(res.chunks):
        pos += len(c.text) + len(c.joiner)
        if i == len(res.chunks) - 1:
            continue
        if c.degraded:
            continue
        assert text[pos - 1] == "\n", \
            f"[{name}] 第 {i} 片未落在段落边界（段落被拆散了）"

    return res


def test_all_samples():
    for name, text in SAMPLES.items():
        check_invariants(text, LIMIT, measure_chars, name)
        check_invariants(text, LIMIT, measure_bytes, name)


def test_paragraphs_never_split_normally():
    """正常文档：每段都小于上限，段落应完整落在单个片段内，绝不降级。"""
    text = "\n\n".join(f"这是第 {i} 段，内容足够长以便触发切分。" * 10 for i in range(120))
    res = build_split(text, LIMIT, measure_chars)
    assert not res.degraded, "不该有任何降级"
    assert not any(c.degraded for c in res.chunks)
    assert len(res.chunks) > 1, "文档较长，应被切成多片"
    # 每个段落必须完整出现在某一个片段里
    for para in text.split("\n\n"):
        assert any(para in c.text for c in res.chunks), "段落被拆到多个片段中了"


def test_single_oversized_paragraph_degrades():
    """单个段落超限才降级，并被记录。"""
    text = "这是一个没有任何换行的超长段落。" * 300
    res = build_split(text, LIMIT, measure_chars)
    assert res.degraded, "超长段落应被记录为降级"
    assert len(res.degraded) == 1
    assert res.degraded[0]["paragraph"] == 0
    assert res.degraded[0]["pieces"] > 1
    assert reconstruct(res.chunks) == text


def test_byte_limit_with_cjk():
    """百度按字节计量：中文 3 字节，必须卡在字节上限而非字符上限。"""
    text = "中文字符测试。" * 5000
    res = build_split(text, 5800, measure_bytes)
    for c in res.chunks:
        assert measure_bytes(c.text) <= 5800
        # 若误按字符算，这些片段会超 5800 字节
    assert len(res.chunks) > 1


def test_crlf_preserved():
    text = "第一段。\r\n\r\n第二段。\r\n\r\n第三段。"
    res = build_split(text, 10, measure_chars)
    assert reconstruct(res.chunks) == text


def test_invalid_limit():
    try:
        build_split("abc", 0)
    except ValueError:
        pass
    else:
        raise AssertionError("limit=0 应抛 ValueError")


if __name__ == "__main__":
    import pytest  # noqa: F401
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))