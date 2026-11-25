/**
 * 侧边栏组件
 */
export default () => ({
  get accounts() { return Alpine.store('mail').displayAccounts; },
  get view() { return Alpine.store('mail').view; },
  get unreadCount() { return Alpine.store('mail').unreadCount; },
  get todos() { return Alpine.store('mail').todos; },
  get savedSearches() { return Alpine.store('mail').savedSearches; },
  applySavedSearch(q) { Alpine.store('mail').applySavedSearch(q); },
  deleteSavedSearch(n) { Alpine.store('mail').deleteSavedSearch(n); },
  onFolderDragStart(account, folder, e) { this.draggingFolder = folder; this.draggingFolderAccount = account; if (e && e.dataTransfer) { e.dataTransfer.effectAllowed = 'move'; try { e.dataTransfer.setData('text/plain', folder); } catch (_) {} } },
  onFolderDrop(account, folder) { if (this.draggingFolderAccount === account && this.draggingFolder) Alpine.store('mail').reorderFolder(account, this.draggingFolder, folder); this.draggingFolder = null; this.draggingFolderAccount = null; },

  expandedAccounts: {},
  draggingFolder: null,
  draggingFolderAccount: null,
  newTodoText: '',

  // ---- 账户重命名 ----
  editingAccount: null,
  renameValue: '',

  startRename(account) {
    this.editingAccount = account.name;
    this.renameValue = account.displayName || account.name;
  },
  commitRename(email) {
    // 防止 enter 提交后 blur 再次触发、或 escape 取消后被 blur 覆盖
    if (this.editingAccount !== email) return;
    Alpine.store('mail').renameAccount(email, this.renameValue);
    this.editingAccount = null;
    this.renameValue = '';
  },
  cancelRename() {
    this.editingAccount = null;
    this.renameValue = '';
  },

  // ---- 账户拖拽排序 ----
  draggingAccount: null,
  dragOverAccount: null,

  onDragStart(account, e) {
    this.draggingAccount = account.name;
    try {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', account.name);
    } catch (_) {}
  },
  onDragOver(account) {
    if (this.draggingAccount && account.name !== this.draggingAccount) {
      this.dragOverAccount = account.name;
    }
  },
  onDragLeave(account) {
    if (this.dragOverAccount === account.name) this.dragOverAccount = null;
  },
  onDrop(account) {
    if (this.draggingAccount) {
      Alpine.store('mail').reorderAccount(this.draggingAccount, account.name);
    }
    this.draggingAccount = null;
    this.dragOverAccount = null;
  },
  onDragEnd() {
    this.draggingAccount = null;
    this.dragOverAccount = null;
  },

  setView(view) {
    Alpine.store('mail').setView(view);
  },

  toggleAccount(name) {
    this.expandedAccounts[name] = !this.expandedAccounts[name];
  },

  selectFolder(account, folder) {
    Alpine.store('mail').setAccountFolder(account, folder);
  },

  async addTodo() {
    if (!this.newTodoText.trim()) return;
    await Alpine.store('mail').addTodo(this.newTodoText.trim());
    this.newTodoText = '';
  },

  async toggleTodo(id) {
    await Alpine.store('mail').toggleTodo(id);
  },

  async removeTodo(id) {
    await Alpine.store('mail').removeTodo(id);
  },
});
