/**
 * API 封装模块
 * 统一处理与后端 localhost:8765 的通信
 */

const BASE_URL = '';  // 前端和API由同一个server提供，使用相对路径

/**
 * 通用请求封装
 * @param {string} path - API路径
 * @param {object} options - fetch选项
 * @returns {Promise<any>}
 */
async function request(path, options = {}) {
  const url = `${BASE_URL}${path}`;
  const config = {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  };

  try {
    const response = await fetch(url, config);
    if (!response.ok) {
      const error = await response.text();
      throw new Error(`API Error ${response.status}: ${error}`);
    }
    // 204 No Content
    if (response.status === 204) return null;
    return await response.json();
  } catch (err) {
    console.error(`[API] ${options.method || 'GET'} ${path} failed:`, err);
    throw err;
  }
}

// ==================== 邮件 ====================

/**
 * 将后端邮件字段映射为前端统一格式
 * 后端: sender, sender_email, time, body, summary
 * 前端: from_name, from_addr, date, body, snippet
 */
function _cleanSnippet(s) {
  if (!s) return '';
  return String(s)
    .replace(/^#{1,6}\s*.*$/gm, '')     // 去掉 markdown 标题行（含“### 结构化摘要”）
    .replace(/\*\*(.+?)\*\*/g, '$1')  // 去粗体标记
    .replace(/【[^】]*】/g, '')           // 去掉 【核心】等段标签
    .replace(/[`#*>]/g, '')              // 残留符号
    .replace(/(^|\n)\s*[·•\-]\s*/g, ' ') // 列表符号
    .replace(/\s+/g, ' ')               // 折叠空白/换行
    .trim();
}

/** 从 recipients（[{name,email,type}]）取某一类地址；剥掉 Exchange 名字外层引号并按邮箱去重 */
function _addrList(recips, type) {
  const out = [];
  const seen = new Set();
  for (const r of (Array.isArray(recips) ? recips : [])) {
    if (!r) continue;
    if ((r.type || 'to').toLowerCase() !== type) continue;
    const email = String(r.email || '').trim();
    const name = String(r.name || '').trim().replace(/^['"]+|['"]+$/g, '');
    if (!email && !name) continue;
    const key = (email || name).toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ name, email });
  }
  return out;
}

/** 地址列表 -> 展示串（有名字用名字，否则用邮箱） */
function _addrText(list) {
  return list.map(a => a.name || a.email).filter(Boolean).join('\u3001');
}

function mapEmail(e) {
  if (!e) return e;
  const _to = _addrList(e.recipients, 'to');
  const _cc = _addrList(e.recipients, 'cc');
  return {
    ...e,
    to_list: _to,
    cc_list: _cc,
    // 兼容既有模板读法：给出可直接显示的字符串（无值时留空，由 UI 决定文案）
    to: _addrText(_to),
    cc: _addrText(_cc),
    from_name: e.sender || e.from_name || '',
    from_addr: e.sender_email || e.from_addr || '',
    from: e.sender_email || e.from || '',
    date: e.time || e.date || '',
    received_at: e.time || e.received_at || '',
    // 列表走 light=1 时没有 body，服务端给了 body_head（压平空白后的前 200 字）
    snippet: _cleanSnippet(e.summary) || (e.body || e.body_head || '').slice(0, 120) || '',
    preview: _cleanSnippet(e.summary) || '',
    html_body: e.html_body || null,   // 富文本正文（含内嵌图片，cid 已重写为 /api/inline URL）
    html: e.html_body || null,        // 兼容旧字段名
    text: e.body || '',
  };
}

/**
 * 获取邮件列表
 * @param {object} params - 查询参数
 */
export async function getEmails(params = {}) {
  const query = new URLSearchParams({
    view: params.view || 'inbox',
    account: params.account || '',
    folder: params.folder || '',
    filter: params.filter || 'all',
    page: String(params.page || 0),
    page_size: String(params.pageSize || 30),
    q: params.q || '',
    tag: params.tag || '',
    mailbox: params.mailbox || '',
    // 列表/搜索只要摘要字段：不带 light 时每封连 body/html_body 一起下发，收件箱
    // 一页可达数百 KB、搜索 50 封可达十几 MB。详情与会话展开各自走
    // /api/emails/{id} 取全量，不受影响。确需整封的调用方传 light: false。
    light: params.light === false ? '0' : '1',
  });
  const data = await request(`/api/emails?${query}`);
  if (data?.emails) {
    data.emails = data.emails.map(mapEmail);
  }
  return data;
}

/**
 * 获取单封邮件详情
 * @param {string} id - 邮件ID
 */
export async function getEmail(id) {
  const email = await request(`/api/emails/${id}`);
  return mapEmail(email);
}

/**
 * 更新邮件状态
 * @param {string} id - 邮件ID
 * @param {object} patch - 更新字段 {starred?, unread?, deleted?}
 */
export async function patchEmail(id, patch) {
  return request(`/api/emails/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
}

/**
 * 删除邮件
 * @param {string} id - 邮件ID
 */
export async function deleteEmail(id) {
  return request(`/api/emails/${id}`, { method: 'DELETE' });
}

/**
 * 发送邮件
 * @param {object} email - {to, cc, subject, body}
 */
export async function sendEmail(email) {
  return request('/api/emails/send', {
    method: 'POST',
    body: JSON.stringify(email),
  });
}

/**
 * 转发邮件
 * @param {string} id - 邮件ID
 * @param {object} data - {to, note}
 */
export async function forwardEmail(id, data) {
  return request(`/api/emails/${id}/forward`, {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

/**
 * 查询邮件动作（发送/转发/删除）的执行状态
 * @param {string} actionId - 动作ID
 * @returns {Promise<{status:string, error?:string}>}
 *   status: pending | done | error | expired | not_found
 */
export async function getActionStatus(actionId) {
  return request(`/api/action/${actionId}`);
}

/**
 * 上传撰写邮件的附件（base64 JSON）
 * @param {string} name - 文件名
 * @param {string} dataB64 - base64 内容（可含 data URL 前缀）
 * @returns {Promise<{ok:boolean, path?:string, name?:string, size?:number, error?:string}>}
 */
export async function uploadAttachment(name, dataB64) {
  return request('/api/upload', {
    method: 'POST',
    body: JSON.stringify({ name, data: dataB64 }),
  });
}

// ==================== 账户 ====================

/**
 * 获取账户列表
 */
export async function getAccounts() {
  return request('/api/accounts');
}

// ==================== 统计/同步 ====================

/**
 * 获取统计信息
 */
export async function getStats() {
  return request('/api/stats');
}

/**
 * 触发邮件同步
 */
export async function triggerSync() {
  return request('/api/sync', { method: 'POST' });
}

// ==================== 待办 ====================

/**
 * 获取待办列表
 */
export async function getTodos() {
  return request('/api/todos');
}

/**
 * 创建待办
 * @param {object} todo - 待办数据
 */
export async function createTodo(todo) {
  return request('/api/todos', {
    method: 'POST',
    body: JSON.stringify(todo),
  });
}

/**
 * 更新待办
 * @param {string} id - 待办ID
 * @param {object} patch - 更新字段
 */
export async function patchTodo(id, patch) {
  return request(`/api/todos/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
}

/**
 * 删除待办
 * @param {string} id - 待办ID
 */
export async function deleteTodo(id) {
  return request(`/api/todos/${id}`, { method: 'DELETE' });
}

// ==================== 设置 ====================

/**
 * 后端设置字段 → 前端字段映射
 * 后端: llm_provider, ollama_url, ollama_model, api_base_url, api_key, api_model, api_timeout, sync_days, sync_interval
 * 前端: llm_provider, ollama_url, ollama_model, api_base_url, api_key, api_model, api_timeout, sync_days, sync_interval
 * 
 * 直接使用后端字段名，不再做转换。settings store 的 data 字段名与后端保持一致。
 */

/**
 * 获取设置
 */
export async function getSettings() {
  return request('/api/settings');
}

/**
 * 保存设置
 * @param {object} settings - 设置数据（字段名与后端一致）
 */
export async function saveSettings(settings) {
  return request('/api/settings', {
    method: 'POST',
    body: JSON.stringify(settings),
  });
}

/**
 * 测试AI连接
 * @param {object} settings - 当前表单数据，传递给后端临时构建AIEngine
 */
export async function testAI(settings) {
  return request('/api/settings/test-ai', {
    method: 'POST',
    body: JSON.stringify(settings || {}),
  });
}

// ==================== 联系人 ====================

/**
 * 搜索联系人
 * @param {string} query - 搜索关键词
 */
export async function searchPeople(query) {
  return request(`/api/people?q=${encodeURIComponent(query)}`);
}

// 通讯录全量（清洗后约 6KB / 2500 条）。一次取回、本地过滤：
// 补全就不必逐键打服务端，弱网下也不会"输入卡一下才出候选"。
// 可发信账户（撰写窗的发件人下拉用）。发件人必须是其中之一，
// 服务端会再校验一次 —— 前端的选择不能成为伪造发件人的入口。
export async function getSenders() {
  return request('/api/senders');
}

export async function getContacts(limit = 3000) {
  return request(`/api/people?limit=${limit}`);
}

// ==================== 规则 AI 生成/评估 ====================

export async function generateRule(description) {
  return request('/api/rules/generate', { method: 'POST', body: JSON.stringify({ description }) });
}

export async function evaluateRule(rule) {
  return request('/api/rules/evaluate', { method: 'POST', body: JSON.stringify({ rule }) });
}

// ==================== 批量操作 / 保存的搜索 ====================

export async function bulkEmails(ids, action, value = null) {
  return request('/api/emails/bulk', { method: 'POST', body: JSON.stringify({ ids, action, value }) });
}
export async function getSavedSearches() {
  return request('/api/saved-searches');
}
export async function addSavedSearch(name, query) {
  return request('/api/saved-searches', { method: 'POST', body: JSON.stringify({ name, query }) });
}
export async function delSavedSearch(name) {
  return request('/api/saved-searches/' + encodeURIComponent(name), { method: 'DELETE' });
}

// ==================== 分类 / AI提取待办 ====================
// ---- 草稿箱 ----
// 只做增删改查。发送始终是另一条路（sendEmail），草稿不会自己发出去。
export async function getDrafts() { return request('/api/drafts'); }
export async function createDraft(d) { return request('/api/drafts', { method: 'POST', body: JSON.stringify(d) }); }
export async function patchDraft(id, patch) { return request('/api/drafts/' + id, { method: 'PATCH', body: JSON.stringify(patch) }); }
export async function deleteDraft(id) { return request('/api/drafts/' + id, { method: 'DELETE' }); }
export async function getCategories() { return request('/api/categories'); }
export async function extractTodos(id, force = false) {
  // force=1 绕过已落库的 action_items，重新问一次 AI（服务端仍会去重）
  const q = force ? '?force=1' : '';
  return request('/api/emails/' + id + '/extract-todos' + q, { method: 'POST' });
}
export async function extractTodosBatch(body) { return request('/api/todos/extract-batch', { method: 'POST', body: JSON.stringify(body) }); }
export async function extractTodosBatchStatus() { return request('/api/todos/extract-batch/status'); }

export async function importOutlookRules() { return request('/api/rules/import-outlook', { method: 'POST' }); }

// ==================== 看板 / 话题聚合 ====================
export async function getDashboard() { return request('/api/dashboard'); }
export async function getTopics(by = 'subject', minSize = 1, category = '', topic = '') {
  let q = '/api/topics?by=' + encodeURIComponent(by) + '&min_size=' + encodeURIComponent(minSize);
  if (category) q += '&category=' + encodeURIComponent(category);
  if (topic) q += '&topic=' + encodeURIComponent(topic);
  return request(q);
}
export async function getInboxTabs() { return request('/api/inbox-tabs'); }
export async function addInboxTab(name, kind, ref) { return request('/api/inbox-tabs', { method: 'POST', body: JSON.stringify({ name, kind, ref }) }); }
export async function addUserFolder(account, name) { return request('/api/user-folders', { method: 'POST', body: JSON.stringify({ account, name }) }); }
export async function getUserFolders() { return request('/api/user-folders'); }
export async function delUserFolder(account, name) { return request('/api/user-folders/delete', { method: 'POST', body: JSON.stringify({ account, name }) }); }
export async function addRule(rule) { return request('/api/rules', { method: 'POST', body: JSON.stringify(rule) }); }
export async function delRule(name) { return request('/api/rules/' + encodeURIComponent(name), { method: 'DELETE' }); }
export async function applyRules() { return request('/api/rules/apply', { method: 'POST', body: '{}' }); }
export async function importOutlookRules() { return request('/api/rules/import-outlook', { method: 'POST', body: '{}' }); }
export async function getMailboxLabels() { return request('/api/mailbox-labels'); }
export async function saveMailboxLabels(rules) { return request('/api/mailbox-labels', { method: 'POST', body: JSON.stringify({ rules }) }); }
export async function delInboxTab(name) { return request('/api/inbox-tabs/' + encodeURIComponent(name), { method: 'DELETE' }); }
export async function getTopicRules() { return request('/api/topic-rules'); }
export async function addTopicRule(name, keywords, query = '', filters = {}) { return request('/api/topic-rules', { method: 'POST', body: JSON.stringify({ name, keywords, query, filters }) }); }
export async function previewTopic(query, keywords = [], filters = {}) { return request('/api/topics/preview', { method: 'POST', body: JSON.stringify({ query, keywords, filters }) }); }
export async function topicFromNl(text) { return request('/api/topics/from-nl', { method: 'POST', body: JSON.stringify({ text }) }); }
export async function reorderTopicRules(names) { return request('/api/topic-rules/reorder', { method: 'POST', body: JSON.stringify({ names }) }); }
export async function updateTopicRule(name, patch) { return request('/api/topic-rules/update', { method: 'POST', body: JSON.stringify(Object.assign({ name }, patch)) }); }
export async function getTopicEmails(name) {
  const data = await request('/api/topic-emails?topic=' + encodeURIComponent(name));
  if (data && data.emails) data.emails = data.emails.map(mapEmail);
  return data;
}
export async function getTopicCounts() { return request('/api/topic-counts'); }
export async function delTopicRule(name) { return request('/api/topic-rules/' + encodeURIComponent(name), { method: 'DELETE' }); }
export async function analyzeTopic(ids) { return request('/api/topics/analyze', { method: 'POST', body: JSON.stringify({ ids }) }); }
