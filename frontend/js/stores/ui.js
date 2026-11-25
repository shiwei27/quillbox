/**
 * UI 状态管理
 * 管理面板开关、主题、侧边栏等UI状态
 */

import * as api from '../api.js';

export default function uiStore() {
  return {
    // 侧边栏
    sidebarCollapsed: false,
    sidebarWidth: 180,        // 默认宽度(px)，可拖拽调整
    _sidebarResizing: false,  // 拖拽进行中标志

    // 邮件列表栏宽度（原先由 tailwind 的 w-list 写死 340px，不可调）
    listWidth: 340,
    _listResizing: false,

    // AI面板（呼出式浮层，默认收起；用 x-show 切换保证可靠收回）
    aiPanelOpen: false,

    // 命令面板
    commandPaletteOpen: false,
    commandQuery: '',
    omniResults: [],
    omniLoading: false,
    _omniTimer: null,

    // 写邮件面板
    composeOpen: false,
    composeMode: 'new', // 'new' | 'reply' | 'forward'

    // 设置面板
    settingsOpen: false,
    settingsTab: 'model',

    // 帮助
    helpOpen: false,

    // 主题: 'light' | 'dark' | 'system'
    themePreference: 'system',
    isDark: false,

    // 同步状态
    syncRunning: false,
    syncProgress: 0,

    // 通知
    notifications: [],

    init() {
      // 版本检测：页面长时间不刷新会一直用旧代码
      try { this.startVersionWatch(); } catch (e) {}
      // 读取侧边栏宽度偏好
      const savedWidth = localStorage.getItem('quillbox-sidebar-width');
      if (savedWidth) {
        this.sidebarWidth = Math.max(120, Math.min(320, parseInt(savedWidth, 10)));
      }

      // 读取列表栏宽度偏好（超出当前窗口能容纳的范围时会在这里被夹回来，
      // 换到小屏幕上不会出现详情栏被挤没的情况）
      const savedListWidth = parseInt(localStorage.getItem('quillbox-list-width'), 10);
      if (savedListWidth) {
        this.listWidth = this.clampListWidth(savedListWidth);
      }

      // 读取主题偏好
      const saved = localStorage.getItem('quillbox-theme');
      if (saved) {
        this.themePreference = saved;
      }
      this.applyTheme();

      // 监听系统主题变化
      window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
        if (this.themePreference === 'system') {
          this.applyTheme();
        }
      });
    },

    // 切换主题
    toggleTheme() {
      if (this.isDark) {
        this.themePreference = 'light';
      } else {
        this.themePreference = 'dark';
      }
      localStorage.setItem('quillbox-theme', this.themePreference);
      this.applyTheme();
    },

    // 设置主题
    setTheme(theme) {
      this.themePreference = theme;
      localStorage.setItem('quillbox-theme', theme);
      this.applyTheme();
    },

    // 打开设置并定位到指定分类（供侧栏“邮件规则”直达）
    openSettings(tab) {
      if (tab) this.settingsTab = tab;
      this.settingsOpen = true;
    },

    // 应用主题到DOM
    applyTheme() {
      if (this.themePreference === 'system') {
        this.isDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
      } else {
        this.isDark = this.themePreference === 'dark';
      }

      if (this.isDark) {
        document.documentElement.classList.add('dark');
      } else {
        document.documentElement.classList.remove('dark');
      }
    },

    // ---- 新版本检测 ----
    // 我在持续改前端，而用户在 RDP 里长时间开着页面，无从判断自己在哪个版本上
    // （真的踩到过：工具栏是新的、收件人那几行还是旧的，因为标签页是在两次改动
    // 之间加载的）。/app 虽然带 no-store，但那只在重新发起请求时起作用。
    appVersion: '',
    newVersion: '',

    // 从自己那个 script 标签的 src 里读版本，不需要额外注入全局变量
    _ownVersion() {
      try {
        const el = document.querySelector('script[src*="bundle.js"]');
        const m = (el && el.getAttribute('src') || '').match(/[?&]v=([\w.\-]+)/);
        return m ? m[1] : '';
      } catch (e) { return ''; }
    },

    startVersionWatch() {
      // 幂等：Alpine.store() 注册时会自动调用一次 init()，index.html 的 x-init 又手动
      // 调了一次，于是每个页面挂了两个定时器、每分钟查两次。
      if (this._versionWatchStarted) return;
      this._versionWatchStarted = true;
      this.appVersion = this._ownVersion();
      if (!this.appVersion) return;          // 取不到就不做，不误报
      const check = async () => {
        try {
          const r = await fetch('/api/version', { cache: 'no-store' });
          const j = await r.json();
          const v = (j && j.version) || '';
          // 只在确实拿到且不同时提示，避免网络抖动误报
          if (v && v !== this.appVersion) this.newVersion = v;
        } catch (e) { /* 网络抖动不提示 */ }
      };
      check();
      // 后台标签页不轮询：浏览器常开着多个 Quillbox 页面，绝大多数看不见
      // （每分钟会白查十几次）。切回前台立刻补查一次，所以新版本提示不会因此变晚。
      setInterval(() => { if (document.visibilityState !== 'hidden') check(); }, 60000);
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') check();
      });
    },

    reloadForNewVersion() {
      // 带上时间戳绕过任何中间缓存，比 location.reload() 更确定
      const u = new URL(window.location.href);
      u.searchParams.set('_r', Date.now());
      window.location.replace(u.toString());
    },

    // 打开写邮件
    openCompose(mode = 'new') {
      this.composeMode = mode;
      this.composeOpen = true;
    },

    // 侧边栏拖拽调整宽度
    startSidebarResize(e) {
      this._sidebarResizing = true;
      const startX = e.clientX;
      const startWidth = this.sidebarWidth;
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';

      const onMouseMove = (ev) => {
        const delta = ev.clientX - startX;
        this.sidebarWidth = Math.max(120, Math.min(320, startWidth + delta));
      };

      const onMouseUp = () => {
        this._sidebarResizing = false;
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);
        localStorage.setItem('quillbox-sidebar-width', String(this.sidebarWidth));
      };

      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
    },

    // 列表栏宽度夹取：上限跟着窗口走，保证详情栏至少留 480px
    clampListWidth(w) {
      const maxW = Math.max(260, Math.min(720, window.innerWidth - 480));
      return Math.max(260, Math.min(maxW, parseInt(w, 10) || 340));
    },

    // 列表栏与详情栏之间的拖拉调整
    startListResize(e) {
      this._listResizing = true;
      const startX = e.clientX;
      const startWidth = this.listWidth;
      document.body.style.cursor = 'col-resize';
      // 不锁选区的话，横向拖动会把邮件正文一路选中
      document.body.style.userSelect = 'none';

      const onMouseMove = (ev) => {
        this.listWidth = this.clampListWidth(startWidth + (ev.clientX - startX));
      };

      const onMouseUp = () => {
        this._listResizing = false;
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);
        localStorage.setItem('quillbox-list-width', String(this.listWidth));
      };

      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
    },

    // 双击手柄复位。可拖范围大，拖歪了这样最省事。
    resetListWidth() {
      this.listWidth = 340;
      localStorage.setItem('quillbox-list-width', String(this.listWidth));
    },

    // 切换命令面板
    toggleCommandPalette() {
      this.commandPaletteOpen = !this.commandPaletteOpen;
      this.commandQuery = '';
    },

    // 切换AI面板
    toggleAIPanel() {
      this.aiPanelOpen = !this.aiPanelOpen;
    },

    // 显示通知
    notify(message, type = 'info', duration = 3000) {
      const id = Date.now();
      this.notifications.push({ id, message, type });
      setTimeout(() => {
        this.notifications = this.notifications.filter(n => n.id !== id);
      }, duration);
    },

    // 命令面板操作列表
    get commands() {
      return [
        { id: 'inbox', label: '转到收件箱', icon: 'inbox', shortcut: 'I', action: () => { Alpine.store('mail').setView('inbox'); this.commandPaletteOpen = false; } },
        { id: 'starred', label: '转到星标', icon: 'star', shortcut: '', action: () => { Alpine.store('mail').setView('starred'); this.commandPaletteOpen = false; } },
        { id: 'compose', label: '写新邮件', icon: 'edit-3', shortcut: 'C', action: () => { this.openCompose('new'); this.commandPaletteOpen = false; } },
        { id: 'sync', label: '同步邮件', icon: 'refresh-cw', shortcut: '', action: () => { Alpine.store('mail').sync(); this.commandPaletteOpen = false; } },
        { id: 'ai', label: '打开AI助手', icon: 'sparkles', shortcut: '', action: () => { this.aiPanelOpen = true; this.commandPaletteOpen = false; } },
        { id: 'settings', label: '打开设置', icon: 'settings', shortcut: '', action: () => { this.settingsOpen = true; this.commandPaletteOpen = false; } },
        { id: 'theme', label: '切换暗色模式', icon: 'moon', shortcut: '', action: () => { this.toggleTheme(); this.commandPaletteOpen = false; } },
        { id: 'todos', label: '查看待办', icon: 'check-square', shortcut: '', action: () => { Alpine.store('mail').setView('todos'); this.commandPaletteOpen = false; } },
      ];
    },

    // 统一 omnibox：问 AI + 搜邮件 + 邮件命中 + 命令
    get filteredCommands() {
      const q = (this.commandQuery || '').trim();
      if (!q) return this.commands;
      const ql = q.toLowerCase();
      const items = [];
      items.push({ kind: 'search', id: '__search', icon: 'search', shortcut: '↵', label: '搜索邮件：“' + q + '”', action: () => this.doSearch(q) });
      items.push({ kind: 'ai', id: '__ai', icon: 'sparkles', label: '用 AI 回答：“' + q + '”', action: () => this.askAI(q) });
      for (const e of (this.omniResults || [])) {
        items.push({
          kind: 'email', id: '__mail_' + e.id, icon: 'mail',
          label: e.subject || '(无主题)',
          sub: ((e.from_name || e.sender || '') + (e.date ? ' · ' + String(e.date).slice(5, 16) : '')),
          action: () => this.openOmniEmail(e.id),
        });
      }
      for (const c of this.commands) {
        if (c.label.toLowerCase().includes(ql) || c.id.includes(ql)) items.push(c);
      }
      return items;
    },
    openOmni() { this.commandQuery = ''; this.omniResults = []; this.commandPaletteOpen = true; },
    runOmni() {
      const q = (this.commandQuery || '').trim();
      if (this._omniTimer) clearTimeout(this._omniTimer);
      if (!q) { this.omniResults = []; this.omniLoading = false; return; }
      this.omniLoading = true;
      this._omniTimer = setTimeout(async () => {
        try {
          const data = await api.getEmails({ q, view: 'inbox', pageSize: 6 });
          if ((this.commandQuery || '').trim() === q) this.omniResults = (data && data.emails) || [];
        } catch (e) { this.omniResults = []; }
        finally { this.omniLoading = false; }
      }, 250);
    },
    askAI(q) {
      q = (q || this.commandQuery || '').trim();
      if (!q) return;
      this.commandPaletteOpen = false;
      this.aiPanelOpen = true;
      try {
        const ai = Alpine.store('ai');
        ai.chatMode = 'global';
        ai.chatInput = q;
        ai.sendMessage();
      } catch (e) {}
    },
    doSearch(q) {
      q = (q || this.commandQuery || '').trim();
      this.commandPaletteOpen = false;
      const mail = Alpine.store('mail');
      if (mail.view !== 'inbox') mail.view = 'inbox';
      mail.search(q);
    },
    openOmniEmail(id) {
      this.commandPaletteOpen = false;
      const mail = Alpine.store('mail');
      if (mail.view !== 'inbox') mail.setView('inbox');
      mail.openEmail(id);
    },
  };
}
