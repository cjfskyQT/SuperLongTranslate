# -*- coding: utf-8 -*-
"""命令行批处理翻译 —— 处理单个超长文件。

示例::

    python cli.py book.txt -t "中文（简体）"
    python cli.py book.txt -e google -s "英语" -t "日语" -o book_zh.txt
    python cli.py big.md -e baidu -c 3
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.chunker import split_for_engine            # noqa: E402
from app.config import load_settings                # noqa: E402
from app.engines import get_engine                  # noqa: E402
from app.engines.base import EngineError, LANG_BY_KEY  # noqa: E402
from app.pipeline import (build_client, join_results,  # noqa: E402
                          translate_long_text)

SUPPORTED = {".txt", ".md", ".markdown", ".text", ".csv", ".srt", ".vtt", ".log"}


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "utf-16"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise SystemExit(f"无法识别文件编码：{path}")


async def run(args) -> int:
    src_path = Path(args.input)
    if not src_path.exists():
        print(f"[x] 文件不存在：{src_path}", file=sys.stderr)
        return 2
    if src_path.suffix.lower() not in SUPPORTED:
        print(f"[!] 非纯文本扩展名 .{src_path.suffix}，仍按文本处理", file=sys.stderr)

    settings = load_settings()
    text = read_text(src_path)
    if not text.strip():
        print("[x] 文件内容为空", file=sys.stderr)
        return 2

    engine = get_engine(args.engine)
    limit = int(args.chunk_limit or 0)
    if limit > 0:
        if engine.measure_is_bytes:
            engine.limit_bytes = min(limit, 5800)
        else:
            engine.limit_chars = max(200, min(limit, 45000))

    creds = {
        "baidu_appid": settings.get("baidu_appid", ""),
        "baidu_secret": settings.get("baidu_secret", ""),
    }
    if engine.health(creds) != "ok":
        print(f"[x] 引擎不可用：{engine.health(creds)}", file=sys.stderr)
        return 3

    src = engine.resolve_lang(args.source or settings["source_lang"], "source")
    dst = engine.resolve_lang(args.target or settings["target_lang"], "target")

    split = split_for_engine(text, engine)
    unit = "字节" if engine.measure_is_bytes else "字符"
    limit_val = engine.limit_bytes if engine.measure_is_bytes else engine.limit_chars

    print(f"文件   : {src_path}")
    print(f"引擎   : {engine.display_name}")
    print(f"语言   : {args.source or settings['source_lang']} -> "
          f"{args.target or settings['target_lang']}")
    print(f"切分   : {split.paragraph_count} 段 -> {len(split.chunks)} 片"
          f"（每片 <= {int(limit_val * 0.97):,} {unit}）")

    # 超长段落：先让用户自己分段，除非显式要求自动切分
    if split.degraded and not args.allow_degradation:
        print("")
        print(f"[!] 有 {len(split.degraded)} 个段落整段超过单次上限，"
              f"按段落切不开：")
        for d in split.degraded[:10]:
            print(f"    第 {d['paragraph'] + 1} 段  {d['chars']:,} 字符"
                  f"（上限 {int(limit_val * 0.97):,} {unit}）")
            print(f"        {d['preview'][:60]}…")
        if len(split.degraded) > 10:
            print(f"    …另有 {len(split.degraded) - 10} 段")
        print("")
        print("    建议：在这些段落的自然停顿处空一行（或换行）分成小段，译文会更自然。")
        print("    确认要让工具在段内自动切分，则加参数：")
        print(f"        python cli.py {args.input} -e {args.engine} --allow-degradation")
        return 5

    if split.degraded:
        print(f"[!] {len(split.degraded)} 个段落超长，已按确认在段内降级切分：")
        for d in split.degraded[:5]:
            print(f"    第 {d['paragraph'] + 1} 段 {d['chars']:,} 字符"
                  f" -> {d['pieces']} 片：{d['preview'][:40]}…")
    print("-" * 60)

    done = {"n": 0, "fail": 0}
    t0 = time.monotonic()

    def on_progress(p) -> None:
        done["n"] = p.done
        done["fail"] = p.failed
        pct = p.done / p.total * 100 if p.total else 100
        eta = f" ETA {p.eta:5.1f}s" if p.eta > 0 else ""
        sys.stdout.write(
            f"\r  [{pct:5.1f}%] {p.done}/{p.total} 片 "
            f"成功 {p.ok} 失败 {p.failed}{eta}   ")
        sys.stdout.flush()

    client = build_client(concurrency=args.concurrency)
    try:
        results = await translate_long_text(
            text, engine, split, src, dst, creds,
            concurrency=args.concurrency, max_retries=args.max_retries,
            progress_cb=on_progress, client=client,
        )
    except EngineError as e:
        print(f"\n[x] 翻译失败：{e}", file=sys.stderr)
        return 4
    finally:
        await client.aclose()

    out_text = join_results(split, results)
    out_path = Path(args.output) if args.output else src_path.with_name(
        src_path.stem + "_translated.txt")
    out_path.write_text(out_text, encoding="utf-8-sig")

    secs = time.monotonic() - t0
    print(f"\r{' ' * 62}")
    print("-" * 60)
    print(f"完成   : {len(text):,} 字符 -> {len(out_text):,} 字符"
          f"（{len(split.chunks)} 片，用时 {secs:.1f}s，"
          f"{len(out_text)/max(secs, .01):.0f} 字符/秒）")
    print(f"输出   : {out_path.resolve()}")
    if done["fail"]:
        print(f"[!] {done['fail']} 片失败，对应位置已保留原文")
    return 0


def main() -> int:
    settings = load_settings()
    ap = argparse.ArgumentParser(
        description="超长文本翻译 CLI（按自然段切分，无 5000 字符限制）")
    ap.add_argument("input", help="输入文件（txt/md/csv/srt…）")
    ap.add_argument("-e", "--engine", default="google",
                    choices=["google", "baidu"], help="翻译引擎，默认 google")
    ap.add_argument("-s", "--source", default=None,
                    help=f"源语言，{'/'.join(list(LANG_BY_KEY)[:4])}…，默认自动检测")
    ap.add_argument("-t", "--target", default="中文（简体）", help="目标语言")
    ap.add_argument("-o", "--output", default=None, help="输出文件，默认 <原名>_translated.txt")
    ap.add_argument("-c", "--concurrency", type=int,
                    default=int(settings["concurrency"]), help="并发数，默认 4")
    ap.add_argument("-r", "--max-retries", type=int, default=4, help="单片最大重试次数")
    ap.add_argument("-l", "--chunk-limit", type=int, default=0,
                    help="每片上限（0=引擎默认）")
    ap.add_argument("--allow-degradation", action="store_true",
                    help="存在整段超长的段落时，允许工具在段内自动切分"
                         "（默认停下来提示你先分段）")
    args = ap.parse_args()

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())