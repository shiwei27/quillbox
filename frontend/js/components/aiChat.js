/**
 * AI聊天面板组件
 */
export default () => ({
  get messages() { return Alpine.store('ai').chatMessages; },
  get loading() { return Alpine.store('ai').chatLoading; },
  get input() { return Alpine.store('ai').chatInput; },
  set input(val) { Alpine.store('ai').chatInput = val; },
  get mode() { return Alpine.store('ai').chatMode; },

  // 自动滚动跟随流式输出的驱动值
  get scrollTick() { return Alpine.store('ai').chatScrollTick; },

  // 是否有选中的邮件（决定“当前邮件”模式是否可用）
  get hasEmail() { return !!Alpine.store('mail').selectedId; },

  // 当前选中邮件对象
  get currentEmail() { return Alpine.store('mail').currentEmail; },

  // 实时上下文指示文案：随模式 / 选中邮件即时变化
  get contextLabel() {
    if (this.mode === 'email') {
      const em = this.currentEmail;
      const subj = em && (em.subject || '').trim();
      return subj ? `正在讨论：${subj}` : '当前邮件（未选择邮件）';
    }
    return '全局对话（基于全部邮件知识库）';
  },

  send() {
    Alpine.store('ai').sendMessage();
    this.$nextTick(() => {
      const container = this.$refs.chatScroll;
      if (container) container.scrollTop = container.scrollHeight;
    });
  },

  setMode(mode) {
    // 无选中邮件时不允许切到“当前邮件”模式
    if (mode === 'email' && !this.hasEmail) {
      try { Alpine.store('ui').notify('请先选择一封邮件再使用“当前邮件”模式', 'info'); } catch {}
      return;
    }
    Alpine.store('ai').chatMode = mode;
  },

  clear() {
    Alpine.store('ai').clearChat();
  },

  exportChat() {
    Alpine.store('ai').exportChat();
  },

  cancel() {
    Alpine.store('ai').cancelChat();
  },

  // 关闭面板：同时停止正在进行的流式请求，确保可靠收回
  close() {
    Alpine.store('ai').cancelChat();
    Alpine.store('ui').aiPanelOpen = false;
  },
});
