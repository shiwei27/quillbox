/**
 * 设置面板组件
 */
export default () => ({
  get tab() { return Alpine.store('ui').settingsTab; },
  set tab(v) { Alpine.store('ui').settingsTab = v; },
  get settings() { return Alpine.store('settings').data; },
  get loading() { return Alpine.store('settings').loading; },
  get saving() { return Alpine.store('settings').saving; },
  get testResult() { return Alpine.store('settings').testResult; },
  get testLoading() { return Alpine.store('settings').testLoading; },

  save() {
    Alpine.store('settings').save();
  },

  testAI() {
    Alpine.store('settings').testAIConnection();
  },

  updateField(key, value) {
    Alpine.store('settings').updateField(key, value);
  },

  openRuleIndex: -1,
  ruleSummary(rule) {
    const fmap = { subject: '主题', from: '发件人', to: '收件人', body: '正文', subject_or_body: '主题/正文', account: '账户', folder: '文件夹', has_attachment: '附件' };
    const conds = (rule.conditions || []).filter(c => c.field === 'has_attachment' || (c.value || '').trim());
    const cs = conds.map(c => (fmap[c.field] || c.field) + ((c.value || '').trim() ? (':' + c.value) : '')).join(rule.matchMode === 'any' ? ' 或 ' : ' 且 ');
    const acts = [];
    if (rule.actCategory) acts.push('分类:' + rule.actCategory);
    if (rule.actStar) acts.push('星标');
    if (rule.actMarkRead) acts.push('已读');
    if (rule.actSkipAi) acts.push('跳过AI');
    const ai = (rule.aiMatch || '').trim() ? ' +AI语义' : '';
    return (cs || '(未设条件)') + ai + (acts.length ? ' → ' + acts.join('、') : '');
  },
  get rulesUI() { return Alpine.store('settings').rulesUI; },
  addRule() { Alpine.store('settings').addRule(); this.openRuleIndex = (this.rulesUI || []).length - 1; },
  removeRule(i) { Alpine.store('settings').removeRule(i); },
  moveRule(i, dir) { Alpine.store('settings').moveRule(i, dir); },
  runRules() { Alpine.store('settings').runRules(); },
  addCondition(ri) { Alpine.store('settings').addCondition(ri); },
  importOutlook() { Alpine.store('settings').importOutlook(); },
  removeCondition(ri, ci) { Alpine.store('settings').removeCondition(ri, ci); },
  get genText() { return Alpine.store('settings').genText; },
  set genText(v) { Alpine.store('settings').genText = v; },
  get genLoading() { return Alpine.store('settings').genLoading; },
  generateRuleFromText() { Alpine.store('settings').generateRuleFromText(); },
  get evalTarget() { return Alpine.store('settings').evalTarget; },
  set evalTarget(v) { Alpine.store('settings').evalTarget = v; },
  get evalResult() { return Alpine.store('settings').evalResult; },
  get evalLoading() { return Alpine.store('settings').evalLoading; },
  evaluateSelected() { Alpine.store('settings').evaluateSelected(); },

  // ── 邮箱标签编辑器 ─────────────────────────────────────────────────
  // 表单里用逗号分隔的文本编辑账户/域名，保存时再拆成数组交给后端校验。
  mbRules: [],
  mbPalette: {},
  mbErrors: [],
  mbSaving: false,
  mbSaved: false,

  async loadMb() {
    try {
      const r = await api.getMailboxLabels();
      this.mbPalette = (r && r.palette) || {};
      this.mbRules = ((r && r.rules) || []).map(x => ({
        name: x.name, color: x.color,
        accountsText: (x.accounts || []).join(', '),
        domainsText: (x.domains || []).join(', '),
      }));
      this.mbErrors = [];
    } catch (e) {
      this.mbErrors = ['读取失败：' + e];
    }
  },

  addMbRule() {
    this.mbRules.push({ name: '', color: 'slate', accountsText: '', domainsText: '' });
  },

  delMbRule(i) { this.mbRules.splice(i, 1); },

  async saveMb() {
    this.mbSaving = true; this.mbSaved = false; this.mbErrors = [];
    try {
      const payload = this.mbRules.map(r => ({
        name: (r.name || '').trim(),
        color: r.color || 'slate',
        accounts: (r.accountsText || '').split(/[,，;]/).map(s => s.trim()).filter(Boolean),
        domains: (r.domainsText || '').split(/[,，;]/).map(s => s.trim()).filter(Boolean),
      }));
      const res = await api.saveMailboxLabels(payload);
      if (res && res.ok) {
        this.mbSaved = true;
        await Alpine.store('mail').loadMailboxLabels();   // 立即反映到徽章与筛选条
        Alpine.store('mail').fetchEmails();
        setTimeout(() => { this.mbSaved = false; }, 3000);
      } else {
        this.mbErrors = (res && res.errors) || ['保存失败'];
      }
    } catch (e) {
      this.mbErrors = ['保存失败：' + e];
    } finally {
      this.mbSaving = false;
    }
  },

  close() {
    Alpine.store('ui').settingsOpen = false;
  },
});
