/**
 * 设置状态管理
 * 字段名与后端 GET/POST /api/settings 完全一致
 */
import * as api from '../api.js';

export default function settingsStore() {
  return {
    // 设置数据 — 字段名与后端API一致
    data: {
      llm_provider:   'ollama',       // 'ollama' | 'api'
      ollama_url:     'http://127.0.0.1:11434',
      ollama_model:   'qwen2.5:7b',
      api_base_url:   '',
      api_key:        '',
      api_model:      '',
      api_timeout:    120,
      sync_days:      7,
      sync_interval:  15,
      prompt_summary: '',
      prompt_draft:   '',
      prompt_chat:    '',
    prompt_topic_digest: '',
    prompt_topic_nl: '',
    prompt_todo_extract: '',
    prompt_rule_gen: '',
      prompt_rules:   '[]',
      signature:      '',
    },

    // 状态
    loading: false,
    saving: false,
    testResult: null,
    testLoading: false,

    // 可视化规则编辑用的中间模型
    rulesUI: [],
    genText: '',
    genLoading: false,
    evalTarget: 0,
    evalResult: '',
    evalLoading: false,

    init() {
      // 幂等：Alpine.store() 注册时会自动调一次 init()，index.html 的 x-init 又手动
      // 调一次，首屏接口因此全部请求两遍。记住第一次的 Promise，
      // 第二次调用直接等它，x-init 里「先 mail 后 settings」的 await 顺序不变。
      // 失败时清掉，保留原来「再调一次就重试」的语义。
      if (this._initPromise) return this._initPromise;
      this._initPromise = this.fetch().catch((e) => { this._initPromise = null; throw e; });
      return this._initPromise;
    },

    /**
     * 从后端加载设置
     */
    async fetch() {
      this.loading = true;
      try {
        const settings = await api.getSettings();
        if (settings) {
          // 后端返回的字段直接合并（字段名一致）
          this.data = { ...this.data, ...settings };
          this._rulesFromJson();
        }
      } catch (err) {
        console.error('[Settings] fetch error:', err);
      } finally {
        this.loading = false;
      }
    },

    /**
     * 保存设置到后端
     */
    async save() {
      this.saving = true;
      try {
        this._rulesToJson();
        await api.saveSettings(this.data);
        Alpine.store('ui').notify('设置已保存', 'success');
        try { Alpine.store('ui').settingsOpen = false; } catch (e) {}
      } catch (err) {
        Alpine.store('ui').notify('保存设置失败', 'error');
      } finally {
        this.saving = false;
      }
    },

    /**
     * 测试AI连接 — 传入当前表单数据
     */
    async testAIConnection() {
      this.testLoading = true;
      this.testResult = null;
      try {
        const result = await api.testAI(this.data);
        this.testResult = {
          success: result.ok,
          message: result.ok
            ? `${result.label || result.provider} 连接正常`
            : `${result.label || result.provider} 连接失败`,
        };
      } catch (err) {
        this.testResult = { success: false, message: err.message || 'AI连接失败' };
      } finally {
        this.testLoading = false;
      }
    },

    /**
     * 更新设置字段
     */
    updateField(key, value) {
      this.data[key] = value;
    },

    // ===== 可视化规则编辑 =====
    _splitKw(s) {
      return (s || '').split(/[,\uff0c\n]/).map(x => x.trim()).filter(Boolean);
    },
    _ruleToUI(r) {
      const FMAP = { subject: 'subject', sender: 'from', recipient: 'to', body: 'body' };
      let conds = [];
      if (Array.isArray(r.conditions)) {
        conds = r.conditions.map(c => ({ field: c.field || 'subject', op: c.op || 'contains', value: c.value || '' }));
      } else if (r.match && typeof r.match === 'object') {
        for (const k in FMAP) {
          (r.match[k] || []).forEach(kw => { if (kw) conds.push({ field: FMAP[k], op: 'contains', value: kw }); });
        }
      }
      if (conds.length === 0) conds.push({ field: 'subject', op: 'contains', value: '' });
      const acts = r.actions || {};
      return {
        enabled: r.enabled !== false,
        name: r.name || '',
        matchMode: r.match_mode || 'all',
        conditions: conds,
        aiMatch: r.ai_match || '',
        actCategory: acts.category || '',
        actFolder: acts.folder || '',
        actStar: !!acts.star,
        actMarkRead: !!acts.mark_read,
        actSkipAi: !!acts.skip_ai,
        actNoAction: !!acts.no_action,
        summary: acts.summary_prompt || r.summary || '',
        draft: acts.draft_prompt || r.draft || '',
      };
    },
    _uiToRule(u) {
      const conditions = (u.conditions || [])
        .filter(c => c.field && (String(c.value || '').trim() !== '' || c.field === 'has_attachment'))
        .map(c => ({ field: c.field, op: c.op || 'contains', value: c.value || '' }));
      return {
        name: u.name || '',
        enabled: u.enabled !== false,
        match_mode: u.matchMode || 'all',
        conditions,
        ai_match: (u.aiMatch || '').trim(),
        actions: {
          category: (u.actCategory || '').trim(),
          // 必须保留：「文件夹规则」对话框建的规则带 folder，
          // 这里不回写的话，用户在设置页保存一次就会把移动动作丢掉。
          folder: (u.actFolder || '').trim(),
          star: !!u.actStar,
          mark_read: !!u.actMarkRead,
          skip_ai: !!u.actSkipAi,
          // 「无需处理」规则：独立判定、只打标签；丢了这个字段规则会变成无动作的普通规则
          no_action: !!u.actNoAction,
          summary_prompt: u.summary || '',
          draft_prompt: u.draft || '',
        },
      };
    },
    _rulesFromJson() {
      let arr = [];
      try { arr = JSON.parse(this.data.prompt_rules || '[]'); } catch (e) { arr = []; }
      if (!Array.isArray(arr)) arr = [];
      this.rulesUI = arr.map(r => this._ruleToUI(r));
    },
    _rulesToJson() {
      const arr = (this.rulesUI || []).map(u => this._uiToRule(u))
        .filter(r => r.name || r.ai_match || r.conditions.length ||
          r.actions.category || r.actions.folder || r.actions.star ||
          r.actions.mark_read || r.actions.skip_ai ||
          r.actions.summary_prompt || r.actions.draft_prompt);
      this.data.prompt_rules = JSON.stringify(arr);
    },
    addRule() {
      this.rulesUI.push(this._ruleToUI({ name: '', match_mode: 'all', conditions: [] }));
    },
    removeRule(i) {
      this.rulesUI.splice(i, 1);
    },
    moveRule(i, dir) {
      const j = i + dir;
      if (j < 0 || j >= this.rulesUI.length) return;
      const [it] = this.rulesUI.splice(i, 1);
      this.rulesUI.splice(j, 0, it);
    },
    async runRules() {
      try {
        this._rulesToJson();
        await api.saveSettings(this.data);
        const r = await api.applyRules();
        try { Alpine.store('ui').notify('已保存并应用到现有邮件，命中 ' + ((r && r.applied) || 0) + ' 封', 'success'); } catch (e) {}
        try { Alpine.store('mail').fetchEmails(); } catch (e) {}
      } catch (e) {
        try { Alpine.store('ui').notify('应用失败', 'error'); } catch (er) {}
      }
    },
    addCondition(ri) {
      if (this.rulesUI[ri]) this.rulesUI[ri].conditions.push({ field: 'subject', op: 'contains', value: '' });
    },
    async importOutlook() {
      try {
        const r = await api.importOutlookRules();
        if (r && r.ok) {
          await this.fetch();
          try { Alpine.store('ui').notify('已从 Outlook 导入 ' + r.imported + ' 条规则', 'success'); } catch (e) {}
        } else {
          try { Alpine.store('ui').notify('导入失败：' + ((r && r.error) || '未知'), 'error'); } catch (e) {}
        }
      } catch (e) {
        try { Alpine.store('ui').notify('导入失败', 'error'); } catch (er) {}
      }
    },
    removeCondition(ri, ci) {
      const r = this.rulesUI[ri];
      if (r && r.conditions.length > 1) r.conditions.splice(ci, 1);
    },
    // 功能1：自然语言 → 规则
    async generateRuleFromText() {
      const desc = (this.genText || '').trim();
      if (!desc || this.genLoading) return;
      this.genLoading = true;
      try {
        const res = await api.generateRule(desc);
        if (res && res.ok && res.rule) {
          this.rulesUI.push(this._ruleToUI(res.rule));
          this.genText = '';
          try { Alpine.store('ui').notify('已生成规则，请检查后点“保存设置”', 'success'); } catch (e) {}
        } else {
          try { Alpine.store('ui').notify('生成失败：' + ((res && res.error) || '未知'), 'error'); } catch (e) {}
        }
      } catch (e) {
        try { Alpine.store('ui').notify('生成失败', 'error'); } catch (er) {}
      } finally {
        this.genLoading = false;
      }
    },
    // 功能2：评估选中规则（结果显示在 AI 助手工具条）
    async evaluateSelected() {
      const u = this.rulesUI[this.evalTarget];
      if (!u) { this.evalResult = '请先在下拉里选择一条规则'; return; }
      if (this.evalLoading) return;
      this.evalLoading = true; this.evalResult = '';
      try {
        const res = await api.evaluateRule(this._uiToRule(u));
        this.evalResult = (res && res.evaluation) || '（无结果）';
      } catch (e) {
        this.evalResult = '评估失败：' + (e.message || e);
      } finally {
        this.evalLoading = false;
      }
    },
  };
}
