# -*- coding: utf-8 -*-
"""智能切分：把任意长度文本切成引擎可安全翻译的片段。

切分策略（段落优先）
--------------------
**默认只以段落为切分单位**，绝不为了凑长度把段落/句子切碎：

1. 先按空行/换行切出自然段；
2. 按段落装箱，整段进同一片段，只在段落之间断开；
3. 只有当**单个段落本身**就超过单请求上限时，才降级到段内句末切分；
4. 段内仍超限（无标点超长串）才做最后一次硬切兜底。

第 3、4 步属于降级路径，会被记录在 :attr:`SplitResult.degraded` 中，
由接口回传前端告警——不静默碎段。

零丢失不变量::

    "".join(chunk.text + chunk.joiner for chunk in chunks) == original_text

另有更强的性质：每个 ``chunk.text`` 必定是原文的一段**连续子串**（只切不插值）。
两者均由 tests/test_chunker.py 的属性测试守护。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Tuple

MeasureFn = Callable[[str], int]

# 段落分隔：空行(可含空格) 或 单个换行
_BLOCK_SPLIT = re.compile(r"(\r?\n[ \t]*\r?\n|\r?\n)")

# 句末终止符（中英文 + 分号 + 省略号）
_TERM_CHARS = frozenset("。！？!?…；;\n")
# 拉丁句号后可能跟的收尾符号
_CLOSERS = frozenset("”’\"')）]】》」』")

# 段内降级时的次级切点
_SECONDARY_CHARS = frozenset("，,、：:—－- \t")


@dataclass
class Chunk:
    """一个待翻译片段。"""

    index: int
    text: str                      # 送翻译的正文
    joiner: str = ""               # 该片段译文之后补回的原始分隔符
    degraded: bool = False         # 该片段来自降级切分（段落内被切开）

    def __len__(self) -> int:
        return len(self.text)


@dataclass
class SplitResult:
    chunks: List[Chunk]
    degraded: List[Dict] = field(default_factory=list)
    paragraph_count: int = 0

    @property
    def has_degradation(self) -> bool:
        return bool(self.degraded)


def measure_chars(text: str) -> int:
    """按 Unicode 字符数计量（Google 端点适用）。"""
    return len(text)


def measure_bytes(text: str) -> int:
    """按 UTF-8 字节数计量（百度官方 API 适用，中文 3 字节）。"""
    return len(text.encode("utf-8"))


# --------------------------------------------------------------------------
# 降级切分（仅用于超长段落）
# --------------------------------------------------------------------------

def _split_sentences(text: str) -> List[str]:
    """在句末标点处切分，保留标点本身，不丢任何字符。"""
    out: List[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        cut = ch in _TERM_CHARS
        # 拉丁句号需后接空白才算句末，避免切碎 "3.14" / "www.a.com"
        if ch == "." and i + 1 < n and text[i + 1] in " \t":
            cut = True
        if cut:
            j = i + 1
            while j < n and text[j] in _CLOSERS:
                j += 1
            out.append(text[start:j])
            start = j
            i = j
        else:
            i += 1
    if start < n:
        out.append(text[start:])
    return [s for s in out if s]


def _hard_split(text: str, limit: int, measure: MeasureFn) -> List[str]:
    """最后兜底：按次级标点切，仍超限则逐字硬切。"""
    if measure(text) <= limit:
        return [text]

    pieces: List[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _SECONDARY_CHARS and measure(buf) >= limit * 0.75:
            pieces.append(buf)
            buf = ""
    if buf:
        pieces.append(buf)

    fixed: List[str] = []
    for p in pieces:
        if measure(p) <= limit:
            fixed.append(p)
            continue
        cur = ""
        for ch in p:
            if cur and measure(cur + ch) > limit:
                fixed.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            fixed.append(cur)
    return [p for p in fixed if p]


def _sentence_pieces(block: str, limit: int, measure: MeasureFn) -> List[str]:
    """把一个超限段落拆成若干 <= limit 的句子块。"""
    pieces: List[str] = []
    cur = ""
    for s in _split_sentences(block):
        if cur and measure(cur + s) > limit:
            pieces.append(cur)
            cur = ""
        cur += s
        while measure(cur) > limit:
            over = _hard_split(cur, limit, measure)
            pieces.extend(over[:-1])
            cur = over[-1]
    if cur:
        pieces.append(cur)

    out: List[str] = []
    for p in pieces:
        out.extend(_hard_split(p, limit, measure))
    return [p for p in out if p]


# --------------------------------------------------------------------------
# 段落切分主流程
# --------------------------------------------------------------------------

@dataclass
class _Block:
    index: int
    text: str
    joiner: str        # 块后的原始分隔符（段间为 \n / \n\n，末块为 ""）
    pieces: List[str] = field(default_factory=list)
    degraded: bool = False


def _iter_blocks(text: str) -> List[_Block]:
    """按空行/换行切出自然段，**原样保留**段间分隔符（含 \\r\\n）。"""
    parts = _BLOCK_SPLIT.split(text)
    blocks: List[_Block] = []
    for i in range(0, len(parts) - 1, 2):
        content, sep = parts[i], parts[i + 1]
        if content:
            blocks.append(_Block(index=len(blocks), text=content, joiner=sep))
    tail = parts[-1] if parts else ""
    if tail:
        blocks.append(_Block(index=len(blocks), text=tail, joiner=""))
    return blocks


def _plan(blocks: List[_Block], limit: int, measure: MeasureFn) -> List[_Block]:
    """给每个段落决定：整段下发，还是降级拆成多片。"""
    for b in blocks:
        if measure(b.text) <= limit:
            b.pieces = [b.text]
        else:
            b.pieces = _sentence_pieces(b.text, limit, measure)
            b.degraded = True
    return blocks


def build_split(
    text: str,
    limit: int,
    measure: MeasureFn = measure_chars,
) -> SplitResult:
    """段落优先切分，返回片段与降级报告。"""
    if limit <= 0:
        raise ValueError("limit 必须为正整数")
    if not text:
        return SplitResult(chunks=[], degraded=[], paragraph_count=0)

    blocks = _plan(_iter_blocks(text), limit, measure)

    chunks: List[Chunk] = []
    cur = ""        # 当前片段正文（已含内部分隔符）
    last_sep = ""   # 当前片段末尾与下一个单位之间的分隔符
    cur_degraded = False

    for b in blocks:
        last = len(b.pieces) - 1
        for pi, piece in enumerate(b.pieces):
            # 降级段落的片间无分隔（译文自带句末标点）；未降级时保留原始段间结构
            sep_after = b.joiner if pi == last else ""
            if cur and measure(cur + last_sep + piece) > limit:
                chunks.append(Chunk(index=len(chunks), text=cur,
                                    joiner=last_sep, degraded=cur_degraded))
                cur, last_sep, cur_degraded = "", "", False
            cur += last_sep + piece
            last_sep = sep_after
            cur_degraded = cur_degraded or b.degraded

    if cur:
        chunks.append(Chunk(index=len(chunks), text=cur,
                            joiner=last_sep, degraded=cur_degraded))

    degraded = [
        {
            "paragraph": b.index,
            "chars": measure_chars(b.text),
            "pieces": len(b.pieces),
            "preview": b.text[:60].replace("\n", " "),
        }
        for b in blocks if b.degraded
    ]

    return SplitResult(chunks=chunks, degraded=degraded, paragraph_count=len(blocks))


def build_chunks(
    text: str,
    limit: int,
    measure: MeasureFn = measure_chars,
) -> List[Chunk]:
    """便捷入口：只要片段列表。"""
    return build_split(text, limit, measure).chunks


def split_for_engine(text: str, engine) -> SplitResult:
    """按引擎的计量方式与上限切分。``engine`` 需提供 measure_is_bytes/limit_*。"""
    if getattr(engine, "measure_is_bytes", False):
        limit, m = engine.limit_bytes, measure_bytes
    else:
        limit, m = engine.limit_chars, measure_chars
    # 留 3% 余量，给表单编码/签名等开销留空间
    limit = max(1, int(limit * 0.97))
    return build_split(text, limit, m)


def reconstruct(chunks: List[Chunk]) -> str:
    """按不变量还原原文（用于测试与校验）。"""
    return "".join(c.text + c.joiner for c in chunks)