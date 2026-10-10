# -*- coding: utf-8 -*-
"""HTTP 层验证：确认 /api/meta、流式 SSE、连通性自检、静态页面都能正常工作。"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8777"


def get(path, timeout=30):
    req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def post_json(path, payload, timeout=180):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def post_json_expect_error(path, payload, timeout=60):
    """预期 4xx：返回 (状态码, 响应体)。"""
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main() -> int:
    fails = []

    def check(name, cond, extra=""):
        print(f"  [{'✓' if cond else '✗'}] {name}{('  ' + extra) if extra else ''}")
        if not cond:
            fails.append(name)

    print("1) 静态页面")
    st, body = get("/")
    check("GET / 返回 200", st == 200)
    check("包含标题", "超长文本翻译" in body.decode("utf-8"))
    st, _ = get("/static/app.js")
    check("app.js 可访问", st == 200)
    st, _ = get("/static/style.css")
    check("style.css 可访问", st == 200)

    print("2) 元信息")
    st, body = get("/api/meta")
    meta = json.loads(body)
    check("GET /api/meta", st == 200)
    check("引擎含 google/baidu",
          {"google", "baidu"} <= {e["name"] for e in meta["engines"]})
    check("语言表非空", len(meta["languages"]) >= 15,
          f"{len(meta['languages'])} 种")
    check("百度标记为按字节计量",
          any(e["name"] == "baidu" and e["measure_is_bytes"] for e in meta["engines"]))
    secret = meta["settings"].get("baidu_secret")
    check("密钥未明文回显", secret in ("",) or "*" in secret, repr(secret))

    print("3) 连通性自检")
    st, body = post_json("/api/check", {"engine": "google",
                                        "text": "Hello world",
                                        "source_lang": "自动检测",
                                        "target_lang": "英语"})
    d = json.loads(body)
    check("google 自检通过", d.get("ok") is True, d.get("message", "")[:70])

    # 百度：是否已配置凭据取决于本机状态，两种情况都要给出正确行为
    st, body = post_json("/api/check", {"engine": "baidu",
                                        "text": "Hello world",
                                        "source_lang": "自动检测",
                                        "target_lang": "英语"})
    d = json.loads(body)
    creds = json.loads(get("/api/meta")[1])["settings"]
    if creds.get("baidu_secret_set"):
        print(f"    (本机已配置百度凭据 appid={creds.get('baidu_appid')})")
        check("已配凭据时走真实鉴权（结果取决于凭据有效性）",
              d.get("ok") is True or "百度接口错误" in d.get("message", ""),
              d.get("message", "")[:70])
    else:
        check("未配密钥时百度给出明确原因",
              d.get("ok") is False and "appid" in d.get("message", ""),
              d.get("message", "")[:70])

    print("4) 一次性接口 /api/translate")
    long_text = "\n\n".join(
        f"这是第 {i} 段测试文本，用于验证超长文档的分段翻译是否正常工作。"
        for i in range(60))
    t0 = time.monotonic()
    st, body = post_json("/api/translate", {
        "text": long_text, "engine": "google",
        "source_lang": "自动检测", "target_lang": "英语",
        "concurrency": 4, "max_retries": 2})
    d = json.loads(body)
    check("返回 ok", d.get("ok") is True, d.get("error", ""))
    if d.get("ok"):
        s = d["stats"]
        print(f"      {s['chars_in']:,} 字符 / {s['paragraphs']} 段 -> "
              f"{s['chunks']} 片 / {s['chars_out']:,} 字符 / "
              f"{s['elapsed']}s / 失败 {len(s['errors'])}")
        check("多段被正确切分", s["chunks"] >= 1)
        check("无失败片段", len(s["errors"]) == 0,
              str(s["errors"][:1])[:80] if s["errors"] else "")
        check("译文非空且已翻译",
              bool(d["text"].strip()) and d["text"].strip() != long_text.strip())
        check("段数守恒",
              d["text"].strip().count("\n\n") == long_text.strip().count("\n\n"),
              f"原文 {long_text.count(chr(10) + chr(10))} / 译文 "
              f"{d['text'].count(chr(10) + chr(10))}")

    print("5) 流式接口 /api/translate/stream (SSE)")
    req = urllib.request.Request(
        BASE + "/api/translate/stream",
        data=json.dumps({"text": long_text, "engine": "google",
                         "source_lang": "自动检测", "target_lang": "日语",
                         "concurrency": 4}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    events, buf = [], b""
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=180) as r:
        ctype = r.headers.get("Content-Type", "")
        while True:
            block = r.read1(4096)
            if not block:
                break
            buf += block
            while b"\n\n" in buf:
                frame, buf = buf.split(b"\n\n", 1)
                for line in frame.decode("utf-8", "ignore").split("\n"):
                    if line.startswith("data:"):
                        try:
                            events.append(json.loads(line[5:].strip()))
                        except json.JSONDecodeError:
                            pass
    kinds = [e.get("type") for e in events]
    check("Content-Type 为 SSE", "text/event-stream" in ctype, ctype)
    check("收到 start 事件", "start" in kinds)
    check("收到 progress 事件", "progress" in kinds)
    check("收到 done 事件", "done" in kinds)
    prog = [e for e in events if e.get("type") == "progress"]
    check("进度单调递增",
          all(a["done"] <= b["done"] for a, b in zip(prog, prog[1:])),
          f"{prog[0]['done'] if prog else '-'} -> {prog[-1]['done'] if prog else '-'}")
    done = next((e for e in events if e.get("type") == "done"), None)
    if done:
        check("done 带译文", bool(done["text"].strip()))
        check("done 无失败", len(done["errors"]) == 0)
        check("done 报告段落数", done["paragraphs"] == 60, str(done["paragraphs"]))

    print("6) 超长段落：默认拦住并提示用户分段")
    # 造一个"整段超长"的文档：单个段落无换行且超过 4500*0.97 字符上限
    big_para = "这是一个没有任何换行的超长段落，用来触发降级保护。" * 250
    over_doc = "短段落一。\n\n" + big_para + "\n\n短段落二。"
    st, body = post_json("/api/preflight", {"text": over_doc, "engine": "google",
                                            "target_lang": "英语"})
    pf = json.loads(body)
    check("预检返回 ok", pf.get("ok") is True, pf.get("error", ""))
    check("预检识别出超长段落", len(pf.get("degraded", [])) == 1,
          f"degraded={len(pf.get('degraded', []))}")
    check("预检不消耗额度（无 done 事件）", "text" not in pf)

    st, body = post_json_expect_error("/api/translate/stream",
                                      {"text": over_doc, "engine": "google",
                                       "target_lang": "英语"})
    check("未确认时流式接口返回 409", st == 409, f"HTTP {st}")
    d = json.loads(body)
    check("409 带有 needs_degradation 标记", d.get("code") == "needs_degradation")
    check("409 列出超长段落", len(d.get("degraded", [])) == 1)

    st, body = post_json_expect_error("/api/translate",
                                      {"text": over_doc, "engine": "google",
                                       "target_lang": "英语"})
    check("未确认时一次性接口也返回 409", st == 409, f"HTTP {st}")

    # 确认后放行
    req = urllib.request.Request(
        BASE + "/api/translate/stream",
        data=json.dumps({"text": over_doc, "engine": "google",
                         "target_lang": "英语", "allow_degradation": True,
                         "concurrency": 4}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    kinds, buf = [], b""
    with urllib.request.urlopen(req, timeout=180) as r:
        while True:
            block = r.read1(4096)
            if not block:
                break
            buf += block
            while b"\n\n" in buf:
                frame, buf = buf.split(b"\n\n", 1)
                for line in frame.decode("utf-8", "ignore").split("\n"):
                    if line.startswith("data:"):
                        try:
                            kinds.append(json.loads(line[5:].strip()))
                        except json.JSONDecodeError:
                            pass
    done = next((k for k in kinds if k.get("type") == "done"), None)
    check("确认后放行并翻译成功", done is not None and bool(done["text"].strip()))
    check("确认后 degraded 事件被回传", bool((done or {}).get("degraded")))

    print("7) 错误处理")
    st, body = post_json_expect_error("/api/translate",
                                      {"text": "", "engine": "google"})
    check("空文本被拒绝", st == 400 and b"ok" in body,
          f"HTTP {st} {body[:70]!r}")
    st, body = post_json_expect_error("/api/translate",
                                      {"text": "x", "engine": "nope"})
    check("未知引擎被拒绝", st == 400, f"HTTP {st}")
    st, body = post_json_expect_error("/api/translate",
                                      {"text": "hello", "engine": "baidu",
                                       "target_lang": "英语"})
    creds = json.loads(get("/api/meta")[1])["settings"]
    if creds.get("baidu_secret_set"):
        # 已配置凭据：要么真翻译成功，要么全部片段失败并明确报错——
        # 绝不能出现 ok:true 却静默返回原文的情况
        ok = json.loads(body).get("ok") if st == 200 else None
        check("已配凭据时不会静默返回原文",
              st == 200 and ok is True or st == 502,
              f"HTTP {st} ok={ok}")
    else:
        check("百度缺密钥时给出明确原因", st == 400 and b"appid" in body,
              body[:70].decode("utf-8", "ignore"))

    print("-" * 62)
    print(f"用时 {time.monotonic() - t0:.1f}s；失败项：{fails or '无'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())