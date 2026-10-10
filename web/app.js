'use strict';

/* ============================================================
   超长文本翻译 —— 前端逻辑
   ============================================================ */

const $ = (id) => document.getElementById(id);

const el = {
  engine: $('engine'), srcLang: $('srcLang'), dstLang: $('dstLang'), autoHint: $('autoHint'),
  concurrency: $('concurrency'), chunkLimit: $('chunkLimit'), maxRetries: $('maxRetries'),
  credPanel: $('credPanel'), baiduAppid: $('baiduAppid'), baiduSecret: $('baiduSecret'),
  unitHint: $('unitHint'), engineBadge: $('engineBadge'),
  source: $('source'), target: $('target'),
  srcCount: $('srcCount'), dstCount: $('dstCount'), splitInfo: $('splitInfo'),
  progress: $('progress'), barFill: $('barFill'), statMain: $('statMain'), statLine: $('statLine'),
  warnBox: $('warnBox'), hintLine: $('hintLine'), toast: $('toast'),
  btnGo: $('btnGo'), btnCopy: $('btnCopy'), btnDownload: $('btnDownload'),
  btnPlay: $('btnPlay'), btnCancel: $('btnCancel'), btnClear: $('btnClear'),
  btnFile: $('btnFile'), btnSample: $('btnSample'), btnSwap: $('btnSwap'),
  btnCheck: $('btnCheck'), btnSaveCred: $('btnSaveCred'),
  fileInput: $('fileInput'), bom: $('bom'), credNote: $('credNote'),
};

let META = { engines: [], languages: [] };
let controller = null;      // AbortController
let running = false;
let lastResult = null;

/* ---------------- 工具 ---------------- */

function toast(msg, kind = '') {
  el.toast.textContent = msg;
  el.toast.className = 'toast ' + kind;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.toast.classList.add('hidden'), 3600);
}

function num(n) { return (n ?? 0).toLocaleString('zh-CN'); }

function fmtDuration(sec) {
  if (!sec || sec < 0) return '--';
  if (sec < 60) return sec.toFixed(0) + ' 秒';
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return m + ' 分 ' + s + ' 秒';
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/* ---------------- 初始化 ---------------- */

async function boot() {
  try {
    const r = await fetch('/api/meta');
    META = await r.json();
  } catch (e) {
    el.engineBadge.textContent = '无法连接后端';
    el.engineBadge.className = 'badge err';
    return;
  }

  fillLangs();
  applySettings(META.settings);
  onEngineChange();

  el.engine.addEventListener('change', onEngineChange);
  el.dstLang.addEventListener('change', () => { el.autoHint.textContent = ''; });
  el.btnGo.addEventListener('click', start);
  el.btnCancel.addEventListener('click', cancel);
  el.btnClear.addEventListener('click', clearAll);
  el.btnSample.addEventListener('click', loadSample);
  el.btnFile.addEventListener('click', () => el.fileInput.click());
  el.fileInput.addEventListener('change', (e) => {
    if (e.target.files[0]) readFile(e.target.files[0]);
    e.target.value = '';
  });
  el.btnSwap.addEventListener('click', swap);
  el.btnCopy.addEventListener('click', copyOut);
  el.btnDownload.addEventListener('click', downloadOut);
  el.btnPlay.addEventListener('click', playOut);
  el.btnCheck.addEventListener('click', checkEngine);
  el.btnSaveCred.addEventListener('click', saveCred);

  el.source.addEventListener('input', debounce(updateSourceStats, 200));
  setupDrop();

  window.addEventListener('beforeunload', (e) => {
    if (running) { e.preventDefault(); e.returnValue = ''; }
  });

  updateSourceStats();
  toast('就绪。粘贴或拖入文本即可。', 'ok');
}

function fillLangs() {
  const opts = META.languages
    .map((l) => `<option value="${l.key}">${l.label}</option>`).join('');
  el.srcLang.innerHTML = opts;
  el.dstLang.innerHTML = opts;
}

function applySettings(s) {
  if (!s) return;
  el.engine.value = s.engine || 'google';
  el.srcLang.value = s.source_lang || META.languages[0]?.key || '自动检测';
  el.dstLang.value = s.target_lang || '中文（简体）';
  el.concurrency.value = s.concurrency ?? 4;
  el.maxRetries.value = s.max_retries ?? 4;
  el.chunkLimit.value = s.chunk_limit ?? 0;
  el.baiduAppid.value = s.baidu_appid || '';
  if (s.baidu_secret_set) el.baiduSecret.placeholder = '已保存（留空则沿用）';
  localStorage.setItem('slt.langs', JSON.stringify([el.srcLang.value, el.dstLang.value]));
}

function engineCfg(name) {
  return META.engines.find((e) => e.name === name) || {};
}

function onEngineChange() {
  const cfg = engineCfg(el.engine.value);
  const isBaidu = el.engine.value === 'baidu';
  el.credPanel.classList.toggle('hidden', !isBaidu);
  el.unitHint.textContent = cfg.measure_is_bytes ? '(字节)' : '(字符)';
  el.chunkLimit.placeholder = cfg.measure_is_bytes
    ? `自动 ${cfg.limit_bytes}` : `自动 ${cfg.limit_chars}`;
  el.chunkLimit.title = isBaidu
    ? '百度官方单次上限 6000 字节，留余量后为 5800'
    : 'Google 端点实测可到 4 万字符以上；调大更快，调小更稳';
  updateBadge();
}

function updateBadge(state, text) {
  el.engineBadge.className = 'badge' + (state ? ' ' + state : '');
  el.engineBadge.textContent = text || (engineCfg(el.engine.value).display_name || el.engine.value);
}

/* ---------------- 原文统计 ---------------- */

/* ---------------- 语种探测与目标语言兜底 ----------------
   目标语言被误选成「自动检测」时，按原文实际语种自动挑一个合理目标：
   原文是中文 -> 英语；原文是其它（日/韩/拉丁字母）-> 中文（简体）。
   与后端 app/langdetect.py 保持同一套规则。 */

function detectLang(text) {
  const s = text.slice(0, 20000);
  let han = 0, kana = 0, hangul = 0, latin = 0;
  for (const ch of s) {
    const o = ch.codePointAt(0);
    if ((o >= 0x4e00 && o <= 0x9fff) || (o >= 0x3400 && o <= 0x4dbf)) han++;
    else if (o >= 0x3040 && o <= 0x30ff) kana++;
    else if ((o >= 0xac00 && o <= 0xd7af) || (o >= 0x1100 && o <= 0x11ff)) hangul++;
    else if ((o >= 0x41 && o <= 0x5a) || (o >= 0x61 && o <= 0x7a)) latin++;
  }
  // 假名 / 谚文是决定性特征：日语汉字再多也不能当成中文
  if (kana >= 5) return 'ja';
  if (hangul >= 5) return 'ko';
  if (han >= 4) return 'zh';
  if (latin >= 8) return 'latin';
  return 'unknown';
}

const LANG_NAME = {
  zh: '中文', ja: '日语', ko: '韩语',
  latin: '拉丁字母语言（英语等）', unknown: '无法判定',
};

const AUTO_LABEL = '自动检测';
const TARGET_ZH = '中文（简体）';
const TARGET_EN = '英语';

function autoPickTarget() {
  if (el.dstLang.value !== AUTO_LABEL) {
    el.autoHint.textContent = '';
    return null;
  }
  const t = el.source.value;
  if (!t.trim()) { el.autoHint.textContent = ''; return null; }
  const lang = detectLang(t);
  if (lang === 'unknown') { el.autoHint.textContent = ''; return null; }
  const target = lang === 'zh' ? TARGET_EN : TARGET_ZH;
  el.dstLang.value = target;
  el.autoHint.textContent = `原文为${LANG_NAME[lang]} → 目标自动设为${target}`;
  return { lang, target };
}

function updateSourceStats() {
  const t = el.source.value;
  const chars = t.length;
  const paras = t.trim() ? t.trim().split(/\r?\n[ \t]*\r?\n|\r?\n/).filter((x) => x.trim()).length : 0;
  el.srcCount.textContent = `${num(chars)} 字符 · ${num(paras)} 段`;

  // 目标语言为「自动检测」时按原文语种自动兜底
  autoPickTarget();

  const cfg = engineCfg(el.engine.value);
  const limit = cfg.measure_is_bytes ? cfg.limit_bytes : cfg.limit_chars;
  const need = cfg.measure_is_bytes ? new Blob([t]).size : chars;
  el.splitInfo.textContent = need > limit * 0.97
    ? `超出单次上限，将切成约 ${Math.ceil(need / (limit * 0.97))} 片`
    : '';
}

function clearAll() {
  el.source.value = '';
  el.target.value = '';
  el.splitInfo.textContent = '';
  el.statLine.textContent = '';
  updateSourceStats();
  setOutputEnabled(false);
}

function swap() {
  if (!el.target.value) return toast('译文为空，无法回填', 'err');
  el.source.value = el.target.value;
  el.target.value = '';
  updateSourceStats();
  toast('已回填到原文框', 'ok');
}

function setOutputEnabled(on) {
  el.btnCopy.disabled = !on;
  el.btnDownload.disabled = !on;
  el.btnPlay.disabled = !on;
}

/* ---------------- 文件 ---------------- */

function readFile(file) {
  const r = new FileReader();
  r.onload = () => {
    el.source.value = String(r.result || '');
    updateSourceStats();
    toast(`已导入 ${file.name}（${num(el.source.value.length)} 字符）`, 'ok');
  };
  r.onerror = () => toast('文件读取失败', 'err');
  r.readAsText(file, 'utf-8');
}

function setupDrop() {
  const pane = el.source.closest('.pane');
  ['dragenter', 'dragover'].forEach((ev) => pane.addEventListener(ev, (e) => {
    e.preventDefault(); pane.classList.add('drop-hint');
  }));
  ['dragleave', 'drop'].forEach((ev) => pane.addEventListener(ev, (e) => {
    e.preventDefault(); pane.classList.remove('drop-hint');
  }));
  pane.addEventListener('drop', (e) => {
    const f = e.dataTransfer?.files?.[0];
    if (f) readFile(f);
  });
}

const SAMPLE = `The Art of Long-Form Translation

Translating a long document is not the same problem as translating a sentence.
Sentence-level translation loses context; paragraph-level translation keeps it.

Why 5000 characters is not a real limit

The cap you see in a browser is a user-interface restriction, not a server limit.
Behind that box sits an endpoint that comfortably accepts far more than 5000
characters in a single request. Splitting the text, sending it in pieces, and
reassembling it in order is the whole trick.

What actually matters

1. Split on paragraph boundaries, so every natural unit stays intact.
2. Send pieces concurrently, but politely — too much concurrency gets you rate-limited.
3. Retry with exponential backoff, and split a failing piece in half rather than giving up.
4. Reassemble in the original order, keeping the paragraph structure the author wrote.

A translator that respects structure will always beat one that respects only speed.`;

function loadSample() {
  el.source.value = SAMPLE;
  updateSourceStats();
  toast('已载入示例文本', 'ok');
}

/* ---------------- 翻译 ---------------- */

function collectPayload(allowDegrade = false) {
  const p = {
    text: el.source.value,
    engine: el.engine.value,
    source_lang: el.srcLang.value,
    target_lang: el.dstLang.value,
    concurrency: +el.concurrency.value || 4,
    max_retries: +el.maxRetries.value || 4,
    chunk_limit: +el.chunkLimit.value || 0,
    baidu_appid: el.baiduAppid.value.trim(),
    baidu_secret: el.baiduSecret.value.trim(),
  };
  if (allowDegrade) p.allow_degradation = true;
  return p;
}

function setRunning(on) {
  running = on;
  el.btnGo.disabled = on;
  el.btnGo.textContent = on ? '翻译中…' : '开始翻译';
  el.btnCancel.classList.toggle('hidden', !on);
  el.progress.classList.toggle('hidden', !on);
}

async function start() {
  if (running) return;
  const text = el.source.value;
  if (!text.trim()) return toast('请先粘贴或导入文本', 'err');

  localStorage.setItem('slt.langs', JSON.stringify([el.srcLang.value, el.dstLang.value]));
  hideWarn();
  el.target.value = '';
  el.statLine.textContent = '';
  setOutputEnabled(false);

  // 先做切分预检（本地计算，不消耗翻译额度）
  let allowDegrade = false;
  autoPickTarget();          // 兜底：目标仍为「自动检测」时先按原文语种定好
  el.hintLine.textContent = '正在检查切分情况…';
  let pre;
  try {
    const r = await fetch('/api/preflight', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectPayload(false)),
    });
    pre = await r.json();
    if (!pre.ok) {
      toast('预检失败：' + (pre.error || '未知错误'), 'err');
      el.hintLine.textContent = '预检失败：' + (pre.error || '');
      return;
    }
  } catch (e) {
    toast('预检失败：' + e.message, 'err');
    el.hintLine.textContent = '预检失败：' + e.message;
    return;
  }

  // 后端也可能做过兜底，同步回界面，保证下拉框显示的就是实际使用的语言
  if (pre.target?.auto_picked) {
    el.dstLang.value = pre.target.label;
    el.autoHint.textContent =
      `原文为${pre.detected_label || '未知语种'} → 目标自动设为${pre.target.label}`;
  }

  // 有超长段落 -> 停下来让用户自己分段，而不是默默切碎
  if (pre.degraded?.length) {
    const proceed = await showDegradeDialog(pre);
    if (!proceed) {
      el.hintLine.textContent =
        `已暂停：${pre.degraded.length} 个段落超长。请在原文框里把它们分段后再试。`;
      return;
    }
    allowDegrade = true;
  }

  setRunning(true);

  controller = new AbortController();
  const t0 = performance.now();
  let gotText = false;

  try {
    const resp = await fetch('/api/translate/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectPayload(allowDegrade)),
      signal: controller.signal,
    });

    if (!resp.ok || !resp.body) {
      let msg = `服务端返回 ${resp.status}`;
      try {
        const j = await resp.json();
        if (j.error) msg = j.error;
      } catch { /* 忽略非 JSON 响应 */ }
      throw new Error(msg);
    }

    for await (const ev of readSSE(resp.body)) {
      if (ev.type === 'start') onStart(ev);
      else if (ev.type === 'progress') onProgress(ev);
      else if (ev.type === 'done') onDone(ev, t0);
      else if (ev.type === 'error') throw new Error(ev.message);
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      el.hintLine.textContent = '已取消。已完成的译文保留在右侧。';
      toast('已取消', '');
    } else {
      toast('翻译失败：' + e.message, 'err');
      el.hintLine.textContent = '失败：' + e.message;
    }
  } finally {
    setRunning(false);
    controller = null;
    if (gotText) setOutputEnabled(true);
  }
}

function cancel() {
  controller?.abort();
}

/* ---------------- 超长段落弹窗 ---------------- */

function showDegradeDialog(pre) {
  const unit = pre.measure === 'bytes' ? '字节' : '字符';
  $('dgCount').textContent = pre.degraded.length;
  $('dgList').innerHTML = pre.degraded.slice(0, 8).map((d) => `
    <div class="dg-item">
      <div class="dg-head">第 ${d.paragraph + 1} 段 · ${num(d.chars)} 字符
        （上限 ${num(pre.limit)} ${unit}）</div>
      <div class="dg-body">${escapeHtml(d.preview)}…</div>
    </div>`).join('') +
    (pre.degraded.length > 8
      ? `<div class="dg-more">…另有 ${pre.degraded.length - 8} 段</div>` : '');
  $('mask').classList.remove('hidden');
  // true = 继续自动切分；false = 我去分段
  return new Promise((resolve) => {
    const done = (v) => {
      $('mask').classList.add('hidden');
      $('btnSplit').onclick = null;
      $('btnForce').onclick = null;
      $('mask').onclick = null;
      resolve(v);
    };
    $('btnSplit').onclick = () => {
      toast('请在原文框中给超长段落空行分段', '');
      done(false);
    };
    $('btnForce').onclick = () => done(true);
    $('mask').onclick = (e) => { if (e.target === $('mask')) done(false); };
    $('btnForce').focus();
  });
}

function onStart(ev) {
  updateBadge('ok', ev.engine_name);
  el.barFill.style.width = '0%';
  el.statMain.textContent = `正在翻译 ${num(ev.chunks)} 片…`;
  el.hintLine.textContent =
    `共 ${num(ev.chars)} 字符 / ${num(ev.paragraphs)} 段，切成 ${num(ev.chunks)} 片` +
    `，每片上限 ${num(ev.limit)} ${ev.measure === 'bytes' ? '字节' : '字符'}。`;

  if (ev.degraded?.length) {
    const items = ev.degraded.slice(0, 6).map((d) =>
      `<div class="witem">第 ${d.paragraph + 1} 段（${num(d.chars)} 字符）
        已按你的确认在段内切为 ${d.pieces} 片：${escapeHtml(d.preview)}…</div>`).join('');
    showWarn(`<b>${ev.degraded.length} 个超长段落已在段内切分</b>（你已确认）。
      其余段落均按完整段落下发，未被拆散。${ev.degraded.length > 6
        ? `<div class="witem">…另有 ${ev.degraded.length - 6} 段</div>` : ''}${items}`);
  }
}

function onProgress(p) {
  const pct = p.total ? (p.done / p.total) * 100 : 0;
  el.barFill.style.width = pct.toFixed(1) + '%';
  const speed = p.chars_out / Math.max(0.001, p.elapsed);
  el.statMain.textContent =
    `${p.done}/${p.total} 片 · 成功 ${p.ok} · 失败 ${p.failed}` +
    ` · ${num(speed)} 字符/秒`;
  el.statLine.textContent = `已输出 ${num(p.chars_out)} 字符 · 用时 ${fmtDuration(p.elapsed)}` +
    (p.eta > 0 ? ` · 剩余约 ${fmtDuration(p.eta)}` : '');
}

function onDone(ev, t0) {
  el.barFill.style.width = '100%';
  el.target.value = ev.text || '';
  el.dstCount.textContent = `${num(ev.text.length)} 字符`;
  const secs = (performance.now() - t0) / 1000;
  el.statLine.textContent =
    `${num(ev.chunks)} 片 / ${num(ev.paragraphs)} 段 · 用时 ${fmtDuration(secs)}`;
  el.hintLine.textContent = ev.errors?.length
    ? `完成，但有 ${ev.errors.length} 片失败（已保留原文占位），见上方提示。`
    : '翻译完成。';
  if (ev.errors?.length) {
    showWarn(`<b>${ev.errors.length} 片翻译失败</b>，对应位置已保留原文。<div class="witem">` +
      ev.errors.slice(0, 5).map((x) => `#${x.chunk + 1}：${escapeHtml(x.error)}`).join('<br>') +
      `</div>`, true);
  }
  lastResult = ev;
  el.btnCopy.disabled = el.btnDownload.disabled = el.btnPlay.disabled = false;
  toast('翻译完成', 'ok');
}

/* 解析 SSE 流 */
async function* readSSE(body) {
  const reader = body.getReader();
  const dec = new TextDecoder('utf-8');
  let buf = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of frame.split('\n')) {
        if (line.startsWith('data:')) {
          const raw = line.slice(5).trim();
          if (!raw) continue;
          try { yield JSON.parse(raw); } catch { /* 忽略半包 */ }
        }
      }
    }
  }
}

/* ---------------- 告警 / 输出 ---------------- */

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function showWarn(html, sticky) {
  el.warnBox.innerHTML = html;
  el.warnBox.classList.remove('hidden');
  if (!sticky) setTimeout(() => { if (!running) hideWarn(); }, 20000);
}
function hideWarn() { el.warnBox.classList.add('hidden'); el.warnBox.innerHTML = ''; }

async function copyOut() {
  try {
    await navigator.clipboard.writeText(el.target.value);
    toast('译文已复制', 'ok');
  } catch {
    el.target.select(); document.execCommand('copy');
    toast('译文已复制', 'ok');
  }
}

function downloadOut() {
  let content = el.target.value;
  if (el.bom.checked) content = '﻿' + content;   // Windows 记事本需要 BOM
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
  const a = document.createElement('a');
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
  a.href = URL.createObjectURL(blob);
  a.download = `译文_${stamp}.txt`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  toast('已下载', 'ok');
}

let audio = null;
function playOut() {
  const lang = (engineCfg(el.engine.value).measure_is_bytes ? 'zh' : 'en');
  const text = el.target.value.slice(0, 4000);
  if (!text) return;
  if (audio) { audio.pause(); audio = null; }
  audio = new Audio(`/api/tts?lang=${encodeURIComponent(lang)}&text=` + encodeURIComponent(text));
  audio.play().then(
    () => toast('开始朗读（仅前 4000 字）', 'ok'),
    () => toast('朗读失败，可能被限流', 'err')
  );
}

/* ---------------- 其它接口 ---------------- */

async function checkEngine() {
  updateBadge('', '检测中…');
  try {
    const r = await fetch('/api/check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...collectPayload(), text: 'Hello world 你好世界' }),
    });
    const d = await r.json();
    updateBadge(d.ok ? 'ok' : 'err', d.ok ? '引擎可用' : '引擎不可用');
    toast(d.message, d.ok ? 'ok' : 'err');
  } catch (e) {
    updateBadge('err', '检测失败');
    toast('检测失败：' + e.message, 'err');
  }
}

async function saveCred() {
  const patch = { baidu_appid: el.baiduAppid.value.trim() };
  if (el.baiduSecret.value.trim()) patch.baidu_secret = el.baiduSecret.value.trim();
  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    const d = await r.json();
    if (d.ok) {
      el.baiduSecret.value = '';
      el.baiduSecret.placeholder = '已保存（留空则沿用）';
      toast('凭据已保存到 data/settings.json', 'ok');
    }
  } catch (e) {
    toast('保存失败：' + e.message, 'err');
  }
}

document.addEventListener('DOMContentLoaded', boot);