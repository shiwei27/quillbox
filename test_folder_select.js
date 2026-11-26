/**
 * 「移动到」下拉的运行时校验：方法形式的 allFolderNames() 能返回去重列表。
 * 用法：node test_folder_select.js
 */
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(
  path.join(__dirname, 'frontend', 'js', 'stores', 'mail.js'), 'utf8');

const s = src.indexOf('allFolderNames() {');
const e = src.indexOf('folderOptions()', s);
if (s < 0 || e < 0) throw new Error('定位失败');
const defs = src.slice(s, e).trim().replace(/,\s*$/, '');
const store = new Function('return {' + defs + '};')();

const failed = [];
function check(name, cond, extra) {
  console.log((cond ? '  [PASS] ' : '  [FAIL] ') + name +
              (cond ? '' : ' -> ' + extra));
  if (!cond) failed.push(name);
}

console.log('=== 方法形式（与既有 folderOptions() 一致）===');
check('是函数而非 getter', typeof store.allFolderNames === 'function',
      typeof store.allFolderNames);

store.accounts = [
  { name: 'a', folders: ['收件箱', '告警通知', '已删除邮件'] },
  { name: 'b', folders: ['告警通知', '存档'] },
];
store.globalFolders = ['全局夹', '', null];
const names = store.allFolderNames();
check('跨账户去重 + 过滤空值',
      JSON.stringify(names) ===
      JSON.stringify(['收件箱', '告警通知', '已删除邮件', '存档', '全局夹']),
      JSON.stringify(names));
check('无重复项', new Set(names).size === names.length);
check('无空串（x-for 的 :key 不能重复/为空）',
      names.every(n => typeof n === 'string' && n.length > 0));

store.accounts = undefined;
store.globalFolders = undefined;
check('未初始化时返回空数组',
      Array.isArray(store.allFolderNames()) &&
      store.allFolderNames().length === 0);

store.accounts = [{ name: 'a' }];   // 账户没有 folders 字段
check('账户缺 folders 字段不炸', store.allFolderNames().length === 0);

console.log('');
if (failed.length) {
  console.log('RESULT FAILED (' + failed.length + '): ' + failed.join(', '));
  process.exit(1);
}
console.log('RESULT ALL_PASS');
