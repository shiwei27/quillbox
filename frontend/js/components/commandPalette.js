/**
 * 命令面板组件
 */
export default () => ({
  get open() { return Alpine.store('ui').commandPaletteOpen; },
  get query() { return Alpine.store('ui').commandQuery; },
  set query(val) { Alpine.store('ui').commandQuery = val; },
  get commands() { return Alpine.store('ui').filteredCommands; },
  selectedIndex: 0,

  execute(cmd) {
    cmd.action();
  },

  onKeydown(e) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      this.selectedIndex = Math.min(this.selectedIndex + 1, this.commands.length - 1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      this.selectedIndex = Math.max(this.selectedIndex - 1, 0);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (this.commands[this.selectedIndex]) {
        this.execute(this.commands[this.selectedIndex]);
      }
    }
  },

  close() {
    Alpine.store('ui').commandPaletteOpen = false;
  },
});
