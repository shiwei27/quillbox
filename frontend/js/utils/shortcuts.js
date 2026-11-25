/**
 * 键盘快捷键管理器
 * Superhuman风格的键盘优先交互
 */

/**
 * 快捷键注册表
 * @type {Map<string, {handler: Function, description: string, when: string}>}
 */
const shortcuts = new Map();

/**
 * 检测是否在输入框内
 */
function isInputFocused() {
  const el = document.activeElement;
  if (!el) return false;
  const tag = el.tagName.toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable;
}

/**
 * 生成快捷键标识符
 */
function getKeyId(e) {
  const parts = [];
  if (e.ctrlKey || e.metaKey) parts.push('mod');
  if (e.altKey) parts.push('alt');
  if (e.shiftKey) parts.push('shift');
  parts.push(e.key.toLowerCase());
  return parts.join('+');
}

/**
 * 注册快捷键
 * @param {string} key - 快捷键 (如 'j', 'mod+k', 'shift+r')
 * @param {Function} handler - 处理函数
 * @param {object} options - 配置
 * @param {string} options.description - 描述
 * @param {string} options.when - 生效条件: 'global' | 'list' | 'detail'
 * @param {boolean} options.allowInInput - 是否在输入框内也生效
 */
export function registerShortcut(key, handler, options = {}) {
  shortcuts.set(key, {
    handler,
    description: options.description || '',
    when: options.when || 'global',
    allowInInput: options.allowInInput || false,
  });
}

/**
 * 取消注册快捷键
 */
export function unregisterShortcut(key) {
  shortcuts.delete(key);
}

/**
 * 获取所有快捷键列表（用于命令面板）
 */
export function getShortcutList() {
  const list = [];
  shortcuts.forEach((value, key) => {
    list.push({ key, ...value });
  });
  return list;
}

/**
 * 检测当前上下文是否满足快捷键的when条件
 * @param {string} when - 'global' | 'list' | 'detail'
 * @returns {boolean}
 */
function checkWhenCondition(when) {
  if (when === 'global') return true;

  // 如果有弹窗打开（compose/settings/command palette），只允许global快捷键
  try {
    const ui = Alpine.store('ui');
    if (ui.composeOpen || ui.settingsOpen || ui.commandPaletteOpen || ui.helpOpen) return false;
  } catch { /* Alpine未初始化 */ }

  if (when === 'list') {
    // 邮件列表上下文：有邮件列表可见
    try {
      const mail = Alpine.store('mail');
      return mail.view !== 'todos';
    } catch { return true; }
  }

  if (when === 'detail') {
    // 邮件详情上下文：有已选中的邮件
    try {
      const mail = Alpine.store('mail');
      return !!mail.selectedId && !!mail.currentEmail;
    } catch { return false; }
  }

  return true;
}

/**
 * 初始化键盘监听
 * 在 Alpine 初始化时调用
 */
export function initShortcuts() {
  document.addEventListener('keydown', (e) => {
    const keyId = getKeyId(e);
    const shortcut = shortcuts.get(keyId);

    if (!shortcut) return;
    if (!shortcut.allowInInput && isInputFocused()) return;
    if (!checkWhenCondition(shortcut.when)) return;

    e.preventDefault();
    e.stopPropagation();
    shortcut.handler(e);
  });
}

/**
 * 注册默认邮件快捷键
 * 需要传入Alpine store的访问方式
 */
export function registerDefaultShortcuts(getStore) {
  // j - 下一封邮件
  registerShortcut('j', () => {
    const mail = getStore('mail');
    mail.selectNext();
  }, { description: '下一封邮件', when: 'list' });

  // k - 上一封邮件
  registerShortcut('k', () => {
    const mail = getStore('mail');
    mail.selectPrev();
  }, { description: '上一封邮件', when: 'list' });

  // ArrowDown / ArrowUp - 上下切换邮件（三栏阅读窗布局，选中即打开；与 j/k 等价）
  registerShortcut('arrowdown', () => {
    const mail = getStore('mail');
    mail.selectNext();
  }, { description: '下一封邮件', when: 'list' });

  registerShortcut('arrowup', () => {
    const mail = getStore('mail');
    mail.selectPrev();
  }, { description: '上一封邮件', when: 'list' });

  // Enter - 打开邮件
  registerShortcut('enter', () => {
    const mail = getStore('mail');
    if (mail.selectedId) {
      mail.openEmail(mail.selectedId);
    }
  }, { description: '打开邮件', when: 'list' });

  // r - 回复
  registerShortcut('r', () => {
    const ui = getStore('ui');
    ui.openCompose('reply');
  }, { description: '回复邮件', when: 'detail' });

  // a - 回复全部
  registerShortcut('a', () => {
    const ui = getStore('ui');
    ui.openCompose('reply-all');
  }, { description: '回复全部', when: 'detail' });

  // s - 星标切换
  registerShortcut('s', () => {
    const mail = getStore('mail');
    mail.toggleStar();
  }, { description: '切换星标', when: 'list' });

  // / - 聚焦搜索
  registerShortcut('/', () => {
    const searchInput = document.querySelector('#search-input');
    if (searchInput) searchInput.focus();
  }, { description: '搜索', when: 'global' });

  // Cmd+K - 命令面板
  registerShortcut('mod+k', () => {
    const ui = getStore('ui');
    ui.toggleCommandPalette();
  }, { description: '命令面板', when: 'global', allowInInput: true });

  // Escape - 关闭面板/返回
  registerShortcut('escape', () => {
    const ui = getStore('ui');
    if (ui.commandPaletteOpen) {
      ui.commandPaletteOpen = false;
    } else if (ui.aiPanelOpen) {
      ui.aiPanelOpen = false;
    } else if (ui.composeOpen) {
      ui.composeOpen = false;
    } else if (ui.settingsOpen) {
      ui.settingsOpen = false;
    }
  }, { description: '关闭/返回', when: 'global', allowInInput: true });

  // e - 归档/删除
  registerShortcut('e', () => {
    const mail = getStore('mail');
    if (!confirm('确定删除这封邮件？')) return;
    mail.deleteSelected();
  }, { description: '删除邮件', when: 'list' });

  // u - 标记未读/已读
  registerShortcut('u', () => {
    const mail = getStore('mail');
    mail.toggleUnread();
  }, { description: '切换已读状态', when: 'list' });

  // c - 新邮件
  registerShortcut('c', () => {
    const ui = getStore('ui');
    ui.openCompose('new');
  }, { description: '写新邮件', when: 'global' });

  // g+i - 转到收件箱 (简化为 i)
  registerShortcut('i', () => {
    const mail = getStore('mail');
    mail.setView('inbox');
  }, { description: '转到收件箱', when: 'global' });
}
