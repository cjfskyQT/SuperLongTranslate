# -*- coding: utf-8 -*-
"""端到端验证：真实调用 Google 引擎翻译一份超长文档。

检查点：段数守恒、无失败片段、译文非空且不是"退回原文"。
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.chunker import reconstruct, split_for_engine   # noqa: E402
from app.engines import get_engine                      # noqa: E402
from app.pipeline import (build_client, join_results,   # noqa: E402
                          translate_long_text)

TOPICS = [
    "分布式系统的一致性模型", "神经网络的注意力机制", "城市化与交通治理",
    "古代丝绸之路的贸易路线", "蛋白质折叠与药物设计", "气候变化的碳循环",
    "古典音乐的结构与织体", "经济学的机会成本", "光合作用的机理",
    "语言演化与语义漂移",
]

SENTENCES = [
    "这一部分讨论了{d}在实际场景中的约束条件与实现代价。",
    "研究者在论文中指出，{d}的边界情况往往决定了系统的整体表现。",
    "需要强调的是，{d}并非孤立存在，它与上下游模块存在紧密耦合。",
    "从工程角度看，{d}带来的收益是渐进的，但风险却是突发的。",
    "实验数据表明，{d}在负载升高时会出现明显的性能拐点。",
]


def make_doc(n_paras: int) -> str:
    """构造一份带唯一标记的超长文档，便于校验完整性。"""
    paras = []
    for i in range(n_paras):
        topic = TOPICS[i % len(TOPICS)]
        body = "".join(SENTENCES[(i + j) % len(SENTENCES)].format(d=topic)
                       for j in range(5))
        paras.append(f"【第{i + 1}段】{body}")
    return "\n\n".join(paras)


def count_paras(text: str) -> int:
    return len([p for p in re.split(r"\r?\n[ \t]*\r?\n|\r?\n", text) if p.strip()])


async def main() -> int:
    engine = get_engine("google")
    doc = make_doc(200)
    print(f"原文   : {len(doc):,} 字符 / {count_paras(doc)} 段")

    split = split_for_engine(doc, engine)
    unit = "字节" if engine.measure_is_bytes else "字符"
    print(f"切分   : {split.paragraph_count} 段 -> {len(split.chunks)} 片 "
          f"(上限 {engine.limit_chars:,} {unit})")
    print(f"降级   : {len(split.degraded)} 个段落被段内切分")

    # 地基不变量
    assert reconstruct(split.chunks) == doc, "切分重组与原文不一致！"
    assert not any(c.degraded for c in split.chunks), \
        "正常文档不应触发降级"
    print("不变量 : 重组一致 ✓ 无降级 ✓")
    print("-" * 62)

    t0 = time.monotonic()
    client = build_client(concurrency=4)
    errors_seen = []

    def on_progress(p):
        if p.done % 5 == 0 or p.done == p.total:
            print(f"  {p.done:3d}/{p.total} 片  成功 {p.ok}  失败 {p.failed}  "
                  f"{p.chars_out:,} 字符  {p.chars_out / max(p.elapsed, .01):.0f} 字符/秒",
                  flush=True)

    try:
        results = await translate_long_text(
            doc, engine, split, "auto", "en",
            concurrency=4, max_retries=3, progress_cb=on_progress, client=client,
        )
    finally:
        await client.aclose()

    secs = time.monotonic() - t0
    out = join_results(split, results)
    failed = [(i, r) for i, r in enumerate(results) if r.error]

    print("-" * 62)
    print(f"耗时   : {secs:.1f}s")
    print(f"输出   : {len(out):,} 字符 / {count_paras(out)} 段")
    print(f"失败   : {len(failed)} 片")
    for i, r in failed[:5]:
        errors_seen.append(f"#{i+1}: {r.error}")

    ok = True
    if failed:
        ok = False
        print("[x] 存在失败片段")
        for e in errors_seen:
            print("    ", e)
    if count_paras(out) != split.paragraph_count:
        ok = False
        print(f"[x] 段数不守恒：原文 {split.paragraph_count} -> 译文 {count_paras(out)}")
    else:
        print("[✓] 段数守恒")
    if not out.strip():
        ok = False
        print("[x] 译文为空")
    if out.strip() == doc.strip():
        ok = False
        print("[x] 译文与原文相同 —— 疑似未真正翻译")
    else:
        print("[✓] 确实发生了翻译（译文与原文不同）")
    if len(out) < len(doc) * 0.3:
        print(f"[!] 译文偏短（{len(out):,} vs {len(doc):,}），建议抽查")

    print("-" * 62)
    print("译文前 260 字：")
    print(out[:260].replace("\n", "\n"))
    print("-" * 62)
    print("最终结果：" + ("全部通过 ✓" if ok else "存在问题 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))