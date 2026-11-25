/**
 * Quillbox - 主应用入口
 * Alpine.js Store 注册 + 组件注册 + 快捷键初始化
 *
 * 注意：type="module" 脚本与 Alpine defer 脚本的执行顺序不保证。
 * 因此使用轮询等待 Alpine 全局对象可用，再注册 stores/components。
 * Alpine CDN 版本在 DOMContentLoaded 后自动调用 Alpine.start()，
 * 但如果在 start() 之前 stores 已注册就没问题。
 * 为确保万无一失，我们同时监听 alpine:init 和在 Alpine 可用时直接注册。
 */

// Stores
import uiStore from './stores/ui.js';
import mailStore from './stores/mail.js';
import aiStore from './stores/ai.js';
import settingsStore from './stores/settings.js';

// Components
import emailList from './components/emailList.js';
import emailDetail from './components/emailDetail.js';
import sidebar from './components/sidebar.js';
import aiChat from './components/aiChat.js';
import compose from './components/compose.js';
import settingsPanel from './components/settingsPanel.js';
import commandPalette from './components/commandPalette.js';
import searchBar from './components/searchBar.js';
import topBar from './components/topBar.js';

// Utils
import { initShortcuts, registerDefaultShortcuts } from './utils/shortcuts.js';

let registered = false;

function registerAll() {
  if (registered) return;
  if (typeof Alpine === 'undefined') return;
  registered = true;
  window.__quillboxModuleLoaded = true;

  // 注册全局 Store
  Alpine.store('ui', uiStore());
  Alpine.store('mail', mailStore());
  Alpine.store('ai', aiStore());
  Alpine.store('settings', settingsStore());

  // 注册组件
  Alpine.data('emailList', emailList);
  Alpine.data('emailDetail', emailDetail);
  Alpine.data('sidebar', sidebar);
  Alpine.data('aiChat', aiChat);
  Alpine.data('compose', compose);
  Alpine.data('settingsPanel', settingsPanel);
  Alpine.data('commandPalette', commandPalette);
  Alpine.data('searchBar', searchBar);
  Alpine.data('topBar', topBar);

  console.log('[Quillbox] Stores and components registered.');
}

// 方式1：监听 alpine:init 事件（如果 Alpine 还没初始化）
document.addEventListener('alpine:init', () => {
  registerAll();
});

// 方式2：如果 Alpine 已经加载（module 在 Alpine 之后执行的情况）
// 轮询等待 Alpine 对象存在，然后注册
(function waitForAlpine() {
  if (typeof Alpine !== 'undefined') {
    registerAll();
    // 如果 Alpine 已经 start 了但 stores 没注册，需要重新初始化
    // 这种情况下页面上的 x-data 等指令已经被处理但 store 为空
    // 此时需要告诉用户刷新，或者尝试重新 start
    return;
  }
  // 每 5ms 检查一次，最多等 3 秒
  let attempts = 0;
  const timer = setInterval(() => {
    attempts++;
    if (typeof Alpine !== 'undefined') {
      clearInterval(timer);
      registerAll();
    } else if (attempts > 600) {
      clearInterval(timer);
      console.error('[Quillbox] Alpine.js failed to load after 3s');
    }
  }, 5);
})();

// ============ 键盘快捷键 ============
document.addEventListener('DOMContentLoaded', () => {
  setTimeout(() => {
    if (typeof Alpine !== 'undefined') {
      const getStore = (name) => Alpine.store(name);
      registerDefaultShortcuts(getStore);
      initShortcuts();
    }
  }, 200);
});
