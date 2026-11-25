/**
 * 邮件状态管理
 * 管理邮件列表、当前邮件、账户、筛选等
 */
import * as api from '../api.js';
import { sanitizeHTML, sanitizeRemoteCount, isImgTrusted } from '../utils/sanitize.js';  // SANITIZE-HARDEN-20260930

export default function mailStore() {
  return {
    // 邮件列表
    emails: [],
    loading: false,
    totalCount: 0,

    // 当前选中/打开的邮件
    selectedId: null,
    currentEmail: null,
    detailLoading: false,

    // 视图和筛选
    view: 'inbox', // 'inbox' | 'starred' | 'unread' | 'sent' | 'trash' | 'todos'
    filter: 'all', // 'all' | 'unread' | 'starred' | 'attachments'
    searchQuery: '',
    currentAccount: '',
    currentFolder: '',
    currentTag: '',
    folderDlg: { open: false, account: '', mode: 'new', name: '', existing: '', field: 'from', op: 'contains', value: '' },
    tagPalette: [{name:'重要',color:'#ef4444'},{name:'工作',color:'#4f46e5'},{name:'个人',color:'#10b981'},{name:'待跟进',color:'#f59e0b'},{name:'账单',color:'#8b5cf6'}],
    selectedIds: [],
    savedSearches: [],
    categories: [],
    activeCategory: '',
    todoExtracting: false,
    dashboard: null,
    dashLoading: false,
    topics: [],
    topicsLoading: false,
    topicBy: 'subject',
    topicMultiOnly: false,
    inboxTabs: [],
    activeTabName: '',
    availableCats: [],
    newTabName: '',
    newTabKind: 'category',
    newTabRef: '',
    showTabManager: false,
    topicRules: [],
    topicPreviewCount: null,
    topicDefBusy: false,
    showHiddenTopics: false,
    defOpen: false,
    _dragTopic: null,
    currentTopic: '',
    topicCounts: {},
    topicDigest: '',
    topicDigestBusy: false,
    topicDef: { name: '', nl: '', keywords: '', query: '', filters: { cc_me: false, to_me: false, has_attachment: false, unread: false, starred: false, from: '', category: '' } },

    // 分页
    page: 0,
    pageSize: 30,
    hasMore: true,

    // 账户
    accounts: [],
    // 账户显示偏好（localStorage 持久化）：别名 + 自定义排序。
    // 注意：仅影响显示，不改动作为过滤/同步键的真实账户地址。
    accountAliases: {},   // { 账户email: 别名 }

    // ── 邮箱来源标签（可自定义）────────────────────────────────────────
    // 规则来自 /api/mailbox-labels（config.yaml 的 mailbox_labels 段），支持
    // 「精确账户」和「域名后缀」两种命中方式：内外网按域名一次配好，公共邮箱
    // （告警/职能邮箱之类）按账户单独打标。标签仍从 account 派生，不占数据库列。
    mailboxRules: [],
    mailboxPalette: {},
    currentMailbox: '',       // 当前按标签筛选的标签名
    folderOrders: {},
    globalFolders: [],
    accountOrder: [],     // [账户email, ...] 自定义顺序

    // 通讯录（清洗后的联系人，一次取回本地过滤，供补全与通讯录页共用）
    contacts: [],
    contactsLoaded: false,
    contactQuery: '',
    pendingComposeTo: '',   // 通讯录 -> 撰写窗 的收件人中转

    // 草稿箱（Agent 经 MCP 写入的回信草稿也落在这里）
    drafts: [],
    editingDraft: null,     // 打开到撰写窗的草稿，compose 预填时读它

    // 待办
    todos: [],
    newTodoText: '',
    todoBatch: { open: false, days: 7, accounts: [], useAI: true, running: false, scanned: 0, total: 0, added: 0 },

    // 统计
    stats: {
      total: 0,
      unread: 0,
      pending_ai: 0,
      sync_running: false,
      last_sync: null,
      ollama_ok: false,
      ai_provider: '',
    },

    init() {
      // 幂等：Alpine.store() 注册时会自动调一次 init()，index.html 的 x-init 又手动
      // 调一次，首屏接口因此全部请求两遍。记住第一次的 Promise，
      // 第二次调用直接等它，x-init 里「先 mail 后 settings」的 await 顺序不变。
      // 失败时清掉，保留原来「再调一次就重试」的语义。
      if (this._initPromise) return this._initPromise;
      this._initPromise = (async () => {
        this._loadAccountPrefs();
        this.loadSavedSearches();
        this.loadCategories();
        this.loadGlobalFolders();
        await Promise.all([
          this.fetchEmails(),
          this.fetchAccounts(),
          this.fetchStats(),
        ]);
      })().catch((e) => { this._initPromise = null; throw e; });
      return this._initPromise;
    },

    // ============ 邮件列表 ============

    async fetchEmails(append = false) {
      // 列表提供者（新界面「今日分拣」设置；经典界面不设）：接管时改由它供数
      const prov = this.listProvider && this.listProvider(append);
      if (prov) return prov;
      this.loading = true;
      try {
        const result = this.currentTopic
          ? await api.getTopicEmails(this.currentTopic)
          : await api.getEmails({
          view: this.view,
          account: this.currentAccount,
          mailbox: this.currentMailbox,
          folder: this.currentFolder,
          filter: this.filter,
          page: this.page,
          pageSize: this.pageSize,
          q: this.searchQuery,
          tag: this.currentTag,
        });

        const emails = Array.isArray(result) ? result : (result.emails || result.items || []);
        if (this.listOwns && this.listOwns()) return;   // 请求在途时切进了分拣视图：丢弃这次收件箱结果，不覆盖分拣列表
        if (append) {
          this.emails = [...this.emails, ...emails];
        } else {
          this.emails = emails;
        }
        this.totalCount = result.total || emails.length;
        // 优先信任后端返回的 has_more；缺失时回退到「本页是否满页」推断。
        this.hasMore = (typeof result.has_more === 'boolean')
          ? result.has_more
          : (emails.length === this.pageSize);

        // 自动选中第一封
        if (!append && this.emails.length > 0 && !this.selectedId) {
          this.selectedId = this.emails[0].id;
        }
      } catch (err) {
        console.error('[Mail] fetchEmails error:', err);
        Alpine.store('ui').notify('加载邮件失败', 'error');
      } finally {
        this.loading = false;
      }
    },

    // 加载更多
    async loadMore() {
      if (!this.hasMore || this.loading) return;
      this.page++;
      await this.fetchEmails(true);
    },

    // 设置视图
    setView(view) {
      this.view = view;
      this.currentTopic = '';
      this.currentTag = '';
      this.activeCategory = '';
      this.page = 0;
      this.selectedId = null;
      this.currentEmail = null;
      this.filter = 'all';
      this.searchQuery = '';   // 切换视图时清除搜索，避免仍停留在搜索结果
      // 侧栏是**导航**，顶栏才是过滤：点侧栏任一视图都该回到该视图的全部内容。
      // 不清这三个的话，进了某个文件夹之后点「收件箱」仍会停在那个文件夹里，
      // 用户被困住出不来。
      // 注意：setAccountFolder / setMailbox 是直接给 this.view 赋值、
      // 不经过这里，所以进入文件夹的路径不会被这段清理反噬。
      this.currentFolder = '';
      this.currentAccount = '';
      this.currentMailbox = '';
      if (view === 'contacts') {
        this.fetchContacts();
      } else if (view === 'drafts') {
        this.fetchDrafts();
      } else if (view === 'todos') {
        this.fetchTodos();
      } else if (view === 'dashboard') {
        this.loadDashboard();
      } else if (view === 'topics') {
        this.loadTopics();
      } else {
        this.fetchEmails();
      }
    },

    // ---- 空态说明 ----
    // 只显示"暂无邮件"的话，"过滤生效但结果为空"和"点击没生效"在界面上无法区分。
    // 用户就因此困惑过：进了 IP 文件夹点未读，后端正确返回 0 封（那里确实没有未读），
    // 界面却只是一片空白，看起来像"点了没反应"。
    get narrowing() {
      const f = {all: '', unread: '未读', starred: '星标',
                 attachments: '带附件'}[this.filter] || '';
      const out = [];
      if (this.currentFolder) out.push('文件夹「' + this.currentFolder + '」');
      else if (this.currentMailbox) out.push('邮箱标签「' + this.currentMailbox + '」');
      else if (this.currentAccount) out.push('账户「' + this.currentAccount + '」');
      if (this.currentTag) out.push('标签「' + this.currentTag + '」');
      if (this.currentTopic) out.push('话题「' + this.currentTopic + '」');
      if (f) out.push(f);
      if (this.searchQuery) out.push('搜索「' + this.searchQuery + '」');
      return out;
    },

    get hasNarrowing() { return this.narrowing.length > 0; },

    get emptyReason() {
      const n = this.narrowing;
      if (!n.length) return '暂无邮件';
      // 把条件说全，用户才能判断是"这里确实没有"还是"我点错了"
      return n.join(' + ') + ' 下没有邮件';
    },

    // 清掉所有收窄条件，回到"看全部"。
    // 刻意不改 view —— 用户点"清除筛选"是想看全部，不是想跳去别的视图。
    clearNarrowing() {
      this.currentFolder = '';
      this.currentAccount = '';
      this.currentMailbox = '';
      this.currentTag = '';
      this.currentTopic = '';
      this.activeCategory = '';
      this.filter = 'all';
      this.searchQuery = '';
      this.page = 0;
      this.fetchEmails();
    },

    // 设置筛选
    setFilter(filter) {
      this.filter = filter;
      this.currentTopic = '';
      this.currentTag = '';
      this.page = 0;
      this.fetchEmails();
    },

    // 搜索
    search(query) {
      this.searchQuery = query;
      this.currentTag = '';
      this.page = 0;
      this.fetchEmails();
    },

    // 设置账户/文件夹
    // 配色键 → tailwind class（白名单，避免把任意字符串塞进 class）
    mailboxColorClass(color) {
      const m = {
        blue:    'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
        amber:   'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
        rose:    'bg-rose-100 text-rose-700 dark:bg-rose-900/40 dark:text-rose-300',
        emerald: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
        violet:  'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
        cyan:    'bg-cyan-100 text-cyan-700 dark:bg-cyan-900/40 dark:text-cyan-300',
        orange:  'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300',
        slate:   'bg-slate-100 text-slate-700 dark:bg-slate-700/40 dark:text-slate-300',
      };
      return m[color] || m.slate;
    },

    async loadMailboxLabels() {
      try {
        const r = await api.getMailboxLabels();
        this.mailboxRules = (r && r.rules) || [];
        this.mailboxPalette = (r && r.palette) || {};
      } catch (e) {
        this.mailboxRules = [];
      }
    },

    // 与后端 core/mailbox_labels.resolve 同口径：账户精确匹配优先，其次域名后缀
    mailboxLabel(account) {
      const a = String(account || '').trim().toLowerCase();
      if (!a) return null;
      for (const r of this.mailboxRules) {
        if ((r.accounts || []).includes(a)) {
          return { name: r.name, cls: this.mailboxColorClass(r.color) };
        }
      }
      const dom = a.includes('@') ? a.split('@').pop() : '';
      if (!dom) return null;
      for (const r of this.mailboxRules) {
        for (const d of (r.domains || [])) {
          if (dom === d || dom.endsWith('.' + d)) {
            return { name: r.name, cls: this.mailboxColorClass(r.color) };
          }
        }
      }
      return null;
    },

    get mailboxChips() {
      return (this.mailboxRules || []).map(r => ({
        name: r.name,
        cls: this.mailboxColorClass(r.color),
        hint: [...(r.accounts || []), ...(r.domains || []).map(d => '@' + d)].join(' / '),
      }));
    },

    // 按标签筛选：走后端 mailbox 参数（一条规则可覆盖整个域名，单个 account 表达不了）
    setMailbox(name) {
      this.currentMailbox = name || '';
      this.currentAccount = '';
      this.currentFolder = '';
      this.currentTag = '';
      this.activeCategory = '';
      this.view = 'inbox';
      this.page = 0;
      this.searchQuery = '';
      this.currentTopic = '';
      this.fetchEmails();
    },

    setAccountFolder(account, folder = '') {
      this.currentMailbox = '';   // 点具体账户/文件夹时退出标签筛选，避免两种筛选叠加
      this.currentAccount = account;
      this.currentFolder = folder;
      this.view = 'inbox';
      this.page = 0;
      this.searchQuery = '';   // 切换账户/文件夹时清除搜索
      this.currentTopic = '';
      this.fetchEmails();
    },

    // ---- 批量选择 ----
    isSelected(id) { return this.selectedIds.includes(id); },
    toggleSelect(id) {
      const i = this.selectedIds.indexOf(id);
      if (i >= 0) this.selectedIds.splice(i, 1); else this.selectedIds.push(id);
    },
    clearSelection() { this.selectedIds = []; },
    selectAllVisible() {
      const ids = this.emails.map(e => e.id);
      this.selectedIds = (this.selectedIds.length >= ids.length && ids.length > 0) ? [] : ids.slice();
    },
    async bulkAction(action, value = null) {
      if (!this.selectedIds.length) return;
      const cnt = this.selectedIds.length;
      try {
        await api.bulkEmails(this.selectedIds.slice(), action, value);
        try { Alpine.store('ui').notify('已对 ' + cnt + ' 封邮件执行操作', 'success'); } catch (e) {}
      } catch (e) {
        try { Alpine.store('ui').notify('批量操作失败', 'error'); } catch (er) {}
      }
      this.clearSelection();
      this.page = 0;
      await this.fetchEmails();
      this.fetchStats();       // 刷新未读总数
      this.fetchAccounts();    // 刷新各账户未读
      this.loadCategories();
      this.loadGlobalFolders();
    },
    async loadCategories() {
      try { this.categories = (await api.getCategories()) || []; } catch (e) { this.categories = []; }
    },
    async loadDashboard() {
      this.dashLoading = true;
      try { this.dashboard = await api.getDashboard(); } catch (e) { this.dashboard = null; }
      finally { this.dashLoading = false; }
    },
    _maxCount(arr) {
      try { return Math.max(1, ...(arr || []).map(x => x.count || 0)); } catch (e) { return 1; }
    },
    async loadTopics() {
      this.topicsLoading = true;
      let category = '', topic = '';
      if (this.activeCategory) {
        category = this.activeCategory;
      } else {
        const t = (this.inboxTabs || []).find(x => x.name === this.activeTabName);
        if (t) { if (t.kind === 'category') category = t.ref || ''; else if (t.kind === 'topic') topic = t.ref || ''; }
        else if ((this.topicRules || []).some(r => r.name === this.activeTabName)) { topic = this.activeTabName; }
      }
      try {
        this.topics = (await api.getTopics('subject', this.topicMultiOnly ? 2 : 1, category, topic)) || [];
      } catch (e) { this.topics = []; }
      finally { this.topicsLoading = false; }
      (this.topics || []).forEach(tp => {
        tp._expanded = false;
        (tp.members || []).forEach(m => { m._expanded = false; m._loading = false; });
      });
      this.loadTopicRules();
      this.loadInboxTabs();
      this.loadAvailableCats();
      this.loadTopicCounts();
    },
    setActiveTab(name) { this.activeTabName = name || ''; this.activeCategory = ''; this.loadTopics(); },
    enterCategory(cat) {
      this.activeCategory = cat || '';
      this.activeTabName = '';
      this.currentTopic = '';
      this.filter = 'all';
      this.searchQuery = '';
      this.currentTag = '';
      this.selectedId = null;
      this.currentEmail = null;
      this.view = 'topics';
      this.loadTopics();
    },
    openFolderDlg() {
      const acc = (this.displayAccounts[0] && this.displayAccounts[0].name) || '';
      this.folderDlg = { open: true, account: acc, mode: 'new', name: '', existing: '', field: 'from', op: 'contains', value: '' };
    },
    /** 所有已知文件夹名（各账户 + 全局自建），供规则编辑器的 datalist 用 */
    allFolderNames() {
      const out = [];
      for (const a of (this.accounts || [])) {
        for (const f of (a.folders || [])) {
          if (f && !out.includes(f)) out.push(f);
        }
      }
      for (const f of (this.globalFolders || [])) {
        if (f && !out.includes(f)) out.push(f);
      }
      return out;
    },
    folderOptions() {
      if (this.folderDlg.account === '__global__') return this.globalFolders || [];
      const a = (this.displayAccounts || []).find(x => x.name === this.folderDlg.account);
      return (a && a.folders) || [];
    },
    async submitFolderRule() {
      const d = this.folderDlg;
      const folder = (d.mode === 'new' ? d.name : d.existing).trim();
      if (!d.account || !folder) { Alpine.store('ui').notify('请选择账户和文件夹', 'error'); return; }
      if (!d.value.trim()) { Alpine.store('ui').notify('请填写规则条件的值', 'error'); return; }
      const isGlobal = d.account === '__global__';
      try {
        if (d.mode === 'new') await api.addUserFolder(d.account, folder);
        const rname = folder + ' \u21D0 ' + d.field + ':' + d.value.trim();
        await api.addRule({ name: rname, match_mode: 'all',
          conditions: (isGlobal ? [] : [{ field: 'account', op: 'is', value: d.account }]).concat([{ field: d.field, op: d.op, value: d.value.trim() }]),
          actions: { folder: folder } });
        const r = await api.applyRules();
        Alpine.store('ui').notify('已创建「' + folder + '」并归类 ' + ((r && r.applied) || 0) + ' 封', 'success');
        d.open = false;
        if (isGlobal) await this.loadGlobalFolders(); else await this.fetchAccounts();
        await this.fetchEmails();
      } catch (e) { Alpine.store('ui').notify('创建失败: ' + e, 'error'); }
    },
    async importOutlook() {
      try { await api.importOutlookRules(); Alpine.store('ui').notify('已请求导入 Outlook 规则(后台执行,稍后同步生效)', 'success'); }
      catch (e) { Alpine.store('ui').notify('导入请求失败', 'error'); }
    },
    async loadGlobalFolders() {
      try { const uf = await api.getUserFolders(); this.globalFolders = (uf && uf['__global__']) || []; }
      catch (e) { this.globalFolders = []; }
    },
    enterGlobalFolder(name) { this.currentTag = ''; this.setAccountFolder('', name); },
    async loadInboxTabs() { try { this.inboxTabs = (await api.getInboxTabs()) || []; } catch (e) { this.inboxTabs = []; } },
    async loadAvailableCats() { try { this.availableCats = (await api.getCategories()) || []; } catch (e) { this.availableCats = []; } },
    async addInboxTab() {
      const name = (this.newTabName || '').trim();
      const kind = this.newTabKind || 'category';
      const ref = (this.newTabRef || '').trim();
      if (!name || !ref) { try { Alpine.store('ui').notify('标签名和来源都要填', 'info'); } catch (e) {} return; }
      try {
        const r = await api.addInboxTab(name, kind, ref);
        if (r && r.ok) {
          this.inboxTabs = r.inbox_tabs || this.inboxTabs;
          this.newTabName = ''; this.newTabRef = ''; this.showTabManager = false;
          try { Alpine.store('ui').notify('已添加标签', 'success'); } catch (e) {}
        } else { try { Alpine.store('ui').notify('添加失败：' + ((r && r.error) || ''), 'error'); } catch (e) {} }
      } catch (e) { try { Alpine.store('ui').notify('添加失败', 'error'); } catch (er) {} }
    },
    async delInboxTab(name) {
      try {
        const r = await api.delInboxTab(name);
        this.inboxTabs = (r && r.inbox_tabs) || [];
        if (this.activeTabName === name) { this.activeTabName = ''; this.loadTopics(); }
      } catch (e) {}
    },
    setTopicBy(by) { this.topicBy = by; this.loadTopics(); },
    setTopicMultiOnly(v) { this.topicMultiOnly = !!v; this.loadTopics(); },
    toggleTopicExpand(idx) {
      const tp = this.topics[idx];
      if (!tp) return;
      tp._expanded = !tp._expanded;
      if (tp._expanded) {
        const members = tp.members || [];
        if (members.length) {
          // 最新一封自动展开；未读历史消息也自动展开（Gmail 行为）
          this._openThreadMsg(members[members.length - 1]);
          members.forEach(m => { if (m.unread) this._openThreadMsg(m); });
        }
      }
    },
    async _openThreadMsg(m) {
      if (!m || m._expanded) return;
      m._expanded = true;
      if (m._body === undefined && !m._loading) {
        m._loading = true;
        try {
          const e = await api.getEmail(m.id);
          m._html = (e && (e.html_body || e.html)) || '';
          m._senderEmail = (e && e.sender_email) || '';
          m._body = (e && (e.body || e.text)) || '';
        } catch (err) { m._html = ''; m._body = '(正文加载失败)'; }
        finally { m._loading = false; }
      }
    },
    toggleThreadMsg(topicIdx, memberIdx) {
      const tp = this.topics[topicIdx];
      if (!tp || !tp.members) return;
      const m = tp.members[memberIdx];
      if (!m) return;
      if (m._expanded) { m._expanded = false; }
      else { this._openThreadMsg(m); }
    },
    threadMsgHtml(m) {
      if (!m) return '';
      const rich = m._html;
      const opts = { allowRemote: this.threadRemoteAllowed(m) };
      if (rich) return sanitizeHTML(rich, opts) || '(无内容)';
      return sanitizeHTML(m._body || '', opts) || '(无内容)';
    },
    threadRemoteAllowed(m) { return !!(m && (m._showRemote || isImgTrusted(m._senderEmail))); },
    threadRemoteBlocked(m) {
      if (!m || !m._html) return 0;
      return sanitizeRemoteCount(m._html, { allowRemote: this.threadRemoteAllowed(m) });
    },
    threadShowRemote(m) { if (m) m._showRemote = true; },
    async analyzeTopic(idx) {
      const tp = this.topics[idx];
      if (!tp || tp._analyzing) return;
      tp._analyzing = true;
      tp.analysis = '';
      try {
        const r = await api.analyzeTopic(tp.ids);
        tp.analysis = (r && r.analysis) || '(无结果)';
      } catch (e) {
        tp.analysis = '分析失败：' + (e.message || e);
      } finally {
        tp._analyzing = false;
      }
    },
    openTopicEmail(id) {
      this.setView('inbox');
      this.openEmail(id);
    },
    get quickTopics() { return (this.topicRules || []).filter(r => r.quick).slice(0, 3); },
    async toggleQuick(name) {
      const r = (this.topicRules || []).find(x => x.name === name); if (!r) return;
      if (!r.quick && this.quickTopics.length >= 3) { try { Alpine.store('ui').notify('快捷栏最多 3 个话题，先移除一个', 'info'); } catch (e) {} return; }
      const v = !r.quick; r.quick = v;
      try { await api.updateTopicRule(name, { quick: v }); } catch (e) {}
    },
    enterTopic(name) {
      this.currentTopic = name;
      this.view = 'inbox';
      this.filter = 'all';
      this.searchQuery = '';
      this.currentAccount = '';
      this.currentFolder = '';
      this.page = 0;
      this.selectedId = null;
      this.currentEmail = null;
      this.topicDigest = '';
      this.fetchEmails();
    },
        get sortedTopics() {
      const rs = (this.topicRules || []).filter(r => this.showHiddenTopics || !r.hidden);
      return [...rs.filter(r => r.pinned), ...rs.filter(r => !r.pinned)];
    },
    async pinTopic(name) {
      const r = (this.topicRules || []).find(x => x.name === name); if (!r) return;
      const v = !r.pinned; r.pinned = v;
      try { await api.updateTopicRule(name, { pinned: v }); } catch (e) {}
    },
    async toggleHideTopic(name) {
      const r = (this.topicRules || []).find(x => x.name === name); if (!r) return;
      const v = !r.hidden; r.hidden = v;
      if (v && this.activeTabName === name) this.setActiveTab('');
      try { await api.updateTopicRule(name, { hidden: v }); } catch (e) {}
    },
    onTopicDragStart(name) { this._dragTopic = name; },
    async onTopicDrop(target) {
      const from = this._dragTopic; this._dragTopic = null;
      if (!from || from === target) return;
      const names = (this.topicRules || []).map(r => r.name);
      const fi = names.indexOf(from), ti = names.indexOf(target);
      if (fi < 0 || ti < 0) return;
      names.splice(ti, 0, names.splice(fi, 1)[0]);
      const by = {}; (this.topicRules || []).forEach(r => by[r.name] = r);
      this.topicRules = names.map(n => by[n]);
      try { await api.reorderTopicRules(names); } catch (e) {}
    },
        async loadTopicCounts() { try { this.topicCounts = (await api.getTopicCounts()) || {}; } catch (e) {} },
    async toggleMute(name) {
      const r = (this.topicRules || []).find(x => x.name === name); if (!r) return;
      const v = !r.mute; r.mute = v;
      try { await api.updateTopicRule(name, { mute: v }); } catch (e) {}
      try { Alpine.store('ui').notify(v ? '已设为免打扰（从收件箱收起）' : '已取消免打扰', 'success'); } catch (e) {}
      if (this.view === 'inbox' && !this.currentTopic) this.fetchEmails();
    },
    async loadTopicDigest() {
      if (!this.currentTopic || this.topicDigestBusy) return;
      const ids = (this.emails || []).slice(0, 20).map(e => e.id).filter(Boolean);
      if (!ids.length) { try { Alpine.store('ui').notify('这个话题暂无邮件可简报', 'info'); } catch (e) {} return; }
      this.topicDigestBusy = true; this.topicDigest = '';
      try {
        const r = await api.analyzeTopic(ids);
        this.topicDigest = (r && r.analysis) || '(无结果)';
      } catch (e) { this.topicDigest = '简报失败：' + (e.message || e); }
      finally { this.topicDigestBusy = false; }
    },
        async loadTopicRules() {
      try { this.topicRules = (await api.getTopicRules()) || []; } catch (e) { this.topicRules = []; }
    },
    _defRule() {
      const d = this.topicDef || {}; const df = d.filters || {}; const f = {};
      ['cc_me','to_me','has_attachment','unread','starred'].forEach(k => { if (df[k]) f[k] = true; });
      if ((df.from||'').trim()) f.from = df.from.trim();
      if ((df.category||'').trim()) f.category = df.category.trim();
      const kws = (d.keywords||'').split(',').map(s => s.trim()).filter(Boolean);
      return { name: (d.name||'').trim(), keywords: kws, query: (d.query||'').trim(), filters: f };
    },
    _hasCond(rule) { return rule.keywords.length || rule.query || Object.keys(rule.filters).length; },
    async nlGenerate() {
      const text = (this.topicDef.nl || '').trim();
      if (!text) { try { Alpine.store('ui').notify('先用一句话描述这个话题', 'info'); } catch (e) {} return; }
      this.topicDefBusy = true;
      try {
        const r = await api.topicFromNl(text);
        if (r && r.ok) {
          const f = r.filters || {};
          this.topicDef.filters.cc_me = !!f.cc_me;
          this.topicDef.filters.to_me = !!f.to_me;
          this.topicDef.filters.has_attachment = !!f.has_attachment;
          this.topicDef.filters.unread = !!f.unread;
          this.topicDef.filters.starred = !!f.starred;
          this.topicDef.filters.from = f.from || '';
          this.topicDef.filters.category = f.category || '';
          this.topicDef.keywords = (r.keywords || []).join(', ');
          this.topicDef.query = r.query || '';
          if (!(this.topicDef.name || '').trim()) this.topicDef.name = text.slice(0, 12);
          await this.previewTopicDef();
          try { Alpine.store('ui').notify('已生成条件，可微调后保存', 'success'); } catch (e) {}
        } else { try { Alpine.store('ui').notify('生成失败：' + ((r && r.error) || ''), 'error'); } catch (e) {} }
      } catch (e) { try { Alpine.store('ui').notify('生成失败', 'error'); } catch (er) {} }
      finally { this.topicDefBusy = false; }
    },
    async previewTopicDef() {
      const rule = this._defRule();
      if (!this._hasCond(rule)) { try { Alpine.store('ui').notify('先设置至少一个条件', 'info'); } catch (e) {} return; }
      this.topicPreviewCount = '…';
      try {
        const r = await api.previewTopic(rule.query, rule.keywords, rule.filters);
        this.topicPreviewCount = (r && typeof r.count === 'number') ? r.count : 0;
      } catch (e) { this.topicPreviewCount = null; }
    },
    async saveTopicDef() {
      const rule = this._defRule();
      if (!rule.name) { try { Alpine.store('ui').notify('给话题起个名字', 'info'); } catch (e) {} return; }
      if (!this._hasCond(rule)) { try { Alpine.store('ui').notify('至少设置一个条件（属性/关键词/语义）', 'info'); } catch (e) {} return; }
      try {
        const r = await api.addTopicRule(rule.name, rule.keywords, rule.query, rule.filters);
        if (r && r.ok) {
          this.topicRules = r.topic_rules || this.topicRules;
          this.topicDef = { name: '', nl: '', keywords: '', query: '', filters: { cc_me: false, to_me: false, has_attachment: false, unread: false, starred: false, from: '', category: '' } };
          this.topicPreviewCount = null;
          this.setActiveTab(rule.name);
          try { Alpine.store('ui').notify('已保存话题', 'success'); } catch (e) {}
        } else { try { Alpine.store('ui').notify('保存失败：' + ((r && r.error) || ''), 'error'); } catch (e) {} }
      } catch (e) { try { Alpine.store('ui').notify('保存失败', 'error'); } catch (er) {} }
    },
    async delTopicRule(name) {
      try { const r = await api.delTopicRule(name); this.topicRules = (r && r.topic_rules) || []; await this.loadTopics(); } catch (e) {}
    },
    async extractTodosFor(id, force = false) {
      if (!id || this.todoExtracting) return;
      this.todoExtracting = true;
      try {
        const r = await api.extractTodos(id, force);
        if (r && r.ok) {
          // 区分三种情况，否则「0 条」既可能是没待办、也可能是全都已存在，
          // 用户无法判断该不该再点一次。
          const from = r.source === 'cached' ? '摘要已提取的' : 'AI 新提取的';
          let msg;
          if (r.count > 0) {
            msg = '已加入 ' + r.count + ' 条待办（' + from + ' ' + (r.considered || r.count) + ' 条）';
          } else if (r.considered > 0) {
            msg = from + ' ' + r.considered + ' 条待办均已在列表中，未重复添加';
          } else {
            msg = '该邮件没有可提取的待办';
          }
          try { Alpine.store('ui').notify(msg, r.count > 0 ? 'success' : 'info'); } catch (e) {}
          if (this.fetchTodos) await this.fetchTodos();
        } else {
          const err = (r && r.error) ? ('：' + r.error) : '';
          try { Alpine.store('ui').notify('提取待办失败' + err, 'error'); } catch (e) {}
        }
      } catch (e) {
        try { Alpine.store('ui').notify('提取待办失败', 'error'); } catch (er) {}
      } finally {
        this.todoExtracting = false;
      }
    },

    // ---- 保存的搜索 / 智能视图 ----
    async loadSavedSearches() {
      try { this.savedSearches = (await api.getSavedSearches()) || []; } catch (e) { this.savedSearches = []; }
    },
    async saveCurrentSearch() {
      const q = (this.searchQuery || '').trim();
      if (!q) { try { Alpine.store('ui').notify('先输入搜索条件再保存', 'info'); } catch (e) {} return; }
      const name = (window.prompt('给这个搜索起个名字：', q.slice(0, 20)) || '').trim();
      if (!name) return;
      try {
        const r = await api.addSavedSearch(name, q);
        if (r && r.saved_searches) this.savedSearches = r.saved_searches;
        try { Alpine.store('ui').notify('已保存搜索「' + name + '」', 'success'); } catch (e) {}
      } catch (e) { try { Alpine.store('ui').notify('保存失败', 'error'); } catch (er) {} }
    },
    applySavedSearch(query) {
      this.searchQuery = query; this.page = 0; this.view = 'inbox'; this.currentFolder = '';
      this.fetchEmails();
    },
    async deleteSavedSearch(name) {
      try { const r = await api.delSavedSearch(name); this.savedSearches = (r && r.saved_searches) || []; } catch (e) {}
    },

    // ============ 邮件详情 ============

    async openEmail(id) {
      this.selectedId = id;
      this.detailLoading = true;
      // 乐观即时标记：打开时若未读且未开启延迟，立即标为已读，不用等正文网络拉取返回
      const email = this.emails.find(e => e.id === id);
      if (email && email.unread && !this.deferRead) {
        email.unread = false;
        this._bumpUnread(-1);
        api.patchEmail(id, { unread: false }).catch(() => {});
      }
      try {
        this.currentEmail = await api.getEmail(id);
        if (this.currentEmail && !this.deferRead) {
          this.currentEmail.unread = false;
        }
      } catch (err) {
        console.error('[Mail] openEmail error:', err);
        Alpine.store('ui').notify('加载邮件详情失败', 'error');
      } finally {
        this.detailLoading = false;
      }
    },

    // ============ 邮件操作 ============

    async toggleStar() {
      if (!this.selectedId) return;
      const email = this.emails.find(e => e.id === this.selectedId);
      if (!email) return;
      email.starred = !email.starred;
      try {
        await api.patchEmail(this.selectedId, { starred: email.starred });
      } catch {
        email.starred = !email.starred;
      }
    },

    async toggleStarById(id) {
      const email = this.emails.find(e => e.id === id);
      if (!email) return;
      email.starred = !email.starred;
      try {
        await api.patchEmail(id, { starred: email.starred });
      } catch {
        email.starred = !email.starred;
      }
    },
    async toggleArchiveById(id) {
      const email = this.emails.find(e => e.id === id);
      if (!email) return;
      const na = !email.archived;
      email.archived = na;
      try {
        await api.patchEmail(id, { archived: na ? 1 : 0 });
        if (na && (this.view === 'inbox' || this.view === 'unread')) {
          this.emails = this.emails.filter(e => e.id !== id);
        }
      } catch {
        email.archived = !na;
      }
    },
    async toggleTagById(id, tag) {
      const email = this.emails.find(e => e.id === id);
      if (!email) return;
      const tags = Array.isArray(email.tags) ? email.tags.slice() : [];
      const i = tags.indexOf(tag);
      if (i >= 0) tags.splice(i, 1); else tags.push(tag);
      email.tags = tags;
      try { await api.patchEmail(id, { tags: tags }); } catch {}
    },
    setTag(tag) {
      this.currentTag = tag;
      this.view = '';
      this.currentTopic = '';
      this.filter = 'all';
      this.searchQuery = '';
      this.page = 0;
      this.selectedId = null;
      this.currentEmail = null;
      this.fetchEmails();
    },
    tagColor(name) {
      const t = (this.tagPalette || []).find(x => x.name === name);
      return t ? t.color : '#9ca3af';
    },

    async toggleUnread() {
      if (!this.selectedId) return;
      const email = this.emails.find(e => e.id === this.selectedId);
      if (!email) return;
      email.unread = !email.unread;
      this._bumpUnread(email.unread ? +1 : -1);
      try {
        await api.patchEmail(this.selectedId, { unread: email.unread });
      } catch {
        email.unread = !email.unread;
        this._bumpUnread(email.unread ? +1 : -1);
      }
    },

    async deleteSelected() {
      if (!this.selectedId) return;
      const delId = this.selectedId;
      const delEmail = this.emails.find(e => e.id === delId);
      try {
        const res = await api.deleteEmail(delId);
        if (delEmail && delEmail.unread) this._bumpUnread(-1);
        this.emails = this.emails.filter(e => e.id !== delId);
        Alpine.store('ui').notify('邮件已删除', 'success');
        this.trackAction(res && res.action_id, { done: '删除已在 Outlook 生效', error: '删除失败' });
        // 选中下一封
        if (this.emails.length > 0) {
          this.selectedId = this.emails[0].id;
          this.openEmail(this.selectedId);
        } else {
          this.selectedId = null;
          this.currentEmail = null;
        }
      } catch (err) {
        Alpine.store('ui').notify('删除失败', 'error');
      }
    },

    // ============ 通讯录 ============

    async fetchContacts(force = false) {
      if (this.contactsLoaded && !force) return;
      try {
        const rows = await api.getContacts();
        this.contacts = Array.isArray(rows) ? rows : [];
        this.contactsLoaded = true;
      } catch (err) {
        console.error('[Mail] fetchContacts error:', err);
      }
    },

    // 通讯录页的过滤结果。纯本地，0 延迟。
    get filteredContacts() {
      const q = (this.contactQuery || '').trim().toLowerCase();
      if (!q) return this.contacts;
      return this.contacts.filter(c =>
        (c.email || '').toLowerCase().includes(q) ||
        (c.name || '').toLowerCase().includes(q));
    },

    // 写邮件给某位联系人
    composeTo(addr) {
      if (!addr) return;
      this.pendingComposeTo = addr;
      Alpine.store('ui').composeMode = 'new';
      Alpine.store('ui').composeOpen = true;
    },

    // 看与某位联系人的往来邮件：走现有搜索，不新增端点
    viewCorrespondence(addr) {
      if (!addr) return;
      this.view = 'inbox';
      this.currentTopic = '';
      this.currentTag = '';
      this.searchQuery = addr;
      this.page = 0;
      this.fetchEmails();
    },

    // ============ 草稿箱 ============

    async fetchDrafts() {
      try {
        this.drafts = await api.getDrafts();
      } catch (err) {
        console.error('[Mail] fetchDrafts error:', err);
      }
    },

    // 保存草稿：有 id 走 PATCH，无 id 走 POST。回 id 供 compose 继续跟踪。
    async saveDraft(payload, draftId = null) {
      try {
        if (draftId) {
          await api.patchDraft(draftId, payload);
          await this.fetchDrafts();
          Alpine.store('ui').notify('草稿已更新', 'success');
          return draftId;
        }
        const res = await api.createDraft(Object.assign({ source: 'ui' }, payload));
        await this.fetchDrafts();
        Alpine.store('ui').notify('已存入草稿箱', 'success');
        return res && res.id;
      } catch (err) {
        Alpine.store('ui').notify('保存草稿失败', 'error');
        return null;
      }
    },

    // 打开草稿到撰写窗。compose 的 prefill() 会读 editingDraft。
    openDraft(id) {
      const d = this.drafts.find(x => x.id === id);
      if (!d) return;
      this.editingDraft = d;
      Alpine.store('ui').composeMode = 'draft';
      Alpine.store('ui').composeOpen = true;
    },

    async removeDraft(id) {
      try {
        await api.deleteDraft(id);
        this.drafts = this.drafts.filter(d => d.id !== id);
      } catch (err) {
        Alpine.store('ui').notify('删除草稿失败', 'error');
      }
    },

    async sendEmail(data) {
      try {
        const res = await api.sendEmail(data);
        if (res && res.ok === false) {
          Alpine.store('ui').notify(res.error || '发送失败', 'error');
          throw new Error(res.error || 'send failed');
        }
        Alpine.store('ui').notify('已提交发送…', 'info');
        Alpine.store('ui').composeOpen = false;
        // 草稿的使命到此结束；删除失败不影响已提交的发送，只记日志
        if (data && data.draft_id) {
          this.removeDraft(data.draft_id).catch(() => {});
          this.editingDraft = null;
        }
        this.trackAction(res && res.action_id, { done: '邮件已发送', error: '发送失败' });
      } catch (err) {
        Alpine.store('ui').notify('发送失败', 'error');
        throw err;
      }
    },

    async forwardEmail(id, to, note, opts = {}) {
      try {
        const res = await api.forwardEmail(id, {
          to, note,
          cc: opts.cc || '',
          bcc: opts.bcc || '',
          is_html: opts.is_html || false,
          attachments: opts.attachments || [],
        });
        if (res && res.ok === false) {
          Alpine.store('ui').notify(res.error || '转发失败', 'error');
          return;
        }
        Alpine.store('ui').notify('已提交转发…', 'info');
        this.trackAction(res && res.action_id, { done: '邮件已转发', error: '转发失败' });
      } catch (err) {
        Alpine.store('ui').notify('转发失败', 'error');
      }
    },

    // 轮询队列动作（发送/转发/删除）的最终执行结果并给出反馈。
    // 这些动作由 daemon 异步消费 Outlook COM 执行，需回查真实结果，
    // 避免“看似成功实际未执行”（如 daemon 未运行或动作过期）。
    trackAction(actionId, labels = {}) {
      if (!actionId) return;
      let attempts = 0;
      const poll = setInterval(async () => {
        attempts++;
        try {
          const st = await api.getActionStatus(actionId);
          if (st.status === 'done') {
            clearInterval(poll);
            Alpine.store('ui').notify(labels.done || '操作成功', 'success');
          } else if (st.status === 'error' || st.status === 'expired') {
            clearInterval(poll);
            const detail = st.error ? `：${st.error}` : '';
            Alpine.store('ui').notify((labels.error || '操作失败') + detail, 'error');
          } else if (st.status === 'not_found') {
            clearInterval(poll);
          }
        } catch {
          clearInterval(poll);
        }
        if (attempts > 30) clearInterval(poll);  // 最长约 60s 后停止轮询
      }, 2000);
    },

    // ============ 键盘导航 ============

    selectNext() {
      const list = this.emails || [];
      if (!list.length) return;
      const idx = list.findIndex(e => e.id === this.selectedId);
      if (idx < 0) { this.gotoEmail(list[0].id); return; }   // 尚未选中：从第一封开始
      if (idx < list.length - 1) { this.gotoEmail(list[idx + 1].id); return; }
      // 已在当前页末尾：还有下一页就先加载，加载完继续下移（列表是无限滚动分页）
      if (this.hasMore && !this.loading) {
        Promise.resolve(this.loadMore()).then(() => {
          const l2 = this.emails || [];
          const i2 = l2.findIndex(e => e.id === this.selectedId);
          if (i2 >= 0 && i2 < l2.length - 1) this.gotoEmail(l2[i2 + 1].id);
        }).catch(() => {});
      }
    },

    selectPrev() {
      const list = this.emails || [];
      if (!list.length) return;
      const idx = list.findIndex(e => e.id === this.selectedId);
      if (idx < 0) { this.gotoEmail(list[0].id); return; }
      if (idx > 0) this.gotoEmail(list[idx - 1].id);
    },

    // 键盘跳转统一入口：选中 + 打开 + 滚动进视口
    gotoEmail(id) {
      if (!id) return;
      this.selectedId = id;
      this.openEmail(id);
      this.scrollRowIntoView(id);
    },

    scrollRowIntoView(id) {
      try {
        const sel = (window.CSS && CSS.escape) ? CSS.escape(String(id)) : String(id);
        requestAnimationFrame(() => {
          const row = document.querySelector('[data-email-id="' + sel + '"]');
          if (row && row.scrollIntoView) row.scrollIntoView({ block: 'nearest' });
        });
      } catch (e) { /* 定位失败不影响选中 */ }
    },

    // ============ 账户/同步 ============

    async fetchAccounts() {
      try {
        this.accounts = await api.getAccounts();
      } catch (err) {
        console.error('[Mail] fetchAccounts error:', err);
      }
    },

    // ============ 账户显示偏好（别名 / 排序）============

    _ACCT_PREFS_KEY: 'quillbox-account-prefs',

    _loadAccountPrefs() {
      try {
        const raw = localStorage.getItem(this._ACCT_PREFS_KEY);
        if (raw) {
          const p = JSON.parse(raw);
          this.accountAliases = (p && typeof p.aliases === 'object' && p.aliases) || {};
          this.accountOrder = Array.isArray(p && p.order) ? p.order : [];
          this.folderOrders = (p && typeof p.folderOrders === 'object' && p.folderOrders) || {};
        }
      } catch (e) {
        console.warn('[Mail] 读取账户偏好失败:', e);
      }
    },

    _saveAccountPrefs() {
      try {
        localStorage.setItem(this._ACCT_PREFS_KEY, JSON.stringify({
          aliases: this.accountAliases,
          order: this.accountOrder,
          folderOrders: this.folderOrders,
        }));
      } catch (e) {
        console.warn('[Mail] 保存账户偏好失败:', e);
      }
    },

    // 重命名账户（仅显示别名；传空则恢复原始地址）
    renameAccount(email, alias) {
      alias = (alias || '').trim();
      const next = { ...this.accountAliases };
      if (alias && alias !== email) next[email] = alias;
      else delete next[email];
      this.accountAliases = next;   // 替换引用以触发响应式
      this._saveAccountPrefs();
    },

    // 拖拽排序：把 draggedEmail 移动到 targetEmail 之前
    reorderAccount(draggedEmail, targetEmail) {
      if (!draggedEmail || draggedEmail === targetEmail) return;
      let order = this.displayAccounts.map(a => a.name).filter(e => e !== draggedEmail);
      const idx = order.indexOf(targetEmail);
      if (idx === -1) order.push(draggedEmail);
      else order.splice(idx, 0, draggedEmail);
      this.accountOrder = order;
      this._saveAccountPrefs();
    },
    _orderFolders(name, folders) {
      const ord = (this.folderOrders && this.folderOrders[name]) || [];
      const rank = (f) => { const i = ord.indexOf(f); return i === -1 ? Number.MAX_SAFE_INTEGER : i; };
      return (folders || []).slice()
        .map((f, i) => ({ f, i }))
        .sort((x, y) => { const rx = rank(x.f), ry = rank(y.f); return rx !== ry ? rx - ry : x.i - y.i; })
        .map((o) => o.f);
    },
    reorderFolder(account, dragged, target) {
      if (!dragged || dragged === target) return;
      const acct = this.displayAccounts.find(a => a.name === account);
      if (!acct) return;
      let order = (acct.folders || []).filter(f => f !== dragged);
      const idx = order.indexOf(target);
      if (idx === -1) order.push(dragged); else order.splice(idx, 0, dragged);
      this.folderOrders = { ...this.folderOrders, [account]: order };
      this._saveAccountPrefs();
    },

    async fetchStats() {
      try {
        this.stats = await api.getStats();
      } catch (err) {
        console.error('[Mail] fetchStats error:', err);
      }
    },

    async sync() {
      try {
        Alpine.store('ui').syncRunning = true;
        await api.triggerSync();
        Alpine.store('ui').notify('同步已开始', 'info');
        // 轮询同步状态
        const poll = setInterval(async () => {
          await this.fetchStats();
          if (!this.stats.sync_running) {
            clearInterval(poll);
            Alpine.store('ui').syncRunning = false;
            await this.fetchEmails();
            Alpine.store('ui').notify('同步完成', 'success');
          }
        }, 2000);
      } catch (err) {
        Alpine.store('ui').syncRunning = false;
        Alpine.store('ui').notify('同步失败', 'error');
      }
    },

    // ============ 待办 ============

    async fetchTodos() {
      try {
        this.todos = await api.getTodos();
      } catch (err) {
        console.error('[Mail] fetchTodos error:', err);
      }
    },

    async addTodo(text) {
      const content = (text || '').trim();
      if (!content) return;
      try {
        await api.createTodo({ content });
        await this.fetchTodos();
      } catch (err) {
        Alpine.store('ui').notify('创建待办失败', 'error');
      }
    },

    async toggleTodo(id) {
      const todo = this.todos.find(t => t.id === id);
      if (!todo) return;
      const to = (todo.status === 'done') ? 'pending' : 'done';
      todo.status = to;
      try {
        await api.patchTodo(id, { status: to });
      } catch {
        todo.status = (to === 'done') ? 'pending' : 'done';
      }
    },

    async removeTodo(id) {
      try {
        await api.deleteTodo(id);
        this.todos = this.todos.filter(t => t.id !== id);
      } catch (err) {
        Alpine.store('ui').notify('删除待办失败', 'error');
      }
    },
    openTodoSource(id) {
      if (!id) return;
      this.view = 'inbox';
      this.fetchEmails();
      this.openEmail(id);
    },
    openTodoBatch() {
      const b = this.todoBatch;
      b.open = true; b.running = false; b.scanned = 0; b.total = 0; b.added = 0;
    },
    async runTodoBatch() {
      const b = this.todoBatch;
      if (b.running) return;
      b.running = true; b.scanned = 0; b.total = 0; b.added = 0;
      try {
        const r = await api.extractTodosBatch({ days: Number(b.days) || 7, accounts: (b.accounts || []).slice(), use_ai: !!b.useAI, max_emails: 200 });
        if (!r || !r.ok) { b.running = false; try { Alpine.store('ui').notify((r && r.error) || '启动失败', 'error'); } catch (e) {} return; }
        const self = this;
        const poll = async function () {
          try {
            const s = await api.extractTodosBatchStatus();
            b.scanned = s.scanned || 0; b.total = s.total || 0; b.added = s.added || 0;
            if (s.running) { setTimeout(poll, 1500); }
            else {
              b.running = false;
              try { Alpine.store('ui').notify('批量提取完成：扫描 ' + (s.scanned||0) + ' 封，新增 ' + (s.added||0) + ' 条待办', 'success'); } catch (e) {}
              await self.fetchTodos();
            }
          } catch (e) { b.running = false; }
        };
        setTimeout(poll, 1500);
      } catch (e) { b.running = false; try { Alpine.store('ui').notify('启动失败', 'error'); } catch (er) {} }
    },

    // ============ 辅助 ============

    // 本地维护未读计数，保证「打开邮件即已读」时侧边栏立即刷新，
    // 无需等待下一次 fetchStats。delta 为 +1 / -1。
    _bumpUnread(delta) {
      if (!this.stats) return;
      for (const k of ['unread', 'unread_inbox']) {
        if (typeof this.stats[k] === 'number') {
          this.stats[k] = Math.max(0, this.stats[k] + delta);
        }
      }
    },

    // 侧边栏徽标：优先用后端的收件箱口径（未删除+未归档+排除免打扰），
    // 它与收件箱/未读列表能显示的内容一一对应，不会出现点不掉的计数。
    // 老后端没有 unread_inbox 时回退到全库未读，再回退到当前页统计。
    get unreadCount() {
      const s = this.stats || {};
      if (typeof s.unread_inbox === 'number') return s.unread_inbox;
      if (typeof s.unread === 'number') return s.unread;
      return this.emails.filter(e => e.unread).length;
    },

    // 应用别名与自定义排序后的账户列表（供侧边栏渲染）。
    // 未纳入自定义顺序的账户保持后端原有相对顺序，排在已排序账户之后。
    get displayAccounts() {
      const accts = this.accounts || [];
      const order = this.accountOrder || [];
      const rank = (name) => {
        const i = order.indexOf(name);
        return i === -1 ? Number.MAX_SAFE_INTEGER : i;
      };
      return accts
        .map((a, i) => ({ a, i }))
        .sort((x, y) => {
          const rx = rank(x.a.name), ry = rank(y.a.name);
          return rx !== ry ? rx - ry : x.i - y.i;
        })
        .map(({ a }) => ({
          ...a,
          displayName: this.accountAliases[a.name] || a.name,
          folders: this._orderFolders(a.name, a.folders || []),
        }));
    },

    get selectedEmail() {
      return this.emails.find(e => e.id === this.selectedId);
    },
  };
}
