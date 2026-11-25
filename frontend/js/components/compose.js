/**
 * 写邮件组件（富文本编辑 + 附件 + 表格）
 */
import * as api from '../api.js';

// 保存编辑器光标位置（模块级，避免被 Alpine 响应式代理包裹导致 Range 失效）
let _savedRange = null;

export default () => ({
  to: '',
  cc: '',
  bcc: '',
  showBcc: false,        // 密送默认折叠：低频字段不该占常用路径的地方
  subject: '',
  body: '',              // HTML 正文
  attachments: [],       // [{name, size, path, uploading}]
  sending: false,
  savingDraft: false,
  draftId: null,         // 非空表示当前编辑的是草稿箱里的某一条

  // ---- 发件人 ----
  senders: [],
  fromAccount: '',

  // ---- 富文本工具栏 ----
  fmt: {},                 // queryCommandState 快照，用于按钮高亮
  curBlock: 'p',           // 当前段落样式，用于下拉回显
  curFont: '',
  curSize: '',
  colorPickerOpen: false,
  hilitePickerOpen: false,
  tablePickerHeader: true,

  // ---- 收件人补全 ----
  acField: '',           // 当前正在补全哪个字段：to / cc / bcc
  acList: [],
  acIndex: 0,
  maximized: false,      // 撰写窗口是否最大化
  tablePickerOpen: false,
  tableRows: 3,
  tableCols: 3,

  get mode() { return Alpine.store('ui').composeMode; },

  init() {
    this.$watch(() => Alpine.store('ui').composeOpen, (open) => {
      if (open) this.prefill();
    });
    // AI 草稿流式生成时实时同步到编辑器
    this.$watch(() => Alpine.store('ai').draftContent, (val) => {
      if (Alpine.store('ui').composeOpen && val) {
        this.setEditorHtml(this._toHtml(val));
      }
    });
  },

  prefill() {
    const mode = this.mode;
    const email = Alpine.store('mail').currentEmail;
    const draft = Alpine.store('mail').editingDraft;
    // 从草稿箱打开：原样恢复，不套用 Re:/Fwd: 那套改写
    if (mode === 'draft' && draft) {
      this.draftId = draft.id;
      this.to = draft.to || '';
      this.cc = draft.cc || '';
      // 草稿刻意不保存 bcc：密送名单落库后会随草稿在服务端草稿箱可见，
      // 那等于把"密送"变成"半公开"。每次撰写重新填。
      this.bcc = '';
      this.showBcc = false;
      this.subject = draft.subject || '';
      this.body = draft.is_html ? (draft.body || '') : this._toHtml(draft.body || '');
      this.attachments = [];
      Alpine.store('mail').editingDraft = null;
      this.$nextTick(() => this.setEditorHtml(this.body));
      return;
    }
    this.draftId = null;
    this.bcc = '';
    this.showBcc = false;
    this.acClose();
    // 可发信账户：只有一个时界面上不显示下拉（永远只有一项的下拉是噪音）
    if (!this.senders.length) {
      (async () => {
        try {
          const d = await api.getSenders();
          this.senders = (d && d.senders) || [];
          if (!this.fromAccount) this.fromAccount = (d && d.default) || '';
        } catch (e) { /* 取不到就退回单账户行为 */ }
      })();
    }
    // 收件人补全的数据源。原来只在访问通讯录页时加载，导致冷启动直接
    // 写邮件时一个候选都出不来（实测 contacts=0）。放在这里按需加载，
    // fetchContacts 内部有 contactsLoaded 短路，只会真取一次（6KB）。
    // 刻意不 await：补全是输入时才用，不该拖慢窗口打开。
    try { Alpine.store('mail').fetchContacts(); } catch (e) {}
    // 通讯录页点"写邮件给 TA"时带过来的收件人
    const pending = Alpine.store('mail').pendingComposeTo;
    if (pending) {
      Alpine.store('mail').pendingComposeTo = '';
      this.$nextTick(() => { this.to = pending + '; '; });
    }
    if (mode === 'reply' && email) {
      this.to = email.from_addr || email.from || '';
      this.subject = email.subject?.startsWith('Re:') ? email.subject : `Re: ${email.subject || ''}`;
      this.body = '';
    } else if (mode === 'reply-all' && email) {
      this.to = email.from_addr || email.from || '';
      const self = (email.account || '').toLowerCase();
      const sender = (email.from_addr || email.from || '').toLowerCase();
      const recips = Array.isArray(email.recipients) ? email.recipients : [];
      const ccAddrs = recips
        .map(r => (r.email || '').trim())
        .filter(a => a && a.toLowerCase() !== self && a.toLowerCase() !== sender);
      this.cc = [...new Set(ccAddrs)].join('; ');
      this.subject = email.subject?.startsWith('Re:') ? email.subject : `Re: ${email.subject || ''}`;
      this.body = '';
    } else if (mode === 'forward' && email) {
      this.to = '';
      this.subject = email.subject?.startsWith('Fwd:') ? email.subject : `Fwd: ${email.subject || ''}`;
      this.body = '';
    } else {
      this.to = '';
      this.cc = '';
      this.subject = '';
      this.body = '';
    }
    this.attachments = [];
    this.$nextTick(() => this.setEditorHtml(this._toHtml(this.body)));
  },

  // ============ 富文本编辑器 ============

  // 从 contenteditable 读取最新 HTML 到 body
  syncBody() {
    const el = this.$refs.editor;
    if (el) this.body = el.innerHTML;
  },

  // 设置编辑器内容
  setEditorHtml(html) {
    this.body = html || '';
    const el = this.$refs.editor;
    if (el) el.innerHTML = this.body;
  },

  // 执行格式化命令（execCommand：主流浏览器均支持，无需额外依赖）
  exec(cmd, val = null) {
    const el = this.$refs.editor;
    if (el) el.focus();
    try { document.execCommand(cmd, false, val); } catch (e) {}
    this.syncBody();
  },

  createLink() {
    const url = prompt('请输入链接地址：', 'https://');
    if (url) this.exec('createLink', url);
  },

  // ============ 内联样式（字体 / 字号 / 颜色）============
  // 不走 execCommand：它的 fontSize 只接受 1~7（老式 <font size>），
  // 表达不了真实 px；foreColor 默认还产出 <font color> 这种上古标记。
  // 这四项本质都是"给选中文字加内联 style"，手工包裹反而更简单可控，
  // 且产出纯内联样式——邮件客户端会剥掉 class，内联才靠得住。
  _styleRange(props) {
    const el = this.$refs.editor;
    if (!el) return;
    this._restoreCaret();
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount) return;
    const range = sel.getRangeAt(0);
    if (!el.contains(range.commonAncestorContainer)) return;

    // 没选中文字：插一个带样式的空 span 并把光标放进去，
    // 后续输入就继承该样式。否则"先设字号再打字"这个最常见用法会失效。
    if (range.collapsed) {
      const span = document.createElement('span');
      Object.assign(span.style, props);
      span.appendChild(document.createTextNode('\u200b'));
      range.insertNode(span);
      const r = document.createRange();
      r.setStart(span.firstChild, 1);
      r.collapse(true);
      sel.removeAllRanges();
      sel.addRange(r);
      el.focus();
      this.syncBody();
      return;
    }

    // 逐个文本节点包裹。整块 extractContents 会在跨段落时
    // 产出 <span> 里套 <p> 的坏结构；surroundContents 则直接抛错。
    let root = range.commonAncestorContainer;
    // TreeWalker 的 root 是文本节点时 nextNode() 恒为 null（root 不被访问）
    if (root.nodeType === 3) root = root.parentNode;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: n => (n.nodeValue && n.nodeValue.length && range.intersectsNode(n))
        ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    if (!nodes.length) return;

    const startC = range.startContainer, endC = range.endContainer;
    const startO = range.startOffset, endO = range.endOffset;
    const spans = [];
    nodes.forEach(n => {
      let node = n;
      // 首尾节点可能只选中了一部分，先把选中的那段切出来。
      // 必须先切尾再切首：同一节点时先切首会让 endOffset 失效。
      if (node === endC && endO < node.nodeValue.length) node.splitText(endO);
      if (node === startC && startO > 0) node = node.splitText(startO);
      if (!node.nodeValue.length) return;
      const span = document.createElement('span');
      Object.assign(span.style, props);
      node.parentNode.insertBefore(span, node);
      span.appendChild(node);
      spans.push(span);
    });

    // 保持选中，方便连着改第二个属性（比如先设字号再设颜色）
    if (spans.length) {
      const r = document.createRange();
      r.setStartBefore(spans[0]);
      r.setEndAfter(spans[spans.length - 1]);
      sel.removeAllRanges();
      sel.addRange(r);
      _savedRange = r;
    }
    el.focus();
    this.syncBody();
  },

  setFont(stack) {
    if (!stack) return;
    this.curFont = stack;
    this._styleRange({ fontFamily: stack });
  },

  setSize(px) {
    if (!px) return;
    this.curSize = String(px);
    this._styleRange({ fontSize: px + 'px' });
  },

  setColor(c) {
    this._styleRange({ color: c });
    this.colorPickerOpen = false;
  },

  setHilite(c) {
    // 'transparent' 用于取消高亮
    this._styleRange({ backgroundColor: c });
    this.hilitePickerOpen = false;
  },

  // ============ 段落样式（标题 / 引用 / 代码块）============

  // 块级内联样式表。**必须写内联** —— 邮件客户端对 h1/h2 的默认样式差异极大，
  // 很多还会整段重置（Outlook 会把标题压回正文大小）。
  // 跟表格边框那次是同一个道理：邮件里只有内联样式靠得住。
  _blockStyles(tag) {
    const M = {
      h1: { fontSize: '24px', fontWeight: '700', lineHeight: '1.35',
            margin: '14px 0 8px' },
      h2: { fontSize: '20px', fontWeight: '700', lineHeight: '1.4',
            margin: '12px 0 6px' },
      h3: { fontSize: '17px', fontWeight: '600', lineHeight: '1.45',
            margin: '10px 0 5px' },
      blockquote: { margin: '8px 0', padding: '4px 0 4px 12px',
                    borderLeft: '3px solid #d1d5db', color: '#4b5563' },
      pre: { fontFamily: 'Consolas, Monaco, monospace', fontSize: '13px',
             background: '#f6f8fa', padding: '10px 12px', borderRadius: '4px',
             border: '1px solid #e5e7eb', whiteSpace: 'pre-wrap',
             margin: '8px 0' },
      p: {},
    };
    return M[tag] || {};
  },

  // 这些属性在切换块型时要先清掉，否则"标题1 → 正文"会残留 24px 大字号，
  // 看起来像没切换成功。
  _BLOCK_STYLE_KEYS: ['fontSize', 'fontWeight', 'lineHeight', 'margin', 'padding',
                      'background', 'backgroundColor', 'borderLeft', 'border',
                      'borderRadius', 'whiteSpace', 'color', 'fontFamily'],

  // 找光标所在的块级元素。失焦时退回用保存的 range。
  _blockAtCaret() {
    const el = this.$refs.editor;
    if (!el) return null;
    const sel = window.getSelection();
    let n = (sel && sel.rangeCount) ? sel.getRangeAt(0).startContainer : null;
    if ((!n || !el.contains(n)) && _savedRange) n = _savedRange.startContainer;
    const BLOCKS = ['P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'PRE',
                    'BLOCKQUOTE', 'DIV', 'LI'];
    while (n && n !== el) {
      if (n.nodeType === 1 && BLOCKS.indexOf(n.tagName) >= 0) return n;
      n = n.parentNode;
    }
    return null;
  },

  // 选区跨了多个段落时，逐个块处理（只改一个块会让"全选设行距"看起来只生效一半）
  _eachSelectedBlock(fn) {
    const el = this.$refs.editor;
    if (!el) return;
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount) return;
    const range = sel.getRangeAt(0);
    const blocks = el.querySelectorAll('p, h1, h2, h3, h4, h5, h6, pre, blockquote, li, div');
    let hit = 0;
    blocks.forEach(b => {
      try {
        if (range.intersectsNode(b)) { fn(b); hit++; }
      } catch (e) { /* 忽略无法比较的节点 */ }
    });
    if (!hit) {
      const b = this._blockAtCaret();
      if (b) fn(b);
    }
  },

  setBlock(tag) {
    if (!tag) return;
    const el = this.$refs.editor;
    if (!el) return;
    this._restoreCaret();
    try {
      document.execCommand('formatBlock', false, '<' + tag + '>');
    } catch (e) { /* 个别浏览器对 pre 不支持，下面的内联样式仍会生效 */ }
    const blk = this._blockAtCaret();
    if (blk) {
      // 先清上一次的块级样式，再套新的
      this._BLOCK_STYLE_KEYS.forEach(k => { blk.style[k] = ''; });
      Object.assign(blk.style, this._blockStyles(tag));
    }
    this.curBlock = tag;
    el.focus();
    this.syncBody();
    this.refreshFmt();
  },

  setLineHeight(v) {
    if (!v) return;
    const el = this.$refs.editor;
    if (!el) return;
    this._restoreCaret();
    this._eachSelectedBlock(b => { b.style.lineHeight = v; });
    el.focus();
    this.syncBody();
  },

  // ============ 工具栏按钮高亮 ============
  // 不反映当前状态的话，用户不知道粗体到底开着没有，只能靠试。
  refreshFmt() {
    const cmds = ['bold', 'italic', 'underline', 'strikeThrough', 'subscript',
                  'superscript', 'insertUnorderedList', 'insertOrderedList',
                  'justifyLeft', 'justifyCenter', 'justifyRight', 'justifyFull'];
    const st = {};
    cmds.forEach(c => {
      try { st[c] = document.queryCommandState(c); } catch (e) { st[c] = false; }
    });
    this.fmt = st;
    // 回显当前段落样式，让下拉显示的是光标所在块的实际类型
    try {
      const v = (document.queryCommandValue('formatBlock') || '').toLowerCase();
      this.curBlock = v || 'p';
    } catch (e) { /* 取不到就不动 */ }
  },

  // ============ 表格：插入之后还得能改 ============

  // 找到光标所在的单元格。失焦时退回用保存的 range，
  // 否则点工具栏按钮的那一刻就已经找不到光标了。
  _cell() {
    const el = this.$refs.editor;
    if (!el) return null;
    const sel = window.getSelection();
    let n = (sel && sel.rangeCount) ? sel.getRangeAt(0).startContainer : null;
    if ((!n || !el.contains(n)) && _savedRange) n = _savedRange.startContainer;
    while (n && n !== el) {
      if (n.nodeType === 1 && (n.tagName === 'TD' || n.tagName === 'TH')) return n;
      n = n.parentNode;
    }
    return null;
  },

  tableAddRow(below = true) {
    const td = this._cell();
    if (!td) { Alpine.store('ui').notify('把光标放到表格里，再插入行', 'info'); return; }
    const tr = td.parentNode;
    const n = tr.children.length;
    const row = document.createElement('tr');
    for (let i = 0; i < n; i++) {
      const c = this._styleCell(document.createElement('td'), false);
      c.innerHTML = '&nbsp;';
      row.appendChild(c);
    }
    tr.parentNode.insertBefore(row, below ? tr.nextSibling : tr);
    // 光标跟到新行，连着加多行时不用每次重新点
    this._placeCaret(row.firstChild);
    this.syncBody();
  },

  tableDelRow() {
    const td = this._cell();
    if (!td) { Alpine.store('ui').notify('把光标放到表格里，再删除行', 'info'); return; }
    const tr = td.parentNode;
    const tbody = tr.parentNode;
    if (tbody.children.length <= 1) {
      // 最后一行删掉就只剩空表格骨架了，整表移除更符合预期
      const table = tbody.closest('table');
      if (table) table.remove();
    } else {
      // 先算好接手光标的行，再删。删完光标若无处可去，
      // 后续任何表格操作都会静默失效（实测过）。
      const idx = Array.prototype.indexOf.call(tr.children, td);
      const next = tr.nextElementSibling || tr.previousElementSibling;
      tr.remove();
      if (next) this._placeCaret(next.children[Math.min(idx, next.children.length - 1)]);
    }
    this.syncBody();
  },

  tableAddCol(right = true) {
    const td = this._cell();
    if (!td) { Alpine.store('ui').notify('把光标放到表格里，再插入列', 'info'); return; }
    const idx = Array.prototype.indexOf.call(td.parentNode.children, td);
    const table = td.closest('table');
    if (!table) return;
    let added = null;
    Array.prototype.forEach.call(table.rows, tr => {
      const ref = tr.children[idx];
      const isHead = ref && ref.tagName === 'TH';
      const c = this._styleCell(document.createElement(isHead ? 'th' : 'td'), isHead);
      c.innerHTML = '&nbsp;';
      if (ref) tr.insertBefore(c, right ? ref.nextSibling : ref);
      else tr.appendChild(c);
      if (tr === td.parentNode) added = c;
    });
    if (added) this._placeCaret(added);
    this.syncBody();
  },

  tableDelCol() {
    const td = this._cell();
    if (!td) { Alpine.store('ui').notify('把光标放到表格里，再删除列', 'info'); return; }
    const idx = Array.prototype.indexOf.call(td.parentNode.children, td);
    const table = td.closest('table');
    if (!table) return;
    if (table.rows[0] && table.rows[0].children.length <= 1) {
      table.remove();
    } else {
      const row = td.parentNode;
      Array.prototype.forEach.call(table.rows, tr => {
        if (tr.children[idx]) tr.children[idx].remove();
      });
      // 同理：删掉光标所在的列之后要把光标交给邻列
      if (row.children.length) {
        this._placeCaret(row.children[Math.min(idx, row.children.length - 1)]);
      }
    }
    this.syncBody();
  },

  // 引用块：execCommand 的 formatBlock 在各浏览器行为不一，自己插更可控，
  // 且能带上邮件里能正常显示的内联左边框
  insertQuote() {
    // 同样不走 insertHTML：实测 margin / padding / border-left 会被剥掉，
    // 只剩 color，发出去的邮件里就看不出这是引用。
    const q = document.createElement('blockquote');
    q.style.margin = '8px 0';
    q.style.padding = '4px 0 4px 12px';
    q.style.borderLeft = '3px solid #d1d5db';
    q.style.color = '#4b5563';
    q.textContent = '引用内容';
    const tail = document.createElement('p');
    tail.appendChild(document.createElement('br'));
    this._insertAtCaret([q, tail]);
    this._placeCaret(q);
    this.syncBody();
  },

  // 保存/恢复编辑器光标（工具栏交互会让编辑器失焦，插入表格前需恢复）
  saveCaret() {
    const el = this.$refs.editor;
    const sel = window.getSelection && window.getSelection();
    if (el && sel && sel.rangeCount) {
      const r = sel.getRangeAt(0);
      if (el.contains(r.commonAncestorContainer)) _savedRange = r;
    }
  },

  _restoreCaret() {
    const el = this.$refs.editor;
    if (!el) return;
    el.focus();
    const sel = window.getSelection();
    if (_savedRange && el.contains(_savedRange.commonAncestorContainer)) {
      sel.removeAllRanges();
      sel.addRange(_savedRange);
    }
  },

  // ============ 结构化内容的插入 ============
  // 刻意不用 execCommand('insertHTML')：它会剥掉简写 CSS 属性。
  // 实测 border:1px solid #999 只剩 border-color、padding 直接消失、
  // border-collapse 也没了。本应用里看不出来（app.css 兜着），
  // 但发出去的 HTML 就真没有边框了——只有收件人才会发现。
  // 直接建 DOM 节点 + el.style 赋值 + Range 插入，不经 sanitizer。
  _insertAtCaret(nodes) {
    const el = this.$refs.editor;
    if (!el) return;
    this._restoreCaret();
    const sel = window.getSelection();
    let range = null;
    if (sel && sel.rangeCount && el.contains(sel.getRangeAt(0).commonAncestorContainer)) {
      range = sel.getRangeAt(0);
    } else {
      // 光标不在编辑器里（比如刚从工具栏点过来）：追加到末尾
      range = document.createRange();
      range.selectNodeContents(el);
      range.collapse(false);
    }
    range.deleteContents();
    const frag = document.createDocumentFragment();
    nodes.forEach(n => frag.appendChild(n));
    range.insertNode(frag);
    this.syncBody();
  },

  // 单元格内联样式。用 .style 逐项赋值而不是 setAttribute 字符串，
  // 表意更清楚，且产出的都是完整属性（不会只剩 border-color）。
  _styleCell(c, head) {
    c.style.border = '1px solid #999999';
    c.style.padding = '4px 8px';
    c.style.minWidth = '48px';
    if (head) {
      c.style.backgroundColor = '#f3f4f6';
      c.style.fontWeight = '600';
    }
    return c;
  },

  // 把光标放进指定单元格。表格结构变更后必须调用：
  // 删掉光标所在的行/列会让光标孤立，后续操作全部静默失效。
  _placeCaret(cell) {
    const el = this.$refs.editor;
    if (!cell || !el) return;
    el.focus();
    const sel = window.getSelection();
    const r = document.createRange();
    r.selectNodeContents(cell);
    r.collapse(true);
    sel.removeAllRanges();
    sel.addRange(r);
    _savedRange = r;
  },

  // 插入表格：使用内联边框样式，保证在收件人邮箱与本应用中都能显示边框
  insertTable(rows, cols, header = false) {
    rows = Math.max(1, Math.min(30, parseInt(rows) || 3));
    cols = Math.max(1, Math.min(12, parseInt(cols) || 3));
    const table = document.createElement('table');
    table.setAttribute('border', '1');
    table.setAttribute('cellspacing', '0');
    table.style.borderCollapse = 'collapse';
    table.style.margin = '8px 0';
    const tbody = document.createElement('tbody');
    for (let r = 0; r < rows; r++) {
      const tr = document.createElement('tr');
      // 表头行用 th + 灰底：内联样式，收件人邮箱里也能看出是表头
      const head = header && r === 0;
      for (let c = 0; c < cols; c++) {
        const cell = this._styleCell(document.createElement(head ? 'th' : 'td'), head);
        cell.innerHTML = '&nbsp;';
        tr.appendChild(cell);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    // 表格后面留一个空段落，否则光标会卡在表格后面出不来
    const tail = document.createElement('p');
    tail.appendChild(document.createElement('br'));
    this._insertAtCaret([table, tail]);
    // 光标落到第一个单元格，插完就能直接打字
    const first = table.querySelector('th, td');
    if (first) this._placeCaret(first);
    this.syncBody();
    this.tablePickerOpen = false;
  },

  _escapeHtml(s) {
    return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  },

  // 纯文本 → HTML（已是 HTML 则原样返回）
  _toHtml(text) {
    if (!text) return '';
    if (/<[a-z][\s\S]*>/i.test(text)) return text;
    return this._escapeHtml(text).replace(/\n/g, '<br>');
  },

  // ============ 附件 ============

  async onFilesSelected(e) {
    const files = Array.from(e.target.files || []);
    e.target.value = '';  // 允许再次选择同一文件
    for (const file of files) {
      if (file.size > 25 * 1024 * 1024) {
        Alpine.store('ui').notify(`「${file.name}」超过 25MB，已跳过`, 'error');
        continue;
      }
      const item = { name: file.name, size: file.size, path: null, uploading: true };
      this.attachments.push(item);
      try {
        const b64 = await this._readFileB64(file);
        const res = await api.uploadAttachment(file.name, b64);
        if (res && res.ok) {
          item.path = res.path;
          item.uploading = false;
        } else {
          throw new Error((res && res.error) || '上传失败');
        }
      } catch (err) {
        this.attachments = this.attachments.filter(a => a !== item);
        Alpine.store('ui').notify(`附件「${file.name}」上传失败`, 'error');
      }
    }
  },

  _readFileB64(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => {
        const s = String(r.result || '');
        const comma = s.indexOf(',');
        resolve(comma >= 0 ? s.slice(comma + 1) : s);
      };
      r.onerror = reject;
      r.readAsDataURL(file);
    });
  },

  removeAttachment(i) {
    this.attachments.splice(i, 1);
  },

  humanSize(n) {
    if (n === null || n === undefined) return '';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  },

  // ============ 发送 ============

  async send() {
    if (!this.to.trim()) {
      Alpine.store('ui').notify('请填写收件人', 'error');
      return;
    }
    this.syncBody();
    if (this.attachments.some(a => a.uploading)) {
      Alpine.store('ui').notify('附件仍在上传，请稍候…', 'info');
      return;
    }
    const paths = this.attachments.filter(a => a.path).map(a => a.path);

    if (this.mode === 'forward') {
      const emailId = Alpine.store('mail').selectedId;
      if (!emailId) {
        Alpine.store('ui').notify('请先选择要转发的邮件', 'error');
        return;
      }
      this.sending = true;
      try {
        await Alpine.store('mail').forwardEmail(emailId, this.to, this.body,
          { is_html: true, attachments: paths });
        this.reset();
        Alpine.store('ui').composeOpen = false;
      } catch { /* handled in store */ }
      finally { this.sending = false; }
      return;
    }

    this.sending = true;
    try {
      const payload = {
        from: this.fromAccount,
        to: this.to,
        cc: this.cc,
        // 服务端只把 bcc 放进 RCPT TO，不写进邮件头——写了就等于泄露密送名单
        bcc: this.bcc,
        subject: this.subject,
        body: this.body,
        is_html: true,
        attachments: paths,
      };
      if (this.mode === 'reply' || this.mode === 'reply-all') {
        const emailId = Alpine.store('mail').selectedId;
        if (emailId) payload.reply_to_entry_id = emailId;
      }
      // 带上来源草稿 id，store 在发送成功后据此清理草稿箱
      if (this.draftId) payload.draft_id = this.draftId;
      await Alpine.store('mail').sendEmail(payload);
      this.reset();
      Alpine.store('ui').composeOpen = false;
    } catch { /* handled in store */ }
    finally { this.sending = false; }
  },

  // ============ 收件人补全 ============
  // 数据来自 store 里一次取回的通讯录，纯本地匹配，0 延迟。

  // 取"正在输入的那一段"。收件人框用 ; 分隔多个地址，
  // 不切段的话已填好的地址会被反复当成查询词。
  _acSeg(value) {
    const s = String(value || '');
    const cut = s.lastIndexOf(';');
    return { head: cut >= 0 ? s.slice(0, cut + 1) : '', tail: (cut >= 0 ? s.slice(cut + 1) : s).trim() };
  },

  acInput(field) {
    this.acField = field;
    const { tail } = this._acSeg(this[field]);
    if (tail.length < 1) { this.acList = []; return; }
    const q = tail.toLowerCase();
    const all = Alpine.store('mail').contacts || [];
    this.acList = all.filter(c =>
      (c.email || '').toLowerCase().includes(q) ||
      (c.name || '').toLowerCase().includes(q)).slice(0, 8);
    this.acIndex = 0;
  },

  acPick(c) {
    if (!c) return;
    const f = this.acField || 'to';
    const { head } = this._acSeg(this[f]);
    this[f] = head + (head ? ' ' : '') + c.email + '; ';
    this.acClose();
  },

  acClose() { this.acList = []; this.acIndex = 0; },

  // 键盘优先：填收件人是纯键盘动作，只能鼠标点会很难用
  acKey(e, field) {
    if (!this.acList.length) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); this.acIndex = (this.acIndex + 1) % this.acList.length; }
    else if (e.key === 'ArrowUp') { e.preventDefault(); this.acIndex = (this.acIndex - 1 + this.acList.length) % this.acList.length; }
    else if (e.key === 'Enter') { e.preventDefault(); this.acPick(this.acList[this.acIndex]); }
    else if (e.key === 'Escape') { this.acClose(); }
  },

  acLabel(c) {
    return (c.name ? c.name + ' <' + c.email + '>' : c.email)
      + (c.count ? '  · ' + c.count + ' 封' : '');
  },

  // 存草稿：不发送，只落库。Agent 写的草稿也走同一张表。
  async saveDraft() {
    this.syncBody();
    if (!this.subject.trim() && !this.body.trim()) {
      Alpine.store('ui').notify('主题与正文不能同时为空', 'error');
      return;
    }
    this.savingDraft = true;
    try {
      const id = await Alpine.store('mail').saveDraft({
        to: this.to, cc: this.cc, subject: this.subject,
        body: this.body, is_html: true,
      }, this.draftId);
      if (id) this.draftId = id;
    } finally {
      this.savingDraft = false;
    }
  },

  async generateDraft(tone) {
    const emailId = Alpine.store('mail').selectedId;
    if (!emailId) {
      Alpine.store('ui').notify('请先选择要回复的邮件', 'error');
      return;
    }
    // 流式内容会通过 init() 里的 draftContent watch 实时写入编辑器
    await Alpine.store('ai').generateDraft(emailId, tone);
  },

  close() {
    Alpine.store('ui').composeOpen = false;
  },

  reset() {
    this.to = '';
    this.cc = '';
    this.bcc = '';
    this.showBcc = false;
    this.subject = '';
    this.body = '';
    this.draftId = null;
    this.attachments = [];
    const el = this.$refs.editor;
    if (el) el.innerHTML = '';
  },
});
