/**
 * 邮件列表组件
 */
const _timeCache = new Map();

export default () => ({
  get emails() { return Alpine.store('mail').emails; },
  get loading() { return Alpine.store('mail').loading; },
  get selectedId() { return Alpine.store('mail').selectedId; },
  get filter() { return Alpine.store('mail').filter; },
  get hasMore() { return Alpine.store('mail').hasMore; },
  get selectedIds() { return Alpine.store('mail').selectedIds; },
  isSelected(id) { return Alpine.store('mail').isSelected(id); },
  toggleSelect(id) { Alpine.store('mail').toggleSelect(id); },
  clearSelection() { Alpine.store('mail').clearSelection(); },
  selectAllVisible() { Alpine.store('mail').selectAllVisible(); },
  bulkAction(a, v = null) { Alpine.store('mail').bulkAction(a, v); },
  get categories() { return Alpine.store('mail').categories; },

  selectEmail(id) {
    Alpine.store('mail').openEmail(id);
  },

  // 显式「加载更多」按钮（滚动触底自动加载的可靠兜底）
  loadMore() {
    Alpine.store('mail').loadMore();
  },

  // 触底自动加载下一页（分页）
  onScroll(e) {
    const el = e.target;
    if (!el) return;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 240) {
      Alpine.store('mail').loadMore();
    }
  },

  setFilter(f) {
    Alpine.store('mail').setFilter(f);
  },

  formatTime(dateStr) {
    if (!dateStr) return '';
    // 缓存格式化结果（同一分钟内不变）
    const cacheKey = dateStr.slice(0, 16); // 精确到分钟
    if (_timeCache.has(cacheKey)) return _timeCache.get(cacheKey);

    const date = new Date(dateStr);
    const now = new Date();
    const diff = now - date;
    let result;
    if (diff < 86400000 && date.getDate() === now.getDate()) {
      result = date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    } else if (date.getFullYear() === now.getFullYear()) {
      result = date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' });
    } else {
      result = date.toLocaleDateString('zh-CN', { year: 'numeric', month: 'short', day: 'numeric' });
    }

    _timeCache.set(cacheKey, result);
    // 缓存上限防止内存泄漏
    if (_timeCache.size > 500) {
      const first = _timeCache.keys().next().value;
      _timeCache.delete(first);
    }
    return result;
  },

  truncate(text, len = 80) {
    if (!text) return '';
    return text.length > len ? text.slice(0, len) + '...' : text;
  },
});
