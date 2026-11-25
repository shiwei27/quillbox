/**
 * Quillbox 新界面（/app2）交互层。
 *
 * 原则：只新增一个 Alpine store「v2」承载新界面独有的状态（账户切换、勾选、命令面板、
 * 写信窗、Toast、键盘）；邮件/待办/AI/设置的数据与请求全部复用 bundle.js 里既有的
 * mail / ai / ui / settings store，不复制业务逻辑。旧 /app 不加载本文件。
 *
 * 加载顺序（app2.html）：window.__QUILLBOX_V2=true → bundle.js → 本文件 → Alpine。
 * __QUILLBOX_V2 让 bundle 跳过旧版默认快捷键（旧版 E=确认后删除，与新版 E=归档冲突）。
 */
(function () {
  'use strict';

  // ---------- 登录失效检测 ----------
  // 远程访问经令牌门禁，令牌更换 / 过期后所有请求回 401，但各处只抛「HTTP 401」，旧页面一直报错却不提示重新登录。
  // 包一层 window.fetch：同源请求（/auth/ 除外）返回 401 → 置 window.__QUILLBOX_AUTH_LOST 并发 quillbox:auth-lost 事件。
  // 只提示、不自动刷新。loopback 免认证，本机访问永远不会触发。EventSource 不经 fetch，靠其他请求带出。
  (function () {
    const orig = window.fetch;
    if (!orig || orig.__quillboxAuthWrapped) return;
    const wrapped = async function (input) {
      const r = await orig.apply(this, arguments);
      try {
        if (r && r.status === 401) {
          const raw = typeof input === 'string' ? input : (input instanceof URL ? input.href : (input && input.url) || '');
          const u = new URL(raw, location.href);
          if (u.origin === location.origin && !u.pathname.startsWith('/auth/')) {
            window.__QUILLBOX_AUTH_LOST = true;
            window.dispatchEvent(new CustomEvent('quillbox:auth-lost', { detail: { path: u.pathname } }));
          }
        }
      } catch (e) { /* 检测失败不影响原请求 */ }
      return r;
    };
    wrapped.__quillboxAuthWrapped = true;
    window.fetch = wrapped;
  })();

  // ---------- 小工具 ----------
  async function req(path, opt) {
    const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opt || {}));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.status === 204 ? null : r.json();
  }
  const S = (name) => Alpine.store(name);
  let _mnCtx = null, _mnSkip = false;   // 正文 @ 提及：当前触发位置（含 DOM 节点，放模块级，避免被 Alpine 代理）
  const pad = (n) => String(n).padStart(2, '0');
  function parseT(t) {
    if (!t) return null;
    const d = new Date(String(t).replace(' ', 'T'));
    return isNaN(d) ? null : d;
  }
  function escHtml(s) {
    return String(s || '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function plainToHtml(s) { return escHtml(s).replace(/\r?\n/g, '<br>'); }
  function stripMd(s) {
    return String(s || '').replace(/^\s*#{1,6}\s*/gm, '').replace(/\*\*(.+?)\*\*/g, '$1').replace(/`([^`]+)`/g, '$1');
  }
  const SYSTEM_SENDER = /(no-?reply|noreply|notice|notify|system|alert|alarm|monitor|mailer-daemon|postmaster|newsletter|meeting@)/i;
  const TONES = [
    { tone: '确认收到', text: '简短确认已收到，说明会尽快处理' },
    { tone: '婉拒 / 延后', text: '礼貌说明暂时无法处理或需要延后' },
    { tone: '需要更多信息', text: '请对方补充关键信息后再推进' },
  ];
  const AI_KIND = { '草拟': '简洁专业', '更正式': '正式', '更简短': '简短', '译为英文': '英文' };

  function v2Store() {
    return {
      acc: '',                 // '' = 全部账户
      view: 'triage',          // triage(今日分拣，默认入口) inbox topics todos starred sent drafts archive trash folder
      // 今日分拣：data = /api/triage 原样；by = id→行（与 mail.emails 共用同一对象，已读/星标改动两边一致）；
      // gone = 本次已处理（归档）掉的；open = 分组展开；openClusters = 展开的告警组；aq = 后台分析队列（并发 4、单封每会话一次）
      tri: { data: null, by: {}, gone: {}, loading: false, error: '', okAt: 0, done: 0,
             open: { act: true, reply: true, watch: true, notice: true, other: false, pending: true }, openClusters: {}, mutedOpen: false,
             aq: { queue: [], running: 0, tried: {}, ok: 0, failed: 0 } },
      scope: null,             // { kind: search|folder|thread|topic, label }
      readerMode: localStorage.getItem('quillbox-v2-reader') === 'original' ? 'original' : 'clean',   // clean | original；用户选过就记住
      aiPaneOpen: false,
      winW: window.innerWidth,
      density: localStorage.getItem('quillbox-v2-density') || 'comfortable',
      panel: '',               // folders | more | contacts | help | chat
      toast: null, _tt: null,
      // 最近操作（撤销栈）：本会话保留 30 条，Z 逐条撤销最上面一条未撤销的；提示条只显示最新一条，旧的仍可在面板里撤销
      ops: [], _opSeq: 0,
      // 高亮 ≠ 打开：J/K 只移动高亮（selectedId），Enter / 点击才打开（currentEmail）
      _hlId: null,
      readOnOpen: localStorage.getItem('quillbox-v2-read-on-open') !== '0',   // 打开即已读（默认开：点击即转状态）
      _readT: null, _asT: null, _asBusy: false,
      // 加入待办弹层（P1-E）
      todoDlg: { open: false, text: '', mode: 'none', pick: '', aiDue: '', aiSrc: '', pri: 'normal', link: true, srcId: '', subject: '' },
      // 上下文区（P1-F）：宽屏在正文下方折叠，窄屏（<1440）为右侧抽屉；数据按需加载
      ctxOpen: false, ctx: { id: null, thread: [], sender: [], loading: false },
      quoteOpen: false,
      snzDlg: { open: false, ids: [], pick: '' },                                   // 稍后提醒
      noteDlg: { open: false, ids: [], edit: null, after: false, outcome: '', atts: [], rm: [], links: [], q: '', results: [], searching: false, busy: false },
      notes: { id: null, list: [], back: [] },                                       // 当前邮件的处理记录
      noteMode: localStorage.getItem('quillbox-v2-note-mode') || 'smart',
      ntf: { mode: localStorage.getItem('quillbox-v2-notify') || 'off', perm: ('Notification' in window) ? Notification.permission : 'unsupported' },
      muteDlg: { open: false, key: '', sender: '', label: '', scope: 'object' },    // 静音告警
      batchSumOpen: false,
      todoDraft: '',
      folders: [],
      ownDomains: [],
      senders: [], defaultSender: '',
      thread: { id: null, count: 0, subject: '' },
      contactQ: '',
      cmd: { open: false, q: '', idx: 0, mails: [], loading: false, _t: null },
      compose: null,
      cAc: { field: '', list: [], idx: 0 },
      mn: { open: false, list: [], idx: 0, q: '', x: 0, y: 0 },
      insight: { id: null, data: null, loading: false, error: '' },
      settingsTab: 'model', openRule: -1,
      mb: { rules: [], palette: {}, errors: [], saving: false, saved: false },
      smartOpen: false,
      noAct: { open: false, scope: 'same_kind', mode: 'keep', preview: null, loading: false, completeTodos: false, busy: false },
      sideMenu: false, _insRetry: {},
      series: { key: '', n: 0, items: [], loading: false },
      seriesBack: [],
      aiRules: { items: [], loading: false },
      chg: { id: '', sk: '', base: '', tok: '', loading: false, error: '', data: null, menu: false, dlg: false, none: true },   // 「变化」弹窗：id = 本期（正在读的这封），base = 对比基准
      accMenu: false, showChecks: false,
      authLost: false, _authChecking: false, autoSyncing: false,
      qb: { chips: [], text: '', focus: false, idx: 0, ai: false },
      _aiFor: '',

      // ======================= 初始化 =======================
      async init() {
        if (this._inited) return; this._inited = true;   // store init 会被调两次（见 AGENTS.md 约定）
        // 登录失效：出现提示条；切回本页 / 获得焦点时自动复查一次（在别的标签页登录后回来即恢复）
        if (window.__QUILLBOX_AUTH_LOST) this.authLost = true;
        window.addEventListener('quillbox:auth-lost', () => { this.authLost = true; });
        const _authBack = () => { if (this.authLost && document.visibilityState !== 'hidden') this.recheckAuth(false); };
        document.addEventListener('visibilitychange', _authBack);
        window.addEventListener('focus', _authBack);
        // 共享 mail store 的两个开关（经典界面不设，行为不变）：
        //   triageNav —— 摘要跟随「打开的邮件」而不是「高亮的邮件」，J/K 翻过去不会触发 AI；
        //   deferRead —— openEmail 不立即标已读，由下面的停留计时决定。
        S('mail').triageNav = true; S('mail').deferRead = !this.readOnOpen;
        // 今日分拣不是 /api/emails 的一个视图：mail store 里任何地方触发的 fetchEmails（同步完成、撤销后刷新……）
        // 在分拣视图下都改由 loadTriage 提供列表，避免被收件箱分页结果覆盖
        S('mail').listProvider = () => (this.view === 'triage' ? this.loadTriage() : null);
        S('mail').listOwns = () => this.view === 'triage';
        try { const qv = new URLSearchParams(location.search).get('view'); if (qv && this.VIEWS.some((v) => v[0] === qv)) this.view = qv; } catch (e) {}
        if (this.view !== 'inbox') setTimeout(() => this.go(this.view), 0);
        window.addEventListener('resize', () => { this.winW = window.innerWidth; });
        document.addEventListener('keydown', (e) => this.onKey(e));
        this._mnInit();
        this._applyTheme();
        Alpine.effect(() => { void S('ui').isDark; this._applyTheme(); });
        Alpine.effect(() => {
          const id = S('mail').selectedId;
          if (id === this.thread.id) return;
          this.thread = { id, count: 0, subject: '' };
          if (id) this._loadThread(id);
          // fetchEmails 会自动「选中」第一封但不打开。新界面里选中=打开=标为已读，
          // 自动打开会让用户没看过的第一封被悄悄标成已读，所以撤销这种自动选中，
          // 显示「选择一封邮件，或按 J / K 浏览」。用户点击 / J / K 走 gotoEmail→openEmail，
          // 那时 detailLoading 已为 true，不会被这里撤销。
          const m = S('mail');
          if (id && id !== this._hlId && (!m.currentEmail || m.currentEmail.id !== id) && !m.detailLoading) m.selectedId = null;
        });
        Alpine.effect(() => {
          const c = S('mail').currentEmail, id = c && c.id, cp = this.compose;
          if (cp && cp.inline && cp.srcId !== id) { cp.body = cp.body || ''; cp.inline = false; cp.min = true; }
          this.quoteOpen = false;
          if ((this.ctxOpen || !this.ctxDrawer) && id && this.ctx.id !== id) this._loadCtx(id);   // 宽屏右栏常驻：打开就加载
          if (id !== this.notes.id) this._loadNotes(id);
          this._loadSeries(id);
        });
        // 变化：打开系列邮件（且系列各期已加载）就准备入口——与上期的对比已预先算好就取来显示摘要，没算过的等用户点开再调 AI
        Alpine.effect(() => {
          const c = S('mail').currentEmail, id = (c && c.id) || '', k = this.series.key, n = (this.series.items || []).length;
          const sk = k + ':' + n;
          if (this.chg.id === id && this.chg.sk === sk) return;
          if (!id || !k || !n || this.series.loading) { if (this.chg.id !== id || this.chg.sk !== sk) this.chg = Object.assign({ id: '', sk: '', base: '', tok: '', loading: false, error: '', data: null, menu: false, dlg: false, none: true }, { id, sk }); return; }
          this._prepChg(id, sk);
        });
        // 停留计时：打开后 1.5s 仍是这封才标已读（P0-01）
        Alpine.effect(() => { const c = S('mail').currentEmail; this._armRead(c && c.id); });
        // 写信区自动保存：每 3s 有改动就存（只存本地草稿箱，不回写邮件服务器，见 _autosave）
        this._asT = setInterval(() => this._autosave(), 3000);
        window.addEventListener('beforeunload', () => { this._autosave(); });

        // 打开的邮件变了 → 取结构化分析（有缓存直接读，没有才调 AI）
        Alpine.effect(() => { const c = S('mail').currentEmail; const id = c && c.id; if (id !== this.insight.id) this._loadInsight(id); });
        // AI 草拟：把流式结果同步到写信窗正文
        Alpine.effect(() => {
          const ai = S('ai'); const txt = ai.draftContent; const busy = ai.draftLoading;
          if (!this.compose || !this._aiFor) return;
          if (txt) this._setBody(plainToHtml(stripMd(txt)));
          if (!busy) { this._aiFor = ''; }
        });
        S('mail').fetchTodos();
        try {
          const [sd, fd] = await Promise.all([req('/api/senders'), req('/api/folders')]);
          this.senders = (sd && sd.senders) || []; this.defaultSender = (sd && sd.default) || '';
          this.folders = Array.isArray(fd) ? fd : [];
        } catch (e) { /* 取不到时写信窗退回默认发件账户 */ }
        await S('mail').loadMailboxLabels();
        this._ownDomains();
        this._initBridge();
        this._ntfStart();
        try { if (!(S('mail').contacts || []).length && S('mail').fetchContacts) S('mail').fetchContacts(); } catch (e) {}
        // 标签页标题带未读数：切到别的标签也能看到有没有新信
        const base = document.title || 'Quillbox';
        Alpine.effect(() => { const n = S('mail').unreadCount || 0; document.title = (n ? '(' + n + ') ' : '') + base; });
      },
      // ======================= 嵌入桥（postMessage，协议 v1） =======================
      // 只与白名单里的父页面通信：来源取 ancestorOrigins / referrer，必须在
      // <meta name=quillbox-embed-origins>（= config.embed.allowed_origins）或同源；收消息再校验
      // event.origin 与 event.source === window.parent。任何一项不满足，桥保持关闭，永不使用 '*'。
      BRIDGE_VERSION: 1,
      _bridgeOk: false, _parentOrigin: '', _lastState: '', _lastTotal: null,
      _initBridge() {
        if (!window.__QUILLBOX_EMBED || window.parent === window) return;
        const meta = document.querySelector('meta[name="quillbox-embed-origins"]');
        const allow = String((meta && meta.content) || '').split(/\s+/).filter((x) => /^https?:\/\//.test(x));
        let po = '';
        try { po = (location.ancestorOrigins && location.ancestorOrigins[0]) || new URL(document.referrer).origin; } catch (e) { po = ''; }
        if (!po || (po !== location.origin && !allow.includes(po))) { console.warn('[Quillbox] 嵌入桥未开启：父页面来源不在白名单', po); return; }
        this._parentOrigin = po; this._bridgeOk = true;
        window.addEventListener('message', (ev) => this._onBridge(ev));
        Alpine.effect(() => {   // 状态变化 → 推给父页面（未读、待办、视图、当前邮件、标题）
          const m = S('mail'), c = m.currentEmail;
          const st = { unread: m.unreadCount || 0, todosOpen: (m.todos || []).filter((t) => t.status !== 'done').length,
            view: this.view, selectedId: (c && c.id) || null, title: c ? (c.subject || '(无主题)') : this.listTitle,
            syncing: !!(m.stats && m.stats.sync_running) };
          const key = JSON.stringify(st);
          if (key !== this._lastState) { this._lastState = key; this._post('state', st); }
          const total = m.stats && m.stats.total;
          if (typeof total === 'number') {
            if (this._lastTotal !== null && total > this._lastTotal) this._post('new-mail', { total, delta: total - this._lastTotal });
            this._lastTotal = total;
          }
        });
        Alpine.effect(() => { const c = S('mail').currentEmail; if (c && c.id) this._post('email-opened', { id: c.id, subject: c.subject || '', from: c.sender_email || '' }); });
        this._post('ready', { version: (document.querySelector('script[src*="bundle.js"]') || {}).src ? new URL(document.querySelector('script[src*="bundle.js"]').src).searchParams.get('v') : '',
          protocol: this.BRIDGE_VERSION, commands: ['ping', 'get-state', 'navigate', 'open-email', 'search', 'open-search', 'compose', 'set-theme'],
          views: this.VIEWS.map((v) => v[0]).concat(['settings', 'dashboard']) });
      },
      _post(type, payload, id) {
        if (!this._bridgeOk) return;
        try { window.parent.postMessage({ source: 'quillbox', v: this.BRIDGE_VERSION, type: 'quillbox:' + type, id: id || undefined, payload: payload || {} }, this._parentOrigin); } catch (e) {}
      },
      async _onBridge(ev) {
        if (ev.origin !== this._parentOrigin || ev.source !== window.parent) return;
        const d = ev.data;
        if (!d || typeof d !== 'object' || d.target !== 'quillbox' || typeof d.type !== 'string') return;
        const p = (d.payload && typeof d.payload === 'object') ? d.payload : {};
        const ack = (ok, extra) => this._post('ack', Object.assign({ ok, command: d.type }, extra || {}), d.id);
        try {
          switch (d.type) {
            case 'ping': ack(true, { protocol: this.BRIDGE_VERSION }); return;
            case 'get-state': this._lastState = ''; ack(true, { state: JSON.parse(JSON.stringify({ unread: S('mail').unreadCount || 0, view: this.view, selectedId: S('mail').selectedId })) }); return;
            case 'navigate': {
              const v = String(p.view || '');
              if (!this.VIEWS.some((x) => x[0] === v) && !['settings', 'dashboard'].includes(v)) { ack(false, { error: 'unknown-view' }); return; }
              this.go(v); ack(true); return;
            }
            case 'open-email': {
              const id = String(p.id || ''); if (!id) { ack(false, { error: 'missing-id' }); return; }
              this.cmd.open = false; this.panel = '';
              await this.jumpTo({ id }); ack(true); return;
            }
            case 'open-search': {   // 门户把 ⌘K 交给 Quillbox：打开命令面板，可预填
              this.openCmd(); if (p.q) { this.cmd.q = String(p.q).slice(0, 200); this.onCmdQ(); } ack(true); return;
            }
            case 'search': {
              const q = String(p.q || '').slice(0, 200); if (!q.trim()) { ack(false, { error: 'empty-query' }); return; }
              this.applySearch(q); ack(true); return;
            }
            case 'compose': {
              this.newCompose();
              const c = this.compose;
              c.to = String(p.to || ''); c.cc = String(p.cc || ''); c.showCc = !!c.cc; c.subject = String(p.subject || '');
              if (p.body) setTimeout(() => this._setBody(p.html ? String(p.body) : String(p.body).replace(/[&<>"']/g, (x) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[x])).replace(/\r?\n/g, '<br>')), 60);
              ack(true); return;   // 只打开写信窗预填，发送仍需用户在界面上点击
            }
            case 'set-theme': {
              const t = String(p.theme || ''); if (!['light', 'dark', 'system'].includes(t)) { ack(false, { error: 'bad-theme' }); return; }
              this.setTheme(t); ack(true); return;
            }
            default: ack(false, { error: 'unknown-command' });
          }
        } catch (e) { ack(false, { error: 'failed' }); }
      },
      _applyTheme() { document.documentElement.setAttribute('data-theme', S('ui').isDark ? 'dark' : 'light'); },
      _ownDomains() {
        const s = new Set();
        (S('mail').mailboxRules || []).forEach((r) => (r.domains || []).forEach((d) => d && s.add(String(d).toLowerCase())));
        (S('mail').accounts || []).forEach((a) => { const n = String(a.name || ''); if (n.includes('@')) s.add(n.split('@')[1].toLowerCase()); });
        this.ownDomains = [...s];
      },
      async _loadThread(id) {
        try {
          const r = await req('/api/emails/' + encodeURIComponent(id) + '/thread');
          if (this.thread.id === id && r && r.ok !== false) this.thread = { id, count: r.count || 0, subject: r.subject || '', messages: r.messages || [] };
        } catch (e) { /* 话题信息可缺省 */ }
      },

      // ======================= 主题 / 密度 =======================
      get themeMode() { return S('ui').themePreference || 'system'; },
      setTheme(k) { S('ui').setTheme(k); this._applyTheme(); },
      get compact() { return this.density === 'compact'; },
      toggleDensity() { this.density = this.compact ? 'comfortable' : 'compact'; localStorage.setItem('quillbox-v2-density', this.density); },
      get narrow() { return this.winW < 1200; },
      get ctxDrawer() { return this.winW < 1440; },
      // §6 断点：wide ≥1440（文字导航）/ mid 1100–1439（图标导航）/ one 760–1099（列表与阅读二选一）/ phone <760（P2，暂按 one 处理）
      get layout() { const w = this.winW; return w >= 1440 ? 'wide' : w >= 1100 ? 'mid' : w >= 760 ? 'one' : 'phone'; },
      get onePane() { return this.winW < 1100; },
      // 侧栏：auto = 宽屏展开、其余收起；用户点过「收起 / 展开」后记住选择（full / mini）
      railPref: localStorage.getItem('quillbox-v2-rail') || 'auto',
      get railFull() { return this.railPref === 'full' || (this.railPref === 'auto' && this.layout === 'wide'); },
      toggleRail() { this.setRail(this.railFull ? 'mini' : 'full'); },
      setRail(v) { this.railPref = v; localStorage.setItem('quillbox-v2-rail', v); },
      setReaderMode(v) { this.readerMode = v === 'original' ? 'original' : 'clean'; localStorage.setItem('quillbox-v2-reader', this.readerMode); },
      backToList() { const m = S('mail'); m.currentEmail = null; this.ctxOpen = false; },   // §6：1100–1439 上下文收进按钮，点开为右侧抽屉

      // ======================= 派生字段 =======================
      isExternal(e) {
        const a = String((e && (e.sender_email || e.from_addr)) || '').toLowerCase();
        if (!a.includes('@') || !this.ownDomains.length) return false;
        const d = a.split('@').pop();
        return !this.ownDomains.some((x) => d === x || d.endsWith('.' + x));
      },
      isNoAction(e) { const t = e && e.tags; return Array.isArray(t) && t.includes('无需处理'); },
      senderCount(e) {
        const a = String((e && e.sender_email) || '').toLowerCase(); if (!a) return 0;
        const c = (S('mail').contacts || []).find((p) => String(p.email || '').toLowerCase() === a);
        return c ? (c.as_sender || c.count || 0) : 0;
      },
      // P2-13：外部警示只给首次来信、或带链接 / 附件且往来很少（≤2 封）的陌生外部发件人；网关钓鱼标记总是提示
      warnExternal(e) {
        if (!e) return '';
        if (this.isPhish(e)) return '邮件网关标记为外部邮件、谨防钓鱼。点击链接或打开附件前请确认身份。';
        if (!this.isExternal(e)) return '';
        const n = this.senderCount(e);
        if (n <= 1) return '第一次收到这个外部发件人的邮件。点击链接或打开附件前请确认身份。';
        const risky = e.has_attachment || /https?:\/\//i.test(String(e.body || '') + String(e.html_body || ''));
        return (risky && n <= 2) ? '往来很少的外部发件人，邮件含链接或附件。打开前请确认身份。' : '';
      },
      accUnread(name) { const a = this.accounts.find((x) => x.name === name); return a ? a.unread : 0; },
      get accUnreadAll() { return this.accounts.reduce((s, a) => s + (a.unread || 0), 0); },
      pri(e) {
        if (this.isNoAction(e)) return 'fyi';
        // AI 结构化分析优先（act/reply/notice/fyi）；未分析的邮件退回同步时的分类口径
        const ins = e && this.insight.id === e.id && this.insight.data;
        const p = (e && e.ai_priority) || (ins && ins.priority);
        if (p) return p;
        const c = (e && e.category) || '';
        if (c.includes('重要行动')) return 'act';
        if (c.includes('通知')) return 'notice';
        return 'fyi';
      },
      // 列表盾牌：只认邮件网关打的钓鱼风险标记（主题含「谨防钓鱼」）。仅凭「外部域名」会让绝大多数通知邮件都挂盾牌，失去警示意义。
      isPhish(e) { return /谨防钓鱼|钓鱼邮件|phishing/i.test(String((e && e.subject) || '')); },
      priTag(e) { const p = this.pri(e); return p === 'act' ? { label: '待处理', cls: 'tag-accent' } : p === 'reply' ? { label: '待回复', cls: 'tag-outline' } : null; },
      priCode(e) { if (this.isNoAction(e)) return '无需处理'; return { act: '需要处理', reply: '等你回复', notice: '系统通知', fyi: '仅供阅读' }[this.pri(e)]; },
      isSystem(e) { return SYSTEM_SENDER.test(String((e && (e.sender_email || e.from_addr)) || '')) || this.pri(e) === 'notice' && !(e.recipients || []).length; },
      fmtTime(t) {
        const d = parseT(t); if (!d) return '';
        const now = new Date();
        if (d.toDateString() === now.toDateString()) return pad(d.getHours()) + ':' + pad(d.getMinutes());
        const y = new Date(now); y.setDate(now.getDate() - 1);
        if (d.toDateString() === y.toDateString()) return '昨天';
        if (d.getFullYear() === now.getFullYear()) return (d.getMonth() + 1) + '/' + d.getDate();
        return d.getFullYear() + '/' + (d.getMonth() + 1) + '/' + d.getDate();
      },
      fmtDate(t) {
        const d = parseT(t); if (!d) return '';
        const now = new Date(), hm = pad(d.getHours()) + ':' + pad(d.getMinutes());
        if (d.toDateString() === now.toDateString()) return '今天 ' + hm;
        return d.getFullYear() + '/' + (d.getMonth() + 1) + '/' + d.getDate() + ' ' + hm;
      },
      rowSum(e) {
        if (e && e.next) return '下一步：' + e.next;   // 分拣行第三行只说下一步，不重复摘要（P1-B）
        return (e.snippet || e.summary || e.body_head || '').replace(/\s+/g, ' ').trim();
      },
      fmtDue(d) {
        if (!d) return '';
        const t = new Date(d + 'T00:00:00'), now = new Date(); now.setHours(0, 0, 0, 0);
        const n = Math.round((t - now) / 86400000);
        const md = (t.getMonth() + 1) + '/' + t.getDate();
        return n < 0 ? '已过期 ' + md : n === 0 ? '今天截止' : n === 1 ? '明天截止' : '截止 ' + md;
      },
      dueLate(d) { if (!d) return false; const now = new Date(); now.setHours(0, 0, 0, 0); return new Date(d + 'T00:00:00') <= now; },
      initial(e) { const s = (e && (e.sender || e.from_name || e.sender_email)) || '?'; return s.trim().charAt(0).toUpperCase(); },

      // ======================= 账户 =======================
      get accounts() {
        const m = S('mail');
        return (m.displayAccounts || []).map((a) => {
          const lab = m.mailboxLabel ? m.mailboxLabel(a.name) : null;
          const alias = m.accountAliases && m.accountAliases[a.name];
          const base = alias || (lab && lab.name) || a.name;
          const unread = Object.values(a.unread || {}).reduce((x, y) => x + (y || 0), 0);
          return { name: a.name, short: String(base).trim().charAt(0).toUpperCase(), label: (alias || (lab && lab.name) ? base + ' · ' : '') + a.name, unread, unreadMap: a.unread || {}, folders: a.folders || [] };
        });
      },
      get accLabel() { return this.acc ? this.acc : '全部账户'; },
      setAcc(a) {
        this.acc = a;
        const m = S('mail');
        if (this.view === 'triage') { m.currentAccount = a; m.selectedId = null; m.currentEmail = null; m.selectedIds = []; this.loadTriage(true); return; }
        if (['topics', 'todos', 'drafts'].includes(this.view)) { this.go('inbox'); return; }
        m.currentAccount = a; m.page = 0; m.selectedId = null; m.currentEmail = null; m.selectedIds = []; this._listTop();
        m.fetchEmails();
      },

      // ======================= 导航 =======================
      VIEWS: [
        ['triage', '今日分拣', 'list-checks'], ['inbox', '收件箱', 'tray'], ['topics', '话题', 'stack'], ['todos', '待办', 'check-square'], ['starred', '星标', 'star'],
        ['sent', '已发送', 'paper-plane-tilt'], ['drafts', '草稿', 'file-text'], ['archive', '归档', 'archive'], ['trash', '回收站', 'trash'],
      ],
      viewCount(k) {
        if (k === 'triage') return this.triTodoCount || '';
        if (k === 'inbox') return S('mail').unreadCount || '';
        if (k === 'todos') return (S('mail').todos || []).filter((t) => t.status !== 'done').length || '';
        return '';
      },
      get isMailView() { return !['topics', 'todos', 'settings', 'dashboard'].includes(this.view); },
      get listTitle() {
        if (this.view === 'folder') return (this.scope && this.scope.title) || '文件夹';
        const v = this.VIEWS.find((x) => x[0] === this.view); return v ? v[1] : '收件箱';
      },
      // 换了一个列表就回到顶部（所有列表视图共用同一个滚动容器；翻页 / 后台刷新不调用这里）
      _listTop() {
        if (typeof document === 'undefined') return;
        document.querySelectorAll('[data-testid=rows], [data-testid=triage]').forEach((el) => { el.scrollTop = 0; });
      },
      _reset(m) {
        this._listTop();
        this.scope = null; this.batchSumOpen = false;
        Object.assign(m, { currentTopic: '', currentTag: '', activeCategory: '', page: 0, selectedId: null, currentEmail: null,
          filter: 'all', searchQuery: '', currentFolder: '', currentMailbox: '', currentAccount: this.acc, selectedIds: [] });
      },
      go(v) {
        const m = S('mail'); this.panel = '';
        if (v === 'settings') { this.view = v; S('settings').fetch(); this.loadMb(); return; }
        if (v === 'dashboard') { this.view = v; m.loadDashboard(); m.fetchTodos(); return; }
        this.view = v; this._reset(m);
        if (v === 'triage') { m.view = 'inbox'; this.tri.gone = {}; this.loadTriage(true); return; }
        const map = { inbox: 'inbox', starred: 'starred', archive: 'archived', trash: 'trash', sent: 'inbox', drafts: 'drafts', todos: 'todos', topics: 'topics' };
        m.view = map[v] || 'inbox';
        if (v === 'sent') m.currentFolder = '已发送邮件';
        if (v === 'drafts') m.fetchDrafts();
        else if (v === 'todos') m.fetchTodos();
        else if (v === 'topics') m.loadTopics();
        else m.fetchEmails();
        if (v === 'inbox') this._autoSync();
      },
      openFolder(name, account) {
        const m = S('mail'); this.view = 'folder'; this.panel = ''; this._reset(m);
        m.view = 'inbox'; m.currentFolder = name; m.currentAccount = account || this.acc;
        this.scope = { kind: 'folder', title: name, label: '文件夹：' + name + (account ? ' · ' + account : '') };
        m.fetchEmails();
        if (/^(收件箱|inbox)$/i.test(String(name || '').trim())) this._autoSync();
      },
      // ======================= 点收件箱自动同步 =======================
      // 点「收件箱」（侧栏 / 文件夹面板里的收件箱 / 其他回到收件箱的入口）→ 静默触发一次增量同步：
      //   · 不弹「同步已开始」，顶部同步图标随 stats.sync_running 自转；15 秒冷却；已有同步在跑只等它、不再发信号；
      //   · 同步结束后，仍停在收件箱第一页且总数 / 未读有变化才静默刷新列表（不重置选中，多选中不刷新），有新邮件给一次轻提示。
      _inInboxScope() {
        const m = S('mail');
        if (this.view === 'inbox') return !m.currentFolder && !m.searchQuery && !m.currentTopic;
        return this.view === 'folder' && /^(收件箱|inbox)$/i.test(String(m.currentFolder || '').trim());
      },
      async _autoSync() {
        const m = S('mail'), t0 = Date.now();
        if (this._aSyncBusy || t0 - (this._aSyncLast || 0) < 15000) return;
        this._aSyncLast = t0; this._aSyncBusy = true; this.autoSyncing = true;   // 同步通常不到 1 秒就完，图标至少转满 1.2 秒才看得见
        try {
          await m.fetchStats();
          const s0 = m.stats || {}, total0 = s0.total, last0 = s0.last_sync, unread0 = m.unreadCount;
          if (!s0.sync_running) {
            try { await req('/api/sync', { method: 'POST' }); } catch (e) { console.warn('[Quillbox] 自动同步触发失败', e); this.flash('同步触发失败，请点顶部「立即同步」重试', null, 'error'); return; }
          }
          for (let i = 0; i < 80; i++) {   // 最多等 2 分钟；daemon 每 5 秒看一次信号文件
            await new Promise((r) => setTimeout(r, 1500));
            await m.fetchStats();
            const s = m.stats || {};
            if (s.sync_running || s.last_sync === last0) continue;
            const added = (typeof s.total === 'number' && typeof total0 === 'number') ? s.total - total0 : 0;
            if (this._inInboxScope() && !m.page && !(m.selectedIds && m.selectedIds.length) && (added !== 0 || m.unreadCount !== unread0)) await m.fetchEmails();
            await new Promise((r) => setTimeout(r, Math.max(0, 1200 - (Date.now() - t0))));
            this.flash(added > 0 ? '收到 ' + added + ' 封新邮件' : '已同步，没有新邮件');
            return;
          }
        } catch (e) { console.warn('[Quillbox] 自动同步', e); }
        finally { this._aSyncBusy = false; this.autoSyncing = false; }
      },
      applySearch(q, label, kind) {
        q = String(q || '').trim(); if (!q) return;
        const m = S('mail');
        if (!this.isMailView || this.view === 'drafts' || this.view === 'triage') { this.view = 'inbox'; this._reset(m); m.view = 'inbox'; }
        m.searchQuery = q; m.page = 0; m.selectedId = null; m.currentEmail = null; m.selectedIds = []; this._listTop();
        this.scope = { kind: kind || 'search', label: label || ('搜索「' + q + '」') };   // 文案只写「搜索」，是否用了 AI 由 scopeText 按结果来源如实标注
        m.fetchEmails();
      },
      applySavedSearch(s) { this.applySearch(s.query, '已存搜索：' + s.name); },
      enterTopicRule(name) {
        const m = S('mail'); this.view = 'inbox'; this.panel = ''; this._reset(m);
        m.view = 'inbox'; this.scope = { kind: 'topic', label: '智能文件夹：' + name };
        m.currentTopic = name; m.fetchEmails();
      },
      openTopic(tp) { this.applySearch('subject:"' + tp.topic.replace(/"/g, '') + '"', '话题：' + tp.topic, 'topic'); },
      openThread() {
        if (!this.thread.subject) return;
        this.applySearch('subject:"' + this.thread.subject.replace(/"/g, '') + '"', '同一话题：' + this.thread.subject, 'thread');
      },
      clearScope() {
        this.qb.chips = []; this.qb.text = '';
        if (this.view === 'folder') { this.go('inbox'); return; }
        const m = S('mail'); this.scope = null; this._listTop();
        Object.assign(m, { searchQuery: '', currentTopic: '', currentFolder: this.view === 'sent' ? '已发送邮件' : '', page: 0, selectedId: null, currentEmail: null });
        m.fetchEmails();
      },
      get scopeText() {
        if (!this.scope) return '';
        const m = S('mail'), n = m.totalCount;
        let t = this.scope.label + (this.scope.kind !== 'folder' && !m.loading ? ' · ' + n + ' 封' : '');
        if (this.scope.kind === 'search' && !m.loading && /(^|\s)(from|to|subject|has|after|before|body):/i.test(m.searchQuery || '')) {
          t += '（条件检索' + (/✦/.test(this.scope.label) ? '，✦ 为 AI 从你的话里理解出的条件' : '') + '）';
        } else if (this.scope.kind === 'search' && !m.loading) {   // 如实说明结果从哪来：关键词命中 / 语义相近补充 / 处理记录命中
          const L = m.emails || [], sem = L.filter((e) => e.semantic).length, nh = L.filter((e) => e.note_hit).length;
          t += (sem || nh) ? '（关键词 ' + (L.length - sem - nh) + (sem ? '，语义相近 ' + sem : '') + (nh ? '，处理记录 ' + nh : '') + '）' : '（关键词检索）';
        }
        return t;
      },
      FILTERS: [['all', '全部'], ['unread', '未读'], ['action', '待处理'], ['attachments', '附件'], ['external', '外部']],
      setFilter(f) { const m = S('mail'); this._listTop(); m.filter = f; m.page = 0; m.selectedId = null; m.currentEmail = null; m.fetchEmails(); },
      get emptyTitle() {
        if (S('mail').searchQuery) return '没有找到相关邮件';
        if (S('mail').filter !== 'all') return '这个筛选下没有邮件';
        return this.view === 'inbox' ? '收件箱已清空' : '这里还没有邮件';
      },
      get emptyText() { return S('mail').searchQuery ? '换个说法试试，比如「示例云 通知」。' : '按 C 写一封新邮件，或 ⌘K 搜索。'; },
      onListScroll(ev) {
        const el = ev.target, m = S('mail');
        if (el.scrollTop + el.clientHeight > el.scrollHeight - 240 && m.hasMore && !m.loading) m.loadMore();
      },

      // ======================= 选择 / 勾选 =======================
      // ======================= 今日分拣（P1-A） =======================
      TRI_SECTIONS: [['act', '需要你处理', '按截止日'], ['reply', '等你回复', '等得最久的在前'], ['watch', '需要知道', '你负责范围内的变更与风险 · 不需回复'], ['notice', '告警与通知', '同一告警折叠'],
        ['other', '其他', '仅供阅读 · 无需处理'], ['pending', '尚未分析', 'AI 正在后台分析']],
      async loadTriage(fresh) {
        const m = S('mail'), t = this.tri;
        t.loading = true; m.loading = true;
        try {
          const r = await req('/api/triage?days=7' + (this.acc ? '&account=' + encodeURIComponent(this.acc) : ''));
          if (!r || !r.ok) throw new Error((r && r.error) || 'triage');
          if (this.view !== 'triage') return;
          const by = {}; (r.rows || []).forEach((x) => { by[x.id] = x; });
          t.data = r; t.by = by; t.gone = {}; t.error = ''; t.okAt = Date.now();
          if (fresh) { t.aq.ok = 0; t.aq.failed = 0; }
          this._triFlatten();
          this._triQueue(false);
        } catch (e) {
          t.error = '读取失败' + (e && e.message && e.message !== 'triage' ? '（' + String(e.message).slice(0, 60) + '）' : '');
        } finally { t.loading = false; m.loading = false; }
      },
      _triLive(ids) { const t = this.tri; return (ids || []).filter((id) => t.by[id] && !t.gone[id]); },
      // 渲染用：每组 { key, title, hint, n, open, items:[{ row, cluster, member }] }；告警组折叠时只出最新一封
      get triSections() {
        const t = this.tri, d = t.data; if (!d) return [];
        return this.TRI_SECTIONS.map(([key, title, hint]) => {
          let items = [];
          if (key === 'notice') {
            for (const c of d.clusters || []) {
              const live = this._triLive(c.ids); if (!live.length) continue;
              const multi = c.ids.length > 1;
              items.push({ row: t.by[live[0]], cluster: multi ? { key: c.key, n: live.length, state: c.state, ids: live } : null, member: false });
              if (multi && t.openClusters[c.key]) live.slice(1).forEach((id) => items.push({ row: t.by[id], cluster: null, member: true }));
            }
          } else items = this._triLive(d.sections[key]).map((id) => ({ row: t.by[id], cluster: null, member: false }));
          const n = key === 'notice' ? (d.clusters || []).reduce((s, c) => s + this._triLive(c.ids).length, 0) : items.length;
          return { key, title, hint, n, open: !!t.open[key], items };
        }).filter((s) => s.n > 0);
      },
      // J/K、E、勾选都基于 mail.emails：按屏幕顺序放「展开分组里可见的行」
      _triFlatten() {
        const m = S('mail'), L = [];
        this.triSections.forEach((s) => { if (s.open) s.items.forEach((it) => L.push(it.row)); });
        m.emails = L; m.hasMore = false; m.totalCount = L.length;
        if (m.selectedId && !L.some((e) => e.id === m.selectedId)) m.selectedId = null;
      },
      triToggle(key) { this.tri.open[key] = !this.tri.open[key]; this._triFlatten(); },
      triToggleCluster(k) { this.tri.openClusters[k] = !this.tri.openClusters[k]; this._triFlatten(); },
      _triRemove(ids, notDone) { ids.forEach((id) => { this.tri.gone[id] = true; }); if (!notDone) this.tri.done += ids.length; this._triFlatten(); },
      // ======================= 稍后提醒（H） =======================
      _at(d, h, m) { const x = new Date(d); x.setHours(h, m || 0, 0, 0); return x; },
      get snzOptions() {
        const now = new Date(), out = [];
        if (now.getHours() < 17) out.push(['today', '今天 18:00', this._at(now, 18)]);
        const tm = new Date(now); tm.setDate(tm.getDate() + 1); out.push(['tomorrow', '明天 9:00', this._at(tm, 9)]);
        const mon = new Date(now); mon.setDate(mon.getDate() + (((8 - mon.getDay()) % 7) || 7)); out.push(['monday', '下周一 9:00', this._at(mon, 9)]);
        return out;
      },
      fmtSnz(ts) { if (!ts) return ''; const d = new Date(ts * 1000); return (d.getMonth() + 1) + '/' + d.getDate() + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()); },
      openSnooze(ids) {
        if (this._triBlocked()) return;
        ids = (Array.isArray(ids) && ids.length) ? ids : this.targets(); if (!ids.length) return;
        const t = this._at(new Date(Date.now() + 86400000), 9);
        const loc = t.getFullYear() + '-' + pad(t.getMonth() + 1) + '-' + pad(t.getDate()) + 'T09:00';
        this.snzDlg = { open: true, ids: ids.slice(), pick: loc };
      },
      async snoozeAt(dt, label) {
        const d = this.snzDlg, ids = d.ids.slice(), m = S('mail'); d.open = false;
        const until = Math.round(dt.getTime() / 1000);
        if (!(until > Date.now() / 1000 + 30)) { this.flash('提醒时间要晚于现在', null, 'error'); return; }
        try { await Promise.all(ids.map((id) => req('/api/emails/' + encodeURIComponent(id) + '/snooze', { method: 'POST', body: JSON.stringify({ until }) }))); }
        catch (e) { this.flash('设置稍后提醒失败', null, 'error'); return; }
        const subj = this._opSubj(ids);
        if (this.view === 'triage') {
          const next = this._nextAfter(ids);
          if (m.currentEmail && ids.includes(m.currentEmail.id)) m.currentEmail = null;
          this._triRemove(ids, true);
          if (next && (m.emails || []).some((e) => e.id === next)) { this._hlId = next; m.selectedId = next; }
        } else ids.forEach((id) => { const e = (m.emails || []).find((x) => x.id === id); if (e) e.snooze_until = until; });
        m.selectedIds = [];
        this.flash('稍后提醒 ' + (label || this.fmtSnz(until)) + ' · ' + ids.length + ' 封' + subj, async () => {
          const rs = await Promise.allSettled(ids.map((id) => req('/api/emails/' + encodeURIComponent(id) + '/snooze', { method: 'DELETE' })));
          ids.forEach((id) => { const e = (m.emails || []).find((x) => x.id === id); if (e) delete e.snooze_until; });
          m.fetchEmails();
          if (rs.some((x) => x.status !== 'fulfilled')) throw new Error('partial');
        });
      },
      snoozePick() { const v = this.snzDlg.pick; if (!v) return; const d = new Date(v); if (isNaN(d)) return; this.snoozeAt(d); },
      async unsnooze(e) {
        try { await req('/api/emails/' + encodeURIComponent(e.id) + '/snooze', { method: 'DELETE' }); delete e.snooze_until; this.flash('已取消稍后提醒'); }
        catch (x) { this.flash('取消失败', null, 'error'); }
      },
      // ======================= 静音告警 7 天 =======================
      openMute(row) {
        if (!row || !row.alert_key) return;
        this.muteDlg = { open: true, key: row.alert_key, sender: String(row.sender_email || '').toLowerCase(),
          label: (row.sender || row.sender_email || '') + ' · ' + String(row.subject || '').slice(0, 40), scope: 'object', who: row.sender || row.sender_email || '' };
      },
      async confirmMute() {
        const d = this.muteDlg; d.open = false;
        const body = d.scope === 'sender' ? { scope: 'sender', key: d.sender, label: d.who + ' 的全部告警', days: 7 } : { scope: 'object', key: d.key, label: d.label, days: 7 };
        let r;
        try { r = await req('/api/mutes', { method: 'POST', body: JSON.stringify(body) }); if (!r || !r.ok) throw new Error('mute'); }
        catch (e) { this.flash('静音失败', null, 'error'); return; }
        const m = S('mail'); if (m.currentEmail && m.currentEmail.alert_key === undefined) { /* 阅读区保持 */ }
        await this.loadTriage(false);
        this.flash('已静音 7 天：' + body.label, async () => { await req('/api/mutes/' + r.id, { method: 'DELETE' }); await this.loadTriage(false); });
      },
      async unmute(c) {
        const head = this.tri.by[c.ids[0]] || {};
        const back = c.mute_scope === 'sender' ? { scope: 'sender', key: String(head.sender_email || '').toLowerCase(), label: (c.sender || '') + ' 的全部告警' }
          : { scope: 'object', key: c.key, label: (c.sender || '') + ' · ' + String(c.subject || '').slice(0, 40) };
        const left = Math.max(1 / 24, ((c.mute_until || 0) - Date.now() / 1000) / 86400);
        try { await req('/api/mutes/' + c.mute_id, { method: 'DELETE' }); } catch (e) { this.flash('取消静音失败', null, 'error'); return; }
        await this.loadTriage(false);
        this.flash('已取消静音：' + back.label, async () => { await req('/api/mutes', { method: 'POST', body: JSON.stringify(Object.assign(back, { days: left })) }); await this.loadTriage(false); });
      },
      get triMuted() { const d = this.tri.data; return (d && d.muted_clusters) || []; },
      get triTodoCount() { const d = this.tri.data; if (!d) return 0; return this._triLive(d.sections.act).length + this._triLive(d.sections.reply).length; },
      get triCleared() { const d = this.tri.data; return !!d && !this.triTodoCount && !this._triLive(d.sections.pending).length; },
      get triProgress() {
        const d = this.tri.data; if (!d) return null;
        const y = d.counts.analyzable, x = Math.min(y, d.counts.analyzed + this.tri.aq.ok);
        return { x, y, pct: y ? Math.round(x * 100 / y) : 100, busy: this.tri.aq.running > 0 || this.tri.aq.queue.length > 0, failed: this.tri.aq.failed };
      },
      get triOkText() { return this.tri.okAt ? new Date(this.tri.okAt).toTimeString().slice(0, 5) : '—'; },
      // 后台分析：列表到手就排队，与面板是否可见无关（P0-04）；并发 4；每封每会话只试一次，「立即分析」可重试失败的
      _triQueue(retry) {
        const d = this.tri.data, q = this.tri.aq; if (!d) return;
        this._triLive(d.sections.pending).forEach((id) => {
          const st = q.tried[id];
          if (!st || (retry && st === 'fail')) { if (st === 'fail') q.failed = Math.max(0, q.failed - 1); q.tried[id] = 'queued'; q.queue.push(id); }
        });
        while (q.running < 4 && q.queue.length) this._triPump();
      },
      async _triPump() {
        const q = this.tri.aq, id = q.queue.shift(); if (!id) return;
        q.running++;
        try {
          const r = await req('/api/emails/' + encodeURIComponent(id) + '/insight', { method: 'POST' });
          if (r && r.ok) { q.tried[id] = 'ok'; q.ok++; } else { q.tried[id] = 'fail'; q.failed++; }
        } catch (e) { q.tried[id] = 'fail'; q.failed++; }
        finally {
          q.running--;
          if (q.queue.length) this._triPump();
          else if (!q.running && this.view === 'triage') this.loadTriage(false);   // 一轮跑完：重新分组
        }
      },
      triAnalyzeNow() { this._triQueue(true); },
      triArchiveCluster(c) { this.archive(c.ids.slice()); },
      _triBlocked() {
        if (this.view === 'triage' && this.tri.error) { this.flash('读取失败，列表是旧数据，暂不能操作。请先重试。', null, 'error'); return true; }
        return false;
      },

      open(id) {
        this._hlId = id;
        const m = S('mail');
        if (this.readOnOpen) {
          const row = (m.emails || []).find((e) => e.id === id);
          if (row && row.unread) {
            row.unread = false;
            m._bumpUnread(-1);
            req('/api/emails/' + encodeURIComponent(id), { method: 'PATCH', body: JSON.stringify({ unread: false }) }).catch(() => {});
          }
        }
        m.gotoEmail(id);
        if (this.narrow) this.aiPaneOpen = false;
      },
      // J/K：只移动高亮，不打开、不标已读、不调 AI（P0-01）
      hl(delta) {
        const m = S('mail'), L = m.emails || []; if (!L.length) return;
        const idx = L.findIndex((e) => e.id === m.selectedId);
        let ni = idx < 0 ? 0 : idx + delta;
        if (ni >= L.length) {   // 到当前页末尾：还有下一页就先加载再下移
          if (m.hasMore && !m.loading) Promise.resolve(m.loadMore()).then(() => { if ((m.emails || []).length > L.length) this.hl(delta); }).catch(() => {});
          return;
        }
        ni = Math.max(0, ni);
        this._hlId = L[ni].id; m.selectedId = L[ni].id; m.scrollRowIntoView(L[ni].id);
      },
      openHl() { const id = S('mail').selectedId; if (id) this.open(id); },
      isOpen(e) { const c = S('mail').currentEmail; return !!(c && e && c.id === e.id); },
      _armRead(id) {
        clearTimeout(this._readT); this._readT = null;
        if (!id || !S('mail').deferRead) return;
        this._readT = setTimeout(() => this._markOpenedRead(id), 300);
      },
      _markOpenedRead(id) {
        const m = S('mail'), c = m.currentEmail; if (!c || c.id !== id) return;
        const row = (m.emails || []).find((e) => e.id === id);
        const unread = row ? row.unread : c.unread;
        if (!unread) return;
        if (row) row.unread = false; c.unread = false;
        m._bumpUnread(-1);
        req('/api/emails/' + encodeURIComponent(id), { method: 'PATCH', body: JSON.stringify({ unread: false }) }).catch(() => {});
      },
      setReadOnOpen(v) {
        this.readOnOpen = !!v; localStorage.setItem('quillbox-v2-read-on-open', v ? '1' : '0');
        S('mail').deferRead = !v;
      },
      get checked() { return S('mail').selectedIds || []; },
      isChecked(id) { return this.checked.includes(id); },
      check(id) { S('mail').toggleSelect(id); this.batchSumOpen = false; },
      selectAll() { S('mail').selectedIds = (S('mail').emails || []).map((e) => e.id); },
      clearChecked() { S('mail').selectedIds = []; this.batchSumOpen = false; this.showChecks = false; },
      targets() { const m = S('mail'); return this.checked.length ? this.checked.slice() : (m.selectedId ? [m.selectedId] : []); },
      get batchSumText() {
        const m = S('mail'); const ms = this.checked.map((id) => (m.emails || []).find((e) => e.id === id)).filter(Boolean);
        if (!ms.length) return '';
        const act = ms.filter((e) => this.pri(e) === 'act').length;
        const unread = ms.filter((e) => e.unread).length;
        return '所选 ' + ms.length + ' 封，' + (act ? act + ' 封标记为需要处理' : '没有标记为需要处理的') + '，' + unread + ' 封未读。'
          + ms.slice(0, 3).map((e) => (e.sender || e.sender_email) + '：' + (this.rowSum(e) || e.subject)).join('；');
      },

      // ======================= 邮件操作 =======================
      _nextAfter(ids) {
        const L = S('mail').emails || [], sel = S('mail').selectedId;
        if (!ids.includes(sel)) return sel;
        const idx = L.findIndex((e) => e.id === sel), rest = L.filter((e) => !ids.includes(e.id));
        const n = rest[Math.min(Math.max(idx, 0), rest.length - 1)];
        return n ? n.id : null;
      },
      // E = 完成并归档：归档邮件，同时把这些邮件关联的未完成待办标为完成；撤销时两样一起恢复，任一失败按「撤销未完成」（P1-C）
      async archive(idsArg, opts) {
        if (this._triBlocked()) return;
        const ids = (Array.isArray(idsArg) && idsArg.length) ? idsArg : this.targets(); if (!ids.length) return;
        const m = S('mail'), unarchive = this.view === 'archive';
        opts = opts || {};
        if (!unarchive && !opts.skipNote && this._wantNote(ids, opts)) { this.openNoteDlg(ids); return; }
        const noteIds = opts.noteIds || [];
        const next = this._nextAfter(ids);
        const tset = new Set();
        if (!unarchive) {
          (m.todos || []).forEach((t) => { if (t.status !== 'done' && ids.includes(t.source_email_id)) tset.add(t.id); });
          ids.forEach((id) => { const r = this.tri.by[id]; (r && r.open_todos || []).forEach((x) => tset.add(x)); });
        }
        const todoIds = [...tset];
        try { await Promise.all(ids.map((id) => req('/api/emails/' + encodeURIComponent(id), { method: 'PATCH', body: JSON.stringify({ archived: unarchive ? 0 : 1 }) }))); }
        catch (e) { this.flash('归档失败', null, 'error'); return; }
        let todoFail = 0;
        if (todoIds.length) {
          const rs = await Promise.allSettled(todoIds.map((t) => req('/api/todos/' + t, { method: 'PATCH', body: JSON.stringify({ status: 'done' }) })));
          todoFail = rs.filter((x) => x.status !== 'fulfilled').length;
          m.fetchTodos();
        }
        if (this.view === 'triage') this._triRemove(ids);
        else m.emails = (m.emails || []).filter((e) => !ids.includes(e.id));
        m.selectedIds = []; this.batchSumOpen = false;
        // 下一封只高亮，不打开、不标已读（P0-01 / P1-C）；归档的正是打开的那封时阅读区清空
        if (m.currentEmail && ids.includes(m.currentEmail.id)) m.currentEmail = null;
        if (next) { this._hlId = next; m.selectedId = next; m.scrollRowIntoView(next); } else m.selectedId = null;
        m.fetchStats();
        const label = unarchive ? '已移回收件箱 ' + ids.length + ' 封'
          : (todoIds.length ? '已完成并归档 ' + ids.length + ' 封（关闭待办 ' + (todoIds.length - todoFail) + ' 条' + (todoFail ? '，' + todoFail + ' 条未能关闭' : '') + '）' : '已归档 ' + ids.length + ' 封')
            + (noteIds.length ? ' · 已记处理说明' : '');
        this.flash(label + this._opSubj(ids), async () => {
          const rs = await Promise.allSettled(ids.map((id) => req('/api/emails/' + encodeURIComponent(id), { method: 'PATCH', body: JSON.stringify({ archived: unarchive ? 1 : 0 }) }))
            .concat(todoIds.map((t) => req('/api/todos/' + t, { method: 'PATCH', body: JSON.stringify({ status: 'pending' }) })))
            .concat(noteIds.map((n) => req('/api/notes/' + n, { method: 'DELETE' }))));   // 撤销完成 = 说明一起撤掉（附件 7 天后才清理）
          m.fetchEmails(); m.fetchStats(); if (todoIds.length) m.fetchTodos();
          if (this.view === 'triage') this.tri.done = Math.max(0, this.tri.done - ids.length);
          if (rs.some((x) => x.status !== 'fulfilled')) throw new Error('partial');
        }, null, (!unarchive && !noteIds.length) ? { label: '补充说明', run: () => this.openNoteDlg(ids, { after: true }) } : null);
      },
      async star() {
        if (this._triBlocked()) return;
        const ids = this.targets(); if (!ids.length) return;
        const m = S('mail'), L = m.emails || [];
        const all = ids.every((id) => (L.find((e) => e.id === id) || {}).starred);
        ids.forEach((id) => { const e = L.find((x) => x.id === id); if (e) e.starred = !all; });
        if (m.currentEmail && ids.includes(m.currentEmail.id)) m.currentEmail.starred = !all;
        try { await req('/api/emails/bulk', { method: 'POST', body: JSON.stringify({ ids, action: all ? 'unstar' : 'star' }) }); }
        catch (e) { this.flash('星标失败', null, 'error'); }
      },
      starOne(e) { const m = S('mail'); m.toggleStarById(e.id); if (m.currentEmail && m.currentEmail.id === e.id) m.currentEmail.starred = !e.starred; },
      async markRead() {
        if (this._triBlocked()) return;
        const ids = this.targets(); if (!ids.length) return;
        const m = S('mail');
        ids.forEach((id) => { const e = (m.emails || []).find((x) => x.id === id); if (e) { e._wasUnread = !!e.unread; e.unread = false; } });
        const was = ids.filter((id) => { const e = (m.emails || []).find((x) => x.id === id); return e && e._wasUnread; });
        try { await req('/api/emails/bulk', { method: 'POST', body: JSON.stringify({ ids, action: 'read' }) }); } catch (e) {}
        m.selectedIds = []; m.fetchStats();
        this.flash('已标为已读 ' + ids.length + ' 封', was.length ? async () => {
          await req('/api/emails/bulk', { method: 'POST', body: JSON.stringify({ ids: was, action: 'unread' }) });
          was.forEach((id) => { const e = (m.emails || []).find((x) => x.id === id); if (e) e.unread = true; }); m.fetchStats();
        } : null);
      },
      async toggleUnread() {
        const m = S('mail'); clearTimeout(this._readT); this._readT = null;
        if (!this.checked.length) { await m.toggleUnread(); return; }
        const ids = this.targets(), L = m.emails || [];
        const allUnread = ids.every((id) => (L.find((e) => e.id === id) || {}).unread);
        ids.forEach((id) => { const e = L.find((x) => x.id === id); if (e) e.unread = !allUnread; });
        try { await req('/api/emails/bulk', { method: 'POST', body: JSON.stringify({ ids, action: allUnread ? 'read' : 'unread' }) }); } catch (e) {}
        m.fetchStats();
      },
      async trash() {
        const m = S('mail');
        if (this.checked.length) {
          const ids = this.targets();
          if (!confirm('删除所选 ' + ids.length + ' 封邮件？')) return;
          await m.bulkAction('delete'); return;
        }
        if (!m.selectedId) return;
        await m.deleteSelected();   // 与旧界面一致：同时在邮件服务器上删除，因此不提供撤销
      },
      // ======================= 加入待办弹层（P1-E） =======================
      _isoDay(off) { const d = new Date(); d.setDate(d.getDate() + (off || 0)); return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); },
      openTodoDlg(email, text) {
        const m = S('mail'); const e = email || m.currentEmail; if (!e) return;
        if (this.isNoAction(e)) { this.flash('这封邮件已标记为无需处理'); return; }
        const ins = this.curInsight && m.currentEmail && m.currentEmail.id === e.id ? this.curInsight : null;
        const aiDue = (ins && ins.due) || e.due || '', aiSrc = (ins && ins.due_src) || e.due_src || '';
        const sug = text || (ins && (ins.todos || [])[0]) || this.actionItems(e)[0] || ('跟进：' + (e.subject || '(无主题)'));
        this.todoDlg = { open: true, text: sug, mode: aiDue ? 'ai' : 'none', pick: aiDue || this._isoDay(1), aiDue, aiSrc, pri: 'normal', link: true, srcId: e.id, subject: e.subject || '' };
        setTimeout(() => { const el = document.querySelector('[data-testid=todo-dlg] input.txt'); if (el) { el.focus(); el.select(); } }, 30);
      },
      get todoDlgDue() {
        const d = this.todoDlg;
        return { ai: d.aiDue, today: this._isoDay(0), tomorrow: this._isoDay(1), pick: d.pick, none: '' }[d.mode] || '';
      },
      async confirmTodoDlg() {
        const d = this.todoDlg; const text = String(d.text || '').trim(); if (!text) return;
        d.open = false;
        await this.addTodoText(text, d.link ? d.srcId : null, this.todoDlgDue, d.pri);
      },
      // ======================= 待办行内编辑（P1-E） =======================
      async todoSet(t, patch) {
        const old = {}; Object.keys(patch).forEach((k) => { old[k] = t[k]; }); Object.assign(t, patch);
        try { await req('/api/todos/' + t.id, { method: 'PATCH', body: JSON.stringify(patch) }); }
        catch (e) { Object.assign(t, old); this.flash('修改待办失败', null, 'error'); }
      },
      async todoDelete(t) {
        const m = S('mail'), keep = { content: t.content, source_email_id: t.source_email_id || null, priority: t.priority || 'normal', due_date: t.due_date || null, status: t.status };
        try { await req('/api/todos/' + t.id, { method: 'DELETE' }); m.todos = (m.todos || []).filter((x) => x.id !== t.id); }
        catch (e) { this.flash('删除待办失败', null, 'error'); return; }
        this.flash('已删除待办：' + String(t.content || '').slice(0, 30), async () => {
          const r = await req('/api/todos', { method: 'POST', body: JSON.stringify(keep) });
          if (keep.status === 'done' && r && r.id) await req('/api/todos/' + r.id, { method: 'PATCH', body: JSON.stringify({ status: 'done' }) });
          await m.fetchTodos();
        });
      },
      // T / 简报条 / 上下文区里的「加入待办」都走弹层
      async addTodo(email) {
        const m = S('mail'); const e = email || m.currentEmail; if (!e) return;
        if (this.isNoAction(e)) { this.flash('这封邮件已标记为无需处理'); return; }
        return this.openTodoDlg(e);
      },
      async addTodoDirect(email) {
        const m = S('mail'); const e = email || m.currentEmail; if (!e) return;
        if (this.isNoAction(e)) { this.flash('这封邮件已标记为无需处理'); return; }
        let items = e.action_items; if (typeof items === 'string') { try { items = JSON.parse(items); } catch (x) { items = []; } }
        const text = (Array.isArray(items) && items[0]) || ('跟进：' + (e.subject || '(无主题)'));
        return this.addTodoText(text, e.id);
      },
      async addTodoText(text, srcId, due, pri) {
        const m = S('mail');
        if ((m.todos || []).some((t) => t.status !== 'done' && (t.content || '').trim() === text.trim())) { this.flash('已在待办中：' + text); return; }
        try {
          await req('/api/todos', { method: 'POST', body: JSON.stringify({ content: text, source_email_id: srcId || null, due_date: due || null, priority: pri || 'normal' }) }); await m.fetchTodos();
          const r = srcId && this.tri.by[srcId]; if (r) r.open_todos = (r.open_todos || []).concat(((m.todos || []).find((t) => t.source_email_id === srcId && t.content === text) || {}).id || []);
          this.flash('已加入待办：' + text + (due ? '（' + this.fmtDue(due) + '）' : ''));
        }
        catch (e) { this.flash('加入待办失败', null, 'error'); }
      },
      actionItems(e) {
        let a = e && e.action_items; if (typeof a === 'string') { try { a = JSON.parse(a); } catch (x) { a = []; } }
        return Array.isArray(a) ? a.filter((x) => typeof x === 'string' && x.trim()).slice(0, 5) : [];
      },
      // ======================= 系统通知 =======================
      // 浏览器 Notification 会进操作系统通知中心（macOS / Windows）。mode：off / important（需要处理、等你回复）/ all。
      // 无需处理、静音告警、稍后提醒中的一律不提醒；AI 还没分析出优先级的新邮件最多等 10 分钟再判断。
      // 嵌入门户时浏览器不允许跨源 iframe 申请通知权限：改由桥接把新邮件推给门户（事件 notify），门户自己弹。
      _ntfStart() {
        if (this._ntfT) return;
        this._ntfPoll(true);
        this._ntfT = setInterval(() => this._ntfPoll(false), 60000);
        document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') this._ntfPoll(false); });
      },
      get ntfEmbedded() { return !!window.__QUILLBOX_EMBED; },
      _ntfSeen() {
        if (!this._ntfSeenSet) { let a = []; try { a = JSON.parse(localStorage.getItem('quillbox-v2-ntf-seen') || '[]'); } catch (e) {} this._ntfSeenSet = new Set(a); this._ntfFresh = !a.length; }
        return this._ntfSeenSet;
      },
      _ntfSave() { localStorage.setItem('quillbox-v2-ntf-seen', JSON.stringify([...this._ntfSeen()].slice(-400))); },
      async _ntfPoll(first) {
        if (this.ntf.mode === 'off' && !this._bridgeOk) return;
        let r; try { r = await req('/api/notify-feed?minutes=30' + (this.acc ? '&account=' + encodeURIComponent(this.acc) : '')); } catch (e) { return; }
        if (!r || !r.ok) return;
        const seen = this._ntfSeen(), now = r.now || Date.now() / 1000, hits = [];
        const seed = first && this._ntfFresh;   // 第一次使用：把眼前已有的当作看过，不补弹
        for (const e of r.emails || []) {
          if (seen.has(e.id)) continue;
          if (seed || e.no_action || e.muted || e.snoozed || !e.unread) { seen.add(e.id); continue; }
          const important = ['act', 'reply'].includes(e.ai_priority);
          if (!e.ai_priority && !e.system && now - (e.received_timestamp || now) < 600) continue;   // 等 AI 分析
          seen.add(e.id);
          if (this.ntf.mode === 'all' || important) hits.push(Object.assign({ important }, e));
        }
        this._ntfFresh = false; this._ntfSave();
        if (!hits.length) return;
        if (this._bridgeOk) this._post('notify', { items: hits.map((e) => ({ id: e.id, subject: e.subject, from: e.sender, important: e.important })) });
        if (this.view === 'triage') this.loadTriage(false);
        S('mail').fetchStats && S('mail').fetchStats();
        if (this.ntf.mode === 'off' || this.ntfEmbedded) return;
        if (hits.length > 3) {
          const imp = hits.filter((e) => e.important).length;
          this._ntfShow(hits.length + ' 封新邮件' + (imp ? '（' + imp + ' 封需要你处理）' : ''), hits.slice(0, 3).map((e) => e.sender + '：' + e.subject).join('\n'), null);
        } else hits.forEach((e) => this._ntfShow((e.important ? (e.ai_priority === 'reply' ? '等你回复 · ' : '需要你处理 · ') : '新邮件 · ') + e.sender, e.subject || '(无主题)', e.id));
      },
      _ntfShow(title, body, id) {
        if (!('Notification' in window) || Notification.permission !== 'granted') return;
        try {
          const n = new Notification(title, { body, tag: id ? 'quillbox-' + id : 'quillbox-batch', renotify: false });
          n.onclick = () => { try { window.focus(); } catch (e) {} if (id) { this._hlId = id; S('mail').gotoEmail(id); } else if (this.view !== 'triage') this.go('triage'); n.close(); };
        } catch (e) { /* 部分浏览器只允许在 Service Worker 里弹，失败就静默 */ }
      },
      async setNotify(mode) {
        if (mode !== 'off' && 'Notification' in window && Notification.permission === 'default') {
          try { await Notification.requestPermission(); } catch (e) {}
        }
        this.ntf.perm = 'Notification' in window ? Notification.permission : 'unsupported';
        if (mode !== 'off' && this.ntf.perm !== 'granted' && !this.ntfEmbedded) { this.flash('浏览器没有授予通知权限：请在地址栏左侧的站点设置里允许通知', null, 'error'); }
        this.ntf.mode = mode; localStorage.setItem('quillbox-v2-notify', mode);
        if (mode !== 'off') this._ntfStart();
      },
      ntfTest() { this._ntfShow('Quillbox 测试通知', '新邮件会这样提醒你；点击通知回到 Quillbox。', null); if (this.ntf.perm !== 'granted') this.flash('还没有通知权限'); },
      get ntfPermText() { return { granted: '已授权', denied: '已被浏览器拒绝（需在站点设置里改为允许）', default: '尚未授权', unsupported: '此浏览器不支持' }[this.ntf.perm] || this.ntf.perm; },

      // ======================= 手机布局（P2） =======================
      get phone() { return this.layout === 'phone'; },
      sw: { id: null, x0: 0, y0: 0, dx: 0, open: null, lp: null, moved: false },
      swStyle(id) { const s = this.sw; const dx = s.id === id ? s.dx : (s.open === id ? -144 : 0); return dx ? 'transform: translateX(' + dx + 'px)' : ''; },
      swStart(ev, id) {
        if (!this.phone) return;
        const t = ev.touches && ev.touches[0]; if (!t) return;
        const s = this.sw; s.id = id; s.x0 = t.clientX; s.y0 = t.clientY; s.dx = s.open === id ? -144 : 0; s.moved = false;
        if (s.open && s.open !== id) s.open = null;
        clearTimeout(s.lp); s.lp = setTimeout(() => { if (!s.moved) { this.showChecks = true; this.check(id); s.id = null; s.dx = 0; if (navigator.vibrate) navigator.vibrate(15); } }, 550);   // 长按：多选
      },
      swMove(ev) {
        const s = this.sw, t = ev.touches && ev.touches[0]; if (!s.id || !t) return;
        const dx = t.clientX - s.x0, dy = t.clientY - s.y0;
        if (Math.abs(dx) > 8 || Math.abs(dy) > 8) { s.moved = true; clearTimeout(s.lp); }
        if (Math.abs(dy) > Math.abs(dx)) return;            // 竖向滚动不当成左滑
        s.dx = Math.max(-160, Math.min(0, dx + (s.open === s.id ? -144 : 0)));
        if (ev.cancelable) ev.preventDefault();
      },
      swEnd() {
        const s = this.sw; clearTimeout(s.lp); if (!s.id) return;
        s.open = s.dx < -70 ? s.id : null; s.id = null; s.dx = 0;
      },
      swTap(ev, id) {   // 左滑展开时点行 = 收回，不打开
        if (this.sw.moved) { this.sw.moved = false; ev.stopPropagation(); return true; }
        if (this.sw.open) { this.sw.open = null; ev.stopPropagation(); return true; }
        return false;
      },
      swSnooze(id) { this.sw.open = null; this.openSnooze([id]); },
      swDone(id) { this.sw.open = null; this._hlId = id; S('mail').selectedId = id; this.archive([id]); },
      phoneMore: false,
      // ======================= 统一查询栏（P2） =======================
      // 条件以标签呈现；剩下的文字按关键词检索（结果来源在范围条里如实标注）；Tab 把整句交给 AI 转成条件（AI 给的标签带 ✦）。
      QB_LABEL: { from: '发件人', to: '收件人', subject: '主题', has: '有附件', after: '之后', before: '之前' },
      qbChipText(c) { return c.k === 'has' ? '有附件' : (c.k === 'after' || c.k === 'before') ? c.v.slice(5).replace('-', '/') + ' ' + this.QB_LABEL[c.k] : this.QB_LABEL[c.k] + '：' + (c.label || c.v); },
      _qbDate(t) {
        t = String(t || '').trim(); const now = new Date(); let d = null;
        let m = t.match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$/) || t.match(/^(\d{1,2})[-/.月](\d{1,2})日?$/);
        if (m) d = m.length === 4 ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(now.getFullYear(), +m[1] - 1, +m[2]);
        else if (t === '今天') d = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        else if (t === '昨天') d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
        else if (t === '本周') { d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - ((now.getDay() + 6) % 7)); }
        else if (t === '上周') { d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - ((now.getDay() + 6) % 7) - 7); }
        else if (t === '本月') d = new Date(now.getFullYear(), now.getMonth(), 1);
        if (!d || isNaN(d)) return '';
        return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
      },
      get qbSugg() {
        const q = this.qb, t = String(q.text || '').trim(); if (!t) return [];
        const out = [{ kind: 'kw', label: '关键词「' + t + '」', hint: '关键词检索' }];
        const low = t.toLowerCase();
        (S('mail').contacts || []).filter((p) => String(p.email || '').toLowerCase().includes(low) || String(p.name || '').toLowerCase().includes(low))
          .slice(0, 3).forEach((p) => out.push({ kind: 'chip', chip: { k: 'from', v: p.email, label: p.name && p.name !== p.email ? p.name : p.email }, label: '发件人：' + (p.name || p.email), hint: p.email }));
        out.push({ kind: 'chip', chip: { k: 'subject', v: t }, label: '主题包含「' + t + '」', hint: '' });
        const d = this._qbDate(t);
        if (d) { out.push({ kind: 'chip', chip: { k: 'after', v: d }, label: d + ' 之后', hint: '' }); out.push({ kind: 'chip', chip: { k: 'before', v: d }, label: d + ' 之前', hint: '' }); }
        if (/附件/.test(t)) out.push({ kind: 'chip', chip: { k: 'has', v: 'attachment' }, label: '有附件', hint: '' });
        out.push({ kind: 'ai', label: '交给 AI 理解「' + t + '」', hint: 'Tab' });
        return out;
      },
      qbInput() {
        const q = this.qb; q.idx = 0; q.focus = true;
        const m = String(q.text || '').match(/^(from|to|subject|has|after|before):("([^"]+)"|(\S+))\s$/i);   // 直接敲操作符也能变标签
        if (m) { this.qbAdd({ k: m[1].toLowerCase(), v: m[3] || m[4] }); q.text = ''; }
      },
      qbAdd(c) {
        if (c.k === 'has') c.v = 'attachment';
        if ((c.k === 'after' || c.k === 'before') && !/^\d{4}-\d{2}-\d{2}$/.test(c.v)) { const d = this._qbDate(c.v); if (!d) return; c.v = d; }
        this.qb.chips = this.qb.chips.filter((x) => !(x.k === c.k && (c.k === 'has' || c.k === 'after' || c.k === 'before' || x.v === c.v))).concat([c]);
      },
      qbRemove(i) { this.qb.chips.splice(i, 1); if (!this.qb.chips.length && !this.qb.text && this.scope && this.scope.kind === 'search') this.clearScope(); else if (this.scope && this.scope.kind === 'search') this.qbRun(); },
      qbPick(s) {
        const q = this.qb;
        if (s.kind === 'kw') { this.qbRun(); return; }
        if (s.kind === 'ai') { this.qbAi(); return; }
        this.qbAdd(Object.assign({}, s.chip)); q.text = ''; q.idx = 0; this.qbRun();
      },
      qbKey(ev) {
        const q = this.qb, L = this.qbSugg;
        if (ev.key === 'ArrowDown' && L.length) { ev.preventDefault(); q.idx = Math.min(L.length - 1, q.idx + 1); }
        else if (ev.key === 'ArrowUp' && L.length) { ev.preventDefault(); q.idx = Math.max(0, q.idx - 1); }
        else if (ev.key === 'Enter') { ev.preventDefault(); if (L.length) this.qbPick(L[q.idx] || L[0]); else this.qbRun(); }
        else if (ev.key === 'Tab' && String(q.text || '').trim()) { ev.preventDefault(); this.qbAi(); }
        else if (ev.key === 'Backspace' && !q.text && q.chips.length) { ev.preventDefault(); this.qbRemove(q.chips.length - 1); }
        else if (ev.key === 'Escape') { ev.preventDefault(); q.focus = false; ev.target.blur(); }
      },
      _qbQuery() {
        const esc = (v) => (/\s/.test(v) ? '"' + String(v).replace(/"/g, '') + '"' : v);
        return this.qb.chips.map((c) => c.k + ':' + esc(c.v)).concat([String(this.qb.text || '').trim()]).filter(Boolean).join(' ');
      },
      qbRun() {
        const q = this.qb, query = this._qbQuery(); if (!query) return;
        const label = q.chips.map((c) => (c.ai ? '✦' : '') + this.qbChipText(c)).concat(q.text.trim() ? ['关键词「' + q.text.trim() + '」'] : []).join(' · ');
        q.text = ''; q.idx = 0; q.focus = false;
        this.applySearch(query, label, 'search');
      },
      // Tab：整句交给 AI 转成条件；AI 给的标签带 ✦，可逐个删；AI 理解不了的部分保留为关键词
      async qbAi() {
        const q = this.qb, t = String(q.text || '').trim(); if (!t || q.ai) return;
        q.ai = true;
        try {
          const r = await req('/api/topics/from-nl', { method: 'POST', body: JSON.stringify({ text: t }) });
          if (!r || !r.ok) throw new Error((r && r.error) || 'nl');
          const f = r.filters || {}, add = [], skipped = [];
          if (f.from) add.push({ k: 'from', v: String(f.from) });
          if (f.has_attachment) add.push({ k: 'has', v: 'attachment' });
          if (f.after) add.push({ k: 'after', v: String(f.after).slice(0, 10) });
          if (f.before) add.push({ k: 'before', v: String(f.before).slice(0, 10) });
          (r.keywords || []).forEach((k) => add.push({ k: 'subject', v: String(k) }));
          ['to_me', 'cc_me', 'unread', 'starred', 'account', 'category'].forEach((k) => { if (f[k]) skipped.push({ to_me: '发给我', cc_me: '抄送我', unread: '未读', starred: '星标', account: '账户', category: '分类' }[k]); });
          add.forEach((c) => this.qbAdd(Object.assign(c, { ai: true })));
          q.text = (r.query || '').trim();
          if (!add.length && !q.text) q.text = t;
          if (skipped.length) this.flash('AI 还理解了「' + skipped.join('、') + '」，查询栏暂不支持这类条件，可用列表上方的筛选');
          this.qbRun();
        } catch (e) { this.flash('AI 没能理解这句话，已按关键词检索', null, 'error'); this.qbRun(); }
        finally { q.ai = false; }
      },
      // ======================= 完成说明 / 处理记录 =======================
      NOTE_OUTCOMES: ['已处理', '已回复', '已转交', '不处理', '其他'],
      // ---- 快捷原因：点一下（或按 1–9）= 选好结果 + 填好说明 + 直接完成 ----
      QR_DEFAULT: ['已处理完成', '已回复对方', '已转交他人跟进', '已电话 / 当面沟通', '已在会议上确认', '仅知悉，无需处理'],
      get quickReasons() {
        let custom = null; try { custom = JSON.parse(localStorage.getItem('quillbox-v2-qr') || 'null'); } catch (e) {}
        let recent = []; try { recent = JSON.parse(localStorage.getItem('quillbox-v2-qr-recent') || '[]'); } catch (e) {}
        const base = Array.isArray(custom) && custom.length ? custom : this.QR_DEFAULT;
        const out = base.slice();
        recent.forEach((r) => { if (out.length < 9 && !out.includes(r)) out.push(r); });   // 最近手写过的补在后面
        return out.slice(0, 9);
      },
      get quickReasonsText() { return this.quickReasons.filter((x) => !this._qrRecentOnly(x)).join('\n'); },
      _qrRecentOnly(x) { let c = null; try { c = JSON.parse(localStorage.getItem('quillbox-v2-qr') || 'null'); } catch (e) {} return !(Array.isArray(c) && c.length ? c : this.QR_DEFAULT).includes(x); },
      setQuickReasons(text) {
        const L = String(text || '').split('\n').map((x) => x.trim()).filter(Boolean).slice(0, 9);
        localStorage.setItem('quillbox-v2-qr', JSON.stringify(L)); this.flash(L.length ? '已保存 ' + L.length + ' 个快捷原因' : '已恢复默认快捷原因');
        if (!L.length) localStorage.removeItem('quillbox-v2-qr');
      },
      _qrOutcome(t) { return /回复/.test(t) ? '已回复' : /转/.test(t) ? '已转交' : /无需|知悉|不处理|忽略/.test(t) ? '不处理' : '已处理'; },
      _qrRemember(text) {
        const t = String(text || '').replace(/\s+/g, ' ').trim(); if (!t || t.length > 24 || this.quickReasons.includes(t)) return;
        let r = []; try { r = JSON.parse(localStorage.getItem('quillbox-v2-qr-recent') || '[]'); } catch (e) {}
        r = [t].concat(r.filter((x) => x !== t)).slice(0, 3); localStorage.setItem('quillbox-v2-qr-recent', JSON.stringify(r));
      },
      useQuickReason(t) {
        const d = this.noteDlg, el = document.querySelector('[data-testid=note-dlg] .v2-ned');
        const typed = el && el.innerText.trim();
        d.outcome = this._qrOutcome(t);
        if (typed || d.atts.length || d.links.length) {   // 已经写了东西 / 加了附件或关联：只补一句，不替用户提交
          if (el) { el.innerHTML = this._safeHtml(el.innerHTML) + (typed ? '<br>' : '') + t.replace(/[<>&]/g, ''); }
          return;
        }
        if (el) el.textContent = t;
        this.confirmNote();
      },
      setNoteMode(v) { this.noteMode = v; localStorage.setItem('quillbox-v2-note-mode', v); },
      // 何时先填说明：smart = 需要处理 / 等你回复才问；always；never。Shift+E 总是问
      _wantNote(ids, opts) {
        if (opts && opts.forceNote) return true;
        if (this.noteMode === 'never') return false;
        if (this.noteMode === 'always') return true;
        const m = S('mail');
        return ids.some((id) => {
          const e = this.tri.by[id] || (m.emails || []).find((x) => x.id === id) || (m.currentEmail && m.currentEmail.id === id ? m.currentEmail : null);
          return e && !this.isNoAction(e) && ['act', 'reply'].includes(this.pri(e));
        });
      },
      openNoteDlg(ids, opts) {
        this.noteDlg = { open: true, ids: ids.slice(), edit: null, after: !!(opts && opts.after), outcome: '', atts: [], rm: [], links: [], q: '', results: [], searching: false, busy: false };
        setTimeout(() => { const el = document.querySelector('[data-testid=note-dlg] .v2-ned'); if (el) { el.innerHTML = ''; } const dlg = document.querySelector('[data-testid=note-dlg]'); if (dlg) dlg.focus(); }, 30);
      },
      openNoteEdit(n) {
        this.noteDlg = { open: true, ids: [n.email_id], edit: n.id, after: true, outcome: n.outcome || '', atts: (n.attachments || []).map((a) => ({ name: a.name, size: a.size, existing: true })),
          rm: [], links: (n.links || []).slice(), q: '', results: [], searching: false, busy: false };
        setTimeout(() => { const el = document.querySelector('[data-testid=note-dlg] .v2-ned'); if (el) el.innerHTML = this._safeHtml(n.note_html || ''); }, 0);
      },
      noteCmd(cmd) {
        const el = document.querySelector('[data-testid=note-dlg] .v2-ned'); if (!el) return; el.focus();
        if (cmd === 'link') { const u = prompt('链接地址（http/https）'); if (u && /^https?:\/\//i.test(u)) document.execCommand('createLink', false, u); return; }
        document.execCommand(cmd, false, null);
      },
      async noteAttach(ev) {
        const files = [...(ev.target.files || [])]; ev.target.value = '';
        for (const f of files) {
          if (f.size > 25 * 1024 * 1024) { this.flash('附件超过 25MB：' + f.name, null, 'error'); continue; }
          const item = { name: f.name, size: f.size, token: '', uploading: true }; this.noteDlg.atts.push(item);
          try {
            const b64 = await new Promise((ok, no) => { const r = new FileReader(); r.onload = () => ok(String(r.result)); r.onerror = no; r.readAsDataURL(f); });
            const r = await req('/api/notes/upload', { method: 'POST', body: JSON.stringify({ name: f.name, data: b64 }) });
            if (!r || !r.ok) throw new Error('up');
            item.token = r.token; item.name = r.name; item.uploading = false;
          } catch (e) { this.noteDlg.atts = this.noteDlg.atts.filter((x) => x !== item); this.flash('附件上传失败：' + f.name, null, 'error'); }
        }
      },
      noteRmAtt(a) { const d = this.noteDlg; if (a.existing) d.rm.push(a.name); d.atts = d.atts.filter((x) => x !== a); },
      noteSearch() {
        clearTimeout(this._nsT); const d = this.noteDlg, qv = String(d.q || '').trim();
        if (!qv) { d.results = []; return; }
        this._nsT = setTimeout(async () => {
          d.searching = true;
          try {
            const r = await req('/api/emails?light=1&q=' + encodeURIComponent(qv));
            if (d.q.trim() !== qv) return;
            const skip = new Set(d.ids.concat(d.links.map((l) => l.id)));
            d.results = ((r && r.emails) || []).filter((e) => !skip.has(e.id)).slice(0, 8).map((e) => ({ id: e.id, subject: e.subject || '(无主题)', sender: e.sender || e.sender_email || '', time: e.time }));
          } catch (e) { d.results = []; } finally { d.searching = false; }
        }, 300);
      },
      noteAddLink(r) { const d = this.noteDlg; if (!d.links.some((l) => l.id === r.id)) d.links.push(r); d.q = ''; d.results = []; },
      async confirmNote() {
        const d = this.noteDlg; if (d.busy) return;
        if (d.atts.some((a) => a.uploading)) { this.flash('附件仍在上传，请稍候…'); return; }
        const el = document.querySelector('[data-testid=note-dlg] .v2-ned');
        const html = el ? el.innerHTML : '';
        if (el && !d.edit) this._qrRemember(el.innerText);
        const payload = { outcome: d.outcome, note_html: (el && el.innerText.trim()) ? html : '', links: d.links.map((l) => l.id),
          attachments: d.atts.filter((a) => a.token).map((a) => a.token) };
        d.busy = true;
        try {
          if (d.edit) {
            await req('/api/notes/' + d.edit, { method: 'PATCH', body: JSON.stringify(Object.assign(payload, { remove_attachments: d.rm })) });
            d.open = false; this.flash('已更新处理记录'); this._loadNotes(d.ids[0], true); return;
          }
          const empty = !payload.outcome && !payload.note_html && !payload.links.length && !payload.attachments.length;
          const noteIds = [];
          if (!empty) {
            for (const id of d.ids) {   // 批量完成：每封各记一条（内容相同；附件只挂第一封，其余在说明里引用）
              const body = noteIds.length ? Object.assign({}, payload, { attachments: [] }) : payload;
              const r = await req('/api/emails/' + encodeURIComponent(id) + '/notes', { method: 'POST', body: JSON.stringify(body) });
              if (r && r.id) noteIds.push(r.id);
            }
          }
          d.open = false;
          if (d.after) { this.flash(noteIds.length ? '已补充处理说明' : '未填写，未保存'); const c = S('mail').currentEmail; if (c && d.ids.includes(c.id)) this._loadNotes(c.id, true); return; }
          await this.archive(d.ids, { skipNote: true, noteIds });
        } catch (e) { this.flash('保存处理记录失败', null, 'error'); }
        finally { d.busy = false; }
      },
      noteKey(ev) {
        const t = ev.target || {};
        if (ev.key === 'Enter' && (ev.metaKey || ev.ctrlKey)) { ev.preventDefault(); this.confirmNote(); return; }
        const inField = (t.classList && t.classList.contains('v2-ned')) || (t.dataset && t.dataset.nq !== undefined) || /INPUT|TEXTAREA/.test(t.tagName || '');
        if (!inField && !this.noteDlg.edit && /^[1-9]$/.test(ev.key)) { const r = this.quickReasons[+ev.key - 1]; if (r) { ev.preventDefault(); this.useQuickReason(r); } return; }
        if (ev.key !== 'Enter') return;
        if (t.classList && t.classList.contains('v2-ned')) return;                  // 编辑器里回车 = 换行
        if (t.dataset && t.dataset.nq !== undefined) { ev.preventDefault(); if (this.noteDlg.results[0]) this.noteAddLink(this.noteDlg.results[0]); return; }
        ev.preventDefault(); this.confirmNote();                                    // 其余位置回车 = 确认（可留空直接完成）
      },
      async _loadNotes(id, force) {
        if (!id) { this.notes = { id: null, list: [], back: [] }; return; }
        if (!force && this.notes.id === id) return;
        this.notes = { id, list: [], back: [] };
        try { const r = await req('/api/emails/' + encodeURIComponent(id) + '/notes'); if (this.notes.id === id && r && r.ok) { this.notes.list = r.notes || []; this.notes.back = r.linked_from || []; } }
        catch (e) { /* 处理记录可缺省 */ }
      },
      async deleteNote(n) {
        try { await req('/api/notes/' + n.id, { method: 'DELETE' }); } catch (e) { this.flash('删除失败', null, 'error'); return; }
        this._loadNotes(n.email_id, true);
        this.flash('已删除处理记录', async () => { await req('/api/notes/' + n.id, { method: 'PATCH', body: JSON.stringify({ deleted: 0 }) }); this._loadNotes(n.email_id, true); });
      },
      noteAttUrl(n, a) { return '/api/notes/' + n.id + '/attachment?name=' + encodeURIComponent(a.name); },
      fmtSize(b) { return b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round((b || 0) / 1024)) + ' KB'; },
      // 处理记录正文只放行少量排版标签；链接仅 http/https/mailto，新窗口打开
      _safeHtml(html) {
        const ok = new Set(['B', 'STRONG', 'I', 'EM', 'U', 'S', 'P', 'BR', 'DIV', 'SPAN', 'UL', 'OL', 'LI', 'A', 'BLOCKQUOTE', 'CODE', 'PRE', 'H3', 'H4']);
        const doc = new DOMParser().parseFromString('<div>' + String(html || '') + '</div>', 'text/html');
        const walk = (node) => {
          [...node.childNodes].forEach((c) => {
            if (c.nodeType === 8) { c.remove(); return; }
            if (c.nodeType !== 1) return;
            if (['SCRIPT', 'STYLE', 'IFRAME', 'OBJECT', 'EMBED', 'svg', 'SVG', 'MATH', 'TEMPLATE', 'FORM', 'INPUT', 'IMG', 'VIDEO', 'AUDIO'].includes(c.tagName)) { c.remove(); return; }
            walk(c);
            if (!ok.has(c.tagName)) { c.replaceWith(...c.childNodes); return; }
            const href = c.tagName === 'A' ? c.getAttribute('href') : null;
            [...c.attributes].forEach((a) => c.removeAttribute(a.name));
            if (href && /^(https?:|mailto:)/i.test(href.trim())) { c.setAttribute('href', href.trim()); c.setAttribute('target', '_blank'); c.setAttribute('rel', 'noopener noreferrer'); }
          });
        };
        const root = doc.body.firstChild; walk(root); return root.innerHTML;
      },

      // ======================= 上下文区（P1-F） =======================
      toggleCtx() { this.ctxOpen = !this.ctxOpen; const c = S('mail').currentEmail; if (this.ctxOpen && c && this.ctx.id !== c.id) this._loadCtx(c.id); },
      async _loadCtx(id) {
        const c = S('mail').currentEmail; if (!c || c.id !== id) return;
        this.ctx = { id, thread: [], sender: [], loading: true };
        try {
          const addr = String(c.sender_email || '').trim();
          const jobs = [this.thread.id === id && this.thread.messages ? Promise.resolve({ messages: this.thread.messages }) : req('/api/emails/' + encodeURIComponent(id) + '/thread')];
          jobs.push(addr ? req('/api/emails?light=1&q=' + encodeURIComponent('from:' + addr + ' after:' + this._isoDay(-30))) : Promise.resolve({ emails: [] }));
          const [th, sd] = await Promise.all(jobs.map((p) => p.catch(() => null)));
          if (this.ctx.id !== id) return;
          this.ctx.thread = ((th && th.messages) || []).filter((x) => x.id !== id).slice(-10).reverse();
          const tids = new Set(this.ctx.thread.map((x) => x.id));
          this.ctx.sender = ((sd && sd.emails) || []).filter((x) => x.id !== id && !tids.has(x.id))
            .sort((a, b) => (b.received_timestamp || 0) - (a.received_timestamp || 0)).slice(0, 10);
        } finally { if (this.ctx.id === id) this.ctx.loading = false; }
      },
      get ctxTodos() { const c = S('mail').currentEmail; return c ? (S('mail').todos || []).filter((t) => t.source_email_id === c.id) : []; },
      get ctxSuggest() {
        const c = S('mail').currentEmail; if (!c || this.isNoAction(c)) return [];
        const norm = (x) => String(x || '').replace(/\s+/g, '').toLowerCase();
        const have = new Set(this.ctxTodos.map((t) => norm(t.content)));
        const ins = this.curInsight, acts = ins && Array.isArray(ins.actions) && ins.actions.length ? ins.actions.map((a) => a.text) : this.paneTodos(c);
        return [...new Set(acts)].filter((x) => !have.has(norm(x)));
      },
      // 从右栏（系列 / 同话题 / 发件人往来 / 被引用）打开：只打开，不往当前列表里塞行——之前会在收件箱顶上多出一封「(未知发件人)」，要重新点收件箱才消失
      ctxGo(x) { if (!x || !x.id) return; S('mail').gotoEmail(x.id); if (this.narrow) this.aiPaneOpen = false; },

      // ======================= AI 简报条（P1-B） =======================
      get brief() {
        const m = S('mail'), e = m.currentEmail; if (!e) return null;
        const mine = this.insight.id === e.id, ins = mine ? this.insight.data : null;
        const noAct = this.isNoAction(e);
        const todosOf = (m.todos || []).filter((t) => t.source_email_id === e.id);
        const norm = (x) => String(x || '').replace(/\s+/g, '').toLowerCase();
        const actions = (ins && Array.isArray(ins.actions) && ins.actions.length ? ins.actions : ((ins && ins.todos) || []).map((t) => ({ text: t, owner: '' })))
          .map((a) => ({ text: a.text, owner: a.owner || '', todo: todosOf.find((t) => norm(t.content) === norm(a.text)) || null,
                         notMine: ((ins && ins.action_states) || {})[this.akey(a.text)] === 'not_mine' }));
        const added = actions.filter((a) => a.todo).length, notMine = actions.filter((a) => !a.todo && a.notMine).length;
        const left = actions.length - added - notMine;
        const loading = mine && this.insight.loading && !ins, err = mine ? this.insight.error : '';
        let state = 'ok';
        if (loading) state = 'loading';
        else if (!ins && err) state = 'failed';
        else if (noAct || (ins && ['fyi', 'notice'].includes(ins.priority) && !actions.length)) state = 'none';
        else if (actions.length && !left) state = 'done';
        else if (actions.length && (added || notMine)) state = 'partial';
        const t = ins && ins.created_at ? new Date(ins.created_at * 1000) : null;
        const when = t ? ((t.toDateString() === new Date().toDateString() ? '' : (t.getMonth() + 1) + '/' + t.getDate() + ' ') + pad(t.getHours()) + ':' + pad(t.getMinutes()) + ' 分析') : '';
        const headline = (ins && (ins.headline || ins.summary)) || this.summaryParts.head || '', meta = (ins && ins.meta) || '';
        const anorm = new Set(actions.map((a) => norm(a.text)));
        // 要点里「键：值」形式的并入关键信息（同名键去重），其余才是要点
        const facts = this._facts(meta), fk = new Set(facts.filter((f) => f.k).map((f) => f.k)), fv = new Set(facts.map((f) => norm(f.v)));
        const points = [];
        ((ins && ins.points) || []).map((p) => String(p || '').trim()).filter((p) => p && norm(p) !== norm(headline) && !anorm.has(norm(p))).forEach((p) => {
          const f = this._facts(p)[0];
          if (f && f.k && this._facts(p).length === 1) { if (!fk.has(f.k) && facts.length < 8) { facts.push(f); fk.add(f.k); } return; }
          if (!fv.has(norm(p))) points.push(p);
        });
        return { pri: this.priCode(e), cls: this.pri(e), state, loading, error: err, stale: !!(ins && ins.stale), when, facts, points: points.slice(0, 4),
          headline: (ins && (ins.headline || ins.summary)) || this.summaryParts.head || '', summary: (ins && ins.summary) || this.summaryParts.head || '',
          meta: (ins && ins.meta) || '', actions, left, progress: actions.length + ' 项行动：' + [added && added + ' 项已加入', notMine && notMine + ' 项不是你的', left && left + ' 项待确认'].filter(Boolean).join('，'),
          due: (ins && ins.due) || '', dueSrc: (ins && ins.due_src) || '', next: (ins && ins.next) || '', link: (ins && ins.link) || '',
          todo: noAct ? '' : ((actions.find((a) => !a.todo) || {}).text || ''), noAct,
          alertRow: (this.tri.by[e.id] && this.tri.by[e.id].alert_key) ? this.tri.by[e.id] : null };
      },
      get pendingActions() { const b = this.brief; return b ? b.left : 0; },
      // AI 给的「部门：…；流转节点：…」拆成键值；没有冒号的整段原样作为一行
      _facts(meta) {
        return String(meta || '').split(/[；;]\s*|\n+/).map((s) => s.trim()).filter(Boolean).slice(0, 8)
          .map((s) => {   // 「13:58」这类时间里的冒号不是键值分隔：键不能以数字结尾
            const m = s.match(/^([^：:]{1,12})[：:]\s*(.+)$/);
            return m && !/\d$/.test(m[1].trim()) ? { k: m[1].trim(), v: m[2] } : { k: '', v: s };
          });
      },
      // ---- 右栏「行动」：行动项与待办只在这里处理----
      get ctxActs() { const b = this.brief; return b ? b.actions : []; },
      // 负责人只在「不是你」时显示（收件人 / 我 / 本人 / 你的账户名 = 你，不占位置）；没写明的放在悬停提示里
      actOwner(a) {
        const o = String((a && a.owner) || '').trim(); if (!o) return '';
        if (/^(收件人|我|本人|你|自己|您)$/.test(o)) return '';
        const acc = String(((S('mail').currentEmail || {}).account) || '').toLowerCase().split('@')[0];
        return acc && o.toLowerCase() === acc ? '' : o;
      },
      get ctxOtherTodos() {   // 这封邮件加入过、但不对应任何 AI 行动项的待办（手动加的 / 整封加入的）
        const norm = (x) => String(x || '').replace(/\s+/g, '').toLowerCase(), have = new Set(this.ctxActs.map((a) => norm(a.text)));
        return this.ctxTodos.filter((t) => !have.has(norm(t.content)));
      },
      get actHint() {
        const b = this.brief; if (!b || !b.actions.length) return '';
        const n = b.actions.length, done = n - b.left;
        return (done ? b.progress : n + ' 项行动') + (this.ctxDrawer ? '' : ' · 在右侧处理');
      },
      openActs() {
        if (this.ctxDrawer && !this.ctxOpen) this.toggleCtx();
        setTimeout(() => {
          const el = document.querySelector('[data-testid=ctx-todos]'); if (!el) return;
          el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
          el.classList.add('hl'); setTimeout(() => el.classList.remove('hl'), 1200);
        }, 60);
      },
      async addAllActs() {
        const e = S('mail').currentEmail, b = this.brief; if (!e || !b) return;
        const todo = b.actions.filter((a) => !a.todo && !a.notMine); if (!todo.length) return;
        const ids = [];
        try {
          for (const a of todo) {
            const r = await req('/api/todos', { method: 'POST', body: JSON.stringify({ content: a.text, source_email_id: e.id, due_date: b.due || null, priority: 'normal' }) });
            if (r && r.id) ids.push(r.id);
          }
        } catch (x) { this.flash('部分加入失败', null, 'error'); }
        await S('mail').fetchTodos();
        if (this.view === 'triage') this.loadTriage(false);
        this.flash('已加入 ' + ids.length + ' 项待办' + (b.due ? '（截止 ' + this.fmtDue(b.due) + '）' : ''), ids.length ? async () => {
          for (const id of ids) await req('/api/todos/' + id, { method: 'DELETE' });
          await S('mail').fetchTodos(); if (this.view === 'triage') this.loadTriage(false);
        } : null);
      },
      akey(t) { return String(t || '').replace(/\s+/g, '').toLowerCase().slice(0, 160); },
      // 行动项「不是我的」：可撤销；全部处理（加入待办 / 不是我的）后，今日分拣把这封移出「需要你处理」
      async setNotMine(a, on) {
        const e = S('mail').currentEmail; if (!e) return;
        const k = this.akey(a.text), ins = this.insight.id === e.id ? this.insight.data : null;
        const apply = (v) => { if (ins) { const st = Object.assign({}, ins.action_states || {}); if (v) st[k] = 'not_mine'; else delete st[k]; ins.action_states = st; } };
        try { await req('/api/emails/' + encodeURIComponent(e.id) + '/actions', { method: 'POST', body: JSON.stringify({ text: a.text, state: on ? 'not_mine' : null }) }); }
        catch (x) { this.flash('操作失败', null, 'error'); return; }
        apply(on);
        if (this.view === 'triage') this.loadTriage(false);
        if (on) this.flash('已标为不是你的：' + String(a.text).slice(0, 24), async () => {
          await req('/api/emails/' + encodeURIComponent(e.id) + '/actions', { method: 'POST', body: JSON.stringify({ text: a.text, state: null }) }); apply(false);
          if (this.view === 'triage') this.loadTriage(false);
        });
      },
      // 「判断有误，我要处理」：这封改为需要处理；同一发件人 + 同一主题模板之后也按它处理（设置 › AI 规则可撤销）
      async markWrong() {
        const e = S('mail').currentEmail; if (!e) return;
        const ins = this.insight.id === e.id ? this.insight.data : null, was = (ins && ins.priority) || e.ai_priority || 'fyi';
        let r; try { r = await req('/api/corrections', { method: 'POST', body: JSON.stringify({ email_id: e.id, to: 'act' }) }); } catch (x) { this.flash('操作失败', null, 'error'); return; }
        if (ins) ins.priority = 'act'; this._setRowPri(e.id, 'act');
        if (this.view === 'triage') this.loadTriage(false);
        this.flash('已改为需要处理；同类邮件以后也这样判断', async () => {
          await req('/api/corrections/' + r.id + '?restore=' + encodeURIComponent(was) + '&email_id=' + encodeURIComponent(e.id), { method: 'DELETE' });
          if (ins) ins.priority = was; this._setRowPri(e.id, was); if (this.view === 'triage') this.loadTriage(false);
        });
      },
      // ---- 系列 ----
      _rowOf(id) { const m = S('mail'); return this.tri.by[id] || (m.emails || []).find((x) => x.id === id) || null; },
      async _loadSeries(id) {
        const r = id && this._rowOf(id);
        // 从系列里点开的往期：行对象没带 series_key，但它在当前系列里——保持系列不变（之前会把整个系列区清掉，无法返回、无法再选）
        const k = (r && r.series_key) || (this.series.key && this.series.items.some((x) => x.id === id) ? this.series.key : '');
        if (r && k && !r.series_key) { r.series_key = k; r.series_n = this.series.n; }
        if (!k) { this.series = { key: '', n: 0, items: [], loading: false }; this.seriesBack = []; return; }
        if (this.series.key !== k) this.seriesBack = [];
        if (this.series.key === k && this.series.items.length) return;
        this.series = { key: k, n: r.series_n || 0, items: [], loading: true };
        try { const d = await req('/api/series?key=' + encodeURIComponent(k) + '&cur=' + encodeURIComponent(id)); if (this.series.key === k) { this.series.items = d.items || []; this.series.n = d.n || this.series.n; } }
        finally { if (this.series.key === k) this.series.loading = false; }
      },
      // 在系列各期之间跳：带上系列信息；记住来处，可以一步步返回
      seriesGo(x) {
        const c = S('mail').currentEmail; if (!x || (c && c.id === x.id)) return;
        if (c) this.seriesBack.push({ id: c.id, subject: c.subject, time: c.time, received_timestamp: c.received_timestamp });
        this.ctxGo(Object.assign({}, x, { series_key: this.series.key, series_n: this.series.n }));
      },
      seriesReturn() {
        const p = this.seriesBack.pop(); if (!p) return;
        this.ctxGo(Object.assign({}, p, { series_key: this.series.key, series_n: this.series.n }));
      },
      get seriesTitle() { const it = this.series.items[0]; return it ? String(it.subject || '').replace(/^((re|fw|fwd|回复|答复|转发)\s*[:：]\s*)+/i, '') : ''; },
      seriesDate(x) { const d = new Date((x.received_timestamp || 0) * 1000); return (d.getMonth() + 1) + '/' + d.getDate(); },
      async splitSeries() {
        const k = this.series.key; if (!k) return;
        try { await req('/api/series/split', { method: 'POST', body: JSON.stringify({ key: k, label: this.seriesTitle }) }); } catch (x) { this.flash('操作失败', null, 'error'); return; }
        this.series = { key: '', n: 0, items: [], loading: false };
        if (this.view === 'triage') this.loadTriage(false); else S('mail').fetchEmails();
        this.flash('已拆分：这些邮件不再按系列折叠', async () => { await req('/api/series/split?key=' + encodeURIComponent(k), { method: 'DELETE' }); if (this.view === 'triage') this.loadTriage(false); else S('mail').fetchEmails(); });
      },
      // ---- 阅读区「变化」卡----
      // 本期固定为正在读的这封，只选对比基准（默认上期）。想比两期旧的：打开其中较新的一期再比。
      CMP_STATUS: { done: ['已完成', 'ok'], progress: ['本期有进展', 'prog'], open: ['进行中（你的待办）', 'open'], unknown: ['状态未知', 'unk'] },
      cmpDate(t) { const d = parseT(t); return d ? (d.getMonth() + 1) + '/' + d.getDate() : ''; },
      seriesDateY(x) {
        const d = new Date(((x && x.received_timestamp) || 0) * 1000), y = d.getFullYear();
        return (y !== new Date().getFullYear() ? y + '/' : '') + (d.getMonth() + 1) + '/' + d.getDate();
      },
      // 右栏日期：间隔明显长于平常（> 2.5 倍中位数且 ≥ 3 周）的地方插一个「… n 个月 …」
      get seriesChips() {
        const L = (this.series.items || []).slice(0, 18), out = [];
        const gaps = L.slice(1).map((x, i) => ((L[i].received_timestamp || 0) - (x.received_timestamp || 0)) / 86400);
        const med = gaps.length ? gaps.slice().sort((a, b) => a - b)[Math.floor(gaps.length / 2)] : 0;
        L.forEach((x, i) => {
          if (i > 0) {
            const g = gaps[i - 1];
            if (g >= 21 && g > med * 2.5) out.push({ k: 'g' + x.id, gap: '… ' + (g >= 45 ? Math.round(g / 30) + ' 个月' : Math.round(g / 7) + ' 周') + ' …' });
          }
          out.push({ k: 's' + x.id, it: x });
        });
        return out;
      },
      get chgBases() {   // 可选基准：比本期更早的各期（新→旧）
        const L = this.series.items || [], i = L.findIndex((x) => x.id === this.chg.id);
        return i < 0 ? [] : L.slice(i + 1);
      },
      get chgBase() { return (this.series.items || []).find((x) => x.id === this.chg.base) || null; },
      chgRel(x) {
        const L = this.series.items || [], i = L.findIndex((y) => y.id === this.chg.id), j = L.indexOf(x);
        if (!x || i < 0) return '';
        if (j === i + 1) return '上期';
        const days = Math.round(((L[i].received_timestamp || 0) - (x.received_timestamp || 0)) / 86400);
        if (days < 14) return days + ' 天前';
        if (days < 60) return Math.round(days / 7) + ' 周前';
        if (days < 330) return '约 ' + Math.round(days / 30) + ' 个月前';
        return '约 ' + Math.round(days / 36.5) / 10 + ' 年前';
      },
      get chgWord() { const b = this.chgBase; return b && this.chgRel(b) !== '上期' ? '那期' : '上期'; },
      async _loadChg(id, base, refresh, sk) {
        const L = this.series.items || [], i = L.findIndex((x) => x.id === id);
        sk = sk || this.chg.sk;
        if (i < 0 || i + 1 >= L.length) { this.chg = Object.assign({ id: '', sk: '', base: '', tok: '', loading: false, error: '', data: null, menu: false, dlg: false, none: true }, { id, sk }); return; }   // 不在系列里 / 第一期：没有可比的
        base = base || L[i + 1].id;
        const tok = id + '|' + base + '|' + Date.now();
        const keep = refresh && this.chg.id === id && this.chg.base === base;
        this.chg = { id, sk, base, tok, loading: true, error: '', data: keep ? this.chg.data : null, menu: false, dlg: this.chg.id === id && this.chg.dlg, none: false };
        try {
          const r = await req('/api/series/compare', { method: 'POST', body: JSON.stringify({ email_id: id, base_id: base, refresh: !!refresh }) });
          if (this.chg.tok !== tok) return;
          if (r && r.ok) { this.chg.data = r; const it = L.find((x) => x.id === base); if (it) it.cmp = true; }
          else this.chg.error = (r && r.error) || '对比失败';
        } catch (e) { if (this.chg.tok === tok) this.chg.error = 'AI 服务本次未返回结果'; }
        finally { if (this.chg.tok === tok) this.chg.loading = false; }
      },
      async _prepChg(id, sk) {
        const L = this.series.items || [], i = L.findIndex((x) => x.id === id);
        if (i < 0 || i + 1 >= L.length) { this.chg = Object.assign({ id: '', sk: '', base: '', tok: '', loading: false, error: '', data: null, menu: false, dlg: false, none: true }, { id, sk }); return; }
        const prev = L[i + 1];
        this.chg = Object.assign({ id: '', sk: '', base: '', tok: '', loading: false, error: '', data: null, menu: false, dlg: false, none: true }, { id, sk, base: prev.id, none: false });
        try {   // 「已对比」标记相对本期：在系列里换期时要重取一次（很快，不调 AI）
          const d = await req('/api/series?key=' + encodeURIComponent(this.series.key) + '&cur=' + encodeURIComponent(id));
          const m = {}; (d.items || []).forEach((x) => { m[x.id] = x; });
          L.forEach((x) => { if (m[x.id]) { x.cmp = !!m[x.id].cmp; x.headline = m[x.id].headline || x.headline; } });
        } catch (e) { /* 取不到就当没算过：入口照常，点开再算 */ }
        if (this.chg.id === id && prev.cmp && !this.chg.data && !this.chg.loading) this._loadChg(id, prev.id, false, sk);   // 命中缓存，不调 AI
      },
      chgOpen() {
        if (!this.chg.id || this.chg.none) return;
        this.chg.dlg = true;
        if (!this.chg.data && !this.chg.loading) this._loadChg(this.chg.id, this.chg.base);
      },
      get chgBaseName() { const b = this.chgBase; if (!b) return ''; const r = this.chgRel(b); return r === '上期' ? '上期 ' + this.seriesDateY(b) : this.seriesDateY(b) + '（' + r + '）'; },
      chgOpenBase() { const b = this.chgBase; this.chg.dlg = false; if (b) this.seriesGo(b); },
      get chgEntryLabel() {
        const b = this.chgBase; if (!b) return '';
        return (this.chg.data ? '变化：与 ' : '看与 ') + this.chgBaseName + (this.chg.data ? ' 相比' : ' 的变化');
      },
      get chgTeaser() {
        if (this.chg.data) return this.chgSummary;
        if (this.chg.loading) return '正在对比…';
        return this.chg.error ? '上次对比失败，点开可重试' : '点开后逐条比对两期正文（约 15 秒，结果会缓存）';
      },
      chgPick(x) { this.chg.menu = false; if (x && x.id !== this.chg.base) this._loadChg(this.chg.id, x.id); },
      chgRefresh() { this._loadChg(this.chg.id, this.chg.base, true); },
      async chgMenu() {
        this.chg.menu = !this.chg.menu;
        if (!this.chg.menu) return;
        try {   // 「已对比」标记随本期变化，打开列表时刷新一次
          const d = await req('/api/series?key=' + encodeURIComponent(this.series.key) + '&cur=' + encodeURIComponent(this.chg.id));
          const m = {}; (d.items || []).forEach((x) => { m[x.id] = x; });
          (this.series.items || []).forEach((x) => { if (m[x.id]) { x.cmp = !!m[x.id].cmp; x.headline = m[x.id].headline || x.headline; } });
        } catch (e) { /* 标记只是提示，取不到不影响选择 */ }
      },
      get chgSummary() {
        const d = this.chg.data; if (!d) return '';
        const f = d.followups || [], cnt = (s) => f.filter((x) => x.status === s).length;
        const sub = [['done', '已完成'], ['progress', '有进展'], ['open', '进行中'], ['unknown', '状态未知']].filter(([k]) => cnt(k)).map(([k, t]) => t + ' ' + cnt(k)).join(' / ');
        return ['新增 ' + (d.new || []).length, '延续 ' + (d.continued || []).length,
          this.chgWord + '行动 ' + f.length + (sub ? '（' + sub + '）' : '')].join(' · ');
      },
      // 收起时只露最要紧的 3 条：上期行动里有进展 / 完成 / 进行中的，其次本期新增
      get chgPreview() {
        const d = this.chg.data; if (!d) return [];
        const rank = { progress: 0, done: 1, open: 2, unknown: 4 };
        const fu = (d.followups || []).map((f) => ({ cls: 'cs-' + this.CMP_STATUS[f.status][1], tag: this.CMP_STATUS[f.status][0], text: f.text, sub: f.progress || f.source, r: rank[f.status] }));
        const nw = (d.new || []).map((x) => ({ cls: 'cs-new', tag: '新增', text: x.text, sub: '', r: 3 }));
        return fu.concat(nw).sort((a, b) => a.r - b.r).slice(0, 3);
      },
      get chgTotal() { const d = this.chg.data; return d ? (d.new || []).length + (d.continued || []).length + (d.followups || []).length : 0; },
      // ---- 设置 › AI 规则 ----
      async loadAiRules() { this.aiRules.loading = true; try { const r = await req('/api/ai-rules'); this.aiRules.items = (r && r.items) || []; } finally { this.aiRules.loading = false; } },
      AIR_TYPES: { stop: '停止提取', sender: '发件人规则', correction: '判断有误', mute: '告警静音', split: '拆分系列' },
      async aiRuleUndo(it) {
        const enc = encodeURIComponent; let back = null;
        try {
          if (it.type === 'stop') { const r = await req('/api/ai-rules/stop?name=' + enc(it.id), { method: 'DELETE' }); const rule = (r.removed || [])[0]; back = rule && (() => req('/api/ai-rules/stop/restore', { method: 'POST', body: JSON.stringify({ rule }) })); }
          else if (it.type === 'correction') { await req('/api/corrections/' + it.id, { method: 'DELETE' }); back = () => req('/api/corrections', { method: 'POST', body: JSON.stringify({ email_id: it.email_id, to: it.to }) }); }
          else if (it.type === 'mute') { await req('/api/mutes/' + it.id, { method: 'DELETE' }); }
          else if (it.type === 'split') { await req('/api/series/split?key=' + enc(it.id), { method: 'DELETE' }); back = () => req('/api/series/split', { method: 'POST', body: JSON.stringify({ key: it.id, label: it.scope }) }); }
          else if (it.type === 'sender') { this.settingsTab = 'rules'; return; }
        } catch (x) { this.flash('撤销失败', null, 'error'); return; }
        await this.loadAiRules();
        this.flash('已撤销：' + it.scope, back ? async () => { await back(); await this.loadAiRules(); } : null);
      },
      async aiRuleEdit(it, v) {
        try {
          if (it.type === 'stop') await req('/api/ai-rules/stop', { method: 'PATCH', body: JSON.stringify({ name: it.id, skip: v === 'skip' }) });
          else if (it.type === 'correction') await req('/api/corrections/' + it.id, { method: 'PATCH', body: JSON.stringify({ to: v }) });
        } catch (x) { this.flash('修改失败', null, 'error'); return; }
        await this.loadAiRules(); this.flash('已修改：' + it.scope);
      },
      // 发件人显示通讯录姓名（没有则用邮件头里的名字），悬停看邮箱
      who(e) {
        if (!e) return '';
        const addr = String(e.sender_email || '').toLowerCase();
        const c = addr && (S('mail').contacts || []).find((p) => String(p.email || '').toLowerCase() === addr);
        const n = (c && c.name && c.name !== c.email) ? c.name : (e.sender && e.sender !== e.sender_email ? e.sender : '');
        return n || e.sender_email || e.sender || '(未知发件人)';
      },
      briefTodo() { const b = this.brief, e = S('mail').currentEmail; if (!b || !e) return; this.openTodoDlg(e, b.todo || ''); },
      briefOpenLink() {
        const b = this.brief; if (!b || !/^https?:\/\//i.test(b.link)) return;
        if (window.__QUILLBOX_EMBED && this._bridgeOk) { this._post('open-external', { url: b.link }); return; }
        window.open(b.link, '_blank', 'noopener,noreferrer');
      },
      briefDone() { const e = S('mail').currentEmail; if (!e) return; this.archive([e.id]); },
      linkHost(u) { try { return new URL(u).host; } catch (e) { return ''; } },
      // AI 摘要卡：一句话用库里已有 summary；要点取流式摘要里的条目
      get summaryParts() {
        const m = S('mail'), ai = S('ai'), e = m.currentEmail;
        if (!e) return { head: '', points: [], loading: false };
        const ins = this.insight.id === e.id ? this.insight.data : null;
        if (ins && ins.summary) return { head: ins.summary, points: ins.points || [], loading: false };
        const stream = ai.summaryEmailId === e.id ? stripMd(ai.summary) : '';
        const lines = stream.split(/\n+/).map((l) => l.replace(/^\s*(?:[-*·•]|\d+[.)、])\s*/, '').trim()).filter(Boolean);
        const head = (e.summary && stripMd(e.summary).split(/\n+/).map((l) => l.replace(/^\s*[-*·•]\s*/, '').trim()).filter(Boolean)[0]) || lines[0] || '';
        const points = lines.filter((l) => l !== head && l.length > 3 && !/^(摘要|结构化摘要|要点|总结)[:：]?$/.test(l)).slice(0, 5);
        return { head, points, loading: (ai.summaryLoading && ai.summaryEmailId === e.id) || this.insight.loading };
      },
      get tones() { return TONES; },

      // ======================= AI 结构化分析 =======================
      async _loadInsight(id, refresh) {
        if (!id) { this.insight = { id: null, data: null, loading: false, error: '' }; return; }
        this.insight = { id, data: refresh ? this.insight.data : null, loading: true, error: '' };
        const u = '/api/emails/' + encodeURIComponent(id) + '/insight';
        try {
          let r = refresh ? null : await req(u);
          if (this.insight.id !== id) return;
          const cur = S('mail').currentEmail;
          if ((!r || !r.insight) && cur && cur.id === id && (cur.tags || []).includes('不分析')) { this.insight.error = ''; this.insight.data = null; return; }
          if (!r || !r.ok || !r.insight || r.insight.stale) {
            // 没有缓存才真正调 AI；先停留 600ms 再发，J/K 快速翻过的邮件不会白白各调一次网关
            if (!refresh) { await new Promise((ok) => setTimeout(ok, 600)); if (this.insight.id !== id) return; }
            r = await req(u + (refresh ? '?refresh=1' : ''), { method: 'POST' });
            if (this.insight.id !== id) return;
          }
          if ((!r || !r.ok) && !refresh && !this._insRetry[id]) {   // 自动重试最多 1 次
            this._insRetry[id] = 1; await new Promise((ok) => setTimeout(ok, 2500)); if (this.insight.id !== id) return;
            r = await req(u, { method: 'POST' }); if (this.insight.id !== id) return;
          }
          if (r && r.ok && r.insight) { this.insight.data = r.insight; this._setRowPri(id, r.insight.priority); }
          else this.insight.error = 'AI 服务本次未返回结果' + (r && r.error ? '（' + r.error + '）' : '');
        } catch (e) { if (this.insight.id === id) this.insight.error = 'AI 分析暂不可用'; }
        finally { if (this.insight.id === id) this.insight.loading = false; }
      },
      _setRowPri(id, p) {
        const e = (S('mail').emails || []).find((x) => x.id === id); if (e) e.ai_priority = p;
        const c = S('mail').currentEmail; if (c && c.id === id) c.ai_priority = p;
      },
      reanalyze() { const c = S('mail').currentEmail; if (c) this._loadInsight(c.id, true); },
      get curInsight() { const c = S('mail').currentEmail; return c && this.insight.id === c.id ? this.insight.data : null; },
      paneTodos(e) { if (this.isNoAction(e)) return []; const ins = this.curInsight; return ins ? (ins.todos || []) : this.actionItems(e); },
      get paneDrafts() { const ins = this.curInsight; return ins ? (ins.drafts || []).map((d) => ({ tone: d.tone, text: d.text, draft: true })) : []; },
      // 需不需要回复：分析结果说了算；还没分析出来时按发件人特征估计
      paneNeedsReply(e) { if (this.isNoAction(e)) return false; const ins = this.curInsight; return ins ? ['act', 'reply'].includes(ins.priority) : !this.isSystem(e); },
      get paneNoReplyText() { if (this.isNoAction(S('mail').currentEmail)) return '已标记为无需处理。'; const p = this.curInsight && this.curInsight.priority; return p === 'fyi' ? '仅供阅读，无需回复。' : '系统邮件，无需回复。'; },
      quickReplyText(e) {
        const n = this.paneDrafts.length, who = e.sender || e.sender_email;
        return '回复 ' + who + '…' + (n ? ' AI 已准备 ' + n + ' 个草稿' : (this.paneNeedsReply(e) ? ' AI 可按语气草拟' : ''));
      },

      // ======================= 无需处理 =======================
      NOACT_SCOPES: [['same_kind', '同一发件人的同类邮件', '主题忽略日期和数字'], ['sender', '该发件人的全部邮件', ''], ['single', '仅这一封', '']],
      openNoAction() {
        const e = S('mail').currentEmail; if (!e) return;
        if (this.isNoAction(e)) { this.removeNoAction(e); return; }
        Object.assign(this.noAct, { open: true, scope: 'same_kind', preview: null, completeTodos: false, busy: false });
        this.previewNoAction();
      },
      async previewNoAction() {
        const e = S('mail').currentEmail; if (!e) return;
        const sc = this.noAct.scope; this.noAct.loading = true;
        try {
          const r = await req('/api/no-action/preview?scope=' + sc + '&email_id=' + encodeURIComponent(e.id));
          if (this.noAct.scope === sc) this.noAct.preview = r && r.ok ? r : null;
        } catch (x) { this.noAct.preview = null; } finally { this.noAct.loading = false; }
      },
      setNoActScope(sc) { this.noAct.scope = sc; this.noAct.preview = null; this.previewNoAction(); },
      async applyNoAction() {
        const e = S('mail').currentEmail; if (!e || this.noAct.busy) return;
        this.noAct.busy = true;
        try {
          const r = await req('/api/no-action', { method: 'POST', body: JSON.stringify({ email_id: e.id, scope: this.noAct.scope, mode: this.noAct.mode, complete_todos: this.noAct.completeTodos }) });
          if (!r || !r.ok) throw new Error((r && r.error) || 'failed');
          this._markNoAction(r.ids || [e.id], true);
          this.noAct.open = false;
          const m = S('mail'); if (r.todos_done) m.fetchTodos();
          this.flash('已标记 ' + r.matched + ' 封为无需处理' + (r.scope !== 'single' ? '，以后同类邮件自动套用' : '') + (r.todos_done ? '，完成 ' + r.todos_done + ' 条待办' : ''),
            async () => { await req('/api/no-action/undo', { method: 'POST', body: JSON.stringify({ ids: r.ids, rule: r.rule, rule_created: r.rule_created }) }); this._markNoAction(r.ids, false); this._loadInsight(e.id, false); });
          this._loadInsight(e.id, false);
        } catch (x) { this.flash('标记失败', null, 'error'); } finally { this.noAct.busy = false; }
      },
      async removeNoAction(e) {
        const tags = (e.tags || []).filter((t) => t !== '无需处理');
        try { await req('/api/emails/' + encodeURIComponent(e.id), { method: 'PATCH', body: JSON.stringify({ tags }) }); } catch (x) { this.flash('操作失败', null, 'error'); return; }
        this._markNoAction([e.id], false); this._loadInsight(e.id, false);
        this.flash('已恢复这封邮件（同类规则仍在「设置 → 邮件规则」里）');
      },
      _markNoAction(ids, on) {
        const set = new Set(ids), m = S('mail');
        const fix = (x) => { if (!x || !set.has(x.id)) return; const t = (x.tags || []).filter((y) => y !== '无需处理'); if (on) t.push('无需处理'); x.tags = t; if (on) x.ai_priority = 'fyi'; else delete x.ai_priority; };
        (m.emails || []).forEach(fix); fix(m.currentEmail);
      },

      // ======================= 设置 =======================
      SET_TABS: [['model', 'AI 模型'], ['prompts', '提示词'], ['rules', '邮件规则'], ['airules', 'AI 规则'], ['mailbox', '邮箱标签'], ['general', '同步与签名']],
      PROMPTS: [['prompt_summary', '邮件摘要', '{subject} {body} {history}'], ['prompt_draft', '回复草稿', '{subject} {body} {tone}'], ['prompt_chat', 'AI 对话系统提示', ''],
        ['prompt_topic_digest', '文件夹简报', ''], ['prompt_topic_nl', '智能文件夹 · 自然语言解析', ''], ['prompt_todo_extract', '待办提取', ''], ['prompt_rule_gen', '规则生成', ''],
        ['prompt_insight', 'AI 简报（结构化分析：优先级 / 结论 / 行动项 / 截止日 / 回复草稿）', '{account} {sender} {sender_email} {subject} {date} {body}']],
      RULE_FIELDS: [['subject', '主题'], ['from', '发件人'], ['to', '收件人'], ['body', '正文'], ['subject_or_body', '主题或正文'], ['account', '账户'], ['folder', '文件夹'], ['has_attachment', '有附件']],
      RULE_OPS: [['contains', '包含'], ['not_contains', '不包含'], ['is', '等于'], ['not_is', '不等于'], ['starts_with', '开头是'], ['ends_with', '结尾是'], ['pattern_is', '同类主题']],
      CATS: ['🚨重要行动', '📢内部通知', '📰订阅'],
      ruleSummary(rule) {
        const fmap = Object.fromEntries(this.RULE_FIELDS);
        const conds = (rule.conditions || []).filter((c) => c.field === 'has_attachment' || String(c.value || '').trim());
        const cs = conds.map((c) => (fmap[c.field] || c.field) + (String(c.value || '').trim() ? '：' + c.value : '')).join(rule.matchMode === 'any' ? ' 或 ' : ' 且 ');
        const acts = [];
        if (rule.actCategory) acts.push('分类 ' + rule.actCategory); if (rule.actFolder) acts.push('移到 ' + rule.actFolder);
        if (rule.actNoAction) acts.push('无需处理');
        if (rule.actStar) acts.push('星标'); if (rule.actMarkRead) acts.push('已读'); if (rule.actSkipAi) acts.push('跳过 AI');
        return (cs || '（未设条件）') + (String(rule.aiMatch || '').trim() ? ' + AI 语义' : '') + (acts.length ? ' → ' + acts.join('、') : '');
      },
      addRule() { S('settings').addRule(); this.openRule = S('settings').rulesUI.length - 1; },
      async saveSettings() { await S('settings').save(); S('ui').settingsOpen = false; },
      async loadMb() {
        try {
          const r = await req('/api/mailbox-labels');
          this.mb.palette = (r && r.palette) || {};
          this.mb.rules = ((r && r.rules) || []).map((x) => ({ name: x.name, color: x.color, accountsText: (x.accounts || []).join(', '), domainsText: (x.domains || []).join(', ') }));
          this.mb.errors = [];
        } catch (e) { this.mb.errors = ['读取失败'];}
      },
      async saveMb() {
        this.mb.saving = true; this.mb.saved = false; this.mb.errors = [];
        const split = (t) => String(t || '').split(/[,，;]/).map((s) => s.trim()).filter(Boolean);
        try {
          const rules = this.mb.rules.map((r) => ({ name: String(r.name || '').trim(), color: r.color || 'slate', accounts: split(r.accountsText), domains: split(r.domainsText) }));
          const res = await req('/api/mailbox-labels', { method: 'POST', body: JSON.stringify({ rules }) });
          if (res && res.ok) { this.mb.saved = true; await S('mail').loadMailboxLabels(); this._ownDomains(); this.flash('邮箱标签已保存'); }
          else this.mb.errors = (res && res.errors) || ['保存失败'];
        } catch (e) { this.mb.errors = ['保存失败'];} finally { this.mb.saving = false; }
      },
      MB_COLORS: { blue: '#3b82f6', amber: '#f59e0b', rose: '#f43f5e', emerald: '#10b981', violet: '#8b5cf6', cyan: '#06b6d4', orange: '#f97316', slate: '#64748b' },

      // ======================= 智能文件夹（话题规则） =======================
      async saveSmart() {
        const m = S('mail'); const before = (m.topicRules || []).length;
        await m.saveTopicDef();
        if ((m.topicRules || []).length > before) { this.smartOpen = false; m.activeTabName = ''; m.loadTopics(); }
      },
      async delSmart(name) { if (confirm('删除智能文件夹「' + name + '」？（只删规则，不删邮件）')) await S('mail').delTopicRule(name); },

      // ======================= 写信窗 =======================
      _senderFor(account) {
        const list = this.senders.map((s) => (typeof s === 'string' ? s : s.account || s.sender || s.name)).filter(Boolean);
        if (account && list.includes(account)) return account;
        if (this.acc && list.includes(this.acc)) return this.acc;
        return this.defaultSender || list[0] || '';
      },
      get senderList() {
        return this.senders.map((s) => (typeof s === 'string' ? { account: s, ready: true } : { account: s.account || s.sender || s.name, ready: s.ready !== false })).filter((s) => s.account);
      },
      newCompose() {
        this._autosave(); this.compose = { mode: 'new', from: this._senderFor(''), to: '', cc: '', showCc: false, subject: '', body: '', srcId: '', draftId: null, attachments: [] };
        this.cmd.open = false; this._focusSoon('to');
        try { S('mail').fetchContacts(); } catch (e) {}
      },
      reply(all, tone) {
        const e = S('mail').currentEmail; if (!e) return;
        const self = String(e.account || '').toLowerCase(), sender = String(e.sender_email || '').toLowerCase();
        const cc = all ? [...new Set((e.recipients || []).map((r) => String(r.email || '').trim()).filter((a) => a && a.toLowerCase() !== self && a.toLowerCase() !== sender))].join('; ') : '';
        this._autosave(); this.compose = { mode: all ? 'reply-all' : 'reply', from: this._senderFor(e.account), to: e.sender_email || '', cc, showCc: !!cc,
          subject: /^re[:：]/i.test(e.subject || '') ? e.subject : 'Re: ' + (e.subject || ''), body: '', srcId: e.id, draftId: null, attachments: [],
          inline: this.isMailView && this.view !== 'drafts' };   // 回复框内嵌在正文下方（P1-D）
        if (tone && tone.draft) { this._setBody(plainToHtml(tone.text)); this._focusSoon('body'); }   // AI 已备好的草稿：直接填入，不再调 AI
        else if (tone) this.composeAi(tone.text ? tone : { tone, text: tone });
        else this._focusSoon('body');
      },
      forward() {
        const e = S('mail').currentEmail; if (!e) return;
        this._autosave(); this.compose = { mode: 'forward', from: this._senderFor(e.account), to: '', cc: '', showCc: false,
          subject: /^fwd?[:：]/i.test(e.subject || '') ? e.subject : 'Fwd: ' + (e.subject || ''), body: '', srcId: e.id, draftId: null, attachments: [] };
        this._focusSoon('to');
      },
      openDraft(d) {
        this._autosave(); this.compose = { mode: 'draft', from: this._senderFor(d.account), to: d.to || '', cc: d.cc || '', showCc: !!d.cc, subject: d.subject || '',
          body: d.is_html ? String(d.body || '') : plainToHtml(d.body || ''),
          srcId: d.reply_to_id || '', draftId: d.id, attachments: [] };
      },
      get composeTitle() {
        const c = this.compose; if (!c) return '';
        return { reply: '回复：', 'reply-all': '回复全部：', forward: '转发：', draft: '草稿：' }[c.mode] !== undefined
          ? ({ reply: '回复：', 'reply-all': '回复全部：', forward: '转发：', draft: '草稿：' }[c.mode] + (c.subject || '').replace(/^(re|fwd?)[:：]\s*/i, ''))
          : '新邮件';
      },
      _focusSoon(ref) { setTimeout(() => { const el = ref === 'body' ? this._editor() : document.querySelector('.v2-compose:not(.v2-inline) [data-cfield="' + ref + '"]'); if (el) el.focus(); }, 30); },
      composeAi(kindOrTone) {
        const c = this.compose; if (!c) return;
        if (!c.srcId) { this.flash('AI 草拟基于来信生成：回复或转发时可用'); return; }
        const tone = typeof kindOrTone === 'string' ? (AI_KIND[kindOrTone] || kindOrTone) : kindOrTone.text;
        this._setBody('');
        S('ai').generateDraft(c.srcId, tone);   // 先清空旧流再挂同步，避免上一份草稿闪进正文
        this._aiFor = c.srcId;
      },
      // 正文是 HTML，编辑器是 contenteditable（经典 compose 组件）。store 里的 body 随输入同步，
      // 发送/存草稿时再从 DOM 取一次最新值，避免最后一次输入还没同步。
      _editor() {
        const c = this.compose;
        return document.querySelector(c && c.inline ? '.v2-inline [data-cfield="body"]' : '.v2-compose:not(.v2-inline) [data-cfield="body"]');
      },
      // 内嵌回复框：AI 草稿标签点选填入；展开 = 转成浮动写信区（可改收件人、加附件、富文本）
      useDraft(d) { if (!this.compose) return; this._setBody(d ? plainToHtml(d.text) : ''); setTimeout(() => { const el = this._editor(); if (el) el.focus(); }, 20); },
      expandCompose() {
        const c = this.compose; if (!c) return;
        const html = this._bodyHtml(); c.body = html; c.inline = false; c.min = false;
        this._setBody(html);   // 浮动写信区的编辑器常驻 DOM，切换后立即写入
        this._focusSoon('body');
      },
      get quoteText() {
        const e = S('mail').currentEmail; if (!e) return '';
        return String(e.body || '').replace(/\r/g, '').trim().slice(0, 1500);
      },
      _setBody(html) {
        if (!this.compose) return;
        this.compose.body = html || '';
        const el = this._editor(); if (el && el.innerHTML !== this.compose.body) el.innerHTML = this.compose.body;
      },
      _bodyHtml() { const el = this._editor(); if (el && this.compose) this.compose.body = el.innerHTML; return (this.compose && this.compose.body) || ''; },
      _bodyText() { const el = this._editor(); return ((el ? el.innerText : String((this.compose && this.compose.body) || '').replace(/<[^>]+>/g, '')) || '').replace(/\u200b/g, '').trim(); },
      get aiBusyText() { return this._aiFor && S('ai').draftLoading ? 'AI 正在草拟…' : ''; },
      async attach(ev) {
        const c = this.compose; const files = [...(ev.target.files || [])]; ev.target.value = '';
        for (const f of files) {
          const item = { name: f.name, size: f.size, path: '', uploading: true }; c.attachments.push(item);
          try {
            const b64 = await new Promise((ok, no) => { const r = new FileReader(); r.onload = () => ok(String(r.result)); r.onerror = no; r.readAsDataURL(f); });
            const res = await req('/api/upload', { method: 'POST', body: JSON.stringify({ name: f.name, data: b64 }) });
            if (!res || !res.ok) throw new Error((res && res.error) || 'upload');
            item.path = res.path; item.uploading = false;
          } catch (e) { c.attachments = c.attachments.filter((a) => a !== item); this.flash('附件上传失败：' + f.name, null, 'error'); }
        }
      },
      // 嵌入门户时的发送：默认开放；config.embed.allow_send: false 时恢复禁用（服务端注入 meta）
      get sendBlocked() {
        if (!window.__QUILLBOX_EMBED) return false;
        const m = document.querySelector('meta[name="quillbox-embed-send"]');
        return !!m && m.content === 'off';
      },
      relogin() {
        const u = new URL('/app', location.origin).href;   // 401 登录页，登录后种 cookie；本页保持不动
        if (window.__QUILLBOX_EMBED && this._bridgeOk) { this._post('open-external', { url: u }); return; }
        window.open(u, '_blank', 'noopener');
      },
      async recheckAuth(manual) {
        if (this._authChecking) return;
        this._authChecking = true;
        try {
          const r = await fetch('/api/version', { cache: 'no-store' });
          if (r.ok) {
            this.authLost = false; window.__QUILLBOX_AUTH_LOST = false;
            this.flash('已重新连接，可以继续操作');
          } else if (manual) {
            this.flash('仍未登录：请先在新标签页完成登录', null, 'error');
          }
        } catch (e) {
          if (manual) this.flash('连接失败，请稍后重试', null, 'error');
        } finally { this._authChecking = false; }
      },
      openFullApp() {
        const u = new URL('/', location.origin).href;
        if (window.__QUILLBOX_EMBED && this._bridgeOk) { this._post('open-external', { url: u }); return; }
        window.open(u, '_blank', 'noopener');
      },
      async send() {
        const c = this.compose; if (!c) return;
        if (this.sendBlocked) { this.flash('嵌入模式下不开放发送：草稿已自动保存，请在 Quillbox 客户端发送'); this._autosave(); return; }
        if (!c.to.trim()) { this.flash('请填写收件人', null, 'error'); return; }
        if (c.mode !== 'forward' && !c.subject.trim()) { this.flash('请填写主题', null, 'error'); return; }
        if (c.attachments.some((a) => a.uploading)) { this.flash('附件仍在上传，请稍候…'); return; }
        const m = S('mail'), paths = c.attachments.map((a) => a.path).filter(Boolean);
        try {
          if (c.mode === 'forward') {
            await m.forwardEmail(c.srcId, c.to, this._bodyHtml(), { cc: c.cc, bcc: c.bcc || '', is_html: true, attachments: paths });
            if (c.draftId) m.removeDraft(c.draftId).catch(() => {});   // 转发不走 draft_id，自动存的草稿手动清掉
          } else {
            const p = { from: c.from, to: c.to, cc: c.cc, bcc: c.bcc || '', subject: c.subject, body: this._bodyHtml(), is_html: true, attachments: paths };
            if ((c.mode === 'reply' || c.mode === 'reply-all' || c.mode === 'draft') && c.srcId) p.reply_to_entry_id = c.srcId;
            if (c.draftId) p.draft_id = c.draftId;
            await m.sendEmail(p);
          }
          this.compose = null;
        } catch (e) { /* store 已提示失败原因，保留写信窗内容 */ }
      },
      _draftPayload(c) { return { to: c.to, cc: c.cc, subject: c.subject, body: this._bodyHtml(), is_html: true, account: c.from, reply_to_id: c.srcId || '', source: 'user' }; },
      _composeEmpty() { const c = this.compose; return !c || (!c.subject.trim() && !this._bodyText() && !String(c.to || '').trim()); },
      // 「存草稿」= 用户明确要存：照旧回写邮件服务器草稿箱（手机可见），并关闭写信区
      async saveDraft() {
        const c = this.compose; if (!c) return;
        if (!c.subject.trim() && !this._bodyText()) { this.compose = null; return; }
        const d = this._draftPayload(c); d.imap = true;
        try {
          if (c.draftId) await req('/api/drafts/' + c.draftId, { method: 'PATCH', body: JSON.stringify(d) });
          else await req('/api/drafts', { method: 'POST', body: JSON.stringify(d) });
          this.compose = null; this.flash('已存草稿');
          if (this.view === 'drafts') S('mail').fetchDrafts();
        } catch (e) { this.flash('存草稿失败', null, 'error'); }
      },
      // 自动保存：只写 Quillbox 本地草稿箱（imap:false），不往邮件服务器堆副本；内容没变不写
      async _autosave() {
        const c = this.compose; if (!c || this._asBusy || this._aiFor) return;
        if (!c.subject.trim() && !this._bodyText()) return;
        const d = this._draftPayload(c), snap = JSON.stringify(d);
        if (snap === c._snap) return;
        this._asBusy = true;
        try {
          if (c.draftId) await req('/api/drafts/' + c.draftId, { method: 'PATCH', body: snap });
          else { const r = await req('/api/drafts', { method: 'POST', body: JSON.stringify(Object.assign({ imap: false }, d)) }); if (r && r.id && this.compose === c) c.draftId = r.id; }
          c._snap = snap; c.savedAt = Date.now();
        } catch (e) { /* 下一轮再试；不打扰用户 */ }
        finally { this._asBusy = false; }
      },
      get composeSaved() { const c = this.compose; return c && c.savedAt ? '已自动保存 ' + new Date(c.savedAt).toTimeString().slice(0, 5) : ''; },
      // Esc / 收起：只收起，不丢稿（P0-02）
      minimizeCompose() { if (!this.compose) return; this._autosave(); this.compose.min = true; },
      restoreCompose() { const c = this.compose; if (!c) return; c.min = false; c.inline = false; this._setBody(c.body || ''); this._focusSoon('body'); },
      // 丢弃：进入最近操作，可撤销（撤销 = 原样恢复写信区，已自动存的草稿重新写回）
      discard() {
        S('ai').cancelDraft(); this._aiFor = '';
        const c = this.compose; if (!c) return;
        const empty = this._composeEmpty(); const snap = Object.assign({}, c, { body: this._bodyHtml(), min: false });
        this.compose = null;
        if (c.draftId) req('/api/drafts/' + c.draftId, { method: 'DELETE' }).catch(() => {});
        if (empty) return;
        this.flash('已丢弃：' + (c.subject || '(无主题)'), async () => {
          snap.draftId = null; snap._snap = ''; this.compose = snap;
          setTimeout(() => { this._setBody(snap.body); this._autosave(); }, 60);
        });
      },
      // ======================= 正文 @ 提及=======================
      // 在正文里输入 @ → 弹联系人（本地通讯录，过滤系统地址）→ 选中后插入 @姓名 标签（mailto 链接 + 内联样式，对方任何客户端都看得到），
      // 并把这个人加进收件人（新邮件 / 转发 → 收件人；回复 / 全部回复 → 抄送）。删掉标签不会移除收件人。
      // 事件委托到所有 [data-cfield=body] 的可编辑区（完整写信窗口、内联回复共用）；输入法组字时不拦截回车 / 方向键。
      _mnInit() {
        if (this._mnInited) return; this._mnInited = true;
        const inBody = (t) => (t && t.closest) ? t.closest('[data-cfield="body"][contenteditable]') : null;
        const upd = (ev) => { if (_mnSkip) return; const ed = inBody(ev.target); if (!ed || !this.compose) { this._mnClose(); return; } this._mnUpdate(ed); };
        document.addEventListener('input', upd, true);
        document.addEventListener('compositionend', upd, true);
        document.addEventListener('keyup', (ev) => { if (this.mn.open && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(ev.key)) { const ed = inBody(ev.target); if (ed) this._mnUpdate(ed); else this._mnClose(); } }, true);
        document.addEventListener('keydown', (ev) => this._mnKey(ev), true);
        document.addEventListener('mousedown', (ev) => { if (this.mn.open && !(ev.target.closest && ev.target.closest('[data-testid=mention-pop]'))) this._mnClose(); }, true);
        window.addEventListener('blur', () => this._mnClose());
      },
      _mnClose() { if (this.mn.open) this.mn.open = false; _mnCtx = null; },
      _mnName(p) {
        let n = String((p && p.name) || '').split('/')[0].replace(/[（(].*$/, '').trim();
        if (!n || n.includes('@')) n = String((p && p.email) || '').split('@')[0];
        return n;
      },
      _mnSearch(q) {
        const ql = String(q || '').trim().toLowerCase();
        const score = (p) => {
          const email = String(p.email || '').toLowerCase(), name = String(p.name || '').toLowerCase(), local = email.split('@')[0];
          if (!email || SYSTEM_SENDER.test(email)) return -1;
          if (!ql) return name && !name.includes('@') ? 1 : -1;       // 只敲了 @：列出有名字的常用联系人
          if (name.startsWith(ql) || local.startsWith(ql)) return 3;
          if (name.includes(ql)) return 2;
          return email.includes(ql) ? 1 : -1;
        };
        return (S('mail').contacts || []).map((p, i) => [p, score(p), i]).filter((x) => x[1] > 0)
          .sort((a, b) => b[1] - a[1] || a[2] - b[2]).slice(0, 8).map((x) => x[0]);
      },
      _mnUpdate(ed) {
        const sel = window.getSelection();
        if (!sel || !sel.rangeCount || !sel.isCollapsed) { this._mnClose(); return; }
        const r = sel.getRangeAt(0), nd = r.startContainer;
        if (nd.nodeType !== 3 || !ed.contains(nd) || (nd.parentElement && nd.parentElement.closest('a[data-mention]'))) { this._mnClose(); return; }
        const before = nd.data.slice(0, r.startOffset), at = before.lastIndexOf('@');
        if (at < 0) { this._mnClose(); return; }
        const prev = at > 0 ? before[at - 1] : '';
        if (prev && /[A-Za-z0-9._%+\-]/.test(prev)) { this._mnClose(); return; }        // a@b.com 之类的邮箱地址不触发
        const q = before.slice(at + 1);
        if (q.length > 24 || /[\s@]/.test(q)) { this._mnClose(); return; }
        const list = this._mnSearch(q);
        if (!list.length) { this._mnClose(); return; }
        const rc = r.getClientRects()[0] || r.getBoundingClientRect(), eb = ed.getBoundingClientRect();
        const top = (rc && (rc.bottom || rc.top)) ? rc.bottom : eb.top + 24, left = (rc && rc.left) ? rc.left : eb.left + 12;
        const H = Math.min(264, list.length * 34 + 10);
        const y = (top + 4 + H > window.innerHeight && (rc.top || top) - H - 4 > 0) ? (rc.top || top) - H - 4 : top + 4;
        const idx = (this.mn.open && this.mn.q === q) ? Math.min(this.mn.idx, list.length - 1) : 0;
        _mnCtx = { ed, node: nd, at, end: r.startOffset };
        this.mn = { open: true, list, idx, q, x: Math.max(8, Math.min(left, window.innerWidth - 380)), y };
      },
      _mnKey(ev) {
        const a = this.mn;
        if (!a.open) return;
        if (ev.isComposing || ev.keyCode === 229) return;                 // 输入法组字中：回车 / 方向键归输入法
        if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') { ev.preventDefault(); ev.stopPropagation(); a.idx = (a.idx + (ev.key === 'ArrowDown' ? 1 : a.list.length - 1)) % a.list.length; }
        else if ((ev.key === 'Enter' || ev.key === 'Tab') && !ev.metaKey && !ev.ctrlKey && !ev.altKey && !ev.shiftKey) { ev.preventDefault(); ev.stopPropagation(); this._mnPick(a.list[a.idx]); }
        else if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); this._mnClose(); }
      },
      _mnPick(p) {
        const ctx = _mnCtx;
        if (!ctx || !p || !ctx.node.isConnected || !ctx.ed.isConnected) { this._mnClose(); return; }
        const email = String(p.email || '').trim(), name = this._mnName(p);
        const range = document.createRange();
        range.setStart(ctx.node, ctx.at); range.setEnd(ctx.node, Math.min(ctx.end, ctx.node.data.length));
        range.deleteContents();
        const chip = document.createElement('a');
        chip.setAttribute('href', 'mailto:' + email); chip.setAttribute('data-mention', email); chip.setAttribute('contenteditable', 'false');
        chip.style.cssText = 'color:#1a56db;background:#e8f0fe;border-radius:3px;padding:0 3px;text-decoration:none;font-weight:600';
        chip.textContent = '@' + name;
        const sp = document.createTextNode('\u00a0');                       // 标签后留一个空格位，光标落在标签外，继续打字不会进标签
        range.insertNode(sp); range.insertNode(chip);
        const sel = window.getSelection(), nr = document.createRange();
        nr.setStart(sp, 1); nr.collapse(true); sel.removeAllRanges(); sel.addRange(nr);
        const ed = ctx.ed;
        this._mnClose();
        _mnSkip = true; try { ed.dispatchEvent(new Event('input', { bubbles: true })); } finally { _mnSkip = false; }   // 让编辑区自己的 @input 把 HTML 同步进 compose.body
        this._mnAddRecipient(email, name);
        ed.focus();
      },
      _mnAddRecipient(email, name) {
        const c = this.compose; if (!c || !email) return;
        const low = email.toLowerCase();
        const list = (f) => String(c[f] || '').split(/[;,，；]+/).map((x) => x.trim()).filter(Boolean);
        const hit = (x) => { const l = x.toLowerCase(); return l === low || l.endsWith('<' + low + '>'); };
        const join = (a) => (a.length ? a.join('; ') + '; ' : '');
        if (list('to').some(hit) || list('bcc').some(hit) || String(c.from || '').toLowerCase().includes(low)) return;
        // 被 @ 的人是正文里直接点名的对象：所有模式都进收件人。改前回复 / 全部回复落到抄送；
        // 全部回复时对方常已在抄送里，挪到收件人。密送是刻意选择，不动。
        const moved = list('cc').some(hit);
        if (moved) c.cc = join(list('cc').filter((x) => !hit(x)));
        const prev = String(c.to || '').trim();
        c.to = (prev ? prev.replace(/[;,，；\s]+$/, '') + '; ' : '') + email + '; ';
        this.flash('已把 ' + name + (moved ? ' 从抄送移到收件人' : ' 加入收件人'), () => {
          c.to = join(list('to').filter((x) => !hit(x)));
          if (moved) { c.cc = join([...list('cc'), email]); c.showCc = true; }
        });
      },
      // 收件人补全（数据源：通讯录，纯本地匹配）
      acInput(field) {
        const c = this.compose; const v = String(c[field] || ''); const cut = v.lastIndexOf(';');
        const tail = (cut >= 0 ? v.slice(cut + 1) : v).trim().toLowerCase();
        if (!tail) { this.cAc = { field: '', list: [], idx: 0 }; return; }
        const list = (S('mail').contacts || []).filter((p) => String(p.email || '').toLowerCase().includes(tail) || String(p.name || '').toLowerCase().includes(tail)).slice(0, 8);
        this.cAc = { field, list, idx: 0 };
      },
      acPick(p) {
        const c = this.compose, f = this.cAc.field || 'to'; if (!c || !p) return;
        const v = String(c[f] || ''); const cut = v.lastIndexOf(';'); const head = cut >= 0 ? v.slice(0, cut + 1) + ' ' : '';
        c[f] = head + p.email + '; '; this.cAc = { field: '', list: [], idx: 0 }; this._focusSoon(f);
      },
      acKey(ev) {
        const a = this.cAc; if (!a.list.length) return;
        if (ev.key === 'ArrowDown') { ev.preventDefault(); a.idx = Math.min(a.list.length - 1, a.idx + 1); }
        else if (ev.key === 'ArrowUp') { ev.preventDefault(); a.idx = Math.max(0, a.idx - 1); }
        else if (ev.key === 'Enter' || ev.key === 'Tab') { ev.preventDefault(); this.acPick(a.list[a.idx]); }
        else if (ev.key === 'Escape') { ev.stopPropagation(); this.cAc = { field: '', list: [], idx: 0 }; }
      },

      // ======================= 命令面板 =======================
      // 聚焦由输入框上的 x-effect 在显示后立即完成（定时器聚焦会吞掉 ⌘K 后紧跟着敲的字）
      openCmd(q) { this.cmd.open = true; this.cmd.q = q || ''; this.cmd.idx = 0; this.cmd.mails = []; this.panel = ''; if (q) this.onCmdQ(); },
      // 关闭时必须让出焦点：否则焦点留在隐藏的输入框里，随后按 R/J 等单键会被当成打字吞掉
      closeCmd() { this.cmd.open = false; const a = document.activeElement; if (a && a.id === 'v2-cmd-input') a.blur(); },
      onCmdQ() {
        const c = this.cmd; c.idx = 0; clearTimeout(c._t);
        const q = c.q.trim(); if (!q) { c.mails = []; c.loading = false; return; }
        c.loading = true;
        c._t = setTimeout(async () => {
          try {
            const r = await req('/api/emails?view=inbox&light=1&page_size=5&q=' + encodeURIComponent(q));
            if (c.q.trim() === q) c.mails = (r && r.emails) || [];
          } catch (e) { c.mails = []; } finally { c.loading = false; }
        }, 220);
      },
      get cmdSections() {
        const q = this.cmd.q.trim(), secs = [], close = (fn) => () => { this.cmd.open = false; fn(); };
        if (q) {
          secs.push({ title: 'AI', items: [
            { icon: 'ph ph-sparkle', label: '在邮件中搜索「' + q + '」', sub: this.cmd.loading ? '搜索中…' : (this.cmd.mails.length ? '找到 ' + (this.cmd.mails.length >= 5 ? '5+' : this.cmd.mails.length) + ' 封' : ''), run: close(() => this.applySearch(q)) },
            { icon: 'ph ph-sparkle', label: '问 AI：「' + q + '」', sub: '在 AI 对话中回答', run: close(() => this.askAI(q)) },
          ] });
          if (this.cmd.mails.length) secs.push({ title: '邮件', items: this.cmd.mails.slice(0, 5).map((e) => ({ icon: 'ph ph-envelope-simple', label: e.subject || '(无主题)', sub: (e.sender || e.sender_email || '') + ' · ' + this.fmtTime(e.time), run: close(() => this.jumpTo(e)) })) });
        } else {
          secs.push({ title: '试试问 AI', items: ['今天需要我处理什么', '等我回复的邮件', '示例云 通知', '本周的会议安排'].map((t) => ({ icon: 'ph ph-sparkle', label: t, run: () => { this.cmd.q = t; this.onCmdQ(); } })) });
        }
        const cmds = [
          { icon: 'ph ph-pencil-simple-line', label: '写邮件', hint: 'C', run: close(() => this.newCompose()) },
          ...this.VIEWS.map(([k, label, icon]) => ({ icon: 'ph ph-' + icon, label: '前往 ' + label, run: close(() => this.go(k)) })),
          { icon: 'ph ph-folders', label: '打开 文件夹', run: close(() => { this.panel = 'folders'; }) },
          { icon: 'ph ph-archive', label: '归档当前邮件', hint: 'E', run: close(() => this.archive()) },
          { icon: 'ph ph-envelope-open', label: '本页全部标为已读', run: close(() => { this.selectAll(); this.markRead(); }) },
          { icon: 'ph ph-arrows-clockwise', label: '立即同步', run: close(() => S('mail').sync()) },
          { icon: 'ph ph-chat-circle-dots', label: 'AI 对话', run: close(() => { this.panel = 'chat'; }) },
          { icon: 'ph ph-address-book', label: '通讯录', run: close(() => this.openContacts()) },
          { icon: 'ph ph-sun', label: '切换为浅色', run: close(() => this.setTheme('light')) },
          { icon: 'ph ph-moon', label: '切换为深色', run: close(() => this.setTheme('dark')) },
          { icon: 'ph ph-desktop', label: '主题跟随系统', run: close(() => this.setTheme('system')) },
          { icon: 'ph ph-rows', label: this.compact ? '列表密度：舒适' : '列表密度：紧凑', run: close(() => this.toggleDensity()) },
          { icon: 'ph ph-gear-six', label: '设置', run: close(() => this.go('settings')) },
          { icon: 'ph ph-chart-bar', label: '统计看板', run: close(() => this.go('dashboard')) },
          { icon: 'ph ph-funnel', label: '新建智能文件夹', run: close(() => { this.go('topics'); this.smartOpen = true; }) },
          { icon: 'ph ph-arrow-clockwise', label: '让 AI 重新分析当前邮件', run: close(() => this.reanalyze()) },
          { icon: 'ph ph-check-circle', label: '标记为无需处理（同类邮件以后不再提取待办）', hint: 'N', run: close(() => this.openNoAction()) },
          { icon: 'ph ph-keyboard', label: '键盘快捷键', hint: '?', run: close(() => { this.panel = 'help'; }) },
        ];
        const f = q ? cmds.filter((c) => c.label.includes(q)) : cmds;
        if (f.length) secs.push({ title: '命令', items: f });
        let i = 0; secs.forEach((s) => s.items.forEach((it) => { it._i = i++; }));
        return secs;
      },
      get cmdFlat() { return this.cmdSections.flatMap((s) => s.items); },
      async jumpTo(e) {
        const m = S('mail');
        if (!(m.emails || []).some((x) => x.id === e.id)) { this.view = 'inbox'; this._reset(m); m.view = 'inbox'; await m.fetchEmails(); }
        m.gotoEmail(e.id);
      },
      askAI(q) {
        const ai = S('ai'); ai.chatMode = 'global'; ai.chatInput = q; this.panel = 'chat';
        ai.sendMessage();
      },
      openContacts() { this.panel = 'contacts'; this.contactQ = ''; S('mail').fetchContacts(); },
      get contactList() {
        const q = this.contactQ.trim().toLowerCase();
        return (S('mail').contacts || []).filter((p) => !q || String(p.email || '').toLowerCase().includes(q) || String(p.name || '').toLowerCase().includes(q)).slice(0, 200);
      },
      composeTo(p) { this.panel = ''; this.newCompose(); this.compose.to = p.email + '; '; this._focusSoon('subject'); },
      openClassic() { if (window.__QUILLBOX_EMBED && this._bridgeOk) { this._post('open-external', { url: new URL('/classic', location.origin).href }); return; } window.open('/classic', '_blank', 'noopener'); },

      // ======================= Toast =======================
      // 带撤销的提示同时进「最近操作」；提示条只是最新一条的入口，被新提示替换后仍可在面板 / 按 Z 撤销（P0-03）
      flash(text, undo, type, extra) {
        clearTimeout(this._tt);
        let op = null;
        if (undo) {
          op = { id: ++this._opSeq, text, undo, at: Date.now(), state: 'done' };
          this.ops.unshift(op); if (this.ops.length > 30) this.ops.length = 30;
        }
        this.toast = { text, undo: undo || null, opId: op ? op.id : null, type: type || 'info', extra: extra || null };
        this._tt = setTimeout(() => { this.toast = null; }, extra ? 10000 : undo ? 8000 : 4000);
      },
      get opsUndoable() { return this.ops.filter((o) => o.state === 'done').length; },
      async undoOp(op) {
        if (!op || op.state !== 'done') return;
        op.state = 'undoing';
        try { await op.undo(); op.state = 'undone'; this.toast = { text: '已撤销：' + op.text, undo: null, opId: null, type: 'info' }; }
        catch (e) { op.state = 'failed'; this.toast = { text: '撤销未完成：' + op.text, undo: null, opId: null, type: 'error' }; }
        clearTimeout(this._tt); this._tt = setTimeout(() => { this.toast = null; }, 4000);
      },
      async undo() {
        const t = this.toast; const op = t && t.opId ? this.ops.find((o) => o.id === t.opId) : null;
        if (op) return this.undoOp(op);
        if (t && t.undo) { this.toast = null; try { await t.undo(); } catch (e) { this.flash('撤销未完成', null, 'error'); } }
      },
      undoLatest() { const op = this.ops.find((o) => o.state === 'done'); if (op) this.undoOp(op); else this.flash('没有可撤销的操作'); },
      opAge(o) { const s = Math.round((Date.now() - o.at) / 1000); return s < 60 ? s + ' 秒前' : Math.round(s / 60) + ' 分钟前'; },
      opState(o) { return { done: '', undoing: '撤销中…', undone: '已撤销', failed: '撤销未完成' }[o.state]; },
      _opSubj(ids) {
        if (ids.length !== 1) return '';
        const e = (S('mail').emails || []).find((x) => x.id === ids[0]) || S('mail').currentEmail;
        return e && e.id === ids[0] ? '：' + String(e.subject || '(无主题)').slice(0, 30) : '';
      },

      // ======================= 键盘 =======================
      onKey(e) {
        const k = e.key, mod = e.metaKey || e.ctrlKey;
        const t = e.target || {}, typing = /INPUT|TEXTAREA|SELECT/.test(t.tagName || '') || t.isContentEditable;
        if (mod && k.toLowerCase() === 'k') { e.preventDefault(); this.cmd.open ? this.closeCmd() : this.openCmd(); if (this._bridgeOk) this._post('shortcut', { key: 'mod+k', handled: true }); return; }
        if (k === 'Escape') {
          if (this.cmd.open) this.closeCmd();
          else if (this.noAct.open) this.noAct.open = false;
          else if (this.panel) this.panel = '';
          else if (this.todoDlg.open) this.todoDlg.open = false;
          else if (this.snzDlg.open) this.snzDlg.open = false;
          else if (this.noteDlg.open) this.noteDlg.open = false;
          else if (this.muteDlg.open) this.muteDlg.open = false;
          else if (this.chg.menu) this.chg.menu = false;
          else if (this.chg.dlg) this.chg.dlg = false;
          else if (this.compose && !this.compose.min && !this.compose.inline) this.minimizeCompose();   // 只收起，不丢稿
          else if (this.compose && this.compose.inline && typing) { t.blur(); }                           // 内嵌回复框：Esc 只离开输入
          else if (this.ctxOpen && this.ctxDrawer) this.ctxOpen = false;
          else if (this.checked.length) this.clearChecked();
          return;
        }
        if (this.cmd.open) {
          // 面板刚打开、输入框还没拿到焦点时敲的字（⌘K 后立刻打字）不能丢：补进查询并把焦点给输入框
          const inp = document.getElementById('v2-cmd-input');
          if (inp && document.activeElement !== inp && k.length === 1 && !mod && !e.altKey) {
            e.preventDefault(); this.cmd.q += k; this.onCmdQ(); inp.focus(); return;
          }
          const items = this.cmdFlat;
          if (k === 'ArrowDown') { e.preventDefault(); this.cmd.idx = Math.min(items.length - 1, this.cmd.idx + 1); this._scrollCmd(); }
          else if (k === 'ArrowUp') { e.preventDefault(); this.cmd.idx = Math.max(0, this.cmd.idx - 1); this._scrollCmd(); }
          else if (k === 'Enter') { e.preventDefault(); const it = items[this.cmd.idx]; if (it) { it.run(); if (!this.cmd.open) this.closeCmd(); } }
          return;
        }
        if (this.todoDlg.open || this.snzDlg.open || this.muteDlg.open || this.noteDlg.open) return;   // 弹层自己处理按键
        if (this.compose && this.compose.inline && typing) { if (mod && k === 'Enter') { e.preventDefault(); this.send(); } return; }
        if (this.compose && !this.compose.min && !this.compose.inline) { if (mod && k === 'Enter') { e.preventDefault(); this.send(); } return; }
        if (typing || mod || e.altKey || this.panel === 'chat' || this.panel === 'contacts' || this.noAct.open) return;
        const m = S('mail'), inMail = this.isMailView && this.view !== 'drafts';
        const map = {
          j: () => inMail && this.hl(1), ArrowDown: () => inMail && this.hl(1),
          k: () => inMail && this.hl(-1), ArrowUp: () => inMail && this.hl(-1),
          Enter: () => inMail && this.openHl(), z: () => this.undoLatest(), '[': () => this.toggleRail(), h: () => inMail && this.openSnooze(),
          e: () => inMail && this.archive(), E: () => inMail && this.archive(null, { forceNote: true }), s: () => inMail && this.star(), u: () => inMail && this.toggleUnread(),
          x: () => { if (inMail && m.selectedId) { this.showChecks = true; this.check(m.selectedId); } },
          r: () => m.currentEmail && this.reply(false), c: () => (this.compose && this.compose.min ? this.restoreCompose() : this.newCompose()),
          t: () => m.currentEmail && this.addTodo(), n: () => m.currentEmail && this.openNoAction(), '/': () => { const el = document.querySelector('[data-testid=qbar] input'); if (el) el.focus(); else this.openCmd(); }, '?': () => { this.panel = 'help'; },
        };
        if (map[k]) { e.preventDefault(); map[k](); }
      },
      _scrollCmd() { requestAnimationFrame(() => { const el = document.querySelector('.v2-cmditem.on'); if (el) el.scrollIntoView({ block: 'nearest' }); }); },
    };
  }

  function register() {
    if (window.__quillboxV2Registered || typeof Alpine === 'undefined') return false;
    window.__quillboxV2Registered = true;
    Alpine.store('v2', v2Store());
    return true;
  }
  document.addEventListener('alpine:init', register);
  register();
})();
