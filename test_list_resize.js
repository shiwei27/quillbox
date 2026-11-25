/**
 * 列表栏拖拉调整逻辑自检（Node，无浏览器）：
 * 从 frontend/js/stores/ui.js 抽出 clampListWidth/startListResize/resetListWidth，
 * 套桩环境跑断言；再对 index.html 与 app.css 做接线检查。
 * 不启服务、不碰数据库。
 *
 * 为什么值得写：这三个方法的正确性全在边界上——夹取上限、事件解绑、
 * userSelect 复原、localStorage 落盘。这些在浏览器里都得靠手感验，
 * 手感验不出「拖完之后 mousemove 监听没摘掉」这种问题。
 *
 * 用法：node test_list_resize.js
 */
const fs = require('fs');
const path = require('path');

const UI = path.join(__dirname, 'frontend', 'js', 'stores', 'ui.js');
const HTML = path.join(__dirname, 'frontend', 'index.html');
const CSS = path.join(__dirname, 'frontend', 'css', 'app.css');

const src = fs.readFileSync(UI, 'utf8');
const START = '    clampListWidth(w) {';
const END = '    // 切换命令面板';
const s = src.indexOf(START);
const e = src.indexOf(END);
if (s < 0 || e < 0 || e <= s) {
  console.error('FAIL: 无法从 ui.js 定位列表拖拉方法块');
  process.exit(2);
}
const body = src.slice(s, e).trim().replace(/,\s*$/, '');

// ---- 桩环境 ----
let listeners = {};
let storage = {};
global.window = { innerWidth: 1600 };
global.document = {
  body: { style: {} },
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  removeEventListener(type, fn) {
    listeners[type] = (listeners[type] || []).filter((f) => f !== fn);
  },
};
global.localStorage = {
  getItem: (k) => (k in storage ? storage[k] : null),
  setItem: (k, v) => { storage[k] = String(v); },
};

const store = eval('({ listWidth: 340, _listResizing: false, ' + body + ' })');

let fails = 0;
function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  // Node 的 console.log 不支持 %-40s 这种宽度指示，手工补空格
  console.log('  ' + (ok ? 'PASS' : 'FAIL') + ' ' + label.padEnd(30) +
    ' got=' + JSON.stringify(got) + ' want=' + JSON.stringify(want));
  if (!ok) fails++;
}

console.log('=== 1. clampListWidth 边界 ===');
check('低于下限夹到 260', store.clampListWidth(10), 260);
check('区间内原样返回', store.clampListWidth(400), 400);
check('高于上限夹到 720', store.clampListWidth(9999), 720);
check('非数字回落默认 340', store.clampListWidth('abc'), 340);

console.log('=== 2. 上限跟随窗口宽度（详情栏至少留 480）===');
global.window.innerWidth = 900;   // 900-480=420，比 720 小，应以 420 为上限
check('窄窗上限 = innerWidth-480', store.clampListWidth(9999), 420);
global.window.innerWidth = 600;   // 600-480=120，低于下限，应保底到 260
check('极窄窗保底到下限', store.clampListWidth(9999), 260);
global.window.innerWidth = 1600;

console.log('=== 3. 拖动流程 ===');
listeners = {};
store.listWidth = 340;
store.startListResize({ clientX: 500 });
check('拖动中标志置位', store._listResizing, true);
check('光标锁为 col-resize', global.document.body.style.cursor, 'col-resize');
check('选区已锁', global.document.body.style.userSelect, 'none');
check('挂了 mousemove', (listeners.mousemove || []).length, 1);
check('挂了 mouseup', (listeners.mouseup || []).length, 1);

listeners.mousemove[0]({ clientX: 620 });     // +120
check('右拖加宽', store.listWidth, 460);
listeners.mousemove[0]({ clientX: 300 });     // -200
check('左拖收窄', store.listWidth, 260);
listeners.mousemove[0]({ clientX: 100 });     // 越过下限
check('越过下限被夹住', store.listWidth, 260);

listeners.mouseup[0]();
check('拖动标志复位', store._listResizing, false);
check('光标已复原', global.document.body.style.cursor, '');
check('选区已解锁', global.document.body.style.userSelect, '');
check('mousemove 已摘除', (listeners.mousemove || []).length, 0);
check('mouseup 已摘除', (listeners.mouseup || []).length, 0);
check('宽度已落盘', storage['quillbox-list-width'], '260');

console.log('=== 4. 双击复位 ===');
store.resetListWidth();
check('复位到 340', store.listWidth, 340);
check('复位也落盘', storage['quillbox-list-width'], '340');

console.log('=== 5. 接线检查（html / css）===');
const html = fs.readFileSync(HTML, 'utf8');
const css = fs.readFileSync(CSS, 'utf8');
check('w-list 固定宽度已移除', html.includes('w-list'), false);
check('列表栏改为 style 绑定', html.includes("$store.ui.listWidth"), true);
check('手柄绑 mousedown', html.includes('startListResize($event)'), true);
check('手柄绑 dblclick', html.includes('resetListWidth()'), true);
check('手柄类名已定义', css.includes('.pane-resize-handle {'), true);
check('手柄光标为 col-resize', /\.pane-resize-handle\s*\{[^}]*col-resize/.test(css), true);
// 侧边栏那套已验证过，不能被这次改动碰到
check('侧边栏手柄仍在', html.includes('startSidebarResize($event)'), true);
check('侧边栏样式仍在', css.includes('.sidebar-resize-handle {'), true);

console.log();
console.log(fails ? 'RESULT FAILED (' + fails + ' 项)' : 'RESULT ALL_PASS');
process.exit(fails ? 1 : 0);
