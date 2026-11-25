/**
 * 顶栏组件
 */
export default () => ({
  get syncRunning() { return Alpine.store('ui').syncRunning; },
  get stats() { return Alpine.store('mail').stats; },
  get isDark() { return Alpine.store('ui').isDark; },

  sync() {
    Alpine.store('mail').sync();
  },

  toggleTheme() {
    Alpine.store('ui').toggleTheme();
  },

  toggleAI() {
    Alpine.store('ui').toggleAIPanel();
  },

  openSettings() {
    Alpine.store('ui').settingsOpen = true;
  },

  toggleSidebar() {
    Alpine.store('ui').sidebarCollapsed = !Alpine.store('ui').sidebarCollapsed;
  },
});
