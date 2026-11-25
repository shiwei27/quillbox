/**
 * 邮件详情组件
 */
import { sanitizeHTML, sanitizeRemoteCount, sanitizeEmailDoc, sanitizeEmailDocRemoteCount, isImgTrusted, trustImgDomain, senderDomainOf } from '../utils/sanitize.js';  // SANITIZE-HARDEN-20260930

export default () => ({
  /** 收件人/抄送明细是否展开（Gmail 式：默认折叠，避免多人邮件把正文顶下去） */
  recipientsOpen: false,
  /** 外部图片：本封已手动放行的邮件 id（切换邮件自动失效） */
  _remoteOkId: null,

  get email() { return Alpine.store('mail').currentEmail; },

  get toList() { const e = this.email; return (e && e.to_list) || []; },
  get ccList() { const e = this.email; return (e && e.cc_list) || []; },

  /** 这封邮件是否有收件人信息。老邮件走 Outlook COM 抓取时没存 recipients，
   *  此时必须显示「未记录」，不能假装收件人是「我」。 */
  get hasRecipientInfo() { return this.toList.length > 0 || this.ccList.length > 0; },

  /** 当前邮箱账户地址，用于把自己显示成「我」 */
  get _selfAddr() {
    const e = this.email;
    return String((e && e.account) || '').trim().toLowerCase();
  },

  displayAddr(a) {
    if (!a) return '';
    if (a.email && a.email.toLowerCase() === this._selfAddr) return '\u6211';
    return a.name || a.email || '';
  },

  /** 折叠时的摘要：「我」/「张三、李四 等 N 人」 */
  get recipientSummary() {
    const all = this.toList.concat(this.ccList);
    if (!all.length) return '';
    const names = all.map(a => this.displayAddr(a)).filter(Boolean);
    if (names.length <= 2) return names.join('\u3001');
    return names.slice(0, 2).join('\u3001') + ' \u7b49 ' + names.length + ' \u4eba';
  },

  toggleRecipients() { this.recipientsOpen = !this.recipientsOpen; },
  get loading() { return Alpine.store('mail').detailLoading; },
  get summary() { return Alpine.store('ai').summary; },
  get summaryLoading() { return Alpine.store('ai').summaryLoading; },
  get summaryCollapsed() { return Alpine.store('ai').summaryCollapsed; },

  /** 摘要展示：去除 Markdown 记号，保留结构与换行（配合 white-space:pre-wrap 渲染） */
  get summaryDisplay() {
    let s = Alpine.store('ai').summary || '';
    s = s.replace(/^\s*#{1,6}\s*/gm, '')
         .replace(/\*\*(.+?)\*\*/g, '$1')
         .replace(/`([^`]+)`/g, '$1')
         .replace(/^\s*[-*]\s+/gm, '\u00b7 ');
    return s.trim();
  },

  /** 是否为富文本（HTML）正文 */
  get isHtml() {
    const e = this.email;
    return !!(e && (e.html_body || e.html));
  },

  /** 消毒后的邮件正文HTML；优先富文本，回退纯文本 */
  get sanitizedBody() {
    const email = this.email;
    if (!email) return '';
    const rich = email.html_body || email.html;
    const opts = { allowRemote: this.remoteAllowed };
    if (rich) return sanitizeHTML(rich, opts) || '(无内容)';
    return sanitizeHTML(email.body || email.text || '', opts) || '(无内容)';
  },

  /** 外部图片是否放行：本封手动放行，或发件人域名已被信任 */
  get remoteAllowed() {
    const e = this.email;
    if (!e) return false;
    return this._remoteOkId === e.id || isImgTrusted(e.sender_email);
  },
  /** 被拦截的外部资源数（放行后为 0，横幅随之隐藏）；按当前显示方式计（原始样式还含样式表里的 url()） */
  get remoteBlocked() {
    const e = this.email;
    if (!e) return 0;
    const rich = e.html_body || e.html;
    if (!rich) return 0;
    const opts = { allowRemote: this.remoteAllowed };
    return this.viewOriginal ? sanitizeEmailDocRemoteCount(rich, opts) : sanitizeRemoteCount(rich, opts);
  },

  // ---- 原始样式（app2）：HTML 邮件放进沙箱 iframe，按邮件自带样式表排版 ----
  /** 新界面选了「原始样式」且是 HTML 邮件；旧界面没有 v2 store，恒为 false */
  get viewOriginal() {
    const v2 = window.Alpine && Alpine.store('v2');
    return !!(v2 && v2.readerMode === 'original') && this.isHtml;
  },
  /** 交给 iframe srcdoc 的整页文档（只能用于 sandbox 不含 allow-scripts 的 iframe） */
  get frameDoc() {
    const e = this.email;
    const rich = e && (e.html_body || e.html);
    return rich ? sanitizeEmailDoc(rich, { allowRemote: this.remoteAllowed }) : '';
  },
  /** x-effect 调用：文档变了才重载；每次新文档加载后接上自适应高度与快捷键转发 */
  mountFrame(f, doc) {
    if (!f._quillboxWired) {
      f._quillboxWired = true;
      f.addEventListener('load', () => this._wireFrame(f));
    }
    if (f._quillboxSrc === doc) return;
    f._quillboxSrc = doc;
    f.srcdoc = doc;
    // load 要等图片全部加载完；文档一解析好就先接上，免得外部图片慢时框一直是默认高度
    let n = 0;
    const early = () => {
      if (f._quillboxSrc !== doc || ++n > 120) return;
      const d = f.contentDocument;
      if (d && d.URL === 'about:srcdoc' && d.readyState !== 'loading') this._wireFrame(f);
      else requestAnimationFrame(early);
    };
    requestAnimationFrame(early);
  },
  _wireFrame(f) {
    const d = f.contentDocument;
    if (!d || !d.documentElement || f._quillboxDoc === d) return;
    f._quillboxDoc = d;
    if (f._quillboxRo) f._quillboxRo.disconnect();
    // 框内文档没有脚本；监听器由本页注册、在本页执行。尺寸变化（图片加载、窗口变宽变窄）都会触发重算
    f._quillboxRo = new ResizeObserver(() => this._fitFrame(f));
    f._quillboxRo.observe(d.documentElement);
    this._fitFrame(f);
    // 焦点在框内时按键不会冒泡到本页：转发一份，J/K/E 等快捷键照常可用；本页处理了就同时拦下框内默认动作
    d.addEventListener('keydown', (e) => {
      const c = new KeyboardEvent('keydown', { key: e.key, code: e.code, ctrlKey: e.ctrlKey, metaKey: e.metaKey,
        shiftKey: e.shiftKey, altKey: e.altKey, repeat: e.repeat, bubbles: true, cancelable: true });
      if (!document.dispatchEvent(c)) e.preventDefault();
    });
  },
  _fitFrame(f) {
    const d = f.contentDocument;
    const de = d && d.documentElement;
    if (!de) return;
    // 文档里已强制 html/body{height:auto}，html 盒高 = 内容高（能缩也能涨）；再加上横向滚动条占的高度
    const sb = Math.max(0, ((f.contentWindow && f.contentWindow.innerHeight) || 0) - de.clientHeight);
    const px = Math.max(40, Math.ceil(de.getBoundingClientRect().height) + sb) + 'px';
    if (f.style.height !== px) f.style.height = px;
  },
  get senderDomain() { const e = this.email; return senderDomainOf(e && e.sender_email); },
  loadRemoteImages() { const e = this.email; if (e) this._remoteOkId = e.id; },
  trustSenderDomain() {
    const e = this.email;
    if (!e) return;
    trustImgDomain(e.sender_email);
    this._remoteOkId = e.id;   // localStorage 不是响应式的，靠这一步触发重算
  },

  init() {
    this.$watch(() => {
      const m = Alpine.store('mail');
      return m.triageNav ? ((m.currentEmail && m.currentEmail.id) || null) : m.selectedId;   // triageNav：新界面设置
    }, (id) => {
      if (id) {
        Alpine.store('ai').fetchSummary(id);
      } else {
        Alpine.store('ai').cancelSummary();
      }
    });
  },

  toggleSummary() {
    Alpine.store('ai').toggleSummary();
  },

  get todoExtracting() { return Alpine.store('mail').todoExtracting; },
  extractTodos() { Alpine.store('mail').extractTodosFor(Alpine.store('mail').selectedId); },

  // ---- 附件 ----
  // 附件内容不在库里（只存了文件名），点击时才回服务器取。
  // 后端会缓存，第二次就是本地读盘。
  get attachmentList() {
    const e = Alpine.store('mail').currentEmail;
    if (!e) return [];
    let a = e.attachments;
    if (typeof a === 'string') {
      try { a = JSON.parse(a); } catch (err) { a = []; }
    }
    if (!Array.isArray(a)) return [];
    return a.map(x => (typeof x === 'string' ? x : (x && x.name) || ''))
            .filter(Boolean);
  },

  attachmentIcon(name) {
    const n = String(name || '').toLowerCase();
    if (/\.(png|jpe?g|gif|bmp|webp|svg)$/.test(n)) return 'image';
    if (/\.(pdf)$/.test(n)) return 'file-text';
    if (/\.(docx?|rtf)$/.test(n)) return 'file-text';
    if (/\.(xlsx?|csv)$/.test(n)) return 'table';
    if (/\.(pptx?)$/.test(n)) return 'presentation';
    if (/\.(zip|rar|7z|gz|tar)$/.test(n)) return 'archive';
    if (/\.(msg|eml)$/.test(n)) return 'mail';
    return 'paperclip';
  },

  downloading: '',

  async downloadAttachment(name) {
    const e = Alpine.store('mail').currentEmail;
    if (!e || !name) return;
    this.downloading = name;
    try {
      const url = '/api/emails/' + encodeURIComponent(e.id)
                + '/attachment?name=' + encodeURIComponent(name);
      const r = await fetch(url);
      const ct = r.headers.get('Content-Type') || '';
      // 取不到时后端回 JSON 错误（刻意不回空文件——那样用户会以为下载成功了）
      if (ct.includes('application/json')) {
        const j = await r.json();
        Alpine.store('ui').notify(j.error || '取附件失败', 'error');
        return;
      }
      const blob = await r.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 10000);
      Alpine.store('ui').notify('已下载「' + name + '」', 'success');
    } catch (err) {
      Alpine.store('ui').notify('取附件失败：' + (err && err.message || err), 'error');
    } finally {
      this.downloading = '';
    }
  },

  reply() {
    Alpine.store('ui').openCompose('reply');
  },

  replyAll() {
    Alpine.store('ui').openCompose('reply-all');
  },

  forward() {
    Alpine.store('ui').openCompose('forward');
  },

  toggleStar() {
    Alpine.store('mail').toggleStar();
  },

  toggleUnread() {
    Alpine.store('mail').toggleUnread();
  },

  deleteEmail() {
    if (!confirm('确定删除这封邮件？')) return;
    Alpine.store('mail').deleteSelected();
  },

  formatDate(dateStr) {
    if (!dateStr) return '';
    return new Date(dateStr).toLocaleString('zh-CN');
  },
});
