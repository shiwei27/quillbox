/**
 * 搜索栏组件
 */
export default () => ({
  focused: false,

  // query 直接代理 mail store 的 searchQuery：
  // 切换视图/文件夹时 store 清空搜索，输入框会同步清空。
  get query() { return Alpine.store('mail').searchQuery; },
  set query(v) { Alpine.store('mail').searchQuery = v; },

  search() {
    Alpine.store('mail').search(this.query);
  },

  clear() {
    Alpine.store('mail').search('');
  },

  onKeydown(e) {
    if (e.key === 'Escape') {
      if (this.advOpen) { this.advOpen = false; return; }
      e.target.blur();
      this.focused = false;
    }
  },

  // ---- 高级搜索（点选式，避免手写操作符）----
  advOpen: false,
  adv: { from: '', to: '', subject: '', body: '', hasAttachment: false, after: '', before: '', text: '' },
  openAdv() {
    const q = (Alpine.store('mail').searchQuery || '');
    if (q && q.indexOf(':') === -1) this.adv.text = q;   // 纯关键词带入；操作符查询不误映射
    this.advOpen = true;
  },
  closeAdv() { this.advOpen = false; },
  _q(s) { return /\s/.test(s) ? ('"' + s + '"') : s; },
  applyAdv() {
    const a = this.adv, p = [];
    if (a.from) p.push('from:' + this._q(a.from));
    if (a.to) p.push('to:' + this._q(a.to));
    if (a.subject) p.push('subject:' + this._q(a.subject));
    if (a.body) p.push('body:' + this._q(a.body));
    if (a.hasAttachment) p.push('has:attachment');
    if (a.after) p.push('after:' + a.after);
    if (a.before) p.push('before:' + a.before);
    if (a.text) p.push(a.text);
    const q = p.join(' ');
    Alpine.store('mail').searchQuery = q;
    Alpine.store('mail').search(q);
    this.advOpen = false;
  },
});
