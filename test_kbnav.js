/**
 * 键盘导航逻辑自检（Node，无浏览器）：
 * 从 frontend/js/stores/mail.js 抽取 selectNext/selectPrev/gotoEmail/scrollRowIntoView 四个方法，
 * 套上桩环境跑断言。不启服务、不碰数据库。
 *
 * 用法：node test_kbnav.js
 */
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, 'frontend', 'js', 'stores', 'mail.js');
const src = fs.readFileSync(SRC, 'utf8');

const START = '    selectNext() {';
const END = '    // ============ 账户/同步 ============';
const s = src.indexOf(START);
const e = src.indexOf(END);
if (s < 0 || e < 0 || e <= s) {
  console.error('FAIL: 无法从 mail.js 定位导航方法块');
  process.exit(2);
}
const body = src.slice(s, e).trim().replace(/,\s*$/, '');

// 桩环境
let scrolled = [];
global.window = { CSS: { escape: (x) => x } };
global.CSS = global.window.CSS;
global.document = {
  querySelector(sel) {
    const m = /\[data-email-id="(.+)"\]/.exec(sel);
    return m ? { scrollIntoView: (opt) => scrolled.push([m[1], opt && opt.block]) } : null;
  },
};
global.requestAnimationFrame = (fn) => fn();

const makeStore = new Function('return {' + body + '};');

function newStore(emails, selectedId, hasMore) {
  const st = makeStore();
  st.emails = emails.slice();
  st.selectedId = selectedId;
  st.hasMore = !!hasMore;
  st.loading = false;
  st.opened = [];
  st.loadMoreCalls = 0;
  st.openEmail = function (id) { st.opened.push(id); };
  st.loadMore = function () {
    st.loadMoreCalls++;
    st.emails = st.emails.concat([{ id: 'p2a' }, { id: 'p2b' }]);
    st.hasMore = false;
    return Promise.resolve();
  };
  return st;
}

const page = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];
const failed = [];
function check(name, cond, extra) {
  console.log((cond ? '  [PASS] ' : '  [FAIL] ') + name + (extra ? ' -> ' + extra : ''));
  if (!cond) failed.push(name);
}

// 1) 尚未选中任何邮件
let st = newStore(page, null, false);
st.selectNext();
check('未选中时 ↓ 选第一封', st.selectedId === 'a' && st.opened[0] === 'a', 'selectedId=' + st.selectedId);

st = newStore(page, null, false);
st.selectPrev();
check('未选中时 ↑ 也落到第一封', st.selectedId === 'a', 'selectedId=' + st.selectedId);

// 2) 中间移动
st = newStore(page, 'b', false);
st.selectNext();
check('中间 ↓ 到下一封', st.selectedId === 'c', 'selectedId=' + st.selectedId);
st.selectPrev();
check('中间 ↑ 回上一封', st.selectedId === 'b', 'selectedId=' + st.selectedId);

// 3) 顶部边界不循环
st = newStore(page, 'a', false);
st.selectPrev();
check('首封 ↑ 保持不动（不循环）', st.selectedId === 'a', 'selectedId=' + st.selectedId);

// 4) 末尾无下一页
st = newStore(page, 'c', false);
st.selectNext();
check('末封 ↓ 且无下一页时不动', st.selectedId === 'c' && st.loadMoreCalls === 0, 'loadMore=' + st.loadMoreCalls);

// 5) 末尾有下一页：先加载再继续下移
st = newStore(page, 'c', true);
st.selectNext();
setTimeout(() => {
  check('末封 ↓ 触发 loadMore', st.loadMoreCalls === 1, 'loadMore=' + st.loadMoreCalls);
  check('加载后继续移到新页第一封', st.selectedId === 'p2a', 'selectedId=' + st.selectedId);

  // 6) 滚动进视口
  scrolled = [];
  const st2 = newStore(page, 'a', false);
  st2.selectNext();
  check('跳转后把选中行滚进视口(block:nearest)',
    scrolled.length === 1 && scrolled[0][0] === 'b' && scrolled[0][1] === 'nearest',
    JSON.stringify(scrolled));

  // 7) 空列表不炸
  const st3 = newStore([], null, false);
  let threw = false;
  try { st3.selectNext(); st3.selectPrev(); } catch (err) { threw = true; }
  check('空列表上下键不抛异常', !threw && st3.selectedId === null);

  // 8) 按键标识映射与注册存在性（确认 ↑/↓ 真的被注册，且 e.key 能映射到注册名）
  const scSrc = fs.readFileSync(path.join(__dirname, 'frontend', 'js', 'utils', 'shortcuts.js'), 'utf8');
  const gk = /function getKeyId\(e\) \{[\s\S]*?\n\}/.exec(scSrc);
  check('shortcuts.js 中能定位 getKeyId', !!gk);
  if (gk) {
    const getKeyId = new Function('return ' + gk[0].replace(/^function /, 'function ') + '; ')();
    const idDown = getKeyId({ key: 'ArrowDown' });
    const idUp = getKeyId({ key: 'ArrowUp' });
    check('ArrowDown 映射为 arrowdown', idDown === 'arrowdown', idDown);
    check('ArrowUp 映射为 arrowup', idUp === 'arrowup', idUp);
    check('两个键都已注册', scSrc.includes("registerShortcut('" + idDown + "'") && scSrc.includes("registerShortcut('" + idUp + "'"));
  }
  check('帮助弹窗打开时不抢按键(helpOpen 已进互斥判定)', scSrc.includes('ui.helpOpen'));

  console.log('\n结果：' + (failed.length === 0 ? '全部通过' : '失败 ' + failed.length + ' 项 -> ' + failed.join(', ')));
  process.exit(failed.length === 0 ? 0 : 1);
}, 20);
