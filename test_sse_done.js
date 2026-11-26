/**
 * createSSE 完成/错误语义自检（Node，无浏览器、不启服务）。
 *
 * 为什么值得单独钉住：这个 bug 的表现是"控制台一条 error"，看着像噪音，
 * 实际是**每次成功都走错误分支**——onDone 从不触发、收尾帧的 todos 被丢、
 * 且只要摘要正文恰好为空就会显示"摘要生成失败"。
 * 只靠"控制台没报错"来验证太间接，日后有人重构 sse.js 会静默回归。
 *
 * 用法：node test_sse_done.js
 */
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, 'frontend', 'js', 'utils', 'sse.js');
const src = fs.readFileSync(SRC, 'utf8').replace(/\r\n/g, '\n');   // Windows 检出可能是 CRLF

const START = 'export function createSSE(';
const END = '/**\n * 使用 fetch 创建 POST SSE 流';
const s = src.indexOf(START);
const e = src.indexOf(END);
if (s < 0 || e < 0 || e <= s) {
  console.error('FAIL: 无法从 sse.js 定位 createSSE');
  process.exit(2);
}
const fnSrc = src.slice(s, e).replace(/^export\s+/, '');

// ---- 桩 EventSource ----
let instances = [];
class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.closed = false;
    this.onmessage = null;
    this.onerror = null;
    instances.push(this);
  }
  close() { this.closed = true; }
  emit(data) { this.onmessage && this.onmessage({ data }); }
  fail(ev) { this.onerror && this.onerror(ev); }
}
global.EventSource = FakeEventSource;

const createSSE = eval(
  // createSSE 里用到模块级的 BASE_URL（sse.js 里是 ''），抽出来跑必须补上
  "const BASE_URL = '';" +
  '(' + fnSrc.replace(/^function\s+createSSE/, 'function') + ')');

let fails = 0;
function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  console.log('  ' + (ok ? 'PASS' : 'FAIL') + ' ' + label.padEnd(38) +
    ' got=' + JSON.stringify(got) + ' want=' + JSON.stringify(want));
  if (!ok) fails++;
}

function scenario() {
  instances = [];
  const log = { msg: [], done: [], err: [] };
  const es = createSSE('/api/stream/summary/x', {
    onMessage: (d) => log.msg.push(d),
    onDone: (d) => log.done.push(d === undefined ? '(no-arg)' : d),
    onError: (e) => log.err.push(String(e && e.message || e)),
  });
  return { es, log };
}

console.log('=== 1. 普通消息：不关流，只回调 onMessage ===');
{
  const { es, log } = scenario();
  es.emit(JSON.stringify({ text: '摘要片段' }));
  check('onMessage 收到', log.msg.length, 1);
  check('文本正确', log.msg[0].text, '摘要片段');
  check('未关流', es.closed, false);
  check('未误触 onDone', log.done.length, 0);
  check('未误触 onError', log.err.length, 0);
}

console.log('=== 2. 收尾帧 {done:true}：关流 + onDone，且带上负载 ===');
{
  const { es, log } = scenario();
  es.emit(JSON.stringify({ text: '正文' }));
  es.emit(JSON.stringify({ todos: ['甲', '乙'], done: true }));
  check('已关流', es.closed, true);
  check('onDone 触发一次', log.done.length, 1);
  check('onDone 拿到 todos', log.done[0].todos, ['甲', '乙']);
  check('收尾帧也给了 onMessage', log.msg.length, 2);
  check('没走错误分支', log.err.length, 0);
}

console.log('=== 3. 服务端关流引发的 onerror：不应发生在正常完成之后 ===');
{
  const { es, log } = scenario();
  es.emit(JSON.stringify({ done: true }));
  const closedBefore = es.closed;
  es.fail({ type: 'error' });   // 已关流后即便再触发，也已拿到 onDone
  check('done 后即已关流', closedBefore, true);
  check('onDone 已先行触发', log.done.length, 1);
}

console.log('=== 4. 后端下发 error 帧：走 onError，不当正文渲染 ===');
{
  const { es, log } = scenario();
  es.emit(JSON.stringify({ error: '邮件不存在' }));
  check('已关流', es.closed, true);
  check('onError 触发', log.err.length, 1);
  check('错误信息透传', log.err[0], '邮件不存在');
  check('未当作正文', log.msg.length, 0);
}

console.log('=== 5. 兼容旧的 [DONE] 文本信号 ===');
{
  const { es, log } = scenario();
  es.emit('[DONE]');
  check('已关流', es.closed, true);
  check('onDone 触发', log.done.length, 1);
}

console.log('=== 6. 非 JSON 纯文本：按文本交给 onMessage ===');
{
  const { es, log } = scenario();
  es.emit('裸文本');
  check('onMessage 收到裸文本', log.msg[0], '裸文本');
  check('未关流', es.closed, false);
}

console.log('=== 7. 真正的传输错误仍走 onError 并关流 ===');
{
  const { es, log } = scenario();
  es.fail({ type: 'error' });
  check('已关流', es.closed, true);
  check('onError 触发', log.err.length, 1);
}

console.log();
console.log(fails ? 'RESULT FAILED (' + fails + ' 项)' : 'RESULT ALL_PASS');
process.exit(fails ? 1 : 0);
