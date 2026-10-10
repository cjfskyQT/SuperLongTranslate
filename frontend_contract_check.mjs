// 前端 DOM 契约校验：确保 app.js 引用的每个元素 id 都真实存在于 index.html。
// 这类"id 拼错"是最难靠肉眼发现、却会让按钮彻底失效的错误。
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)));
const html = readFileSync(join(root, 'web', 'index.html'), 'utf8');
const js = readFileSync(join(root, 'web', 'app.js'), 'utf8');
const css = readFileSync(join(root, 'web', 'style.css'), 'utf8');

const htmlIds = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));

// app.js 通过 $('id') 取用（含 el 映射块与散落调用），全文件扫描
const jsIds = [...js.matchAll(/\$\(\s*'([^']+)'\s*\)/g)].map((m) => m[1]);

// 其它散落的 getElementById
const loose = [...js.matchAll(/getElementById\('([^']+)'\)/g)].map((m) => m[1]);

const all = [...new Set([...jsIds, ...loose])];
const missing = all.filter((id) => !htmlIds.has(id));
const unused = [...htmlIds].filter((id) => !all.includes(id));

console.log(`index.html 中 id  : ${htmlIds.size}`);
console.log(`app.js 引用 id    : ${all.length}`);
console.log(`一一对应          : ${all.length - missing.length}/${all.length}`);
if (missing.length) console.log(`✗ 页面中不存在    : ${missing.join(', ')}`);
if (unused.length) console.log(`· 页面有但未引用  : ${unused.join(', ')}`);

// 语法校验由 node 解析本文件本身完成；再用动态 import 确认 app.js 无语法错误
let syntaxOk = true;
try {
  await import('node:vm').then((vm) =>
    new vm.Script(js.replace(/^\s*document\.addEventListener.*$/m, ''), { filename: 'app.js' })
  );
} catch (e) {
  syntaxOk = false;
  console.log(`✗ app.js 语法错误 : ${e.message}`);
}
if (syntaxOk) console.log('✓ app.js 语法正确');

// CSS 里引用的关键 class 是否都存在于 HTML
for (const cls of ['hidden', 'primary', 'ghost', 'pane', 'drop-hint']) {
  const inCss = css.includes('.' + cls);
  const inHtml = html.includes(`"${cls}`) || html.includes(` ${cls}"`) || new RegExp(`class="[^"]*\\b${cls}\\b`).test(html);
  if (inCss && !inHtml) console.log(`· CSS 定义了 .${cls} 但页面未使用（无害）`);
}
console.log('· CSS 关键 class 检查完成');

process.exit(missing.length || !syntaxOk ? 1 : 0);