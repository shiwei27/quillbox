/**
 * 收件人 / 抄送显示逻辑自检（Node，无浏览器、不联网）。
 *
 * 钉死一个已修的缺陷：后端一直返回 recipients（type 区分 to/cc），
 * 但 api.js::mapEmail 没派生 to/cc，index.html 又读了不存在的 email.to，
 * 于是详情页恒显示「收件人: 我」、抄送完全不显示。
 *
 * 用法：node test_recipients.js
 */
const fs = require('fs');
const path = require('path');

function sliceOf(src, startMark, endMark, label) {
  const s = src.indexOf(startMark);
  const e = src.indexOf(endMark, s);
  if (s < 0 || e < 0) throw new Error('定位失败: ' + label);
  return src.slice(s, e);
}

// ── 从 api.js 取出 _addrList / _addrText ──
const apiSrc = fs.readFileSync(
  path.join(__dirname, 'frontend', 'js', 'api.js'), 'utf8');
const addrDefs = sliceOf(apiSrc, 'function _addrList', 'function mapEmail',
                         'api.js _addrList');
const { _addrList, _addrText } = new Function(
  addrDefs + '\nreturn { _addrList, _addrText };')();

// ── 从 emailDetail.js 取出收件人相关成员 ──
const detailSrc = fs.readFileSync(
  path.join(__dirname, 'frontend', 'js', 'components', 'emailDetail.js'),
  'utf8');
let members = sliceOf(detailSrc, 'get toList()', 'get loading()',
                      'emailDetail.js 收件人成员');
members = members.trim().replace(/,\s*$/, '');
const makeCmp = () => new Function('return {' + members + '};')();

const failed = [];
function check(name, cond, extra) {
  console.log((cond ? '  [PASS] ' : '  [FAIL] ') + name +
              (extra !== undefined && !cond ? ' -> ' + extra : ''));
  if (!cond) failed.push(name);
}

// 真实形态样本（取自库里 recipients 实际存储结构）
const REAL = [
  { name: "'陈晨14/CORP-A'", email: 'frank@example.com', type: 'to' },
  { name: '', email: 'dave@example.com', type: 'cc' },
  { name: '', email: 'alice@example.com', type: 'cc' },
  { name: '', email: 'grace@example.com', type: 'cc' },
];

console.log('=== 1. to / cc 拆分（缺陷主体：抄送此前完全拿不到）===');
const to = _addrList(REAL, 'to');
const cc = _addrList(REAL, 'cc');
check('to 取到 1 个', to.length === 1, to.length);
check('cc 取到 3 个', cc.length === 3, cc.length);
check('to 不含 cc 地址',
  !to.some(a => a.email === 'dave@example.com'));

console.log('=== 2. Exchange 名字外层引号被剥掉 ===');
check('引号已剥离', to[0].name === '陈晨14/CORP-A', to[0].name);
check('邮箱保留', to[0].email === 'frank@example.com');

console.log('=== 3. 去重（同一地址重复出现只保留一次）===');
const dup = _addrList([
  { name: 'A', email: 'x@y.com', type: 'to' },
  { name: '', email: 'X@Y.com', type: 'to' },
  { name: '', email: 'z@y.com', type: 'to' },
], 'to');
check('大小写不同的同一邮箱只留一个', dup.length === 2, dup.length);

console.log('=== 4. 空值与缺省 ===');
check('recipients 为 null -> []', _addrList(null, 'to').length === 0);
check('recipients 非数组 -> []', _addrList('oops', 'to').length === 0);
check('缺 type 默认算 to',
  _addrList([{ email: 'a@b.com' }], 'to').length === 1);
check('type 大写也能匹配',
  _addrList([{ email: 'a@b.com', type: 'CC' }], 'cc').length === 1);
check('name/email 全空的条目被丢弃',
  _addrList([{ name: '', email: '' }], 'to').length === 0);

console.log('=== 5. 展示串 ===');
check('_addrText 用顿号连接',
  _addrText([{ name: '张三', email: 'a@b' }, { name: '', email: 'c@d' }])
    === '张三、c@d');
check('无名字回退邮箱',
  _addrText([{ name: '', email: 'only@mail' }]) === 'only@mail');

console.log('=== 6. 详情页：自己显示为「我」===');
const cmp = makeCmp();
cmp.email = {
  account: 'alice@example.com',
  to_list: _addrList(REAL, 'to'),
  cc_list: _addrList(REAL, 'cc'),
};
check('自己的地址显示为 我',
  cmp.displayAddr({ name: '', email: 'alice@example.com' }) === '我');
check('大小写不敏感',
  cmp.displayAddr({ name: '', email: 'ALICE@example.com' }) === '我');
check('他人显示名字',
  cmp.displayAddr({ name: '张三', email: 'z@h.com' }) === '张三');
check('他人无名字显示邮箱',
  cmp.displayAddr({ name: '', email: 'z@h.com' }) === 'z@h.com');

console.log('=== 7. 折叠摘要 ===');
check('hasRecipientInfo 为真', cmp.hasRecipientInfo === true);
check('4 人 -> 前两位 + 等 4 人',
  cmp.recipientSummary === '陈晨14/CORP-A、dave@example.com 等 4 人',
  cmp.recipientSummary);

const two = makeCmp();
two.email = {
  account: 'me@x.com',
  to_list: [{ name: '张三', email: 'a@x.com' }, { name: '', email: 'me@x.com' }],
  cc_list: [],
};
check('2 人时不加「等 N 人」', two.recipientSummary === '张三、我',
  two.recipientSummary);

console.log('=== 8. 老邮件没有 recipients：必须报「未记录」而不是假的「我」===');
const empty = makeCmp();
empty.email = { account: 'me@x.com', to_list: [], cc_list: [] };
check('hasRecipientInfo 为假', empty.hasRecipientInfo === false);
check('摘要为空串（UI 走未记录分支）', empty.recipientSummary === '');

const noField = makeCmp();
noField.email = { account: 'me@x.com' };   // 完全没有 to_list/cc_list 字段
check('缺字段不抛异常', noField.hasRecipientInfo === false);

console.log('=== 9. 只有抄送、没有收件人 ===');
const ccOnly = makeCmp();
ccOnly.email = {
  account: 'me@x.com', to_list: [],
  cc_list: [{ name: '李四', email: 'l@x.com' }],
};
check('仍判定有收件人信息', ccOnly.hasRecipientInfo === true);
check('摘要取到抄送人', ccOnly.recipientSummary === '李四',
  ccOnly.recipientSummary);

console.log('=== 10. 折叠开关 ===');
const t = makeCmp();
t.recipientsOpen = false;
t.toggleRecipients();
check('toggle 后为 true', t.recipientsOpen === true);
t.toggleRecipients();
check('再 toggle 回 false', t.recipientsOpen === false);

console.log('');
if (failed.length) {
  console.log('RESULT FAILED (' + failed.length + '): ' + failed.join(', '));
  process.exit(1);
}
console.log('RESULT ALL_PASS');
