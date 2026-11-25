/**
 * Quillbox - Bundled JS (auto-generated)
 * All modules combined into single IIFE to avoid ES module loading issues.
 */
(function() {
'use strict';

// ========== api.js ==========
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
async function getEmails(params = {}) {
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
async function getEmail(id) {
  const email = await request(`/api/emails/${id}`);
  return mapEmail(email);
}

/**
 * 更新邮件状态
 * @param {string} id - 邮件ID
 * @param {object} patch - 更新字段 {starred?, unread?, deleted?}
 */
async function patchEmail(id, patch) {
  return request(`/api/emails/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
}

/**
 * 删除邮件
 * @param {string} id - 邮件ID
 */
async function deleteEmail(id) {
  return request(`/api/emails/${id}`, { method: 'DELETE' });
}

/**
 * 发送邮件
 * @param {object} email - {to, cc, subject, body}
 */
async function sendEmail(email) {
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
async function forwardEmail(id, data) {
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
async function getActionStatus(actionId) {
  return request(`/api/action/${actionId}`);
}

/**
 * 上传撰写邮件的附件（base64 JSON）
 * @param {string} name - 文件名
 * @param {string} dataB64 - base64 内容（可含 data URL 前缀）
 * @returns {Promise<{ok:boolean, path?:string, name?:string, size?:number, error?:string}>}
 */
async function uploadAttachment(name, dataB64) {
  return request('/api/upload', {
    method: 'POST',
    body: JSON.stringify({ name, data: dataB64 }),
  });
}

// ==================== 账户 ====================

/**
 * 获取账户列表
 */
async function getAccounts() {
  return request('/api/accounts');
}

// ==================== 统计/同步 ====================

/**
 * 获取统计信息
 */
async function getStats() {
  return request('/api/stats');
}

/**
 * 触发邮件同步
 */
async function triggerSync() {
  return request('/api/sync', { method: 'POST' });
}

// ==================== 待办 ====================

/**
 * 获取待办列表
 */
async function getTodos() {
  return request('/api/todos');
}

/**
 * 创建待办
 * @param {object} todo - 待办数据
 */
async function createTodo(todo) {
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
async function patchTodo(id, patch) {
  return request(`/api/todos/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
}

/**
 * 删除待办
 * @param {string} id - 待办ID
 */
async function deleteTodo(id) {
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
async function getSettings() {
  return request('/api/settings');
}

/**
 * 保存设置
 * @param {object} settings - 设置数据（字段名与后端一致）
 */
async function saveSettings(settings) {
  return request('/api/settings', {
    method: 'POST',
    body: JSON.stringify(settings),
  });
}

/**
 * 测试AI连接
 * @param {object} settings - 当前表单数据，传递给后端临时构建AIEngine
 */
async function testAI(settings) {
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
async function searchPeople(query) {
  return request(`/api/people?q=${encodeURIComponent(query)}`);
}

// 通讯录全量（清洗后约 6KB / 2500 条）。一次取回、本地过滤：
// 补全就不必逐键打服务端，弱网下也不会"输入卡一下才出候选"。
// 可发信账户（撰写窗的发件人下拉用）。发件人必须是其中之一，
// 服务端会再校验一次 —— 前端的选择不能成为伪造发件人的入口。
async function getSenders() {
  return request('/api/senders');
}

async function getContacts(limit = 3000) {
  return request(`/api/people?limit=${limit}`);
}

// ==================== 规则 AI 生成/评估 ====================

async function generateRule(description) {
  return request('/api/rules/generate', { method: 'POST', body: JSON.stringify({ description }) });
}

async function evaluateRule(rule) {
  return request('/api/rules/evaluate', { method: 'POST', body: JSON.stringify({ rule }) });
}

// ==================== 批量操作 / 保存的搜索 ====================

async function bulkEmails(ids, action, value = null) {
  return request('/api/emails/bulk', { method: 'POST', body: JSON.stringify({ ids, action, value }) });
}
async function getSavedSearches() {
  return request('/api/saved-searches');
}
async function addSavedSearch(name, query) {
  return request('/api/saved-searches', { method: 'POST', body: JSON.stringify({ name, query }) });
}
async function delSavedSearch(name) {
  return request('/api/saved-searches/' + encodeURIComponent(name), { method: 'DELETE' });
}

// ==================== 分类 / AI提取待办 ====================
// ---- 草稿箱 ----
// 只做增删改查。发送始终是另一条路（sendEmail），草稿不会自己发出去。
async function getDrafts() { return request('/api/drafts'); }
async function createDraft(d) { return request('/api/drafts', { method: 'POST', body: JSON.stringify(d) }); }
async function patchDraft(id, patch) { return request('/api/drafts/' + id, { method: 'PATCH', body: JSON.stringify(patch) }); }
async function deleteDraft(id) { return request('/api/drafts/' + id, { method: 'DELETE' }); }
async function getCategories() { return request('/api/categories'); }
async function extractTodos(id, force = false) {
  // force=1 绕过已落库的 action_items，重新问一次 AI（服务端仍会去重）
  const q = force ? '?force=1' : '';
  return request('/api/emails/' + id + '/extract-todos' + q, { method: 'POST' });
}
async function extractTodosBatch(body) { return request('/api/todos/extract-batch', { method: 'POST', body: JSON.stringify(body) }); }
async function extractTodosBatchStatus() { return request('/api/todos/extract-batch/status'); }

async function importOutlookRules() { return request('/api/rules/import-outlook', { method: 'POST' }); }

// ==================== 看板 / 话题聚合 ====================
async function getDashboard() { return request('/api/dashboard'); }
async function getTopics(by = 'subject', minSize = 1, category = '', topic = '') {
  let q = '/api/topics?by=' + encodeURIComponent(by) + '&min_size=' + encodeURIComponent(minSize);
  if (category) q += '&category=' + encodeURIComponent(category);
  if (topic) q += '&topic=' + encodeURIComponent(topic);
  return request(q);
}
async function getInboxTabs() { return request('/api/inbox-tabs'); }
async function addInboxTab(name, kind, ref) { return request('/api/inbox-tabs', { method: 'POST', body: JSON.stringify({ name, kind, ref }) }); }
async function addUserFolder(account, name) { return request('/api/user-folders', { method: 'POST', body: JSON.stringify({ account, name }) }); }
async function getUserFolders() { return request('/api/user-folders'); }
async function delUserFolder(account, name) { return request('/api/user-folders/delete', { method: 'POST', body: JSON.stringify({ account, name }) }); }
async function addRule(rule) { return request('/api/rules', { method: 'POST', body: JSON.stringify(rule) }); }
async function delRule(name) { return request('/api/rules/' + encodeURIComponent(name), { method: 'DELETE' }); }
async function applyRules() { return request('/api/rules/apply', { method: 'POST', body: '{}' }); }
async function importOutlookRules() { return request('/api/rules/import-outlook', { method: 'POST', body: '{}' }); }
async function getMailboxLabels() { return request('/api/mailbox-labels'); }
async function saveMailboxLabels(rules) { return request('/api/mailbox-labels', { method: 'POST', body: JSON.stringify({ rules }) }); }
async function delInboxTab(name) { return request('/api/inbox-tabs/' + encodeURIComponent(name), { method: 'DELETE' }); }
async function getTopicRules() { return request('/api/topic-rules'); }
async function addTopicRule(name, keywords, query = '', filters = {}) { return request('/api/topic-rules', { method: 'POST', body: JSON.stringify({ name, keywords, query, filters }) }); }
async function previewTopic(query, keywords = [], filters = {}) { return request('/api/topics/preview', { method: 'POST', body: JSON.stringify({ query, keywords, filters }) }); }
async function topicFromNl(text) { return request('/api/topics/from-nl', { method: 'POST', body: JSON.stringify({ text }) }); }
async function reorderTopicRules(names) { return request('/api/topic-rules/reorder', { method: 'POST', body: JSON.stringify({ names }) }); }
async function updateTopicRule(name, patch) { return request('/api/topic-rules/update', { method: 'POST', body: JSON.stringify(Object.assign({ name }, patch)) }); }
async function getTopicEmails(name) {
  const data = await request('/api/topic-emails?topic=' + encodeURIComponent(name));
  if (data && data.emails) data.emails = data.emails.map(mapEmail);
  return data;
}
async function getTopicCounts() { return request('/api/topic-counts'); }
async function delTopicRule(name) { return request('/api/topic-rules/' + encodeURIComponent(name), { method: 'DELETE' }); }
async function analyzeTopic(ids) { return request('/api/topics/analyze', { method: 'POST', body: JSON.stringify({ ids }) }); }


// API namespace for stores
var api = {
  getEmails, getEmail, patchEmail, deleteEmail, sendEmail, forwardEmail,
  getActionStatus,
  getAccounts, getStats, triggerSync,
  getTodos, createTodo, patchTodo, deleteTodo, extractTodosBatch, extractTodosBatchStatus,
  getSettings, saveSettings, testAI, searchPeople, getContacts, getSenders,
  uploadAttachment,
  generateRule, evaluateRule,
  bulkEmails, getSavedSearches, addSavedSearch, delSavedSearch,
  getCategories, extractTodos,
  getDrafts, createDraft, patchDraft, deleteDraft,
  importOutlookRules, getMailboxLabels, saveMailboxLabels,
  getDashboard, getTopics, analyzeTopic, getTopicRules, addTopicRule, delTopicRule,
  getInboxTabs, addInboxTab, delInboxTab, previewTopic, topicFromNl, reorderTopicRules, updateTopicRule, getTopicEmails, getTopicCounts,
  addUserFolder, delUserFolder, addRule, delRule, applyRules, getUserFolders,
};

// ========== utils/sse.js ==========
/**
 * SSE (Server-Sent Events) 流式读取工具
 * 支持 GET 和 POST 请求的 SSE 流
 */

// (BASE_URL already declared)

/**
 * 创建 GET SSE 连接
 * @param {string} path - API路径
 * @param {object} options - 配置选项
 * @param {function} options.onMessage - 收到消息回调
 * @param {function} options.onError - 错误回调
 * @param {function} options.onDone - 完成回调
 * @returns {EventSource} EventSource实例（可用于关闭连接）
 */
function createSSE(path, { onMessage, onError, onDone }) {
  const url = `${BASE_URL}${path}`;
  const eventSource = new EventSource(url);

  eventSource.onmessage = (event) => {
    if (event.data === '[DONE]') {
      eventSource.close();
      onDone?.();
      return;
    }
    try {
      const data = JSON.parse(event.data);
      // 后端的收尾帧是 {"done": true, ...}，不是 '[DONE]'。不认它就永远不主动关流，
      // 等服务端关闭时 EventSource 会触发 onerror，把每次成功都记成失败。
      // createPostSSE 早就按 parsed.done 判完成，这里与它对齐。
      if (data && data.error) {
        eventSource.close();
        onError?.(new Error(data.error));
        return;
      }
      if (data && data.done === true) {
        eventSource.close();
        onMessage?.(data);      // 收尾帧可能带 todos 等负载，先交给上层
        onDone?.(data);         // 原样传下去，别在传输层丢数据
        return;
      }
      onMessage?.(data);
    } catch {
      // 纯文本消息
      onMessage?.(event.data);
    }
  };

  eventSource.onerror = (err) => {
    eventSource.close();
    onError?.(err);
  };

  return eventSource;
}

/**
 * 使用 fetch 创建 POST SSE 流
 * 适用于需要发送请求体的 SSE 端点
 * @param {string} path - API路径
 * @param {object} body - 请求体
 * @param {object} options - 配置选项
 * @param {function} options.onChunk - 收到文本块回调
 * @param {function} options.onError - 错误回调
 * @param {function} options.onDone - 完成回调
 * @returns {AbortController} 用于取消请求
 */
function createPostSSE(path, body, { onChunk, onError, onDone }) {
  const url = `${BASE_URL}${path}`;
  const controller = new AbortController();

  fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        // 按 SSE 格式解析 (data: ...\n\n)
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const data = line.slice(6);
            if (data === '[DONE]') {
              onDone?.();
              return;
            }
            let parsed = null;
            try { parsed = JSON.parse(data); } catch { parsed = null; }

            if (parsed && typeof parsed === 'object') {
              // 控制消息：错误 / 完成 —— 绝不能作为正文渲染
              if (parsed.error) {
                onError?.(new Error(parsed.error));
                return;
              }
              const text = parsed.content ?? parsed.text ?? parsed.chunk ?? '';
              if (text) onChunk?.(text);
              // 收到后端明确的完成信号即结束，不依赖 HTTP 流关闭，
              // 避免代理缓冲导致 onDone 迟迟不触发、对话框“收不回”。
              if (parsed.done) {
                onDone?.();
                return;
              }
            } else if (data.trim()) {
              // 非 JSON 的纯文本块
              onChunk?.(data);
            }
          }
        }
      }
      onDone?.();
    })
    .catch((err) => {
      if (err.name !== 'AbortError') {
        onError?.(err);
      }
    });

  return controller;
}

/**
 * 流式获取AI摘要
 * @param {string} emailId - 邮件ID
 * @param {object} callbacks - 回调函数
 * @returns {EventSource}
 */
function streamSummary(emailId, callbacks) {
  return createSSE(`/api/stream/summary/${emailId}`, callbacks);
}

/**
 * 流式AI聊天（针对特定邮件）
 * @param {string} message - 用户消息
 * @param {string} emailId - 邮件ID
 * @param {object} callbacks - 回调函数
 * @returns {AbortController}
 */
function streamChat(message, emailId, history, callbacks) {
  return createPostSSE('/api/stream/chat', { message, email_id: emailId, history: history || [] }, callbacks);
}

/**
 * 全局AI聊天
 * @param {string} message - 用户消息
 * @param {object} callbacks - 回调函数
 * @returns {AbortController}
 */
function streamGlobalChat(message, history, callbacks) {
  return createPostSSE('/api/stream/global-chat', { message, history: history || [] }, callbacks);
}

/**
 * 流式生成草稿
 * @param {string} emailId - 邮件ID
 * @param {string} tone - 语气
 * @param {object} callbacks - 回调函数
 * @returns {AbortController}
 */
function streamDraft(emailId, tone, callbacks) {
  return createPostSSE('/api/stream/draft', { email_id: emailId, tone }, callbacks);
}

// ========== utils/sanitize.js ==========
/**
 * 邮件正文 HTML 消毒（白名单）
 *
 * 加固后修掉了原实现的四类缺口：
 *   1. URL 协议用黑名单 + trim() 判断，`java\tscript:` / 前导控制字符可绕过
 *      （浏览器 URL 解析会删掉任意位置的 Tab/换行）→ 改为**协议白名单**，先按浏览器规则归一化再判断；
 *   2. 外部图片（含 CSS url()）直接加载：追踪像素 = 向发件人回报「已读 + IP」→ **默认拦截**，
 *      由界面按封放行或信任发件人域名；
 *   3. 保留 class / position:fixed：邮件可借用本应用的 Tailwind 类（fixed inset-0 z-50…）
 *      盖住整个界面做钓鱼浮层 → 去掉 class，禁止非 static/relative 定位；
 *   4. <style>/<svg>/<math>/<noscript>/<template> 等被「拆壳保留内容」：CSS 源码漏成正文、
 *      外来命名空间留下 mXSS 面 → 连同内容整块删除；注释一并删除（含 Outlook 条件注释）。
 * 相对地址只放行 `/api/inline/`（内嵌图）与 `#锚点`，避免邮件构造指向本机 API 的同源请求。
 *
 * 「原始样式」整页文档 sanitizeEmailDoc()：给 app2 的沙箱 iframe 用（见该函数注释）。
 * 上面第 3、4 条是针对「直接插进应用页面」的；放进 iframe 后邮件样式管不到应用界面，
 * 才可以保留 class 与消毒后的 <style>，让 Outlook/Word 邮件按原样排版。
 */

// 允许的标签白名单
const ALLOWED_TAGS = new Set([
  'p', 'br', 'hr', 'div', 'span', 'pre', 'code', 'blockquote',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'ul', 'ol', 'li', 'dl', 'dt', 'dd',
  'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'caption',
  'a', 'img', 'b', 'i', 'u', 's', 'em', 'strong', 'small', 'sub', 'sup',
  'abbr', 'cite', 'mark', 'del', 'ins',
]);

// 允许的属性白名单（刻意不含 class，理由见文件头第 3 条）
const ALLOWED_ATTRS = new Set([
  'href', 'src', 'alt', 'title', 'width', 'height',
  'style', 'colspan', 'rowspan', 'target', 'rel',
  'border', 'cellpadding', 'cellspacing', 'align', 'valign', 'bgcolor',
]);

// 「原始样式」（沙箱 iframe）额外放行的标签 / 属性：只影响排版，不发请求
const _FRAME_TAGS = new Set([...ALLOWED_TAGS,
  'font', 'center', 'big', 'strike', 'tt', 'q', 'kbd', 'samp', 'var', 'wbr', 'nobr', 'colgroup', 'col',
  'section', 'article', 'header', 'footer', 'main', 'aside', 'nav', 'figure', 'figcaption', 'address',
  'time', 'bdi', 'bdo', 'details', 'summary',
]);
// name 只许用在 <a> 上（锚点）：<img name=…> 会成为 document 的命名属性，覆盖 documentElement 等
const _FRAME_ATTRS = new Set([...ALLOWED_ATTRS,
  'class', 'id', 'name', 'lang', 'dir', 'face', 'color', 'size', 'nowrap', 'span', 'start', 'type', 'value',
  'reversed', 'background', 'frame', 'rules', 'summary', 'scope', 'headers', 'abbr', 'datetime', 'open',
  'hspace', 'vspace', 'text', 'link', 'vlink', 'alink',
]);
const _PROF_CLEAN = { tags: ALLOWED_TAGS, attrs: ALLOWED_ATTRS, frame: false };
const _PROF_FRAME = { tags: _FRAME_TAGS, attrs: _FRAME_ATTRS, frame: true };

// 连同内容整块删除的标签
const _SAN_DROP = new Set([
  'script', 'iframe', 'object', 'embed', 'form', 'input', 'textarea', 'button',
  'link', 'meta', 'base', 'applet', 'style', 'title', 'head', 'noscript', 'template',
  'svg', 'math', 'select', 'option', 'frame', 'frameset', 'noembed', 'noframes',
  'xmp', 'plaintext', 'portal', 'video', 'audio', 'source', 'track', 'canvas', 'dialog',
]);

const _SAN_HREF_SCHEMES = new Set(['http', 'https', 'mailto', 'tel']);

/** 按浏览器 URL 解析的规则归一化：删任意位置的 Tab/CR/LF，去首尾 C0 控制字符与空格，反斜杠视同斜杠 */
function _sanUrl(v) {
  return String(v || '')
    .replace(/[\t\n\r]/g, '')
    .replace(/^[\u0000-\u0020]+|[\u0000-\u0020]+$/g, '')
    .replace(/\\/g, '/');
}

function _sanScheme(u) {
  const m = /^([a-z][a-z0-9+.\-]*):/i.exec(u);
  return m ? m[1].toLowerCase() : '';
}

function _sanHrefOk(u) {
  const s = _sanScheme(u);
  if (s) return _SAN_HREF_SCHEMES.has(s);
  if (u.startsWith('//')) return true;          // 协议相对 = 外链
  return u.startsWith('#');
}

/** src 分类：local（内嵌/data 图）| remote（外部）| bad（其他一律删） */
function _sanSrcKind(u) {
  if (/\.\.|%2e/i.test(u)) return 'bad';        // 防 /api/inline/../../api/... 拐到别的同源端点
  const s = _sanScheme(u);
  if (s === 'http' || s === 'https') return 'remote';
  if (s === 'data') return /^data:image\/(png|gif|jpe?g|webp|bmp);/i.test(u) ? 'local' : 'bad';
  if (s === 'cid') return 'local';
  if (s) return 'bad';
  if (u.startsWith('//')) return 'remote';
  if (u.startsWith('/api/inline/')) return 'local';
  return 'bad';
}

function _sanStyle(style, allowRemote, st) {
  const out = [];
  const src = String(style || '').replace(/\/\*[\s\S]*?\*\//g, '');
  for (const decl of src.split(';')) {
    const d = decl.trim();
    const i = d.indexOf(':');
    if (i < 1) continue;
    const prop = d.slice(0, i).trim().toLowerCase();
    const val = d.slice(i + 1).trim();
    const lv = val.toLowerCase().replace(/\s+/g, '');
    if (!/^-?[a-z][a-z-]*$/.test(prop)) continue;
    if (val.indexOf('\\') >= 0) continue;         // CSS 转义可把 url( 写成 u\72l(，一律丢弃
    if (prop === 'position' && lv !== 'static' && lv !== 'relative') continue;
    if (prop === 'behavior' || prop === '-moz-binding') continue;
    if (lv.indexOf('expression(') >= 0 || lv.indexOf('@import') >= 0) continue;
    if (/url\(|src\(|image-set\(|image\(/.test(lv)) {
      if (!allowRemote) { st.remote++; continue; }
      if (/javascript:|vbscript:/.test(lv)) continue;
    }
    out.push(prop + ':' + val);
  }
  return out.join(';');
}

let _sanMemo = { key: null, val: null };

function _sanRun(html, opts) {
  const allowRemote = !!(opts && opts.allowRemote);
  if (!html) return { html: '', remote: 0 };
  const key = (allowRemote ? '1' : '0') + html;
  if (_sanMemo.key === key) return _sanMemo.val;
  let val;
  // 如果是纯文本（无HTML标签），直接转义返回
  if (!/<[a-z][\s\S]*>/i.test(html)) {
    val = { html: escapeHtml(html), remote: 0 };
  } else {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const st = { remote: 0 };
    sanitizeNode(doc.body, allowRemote, st);
    val = { html: doc.body.innerHTML, remote: st.remote };
  }
  _sanMemo = { key, val };
  return val;
}

/**
 * 消毒HTML字符串
 * @param {string} html - 原始HTML
 * @param {{allowRemote?: boolean}} [opts] - allowRemote=true 时放行外部图片
 * @returns {string} 安全的HTML
 */
function sanitizeHTML(html, opts) {
  return _sanRun(html, opts).html;
}

/** 同一输入下被拦截的外部资源数（图片 + CSS url()） */
function sanitizeRemoteCount(html, opts) {
  return _sanRun(html, opts).remote;
}

// ---- 原始样式：沙箱 iframe 用的整页文档 ----
//
// 阅读模式把邮件直接插进应用页面，所以必须去掉 <style> 与 class（文件头第 3、4 条）；
// 代价是 Outlook/Word 邮件靠样式表排版（p.MsoNormal{margin:0} 等），去掉后行距、表格、列表全变样。
// 原始样式改为生成一整页文档，交给 app2 的 <iframe sandbox srcdoc> 显示：
//   - 样式管不到 iframe 外，可以保留 class 和消毒后的 <style>；
//   - sandbox 不含 allow-scripts / allow-forms / allow-top-navigation：框内脚本、表单、改顶层页面一律无效；
//   - 文档内再带一条 CSP（默认拦外部图片/字体），万一净化有漏，请求也发不出去。
// 样式表交给浏览器自己的 CSS 解析器（CSSStyleSheet），在解析结果上逐条过滤再序列化：
// 转义（u\72l(）、注释、@import 都已被解析器归一化，不靠正则猜。
const _SHEET_RULES = ['CSSStyleRule', 'CSSMediaRule', 'CSSSupportsRule', 'CSSFontFaceRule', 'CSSPageRule',
  'CSSKeyframesRule', 'CSSKeyframeRule', 'CSSLayerBlockRule', 'CSSLayerStatementRule', 'CSSContainerRule',
  'CSSNestedDeclarations'];
const _SHEET_RES = /url\(|src\(|image-set\(|image\(|cross-fade\(|element\(|paint\(/;
const _SHEET_URL = /url\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|([^)\s]*))\s*\)/gi;

function _sheetRuleOk(r) {
  return _SHEET_RULES.some((n) => typeof globalThis[n] === 'function' && r instanceof globalThis[n]);
}

/** 还原 CSS 转义。自定义属性（--x）的值浏览器按原文保留，--u:u\72l(…) 不会被归一化，经 var() 代入后照样是 url() */
function _cssUnesc(s) {
  return s.replace(/\\([0-9a-f]{1,6})[ \t\n\r\f]?|\\([\s\S])/gi, (m, h, c) => {
    if (c !== undefined) return c;
    const n = parseInt(h, 16);
    return n > 0 && n <= 0x10ffff && !(n >= 0xd800 && n <= 0xdfff) ? String.fromCodePoint(n) : '\ufffd';
  });
}

/** 一个值里引用的资源：'none' | 'local' | 'remote' | 'bad'（取最差的） */
function _sheetValKind(lv) {
  if (lv.indexOf('\\') >= 0) lv = _cssUnesc(lv).toLowerCase();   // 还原后引号可能失配：解析不出来的地址按 bad 处理（删）
  if (!_SHEET_RES.test(lv)) return 'none';
  if (/javascript:|vbscript:|expression\(/.test(lv)) return 'bad';
  // url() 之外的图像函数里可以直接写字符串地址，无法逐个判定，一律按外部资源
  if (/src\(|image-set\(|image\(|cross-fade\(|element\(|paint\(/.test(lv.replace(/url\(/g, ''))) return 'remote';
  let worst = 'local';
  for (const m of lv.matchAll(_SHEET_URL)) {
    const k = _sanSrcKind(_sanUrl((m[1] || m[2] || m[3] || '').replace(/\\(.)/g, '$1')));
    if (k === 'bad') return 'bad';
    if (k === 'remote') worst = 'remote';
  }
  return worst;
}

/** 过滤一组声明；返回 false 表示整条规则应删除（@font-face 的字体被拦） */
function _sheetDecls(style, allowRemote, st, fontFace) {
  for (const p of Array.from(style)) {
    const lv = String(style.getPropertyValue(p)).toLowerCase();
    let drop = p === 'behavior' || p === '-moz-binding' ||
      // 与内联样式同规则；Outlook 本身也不支持定位
      (p === 'position' && !/^\s*(static|relative)\s*$/.test(lv));
    if (!drop) {
      const kind = _sheetValKind(lv);
      if (kind === 'bad') drop = true;
      else if (kind === 'remote' && (fontFace || !allowRemote)) {
        // 外部字体始终不加载（应用 CSP 的 font-src 也不放行），只在未放行时计入拦截数
        if (!allowRemote) st.remote++;
        if (fontFace) return false;
        drop = true;
      }
    }
    if (drop) style.removeProperty(p);
  }
  return true;
}

function _sheetWalk(list, del, allowRemote, st) {
  for (let i = list.length - 1; i >= 0; i--) {
    const r = list[i];
    let keep = _sheetRuleOk(r);
    if (keep && r.style) keep = _sheetDecls(r.style, allowRemote, st, typeof CSSFontFaceRule === 'function' && r instanceof CSSFontFaceRule);
    if (keep && r.cssRules) _sheetWalk(r.cssRules, (j) => r.deleteRule(j), allowRemote, st);
    if (!keep) del(i);
  }
}

/** 消毒一段样式表文本；浏览器不支持 CSSStyleSheet 构造时整段丢弃（退回到无样式表） */
function _sanSheet(text, allowRemote, st) {
  let sheet;
  try { sheet = new CSSStyleSheet(); sheet.replaceSync(String(text || '')); } catch (e) { return ''; }
  _sheetWalk(sheet.cssRules, (i) => sheet.deleteRule(i), allowRemote, st);
  // 序列化结果放进 <style> 元素：把 < 转成 CSS 转义，任何字符串都拼不出 </style> 跳出样式表
  return Array.from(sheet.cssRules, (r) => r.cssText).join('\n').replace(/</g, '\\3c ');
}

/** 收集邮件里的样式表（<head> 与正文里的 <style>），各自消毒；media="print" 等非屏幕样式不要 */
function _docSheets(doc, allowRemote, st) {
  const out = [];
  for (const el of doc.querySelectorAll('style')) {
    if (el.closest('svg, math, noscript, template')) continue;
    let text = el.textContent || '';
    const media = (el.getAttribute('media') || '').trim().toLowerCase();
    if (media && !/^(all|screen)$/.test(media)) {
      if (!/^[a-z0-9 ,:()\-.]+$/.test(media) || /\bprint\b/.test(media)) continue;
      text = '@media ' + media + '{' + text + '}';
    }
    const css = _sanSheet(text, allowRemote, st);
    if (css) out.push(css);
  }
  return out;
}

// 默认字体与字号放在邮件样式表之前（邮件自己的规则优先）；高度相关的放在之后：
// iframe 高度按内容自适应，邮件里的 html/body{height:100%} 只会让高度量不准
const _FRAME_BASE_CSS = ':root{color-scheme:light}html{background:#fff;color:#111}' +
  'body{margin:8px;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;font-size:14px;overflow-wrap:break-word}' +
  'img{max-width:100%}img[data-blocked-src]{background:#f1f5f9}';
const _FRAME_TAIL_CSS = 'html,body{height:auto!important;min-height:0!important}html{overflow-y:hidden!important}';

function _frameCsp(allowRemote) {
  let origin = '';
  try { origin = location.origin && location.origin !== 'null' ? location.origin : ''; } catch (e) { /* 取不到就不放行内嵌图 */ }
  const inline = origin ? ' ' + origin + '/api/inline/' : '';
  return "default-src 'none'; style-src 'unsafe-inline'; img-src data:" + inline + (allowRemote ? ' http: https:' : '') +
    "; font-src data:; base-uri 'none'; form-action 'none'";
}

let _frameMemo = { key: null, val: null };

function _frameRun(html, opts) {
  const allowRemote = !!(opts && opts.allowRemote);
  if (!html) return { html: '', remote: 0 };
  const key = (allowRemote ? '1' : '0') + html;
  if (_frameMemo.key === key) return _frameMemo.val;
  const st = { remote: 0 };
  let sheets = [], body;
  if (!/<[a-z][\s\S]*>/i.test(html)) {
    body = '<body><div style="white-space:pre-wrap">' + escapeHtml(html) + '</div></body>';
  } else {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    sheets = _docSheets(doc, allowRemote, st);          // 先收样式表：正文消毒会删掉 <style>
    sanitizeNode(doc.body, allowRemote, st, _PROF_FRAME);
    _sanAttrs(doc.body, 'body', allowRemote, st, _PROF_FRAME);   // <body style/class/lang/bgcolor> 也是排版的一部分
    body = doc.body.outerHTML;
  }
  const esc = (v) => v.replace(/&/g, '&amp;').replace(/"/g, '&quot;');
  const out = '<!DOCTYPE html><html><head><meta charset="utf-8">' +
    '<meta http-equiv="Content-Security-Policy" content="' + esc(_frameCsp(allowRemote)) + '">' +
    '<meta name="referrer" content="no-referrer">' +
    '<style>' + _FRAME_BASE_CSS + '</style>' +
    sheets.map((c) => '<style>' + c + '</style>').join('') +
    '<style>' + _FRAME_TAIL_CSS + '</style></head>' + body + '</html>';
  const val = { html: out, remote: st.remote };
  _frameMemo = { key, val };
  return val;
}

/**
 * 「原始样式」整页文档：只能交给 sandbox 不含 allow-scripts 的 iframe（srcdoc）显示，不得插进应用页面。
 * @param {string} html - 原始 HTML 正文
 * @param {{allowRemote?: boolean}} [opts]
 * @returns {string} 完整 HTML 文档
 */
function sanitizeEmailDoc(html, opts) {
  return _frameRun(html, opts).html;
}

/** sanitizeEmailDoc 同一输入下被拦截的外部资源数（图片、背景图、样式表里的 url()、外部字体） */
function sanitizeEmailDocRemoteCount(html, opts) {
  return _frameRun(html, opts).remote;
}

// ---- 外部图片的信任域名（按发件人域名，存 localStorage） ----
const _SAN_TRUST_KEY = 'quillbox.trustedImageDomains';

function _sanDomainOf(addr) {
  const s = String(addr || '').trim().toLowerCase();
  const i = s.lastIndexOf('@');
  return i >= 0 ? s.slice(i + 1) : '';
}

function imgTrustedDomains() {
  try {
    const v = JSON.parse(localStorage.getItem(_SAN_TRUST_KEY) || '[]');
    return Array.isArray(v) ? v : [];
  } catch (e) { return []; }
}

function senderDomainOf(addr) { return _sanDomainOf(addr); }

function isImgTrusted(senderAddr) {
  const d = _sanDomainOf(senderAddr);
  return !!d && imgTrustedDomains().indexOf(d) >= 0;
}

function trustImgDomain(senderAddr) {
  const d = _sanDomainOf(senderAddr);
  if (!d) return;
  const list = imgTrustedDomains();
  if (list.indexOf(d) < 0) {
    list.push(d);
    try { localStorage.setItem(_SAN_TRUST_KEY, JSON.stringify(list)); } catch (e) { /* 存不下就只对本次生效 */ }
  }
}

/**
 * 递归消毒DOM节点
 */
function sanitizeNode(node, allowRemote, st, prof = _PROF_CLEAN) {
  for (const child of [...node.childNodes]) {
    if (child.nodeType === Node.COMMENT_NODE) { child.remove(); continue; }
    if (child.nodeType !== Node.ELEMENT_NODE) continue;
    const tagName = child.tagName.toLowerCase();

    if (_SAN_DROP.has(tagName)) { child.remove(); continue; }

    // 非白名单标签：先清洗子树，再拆壳保留内容
    if (!prof.tags.has(tagName)) {
      sanitizeNode(child, allowRemote, st, prof);
      child.replaceWith(...[...child.childNodes]);
      continue;
    }

    _sanAttrs(child, tagName, allowRemote, st, prof);

    // 给外部链接添加安全属性（iframe 里的 #锚点 不开新窗口，只在框内跳转）
    if (tagName === 'a') {
      if (prof.frame && (child.getAttribute('href') || '').startsWith('#')) child.removeAttribute('target');
      else child.setAttribute('target', '_blank');
      child.setAttribute('rel', 'noopener noreferrer');
    }

    sanitizeNode(child, allowRemote, st, prof);
  }
}

function _sanAttrs(el, tagName, allowRemote, st, prof) {
  for (const attr of [...el.attributes]) {
    const name = attr.name.toLowerCase();
    if (!prof.attrs.has(name) || (name === 'name' && tagName !== 'a')) { el.removeAttribute(attr.name); continue; }
    if (name === 'href') {
      if (!_sanHrefOk(_sanUrl(attr.value))) el.removeAttribute(attr.name);
    } else if (name === 'src' || name === 'background') {
      const kind = _sanSrcKind(_sanUrl(attr.value));
      if (kind === 'bad') {
        el.removeAttribute(attr.name);
      } else if (kind === 'remote' && !allowRemote) {
        el.removeAttribute(attr.name);
        if (name === 'src') el.setAttribute('data-blocked-src', '1');
        st.remote++;
      }
    } else if (name === 'style') {
      const s = _sanStyle(attr.value, allowRemote, st);
      if (s) el.setAttribute('style', s); else el.removeAttribute('style');
    }
  }
}

/**
 * HTML实体转义（纯文本用）
 */
function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// ========== utils/shortcuts.js ==========
/**
 * 键盘快捷键管理器
 * Superhuman风格的键盘优先交互
 */

/**
 * 快捷键注册表
 * @type {Map<string, {handler: Function, description: string, when: string}>}
 */
const shortcuts = new Map();

/**
 * 检测是否在输入框内
 */
function isInputFocused() {
  const el = document.activeElement;
  if (!el) return false;
  const tag = el.tagName.toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable;
}

/**
 * 生成快捷键标识符
 */
function getKeyId(e) {
  const parts = [];
  if (e.ctrlKey || e.metaKey) parts.push('mod');
  if (e.altKey) parts.push('alt');
  if (e.shiftKey) parts.push('shift');
  parts.push(e.key.toLowerCase());
  return parts.join('+');
}

/**
 * 注册快捷键
 * @param {string} key - 快捷键 (如 'j', 'mod+k', 'shift+r')
 * @param {Function} handler - 处理函数
 * @param {object} options - 配置
 * @param {string} options.description - 描述
 * @param {string} options.when - 生效条件: 'global' | 'list' | 'detail'
 * @param {boolean} options.allowInInput - 是否在输入框内也生效
 */
function registerShortcut(key, handler, options = {}) {
  shortcuts.set(key, {
    handler,
    description: options.description || '',
    when: options.when || 'global',
    allowInInput: options.allowInInput || false,
  });
}

/**
 * 取消注册快捷键
 */
function unregisterShortcut(key) {
  shortcuts.delete(key);
}

/**
 * 获取所有快捷键列表（用于命令面板）
 */
function getShortcutList() {
  const list = [];
  shortcuts.forEach((value, key) => {
    list.push({ key, ...value });
  });
  return list;
}

/**
 * 检测当前上下文是否满足快捷键的when条件
 * @param {string} when - 'global' | 'list' | 'detail'
 * @returns {boolean}
 */
function checkWhenCondition(when) {
  if (when === 'global') return true;

  // 如果有弹窗打开（compose/settings/command palette），只允许global快捷键
  try {
    const ui = Alpine.store('ui');
    if (ui.composeOpen || ui.settingsOpen || ui.commandPaletteOpen || ui.helpOpen) return false;
  } catch { /* Alpine未初始化 */ }

  if (when === 'list') {
    // 邮件列表上下文：有邮件列表可见
    try {
      const mail = Alpine.store('mail');
      return mail.view !== 'todos';
    } catch { return true; }
  }

  if (when === 'detail') {
    // 邮件详情上下文：有已选中的邮件
    try {
      const mail = Alpine.store('mail');
      return !!mail.selectedId && !!mail.currentEmail;
    } catch { return false; }
  }

  return true;
}

/**
 * 初始化键盘监听
 * 在 Alpine 初始化时调用
 */
function initShortcuts() {
  document.addEventListener('keydown', (e) => {
    const keyId = getKeyId(e);
    const shortcut = shortcuts.get(keyId);

    if (!shortcut) return;
    if (!shortcut.allowInInput && isInputFocused()) return;
    if (!checkWhenCondition(shortcut.when)) return;

    e.preventDefault();
    e.stopPropagation();
    shortcut.handler(e);
  });
}

/**
 * 注册默认邮件快捷键
 * 需要传入Alpine store的访问方式
 */
function registerDefaultShortcuts(getStore) {
  // j - 下一封邮件
  registerShortcut('j', () => {
    const mail = getStore('mail');
    mail.selectNext();
  }, { description: '下一封邮件', when: 'list' });

  // k - 上一封邮件
  registerShortcut('k', () => {
    const mail = getStore('mail');
    mail.selectPrev();
  }, { description: '上一封邮件', when: 'list' });

  // ArrowDown / ArrowUp - 上下切换邮件（三栏阅读窗布局，选中即打开；与 j/k 等价）
  registerShortcut('arrowdown', () => {
    const mail = getStore('mail');
    mail.selectNext();
  }, { description: '下一封邮件', when: 'list' });

  registerShortcut('arrowup', () => {
    const mail = getStore('mail');
    mail.selectPrev();
  }, { description: '上一封邮件', when: 'list' });

  // Enter - 打开邮件
  registerShortcut('enter', () => {
    const mail = getStore('mail');
    if (mail.selectedId) {
      mail.openEmail(mail.selectedId);
    }
  }, { description: '打开邮件', when: 'list' });

  // r - 回复
  registerShortcut('r', () => {
    const ui = getStore('ui');
    ui.openCompose('reply');
  }, { description: '回复邮件', when: 'detail' });

  // a - 回复全部
  registerShortcut('a', () => {
    const ui = getStore('ui');
    ui.openCompose('reply-all');
  }, { description: '回复全部', when: 'detail' });

  // s - 星标切换
  registerShortcut('s', () => {
    const mail = getStore('mail');
    mail.toggleStar();
  }, { description: '切换星标', when: 'list' });

  // / - 聚焦搜索
  registerShortcut('/', () => {
    const searchInput = document.querySelector('#search-input');
    if (searchInput) searchInput.focus();
  }, { description: '搜索', when: 'global' });

  // Cmd+K - 命令面板
  registerShortcut('mod+k', () => {
    const ui = getStore('ui');
    ui.toggleCommandPalette();
  }, { description: '命令面板', when: 'global', allowInInput: true });

  // Escape - 关闭面板/返回
  registerShortcut('escape', () => {
    const ui = getStore('ui');
    if (ui.commandPaletteOpen) {
      ui.commandPaletteOpen = false;
    } else if (ui.aiPanelOpen) {
      ui.aiPanelOpen = false;
    } else if (ui.composeOpen) {
      ui.composeOpen = false;
    } else if (ui.settingsOpen) {
      ui.settingsOpen = false;
    }
  }, { description: '关闭/返回', when: 'global', allowInInput: true });

  // e - 归档/删除
  registerShortcut('e', () => {
    const mail = getStore('mail');
    if (!confirm('确定删除这封邮件？')) return;
    mail.deleteSelected();
  }, { description: '删除邮件', when: 'list' });

  // u - 标记未读/已读
  registerShortcut('u', () => {
    const mail = getStore('mail');
    mail.toggleUnread();
  }, { description: '切换已读状态', when: 'list' });

  // c - 新邮件
  registerShortcut('c', () => {
    const ui = getStore('ui');
    ui.openCompose('new');
  }, { description: '写新邮件', when: 'global' });

  // g+i - 转到收件箱 (简化为 i)
  registerShortcut('i', () => {
    const mail = getStore('mail');
    mail.setView('inbox');
  }, { description: '转到收件箱', when: 'global' });
}

// ========== stores/ui.js ==========
/**
 * UI 状态管理
 * 管理面板开关、主题、侧边栏等UI状态
 */


function uiStore() {
  return {
    // 侧边栏
    sidebarCollapsed: false,
    sidebarWidth: 180,        // 默认宽度(px)，可拖拽调整
    _sidebarResizing: false,  // 拖拽进行中标志

    // 邮件列表栏宽度（原先由 tailwind 的 w-list 写死 340px，不可调）
    listWidth: 340,
    _listResizing: false,

    // AI面板（呼出式浮层，默认收起；用 x-show 切换保证可靠收回）
    aiPanelOpen: false,

    // 命令面板
    commandPaletteOpen: false,
    commandQuery: '',
    omniResults: [],
    omniLoading: false,
    _omniTimer: null,

    // 写邮件面板
    composeOpen: false,
    composeMode: 'new', // 'new' | 'reply' | 'forward'

    // 设置面板
    settingsOpen: false,
    settingsTab: 'model',

    // 帮助
    helpOpen: false,

    // 主题: 'light' | 'dark' | 'system'
    themePreference: 'system',
    isDark: false,

    // 同步状态
    syncRunning: false,
    syncProgress: 0,

    // 通知
    notifications: [],

    init() {
      // 版本检测：页面长时间不刷新会一直用旧代码
      try { this.startVersionWatch(); } catch (e) {}
      // 读取侧边栏宽度偏好
      const savedWidth = localStorage.getItem('quillbox-sidebar-width');
      if (savedWidth) {
        this.sidebarWidth = Math.max(120, Math.min(320, parseInt(savedWidth, 10)));
      }

      // 读取列表栏宽度偏好（超出当前窗口能容纳的范围时会在这里被夹回来，
      // 换到小屏幕上不会出现详情栏被挤没的情况）
      const savedListWidth = parseInt(localStorage.getItem('quillbox-list-width'), 10);
      if (savedListWidth) {
        this.listWidth = this.clampListWidth(savedListWidth);
      }

      // 读取主题偏好
      const saved = localStorage.getItem('quillbox-theme');
      if (saved) {
        this.themePreference = saved;
      }
      this.applyTheme();

      // 监听系统主题变化
      window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
        if (this.themePreference === 'system') {
          this.applyTheme();
        }
      });
    },

    // 切换主题
    toggleTheme() {
      if (this.isDark) {
        this.themePreference = 'light';
      } else {
        this.themePreference = 'dark';
      }
      localStorage.setItem('quillbox-theme', this.themePreference);
      this.applyTheme();
    },

    // 设置主题
    setTheme(theme) {
      this.themePreference = theme;
      localStorage.setItem('quillbox-theme', theme);
      this.applyTheme();
    },

    // 打开设置并定位到指定分类（供侧栏“邮件规则”直达）
    openSettings(tab) {
      if (tab) this.settingsTab = tab;
      this.settingsOpen = true;
    },

    // 应用主题到DOM
    applyTheme() {
      if (this.themePreference === 'system') {
        this.isDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
      } else {
        this.isDark = this.themePreference === 'dark';
      }

      if (this.isDark) {
        document.documentElement.classList.add('dark');
      } else {
        document.documentElement.classList.remove('dark');
      }
    },

    // ---- 新版本检测 ----
    // 我在持续改前端，而用户在 RDP 里长时间开着页面，无从判断自己在哪个版本上
    // （真的踩到过：工具栏是新的、收件人那几行还是旧的，因为标签页是在两次改动
    // 之间加载的）。/app 虽然带 no-store，但那只在重新发起请求时起作用。
    appVersion: '',
    newVersion: '',

    // 从自己那个 script 标签的 src 里读版本，不需要额外注入全局变量
    _ownVersion() {
      try {
        const el = document.querySelector('script[src*="bundle.js"]');
        const m = (el && el.getAttribute('src') || '').match(/[?&]v=([\w.\-]+)/);
        return m ? m[1] : '';
      } catch (e) { return ''; }
    },

    startVersionWatch() {
      // 幂等：Alpine.store() 注册时会自动调用一次 init()，index.html 的 x-init 又手动
      // 调了一次，于是每个页面挂了两个定时器、每分钟查两次。
      if (this._versionWatchStarted) return;
      this._versionWatchStarted = true;
      this.appVersion = this._ownVersion();
      if (!this.appVersion) return;          // 取不到就不做，不误报
      const check = async () => {
        try {
          const r = await fetch('/api/version', { cache: 'no-store' });
          const j = await r.json();
          const v = (j && j.version) || '';
          // 只在确实拿到且不同时提示，避免网络抖动误报
          if (v && v !== this.appVersion) this.newVersion = v;
        } catch (e) { /* 网络抖动不提示 */ }
      };
      check();
      // 后台标签页不轮询：浏览器常开着多个 Quillbox 页面，绝大多数看不见
      // （每分钟会白查十几次）。切回前台立刻补查一次，所以新版本提示不会因此变晚。
      setInterval(() => { if (document.visibilityState !== 'hidden') check(); }, 60000);
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') check();
      });
    },

    reloadForNewVersion() {
      // 带上时间戳绕过任何中间缓存，比 location.reload() 更确定
      const u = new URL(window.location.href);
      u.searchParams.set('_r', Date.now());
      window.location.replace(u.toString());
    },

    // 打开写邮件
    openCompose(mode = 'new') {
      this.composeMode = mode;
      this.composeOpen = true;
    },

    // 侧边栏拖拽调整宽度
    startSidebarResize(e) {
      this._sidebarResizing = true;
      const startX = e.clientX;
      const startWidth = this.sidebarWidth;
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';

      const onMouseMove = (ev) => {
        const delta = ev.clientX - startX;
        this.sidebarWidth = Math.max(120, Math.min(320, startWidth + delta));
      };

      const onMouseUp = () => {
        this._sidebarResizing = false;
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);
        localStorage.setItem('quillbox-sidebar-width', String(this.sidebarWidth));
      };

      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
    },

    // 列表栏宽度夹取：上限跟着窗口走，保证详情栏至少留 480px
    clampListWidth(w) {
      const maxW = Math.max(260, Math.min(720, window.innerWidth - 480));
      return Math.max(260, Math.min(maxW, parseInt(w, 10) || 340));
    },

    // 列表栏与详情栏之间的拖拉调整
    startListResize(e) {
      this._listResizing = true;
      const startX = e.clientX;
      const startWidth = this.listWidth;
      document.body.style.cursor = 'col-resize';
      // 不锁选区的话，横向拖动会把邮件正文一路选中
      document.body.style.userSelect = 'none';

      const onMouseMove = (ev) => {
        this.listWidth = this.clampListWidth(startWidth + (ev.clientX - startX));
      };

      const onMouseUp = () => {
        this._listResizing = false;
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);
        localStorage.setItem('quillbox-list-width', String(this.listWidth));
      };

      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
    },

    // 双击手柄复位。可拖范围大，拖歪了这样最省事。
    resetListWidth() {
      this.listWidth = 340;
      localStorage.setItem('quillbox-list-width', String(this.listWidth));
    },

    // 切换命令面板
    toggleCommandPalette() {
      this.commandPaletteOpen = !this.commandPaletteOpen;
      this.commandQuery = '';
    },

    // 切换AI面板
    toggleAIPanel() {
      this.aiPanelOpen = !this.aiPanelOpen;
    },

    // 显示通知
    notify(message, type = 'info', duration = 3000) {
      const id = Date.now();
      this.notifications.push({ id, message, type });
      setTimeout(() => {
        this.notifications = this.notifications.filter(n => n.id !== id);
      }, duration);
    },

    // 命令面板操作列表
    get commands() {
      return [
        { id: 'inbox', label: '转到收件箱', icon: 'inbox', shortcut: 'I', action: () => { Alpine.store('mail').setView('inbox'); this.commandPaletteOpen = false; } },
        { id: 'starred', label: '转到星标', icon: 'star', shortcut: '', action: () => { Alpine.store('mail').setView('starred'); this.commandPaletteOpen = false; } },
        { id: 'compose', label: '写新邮件', icon: 'edit-3', shortcut: 'C', action: () => { this.openCompose('new'); this.commandPaletteOpen = false; } },
        { id: 'sync', label: '同步邮件', icon: 'refresh-cw', shortcut: '', action: () => { Alpine.store('mail').sync(); this.commandPaletteOpen = false; } },
        { id: 'ai', label: '打开AI助手', icon: 'sparkles', shortcut: '', action: () => { this.aiPanelOpen = true; this.commandPaletteOpen = false; } },
        { id: 'settings', label: '打开设置', icon: 'settings', shortcut: '', action: () => { this.settingsOpen = true; this.commandPaletteOpen = false; } },
        { id: 'theme', label: '切换暗色模式', icon: 'moon', shortcut: '', action: () => { this.toggleTheme(); this.commandPaletteOpen = false; } },
        { id: 'todos', label: '查看待办', icon: 'check-square', shortcut: '', action: () => { Alpine.store('mail').setView('todos'); this.commandPaletteOpen = false; } },
      ];
    },

    // 统一 omnibox：问 AI + 搜邮件 + 邮件命中 + 命令
    get filteredCommands() {
      const q = (this.commandQuery || '').trim();
      if (!q) return this.commands;
      const ql = q.toLowerCase();
      const items = [];
      items.push({ kind: 'search', id: '__search', icon: 'search', shortcut: '↵', label: '搜索邮件：“' + q + '”', action: () => this.doSearch(q) });
      items.push({ kind: 'ai', id: '__ai', icon: 'sparkles', label: '用 AI 回答：“' + q + '”', action: () => this.askAI(q) });
      for (const e of (this.omniResults || [])) {
        items.push({
          kind: 'email', id: '__mail_' + e.id, icon: 'mail',
          label: e.subject || '(无主题)',
          sub: ((e.from_name || e.sender || '') + (e.date ? ' · ' + String(e.date).slice(5, 16) : '')),
          action: () => this.openOmniEmail(e.id),
        });
      }
      for (const c of this.commands) {
        if (c.label.toLowerCase().includes(ql) || c.id.includes(ql)) items.push(c);
      }
      return items;
    },
    openOmni() { this.commandQuery = ''; this.omniResults = []; this.commandPaletteOpen = true; },
    runOmni() {
      const q = (this.commandQuery || '').trim();
      if (this._omniTimer) clearTimeout(this._omniTimer);
      if (!q) { this.omniResults = []; this.omniLoading = false; return; }
      this.omniLoading = true;
      this._omniTimer = setTimeout(async () => {
        try {
          const data = await api.getEmails({ q, view: 'inbox', pageSize: 6 });
          if ((this.commandQuery || '').trim() === q) this.omniResults = (data && data.emails) || [];
        } catch (e) { this.omniResults = []; }
        finally { this.omniLoading = false; }
      }, 250);
    },
    askAI(q) {
      q = (q || this.commandQuery || '').trim();
      if (!q) return;
      this.commandPaletteOpen = false;
      this.aiPanelOpen = true;
      try {
        const ai = Alpine.store('ai');
        ai.chatMode = 'global';
        ai.chatInput = q;
        ai.sendMessage();
      } catch (e) {}
    },
    doSearch(q) {
      q = (q || this.commandQuery || '').trim();
      this.commandPaletteOpen = false;
      const mail = Alpine.store('mail');
      if (mail.view !== 'inbox') mail.view = 'inbox';
      mail.search(q);
    },
    openOmniEmail(id) {
      this.commandPaletteOpen = false;
      const mail = Alpine.store('mail');
      if (mail.view !== 'inbox') mail.setView('inbox');
      mail.openEmail(id);
    },
  };
}

// ========== stores/mail.js ==========
/**
 * 邮件状态管理
 * 管理邮件列表、当前邮件、账户、筛选等
 */


function mailStore() {
  return {
    // 邮件列表
    emails: [],
    loading: false,
    totalCount: 0,

    // 当前选中/打开的邮件
    selectedId: null,
    currentEmail: null,
    detailLoading: false,

    // 视图和筛选
    view: 'inbox', // 'inbox' | 'starred' | 'unread' | 'sent' | 'trash' | 'todos'
    filter: 'all', // 'all' | 'unread' | 'starred' | 'attachments'
    searchQuery: '',
    currentAccount: '',
    currentFolder: '',
    currentTag: '',
    folderDlg: { open: false, account: '', mode: 'new', name: '', existing: '', field: 'from', op: 'contains', value: '' },
    tagPalette: [{name:'重要',color:'#ef4444'},{name:'工作',color:'#4f46e5'},{name:'个人',color:'#10b981'},{name:'待跟进',color:'#f59e0b'},{name:'账单',color:'#8b5cf6'}],
    selectedIds: [],
    savedSearches: [],
    categories: [],
    activeCategory: '',
    todoExtracting: false,
    dashboard: null,
    dashLoading: false,
    topics: [],
    topicsLoading: false,
    topicBy: 'subject',
    topicMultiOnly: false,
    inboxTabs: [],
    activeTabName: '',
    availableCats: [],
    newTabName: '',
    newTabKind: 'category',
    newTabRef: '',
    showTabManager: false,
    topicRules: [],
    topicPreviewCount: null,
    topicDefBusy: false,
    showHiddenTopics: false,
    defOpen: false,
    _dragTopic: null,
    currentTopic: '',
    topicCounts: {},
    topicDigest: '',
    topicDigestBusy: false,
    topicDef: { name: '', nl: '', keywords: '', query: '', filters: { cc_me: false, to_me: false, has_attachment: false, unread: false, starred: false, from: '', category: '' } },

    // 分页
    page: 0,
    pageSize: 30,
    hasMore: true,

    // 账户
    accounts: [],
    // 账户显示偏好（localStorage 持久化）：别名 + 自定义排序。
    // 注意：仅影响显示，不改动作为过滤/同步键的真实账户地址。
    accountAliases: {},   // { 账户email: 别名 }

    // ── 邮箱来源标签（可自定义）────────────────────────────────────────
    // 规则来自 /api/mailbox-labels（config.yaml 的 mailbox_labels 段），支持
    // 「精确账户」和「域名后缀」两种命中方式：内外网按域名一次配好，公共邮箱
    // （告警/职能邮箱之类）按账户单独打标。标签仍从 account 派生，不占数据库列。
    mailboxRules: [],
    mailboxPalette: {},
    currentMailbox: '',       // 当前按标签筛选的标签名
    folderOrders: {},
    globalFolders: [],
    accountOrder: [],     // [账户email, ...] 自定义顺序

    // 通讯录（清洗后的联系人，一次取回本地过滤，供补全与通讯录页共用）
    contacts: [],
    contactsLoaded: false,
    contactQuery: '',
    pendingComposeTo: '',   // 通讯录 -> 撰写窗 的收件人中转

    // 草稿箱（Agent 经 MCP 写入的回信草稿也落在这里）
    drafts: [],
    editingDraft: null,     // 打开到撰写窗的草稿，compose 预填时读它

    // 待办
    todos: [],
    newTodoText: '',
    todoBatch: { open: false, days: 7, accounts: [], useAI: true, running: false, scanned: 0, total: 0, added: 0 },

    // 统计
    stats: {
      total: 0,
      unread: 0,
      pending_ai: 0,
      sync_running: false,
      last_sync: null,
      ollama_ok: false,
      ai_provider: '',
    },

    init() {
      // 幂等：Alpine.store() 注册时会自动调一次 init()，index.html 的 x-init 又手动
      // 调一次，首屏接口因此全部请求两遍。记住第一次的 Promise，
      // 第二次调用直接等它，x-init 里「先 mail 后 settings」的 await 顺序不变。
      // 失败时清掉，保留原来「再调一次就重试」的语义。
      if (this._initPromise) return this._initPromise;
      this._initPromise = (async () => {
        this._loadAccountPrefs();
        this.loadSavedSearches();
        this.loadCategories();
        this.loadGlobalFolders();
        await Promise.all([
          this.fetchEmails(),
          this.fetchAccounts(),
          this.fetchStats(),
        ]);
      })().catch((e) => { this._initPromise = null; throw e; });
      return this._initPromise;
    },

    // ============ 邮件列表 ============

    async fetchEmails(append = false) {
      // 列表提供者（新界面「今日分拣」设置；经典界面不设）：接管时改由它供数
      const prov = this.listProvider && this.listProvider(append);
      if (prov) return prov;
      this.loading = true;
      try {
        const result = this.currentTopic
          ? await api.getTopicEmails(this.currentTopic)
          : await api.getEmails({
          view: this.view,
          account: this.currentAccount,
          mailbox: this.currentMailbox,
          folder: this.currentFolder,
          filter: this.filter,
          page: this.page,
          pageSize: this.pageSize,
          q: this.searchQuery,
          tag: this.currentTag,
        });

        const emails = Array.isArray(result) ? result : (result.emails || result.items || []);
        if (this.listOwns && this.listOwns()) return;   // 请求在途时切进了分拣视图：丢弃这次收件箱结果，不覆盖分拣列表
        if (append) {
          this.emails = [...this.emails, ...emails];
        } else {
          this.emails = emails;
        }
        this.totalCount = result.total || emails.length;
        // 优先信任后端返回的 has_more；缺失时回退到「本页是否满页」推断。
        this.hasMore = (typeof result.has_more === 'boolean')
          ? result.has_more
          : (emails.length === this.pageSize);

        // 自动选中第一封
        if (!append && this.emails.length > 0 && !this.selectedId) {
          this.selectedId = this.emails[0].id;
        }
      } catch (err) {
        console.error('[Mail] fetchEmails error:', err);
        Alpine.store('ui').notify('加载邮件失败', 'error');
      } finally {
        this.loading = false;
      }
    },

    // 加载更多
    async loadMore() {
      if (!this.hasMore || this.loading) return;
      this.page++;
      await this.fetchEmails(true);
    },

    // 设置视图
    setView(view) {
      this.view = view;
      this.currentTopic = '';
      this.currentTag = '';
      this.activeCategory = '';
      this.page = 0;
      this.selectedId = null;
      this.currentEmail = null;
      this.filter = 'all';
      this.searchQuery = '';   // 切换视图时清除搜索，避免仍停留在搜索结果
      // 侧栏是**导航**，顶栏才是过滤：点侧栏任一视图都该回到该视图的全部内容。
      // 不清这三个的话，进了某个文件夹之后点「收件箱」仍会停在那个文件夹里，
      // 用户被困住出不来。
      // 注意：setAccountFolder / setMailbox 是直接给 this.view 赋值、
      // 不经过这里，所以进入文件夹的路径不会被这段清理反噬。
      this.currentFolder = '';
      this.currentAccount = '';
      this.currentMailbox = '';
      if (view === 'contacts') {
        this.fetchContacts();
      } else if (view === 'drafts') {
        this.fetchDrafts();
      } else if (view === 'todos') {
        this.fetchTodos();
      } else if (view === 'dashboard') {
        this.loadDashboard();
      } else if (view === 'topics') {
        this.loadTopics();
      } else {
        this.fetchEmails();
      }
    },

    // ---- 空态说明 ----
    // 只显示"暂无邮件"的话，"过滤生效但结果为空"和"点击没生效"在界面上无法区分。
    // 用户就因此困惑过：进了 IP 文件夹点未读，后端正确返回 0 封（那里确实没有未读），
    // 界面却只是一片空白，看起来像"点了没反应"。
    get narrowing() {
      const f = {all: '', unread: '未读', starred: '星标',
                 attachments: '带附件'}[this.filter] || '';
      const out = [];
      if (this.currentFolder) out.push('文件夹「' + this.currentFolder + '」');
      else if (this.currentMailbox) out.push('邮箱标签「' + this.currentMailbox + '」');
      else if (this.currentAccount) out.push('账户「' + this.currentAccount + '」');
      if (this.currentTag) out.push('标签「' + this.currentTag + '」');
      if (this.currentTopic) out.push('话题「' + this.currentTopic + '」');
      if (f) out.push(f);
      if (this.searchQuery) out.push('搜索「' + this.searchQuery + '」');
      return out;
    },

    get hasNarrowing() { return this.narrowing.length > 0; },

    get emptyReason() {
      const n = this.narrowing;
      if (!n.length) return '暂无邮件';
      // 把条件说全，用户才能判断是"这里确实没有"还是"我点错了"
      return n.join(' + ') + ' 下没有邮件';
    },

    // 清掉所有收窄条件，回到"看全部"。
    // 刻意不改 view —— 用户点"清除筛选"是想看全部，不是想跳去别的视图。
    clearNarrowing() {
      this.currentFolder = '';
      this.currentAccount = '';
      this.currentMailbox = '';
      this.currentTag = '';
      this.currentTopic = '';
      this.activeCategory = '';
      this.filter = 'all';
      this.searchQuery = '';
      this.page = 0;
      this.fetchEmails();
    },

    // 设置筛选
    setFilter(filter) {
      this.filter = filter;
      this.currentTopic = '';
      this.currentTag = '';
      this.page = 0;
      this.fetchEmails();
    },

    // 搜索
    search(query) {
      this.searchQuery = query;
      this.currentTag = '';
      this.page = 0;
      this.fetchEmails();
    },

    // 设置账户/文件夹
    // 配色键 → tailwind class（白名单，避免把任意字符串塞进 class）
    mailboxColorClass(color) {
      const m = {
        blue:    'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
        amber:   'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
        rose:    'bg-rose-100 text-rose-700 dark:bg-rose-900/40 dark:text-rose-300',
        emerald: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
        violet:  'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
        cyan:    'bg-cyan-100 text-cyan-700 dark:bg-cyan-900/40 dark:text-cyan-300',
        orange:  'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300',
        slate:   'bg-slate-100 text-slate-700 dark:bg-slate-700/40 dark:text-slate-300',
      };
      return m[color] || m.slate;
    },

    async loadMailboxLabels() {
      try {
        const r = await api.getMailboxLabels();
        this.mailboxRules = (r && r.rules) || [];
        this.mailboxPalette = (r && r.palette) || {};
      } catch (e) {
        this.mailboxRules = [];
      }
    },

    // 与后端 core/mailbox_labels.resolve 同口径：账户精确匹配优先，其次域名后缀
    mailboxLabel(account) {
      const a = String(account || '').trim().toLowerCase();
      if (!a) return null;
      for (const r of this.mailboxRules) {
        if ((r.accounts || []).includes(a)) {
          return { name: r.name, cls: this.mailboxColorClass(r.color) };
        }
      }
      const dom = a.includes('@') ? a.split('@').pop() : '';
      if (!dom) return null;
      for (const r of this.mailboxRules) {
        for (const d of (r.domains || [])) {
          if (dom === d || dom.endsWith('.' + d)) {
            return { name: r.name, cls: this.mailboxColorClass(r.color) };
          }
        }
      }
      return null;
    },

    get mailboxChips() {
      return (this.mailboxRules || []).map(r => ({
        name: r.name,
        cls: this.mailboxColorClass(r.color),
        hint: [...(r.accounts || []), ...(r.domains || []).map(d => '@' + d)].join(' / '),
      }));
    },

    // 按标签筛选：走后端 mailbox 参数（一条规则可覆盖整个域名，单个 account 表达不了）
    setMailbox(name) {
      this.currentMailbox = name || '';
      this.currentAccount = '';
      this.currentFolder = '';
      this.currentTag = '';
      this.activeCategory = '';
      this.view = 'inbox';
      this.page = 0;
      this.searchQuery = '';
      this.currentTopic = '';
      this.fetchEmails();
    },

    setAccountFolder(account, folder = '') {
      this.currentMailbox = '';   // 点具体账户/文件夹时退出标签筛选，避免两种筛选叠加
      this.currentAccount = account;
      this.currentFolder = folder;
      this.view = 'inbox';
      this.page = 0;
      this.searchQuery = '';   // 切换账户/文件夹时清除搜索
      this.currentTopic = '';
      this.fetchEmails();
    },

    // ---- 批量选择 ----
    isSelected(id) { return this.selectedIds.includes(id); },
    toggleSelect(id) {
      const i = this.selectedIds.indexOf(id);
      if (i >= 0) this.selectedIds.splice(i, 1); else this.selectedIds.push(id);
    },
    clearSelection() { this.selectedIds = []; },
    selectAllVisible() {
      const ids = this.emails.map(e => e.id);
      this.selectedIds = (this.selectedIds.length >= ids.length && ids.length > 0) ? [] : ids.slice();
    },
    async bulkAction(action, value = null) {
      if (!this.selectedIds.length) return;
      const cnt = this.selectedIds.length;
      try {
        await api.bulkEmails(this.selectedIds.slice(), action, value);
        try { Alpine.store('ui').notify('已对 ' + cnt + ' 封邮件执行操作', 'success'); } catch (e) {}
      } catch (e) {
        try { Alpine.store('ui').notify('批量操作失败', 'error'); } catch (er) {}
      }
      this.clearSelection();
      this.page = 0;
      await this.fetchEmails();
      this.fetchStats();       // 刷新未读总数
      this.fetchAccounts();    // 刷新各账户未读
      this.loadCategories();
      this.loadGlobalFolders();
    },
    async loadCategories() {
      try { this.categories = (await api.getCategories()) || []; } catch (e) { this.categories = []; }
    },
    async loadDashboard() {
      this.dashLoading = true;
      try { this.dashboard = await api.getDashboard(); } catch (e) { this.dashboard = null; }
      finally { this.dashLoading = false; }
    },
    _maxCount(arr) {
      try { return Math.max(1, ...(arr || []).map(x => x.count || 0)); } catch (e) { return 1; }
    },
    async loadTopics() {
      this.topicsLoading = true;
      let category = '', topic = '';
      if (this.activeCategory) {
        category = this.activeCategory;
      } else {
        const t = (this.inboxTabs || []).find(x => x.name === this.activeTabName);
        if (t) { if (t.kind === 'category') category = t.ref || ''; else if (t.kind === 'topic') topic = t.ref || ''; }
        else if ((this.topicRules || []).some(r => r.name === this.activeTabName)) { topic = this.activeTabName; }
      }
      try {
        this.topics = (await api.getTopics('subject', this.topicMultiOnly ? 2 : 1, category, topic)) || [];
      } catch (e) { this.topics = []; }
      finally { this.topicsLoading = false; }
      (this.topics || []).forEach(tp => {
        tp._expanded = false;
        (tp.members || []).forEach(m => { m._expanded = false; m._loading = false; });
      });
      this.loadTopicRules();
      this.loadInboxTabs();
      this.loadAvailableCats();
      this.loadTopicCounts();
    },
    setActiveTab(name) { this.activeTabName = name || ''; this.activeCategory = ''; this.loadTopics(); },
    enterCategory(cat) {
      this.activeCategory = cat || '';
      this.activeTabName = '';
      this.currentTopic = '';
      this.filter = 'all';
      this.searchQuery = '';
      this.currentTag = '';
      this.selectedId = null;
      this.currentEmail = null;
      this.view = 'topics';
      this.loadTopics();
    },
    openFolderDlg() {
      const acc = (this.displayAccounts[0] && this.displayAccounts[0].name) || '';
      this.folderDlg = { open: true, account: acc, mode: 'new', name: '', existing: '', field: 'from', op: 'contains', value: '' };
    },
    /** 所有已知文件夹名（各账户 + 全局自建），供规则编辑器的 datalist 用 */
    allFolderNames() {
      const out = [];
      for (const a of (this.accounts || [])) {
        for (const f of (a.folders || [])) {
          if (f && !out.includes(f)) out.push(f);
        }
      }
      for (const f of (this.globalFolders || [])) {
        if (f && !out.includes(f)) out.push(f);
      }
      return out;
    },
    folderOptions() {
      if (this.folderDlg.account === '__global__') return this.globalFolders || [];
      const a = (this.displayAccounts || []).find(x => x.name === this.folderDlg.account);
      return (a && a.folders) || [];
    },
    async submitFolderRule() {
      const d = this.folderDlg;
      const folder = (d.mode === 'new' ? d.name : d.existing).trim();
      if (!d.account || !folder) { Alpine.store('ui').notify('请选择账户和文件夹', 'error'); return; }
      if (!d.value.trim()) { Alpine.store('ui').notify('请填写规则条件的值', 'error'); return; }
      const isGlobal = d.account === '__global__';
      try {
        if (d.mode === 'new') await api.addUserFolder(d.account, folder);
        const rname = folder + ' \u21D0 ' + d.field + ':' + d.value.trim();
        await api.addRule({ name: rname, match_mode: 'all',
          conditions: (isGlobal ? [] : [{ field: 'account', op: 'is', value: d.account }]).concat([{ field: d.field, op: d.op, value: d.value.trim() }]),
          actions: { folder: folder } });
        const r = await api.applyRules();
        Alpine.store('ui').notify('已创建「' + folder + '」并归类 ' + ((r && r.applied) || 0) + ' 封', 'success');
        d.open = false;
        if (isGlobal) await this.loadGlobalFolders(); else await this.fetchAccounts();
        await this.fetchEmails();
      } catch (e) { Alpine.store('ui').notify('创建失败: ' + e, 'error'); }
    },
    async importOutlook() {
      try { await api.importOutlookRules(); Alpine.store('ui').notify('已请求导入 Outlook 规则(后台执行,稍后同步生效)', 'success'); }
      catch (e) { Alpine.store('ui').notify('导入请求失败', 'error'); }
    },
    async loadGlobalFolders() {
      try { const uf = await api.getUserFolders(); this.globalFolders = (uf && uf['__global__']) || []; }
      catch (e) { this.globalFolders = []; }
    },
    enterGlobalFolder(name) { this.currentTag = ''; this.setAccountFolder('', name); },
    async loadInboxTabs() { try { this.inboxTabs = (await api.getInboxTabs()) || []; } catch (e) { this.inboxTabs = []; } },
    async loadAvailableCats() { try { this.availableCats = (await api.getCategories()) || []; } catch (e) { this.availableCats = []; } },
    async addInboxTab() {
      const name = (this.newTabName || '').trim();
      const kind = this.newTabKind || 'category';
      const ref = (this.newTabRef || '').trim();
      if (!name || !ref) { try { Alpine.store('ui').notify('标签名和来源都要填', 'info'); } catch (e) {} return; }
      try {
        const r = await api.addInboxTab(name, kind, ref);
        if (r && r.ok) {
          this.inboxTabs = r.inbox_tabs || this.inboxTabs;
          this.newTabName = ''; this.newTabRef = ''; this.showTabManager = false;
          try { Alpine.store('ui').notify('已添加标签', 'success'); } catch (e) {}
        } else { try { Alpine.store('ui').notify('添加失败：' + ((r && r.error) || ''), 'error'); } catch (e) {} }
      } catch (e) { try { Alpine.store('ui').notify('添加失败', 'error'); } catch (er) {} }
    },
    async delInboxTab(name) {
      try {
        const r = await api.delInboxTab(name);
        this.inboxTabs = (r && r.inbox_tabs) || [];
        if (this.activeTabName === name) { this.activeTabName = ''; this.loadTopics(); }
      } catch (e) {}
    },
    setTopicBy(by) { this.topicBy = by; this.loadTopics(); },
    setTopicMultiOnly(v) { this.topicMultiOnly = !!v; this.loadTopics(); },
    toggleTopicExpand(idx) {
      const tp = this.topics[idx];
      if (!tp) return;
      tp._expanded = !tp._expanded;
      if (tp._expanded) {
        const members = tp.members || [];
        if (members.length) {
          // 最新一封自动展开；未读历史消息也自动展开（Gmail 行为）
          this._openThreadMsg(members[members.length - 1]);
          members.forEach(m => { if (m.unread) this._openThreadMsg(m); });
        }
      }
    },
    async _openThreadMsg(m) {
      if (!m || m._expanded) return;
      m._expanded = true;
      if (m._body === undefined && !m._loading) {
        m._loading = true;
        try {
          const e = await api.getEmail(m.id);
          m._html = (e && (e.html_body || e.html)) || '';
          m._senderEmail = (e && e.sender_email) || '';
          m._body = (e && (e.body || e.text)) || '';
        } catch (err) { m._html = ''; m._body = '(正文加载失败)'; }
        finally { m._loading = false; }
      }
    },
    toggleThreadMsg(topicIdx, memberIdx) {
      const tp = this.topics[topicIdx];
      if (!tp || !tp.members) return;
      const m = tp.members[memberIdx];
      if (!m) return;
      if (m._expanded) { m._expanded = false; }
      else { this._openThreadMsg(m); }
    },
    threadMsgHtml(m) {
      if (!m) return '';
      const rich = m._html;
      const opts = { allowRemote: this.threadRemoteAllowed(m) };
      if (rich) return sanitizeHTML(rich, opts) || '(无内容)';
      return sanitizeHTML(m._body || '', opts) || '(无内容)';
    },
    threadRemoteAllowed(m) { return !!(m && (m._showRemote || isImgTrusted(m._senderEmail))); },
    threadRemoteBlocked(m) {
      if (!m || !m._html) return 0;
      return sanitizeRemoteCount(m._html, { allowRemote: this.threadRemoteAllowed(m) });
    },
    threadShowRemote(m) { if (m) m._showRemote = true; },
    async analyzeTopic(idx) {
      const tp = this.topics[idx];
      if (!tp || tp._analyzing) return;
      tp._analyzing = true;
      tp.analysis = '';
      try {
        const r = await api.analyzeTopic(tp.ids);
        tp.analysis = (r && r.analysis) || '(无结果)';
      } catch (e) {
        tp.analysis = '分析失败：' + (e.message || e);
      } finally {
        tp._analyzing = false;
      }
    },
    openTopicEmail(id) {
      this.setView('inbox');
      this.openEmail(id);
    },
    get quickTopics() { return (this.topicRules || []).filter(r => r.quick).slice(0, 3); },
    async toggleQuick(name) {
      const r = (this.topicRules || []).find(x => x.name === name); if (!r) return;
      if (!r.quick && this.quickTopics.length >= 3) { try { Alpine.store('ui').notify('快捷栏最多 3 个话题，先移除一个', 'info'); } catch (e) {} return; }
      const v = !r.quick; r.quick = v;
      try { await api.updateTopicRule(name, { quick: v }); } catch (e) {}
    },
    enterTopic(name) {
      this.currentTopic = name;
      this.view = 'inbox';
      this.filter = 'all';
      this.searchQuery = '';
      this.currentAccount = '';
      this.currentFolder = '';
      this.page = 0;
      this.selectedId = null;
      this.currentEmail = null;
      this.topicDigest = '';
      this.fetchEmails();
    },
        get sortedTopics() {
      const rs = (this.topicRules || []).filter(r => this.showHiddenTopics || !r.hidden);
      return [...rs.filter(r => r.pinned), ...rs.filter(r => !r.pinned)];
    },
    async pinTopic(name) {
      const r = (this.topicRules || []).find(x => x.name === name); if (!r) return;
      const v = !r.pinned; r.pinned = v;
      try { await api.updateTopicRule(name, { pinned: v }); } catch (e) {}
    },
    async toggleHideTopic(name) {
      const r = (this.topicRules || []).find(x => x.name === name); if (!r) return;
      const v = !r.hidden; r.hidden = v;
      if (v && this.activeTabName === name) this.setActiveTab('');
      try { await api.updateTopicRule(name, { hidden: v }); } catch (e) {}
    },
    onTopicDragStart(name) { this._dragTopic = name; },
    async onTopicDrop(target) {
      const from = this._dragTopic; this._dragTopic = null;
      if (!from || from === target) return;
      const names = (this.topicRules || []).map(r => r.name);
      const fi = names.indexOf(from), ti = names.indexOf(target);
      if (fi < 0 || ti < 0) return;
      names.splice(ti, 0, names.splice(fi, 1)[0]);
      const by = {}; (this.topicRules || []).forEach(r => by[r.name] = r);
      this.topicRules = names.map(n => by[n]);
      try { await api.reorderTopicRules(names); } catch (e) {}
    },
        async loadTopicCounts() { try { this.topicCounts = (await api.getTopicCounts()) || {}; } catch (e) {} },
    async toggleMute(name) {
      const r = (this.topicRules || []).find(x => x.name === name); if (!r) return;
      const v = !r.mute; r.mute = v;
      try { await api.updateTopicRule(name, { mute: v }); } catch (e) {}
      try { Alpine.store('ui').notify(v ? '已设为免打扰（从收件箱收起）' : '已取消免打扰', 'success'); } catch (e) {}
      if (this.view === 'inbox' && !this.currentTopic) this.fetchEmails();
    },
    async loadTopicDigest() {
      if (!this.currentTopic || this.topicDigestBusy) return;
      const ids = (this.emails || []).slice(0, 20).map(e => e.id).filter(Boolean);
      if (!ids.length) { try { Alpine.store('ui').notify('这个话题暂无邮件可简报', 'info'); } catch (e) {} return; }
      this.topicDigestBusy = true; this.topicDigest = '';
      try {
        const r = await api.analyzeTopic(ids);
        this.topicDigest = (r && r.analysis) || '(无结果)';
      } catch (e) { this.topicDigest = '简报失败：' + (e.message || e); }
      finally { this.topicDigestBusy = false; }
    },
        async loadTopicRules() {
      try { this.topicRules = (await api.getTopicRules()) || []; } catch (e) { this.topicRules = []; }
    },
    _defRule() {
      const d = this.topicDef || {}; const df = d.filters || {}; const f = {};
      ['cc_me','to_me','has_attachment','unread','starred'].forEach(k => { if (df[k]) f[k] = true; });
      if ((df.from||'').trim()) f.from = df.from.trim();
      if ((df.category||'').trim()) f.category = df.category.trim();
      const kws = (d.keywords||'').split(',').map(s => s.trim()).filter(Boolean);
      return { name: (d.name||'').trim(), keywords: kws, query: (d.query||'').trim(), filters: f };
    },
    _hasCond(rule) { return rule.keywords.length || rule.query || Object.keys(rule.filters).length; },
    async nlGenerate() {
      const text = (this.topicDef.nl || '').trim();
      if (!text) { try { Alpine.store('ui').notify('先用一句话描述这个话题', 'info'); } catch (e) {} return; }
      this.topicDefBusy = true;
      try {
        const r = await api.topicFromNl(text);
        if (r && r.ok) {
          const f = r.filters || {};
          this.topicDef.filters.cc_me = !!f.cc_me;
          this.topicDef.filters.to_me = !!f.to_me;
          this.topicDef.filters.has_attachment = !!f.has_attachment;
          this.topicDef.filters.unread = !!f.unread;
          this.topicDef.filters.starred = !!f.starred;
          this.topicDef.filters.from = f.from || '';
          this.topicDef.filters.category = f.category || '';
          this.topicDef.keywords = (r.keywords || []).join(', ');
          this.topicDef.query = r.query || '';
          if (!(this.topicDef.name || '').trim()) this.topicDef.name = text.slice(0, 12);
          await this.previewTopicDef();
          try { Alpine.store('ui').notify('已生成条件，可微调后保存', 'success'); } catch (e) {}
        } else { try { Alpine.store('ui').notify('生成失败：' + ((r && r.error) || ''), 'error'); } catch (e) {} }
      } catch (e) { try { Alpine.store('ui').notify('生成失败', 'error'); } catch (er) {} }
      finally { this.topicDefBusy = false; }
    },
    async previewTopicDef() {
      const rule = this._defRule();
      if (!this._hasCond(rule)) { try { Alpine.store('ui').notify('先设置至少一个条件', 'info'); } catch (e) {} return; }
      this.topicPreviewCount = '…';
      try {
        const r = await api.previewTopic(rule.query, rule.keywords, rule.filters);
        this.topicPreviewCount = (r && typeof r.count === 'number') ? r.count : 0;
      } catch (e) { this.topicPreviewCount = null; }
    },
    async saveTopicDef() {
      const rule = this._defRule();
      if (!rule.name) { try { Alpine.store('ui').notify('给话题起个名字', 'info'); } catch (e) {} return; }
      if (!this._hasCond(rule)) { try { Alpine.store('ui').notify('至少设置一个条件（属性/关键词/语义）', 'info'); } catch (e) {} return; }
      try {
        const r = await api.addTopicRule(rule.name, rule.keywords, rule.query, rule.filters);
        if (r && r.ok) {
          this.topicRules = r.topic_rules || this.topicRules;
          this.topicDef = { name: '', nl: '', keywords: '', query: '', filters: { cc_me: false, to_me: false, has_attachment: false, unread: false, starred: false, from: '', category: '' } };
          this.topicPreviewCount = null;
          this.setActiveTab(rule.name);
          try { Alpine.store('ui').notify('已保存话题', 'success'); } catch (e) {}
        } else { try { Alpine.store('ui').notify('保存失败：' + ((r && r.error) || ''), 'error'); } catch (e) {} }
      } catch (e) { try { Alpine.store('ui').notify('保存失败', 'error'); } catch (er) {} }
    },
    async delTopicRule(name) {
      try { const r = await api.delTopicRule(name); this.topicRules = (r && r.topic_rules) || []; await this.loadTopics(); } catch (e) {}
    },
    async extractTodosFor(id, force = false) {
      if (!id || this.todoExtracting) return;
      this.todoExtracting = true;
      try {
        const r = await api.extractTodos(id, force);
        if (r && r.ok) {
          // 区分三种情况，否则「0 条」既可能是没待办、也可能是全都已存在，
          // 用户无法判断该不该再点一次。
          const from = r.source === 'cached' ? '摘要已提取的' : 'AI 新提取的';
          let msg;
          if (r.count > 0) {
            msg = '已加入 ' + r.count + ' 条待办（' + from + ' ' + (r.considered || r.count) + ' 条）';
          } else if (r.considered > 0) {
            msg = from + ' ' + r.considered + ' 条待办均已在列表中，未重复添加';
          } else {
            msg = '该邮件没有可提取的待办';
          }
          try { Alpine.store('ui').notify(msg, r.count > 0 ? 'success' : 'info'); } catch (e) {}
          if (this.fetchTodos) await this.fetchTodos();
        } else {
          const err = (r && r.error) ? ('：' + r.error) : '';
          try { Alpine.store('ui').notify('提取待办失败' + err, 'error'); } catch (e) {}
        }
      } catch (e) {
        try { Alpine.store('ui').notify('提取待办失败', 'error'); } catch (er) {}
      } finally {
        this.todoExtracting = false;
      }
    },

    // ---- 保存的搜索 / 智能视图 ----
    async loadSavedSearches() {
      try { this.savedSearches = (await api.getSavedSearches()) || []; } catch (e) { this.savedSearches = []; }
    },
    async saveCurrentSearch() {
      const q = (this.searchQuery || '').trim();
      if (!q) { try { Alpine.store('ui').notify('先输入搜索条件再保存', 'info'); } catch (e) {} return; }
      const name = (window.prompt('给这个搜索起个名字：', q.slice(0, 20)) || '').trim();
      if (!name) return;
      try {
        const r = await api.addSavedSearch(name, q);
        if (r && r.saved_searches) this.savedSearches = r.saved_searches;
        try { Alpine.store('ui').notify('已保存搜索「' + name + '」', 'success'); } catch (e) {}
      } catch (e) { try { Alpine.store('ui').notify('保存失败', 'error'); } catch (er) {} }
    },
    applySavedSearch(query) {
      this.searchQuery = query; this.page = 0; this.view = 'inbox'; this.currentFolder = '';
      this.fetchEmails();
    },
    async deleteSavedSearch(name) {
      try { const r = await api.delSavedSearch(name); this.savedSearches = (r && r.saved_searches) || []; } catch (e) {}
    },

    // ============ 邮件详情 ============

    async openEmail(id) {
      this.selectedId = id;
      this.detailLoading = true;
      // 乐观即时标记：打开时若未读且未开启延迟，立即标为已读，不用等正文网络拉取返回
      const email = this.emails.find(e => e.id === id);
      if (email && email.unread && !this.deferRead) {
        email.unread = false;
        this._bumpUnread(-1);
        api.patchEmail(id, { unread: false }).catch(() => {});
      }
      try {
        this.currentEmail = await api.getEmail(id);
        if (this.currentEmail && !this.deferRead) {
          this.currentEmail.unread = false;
        }
      } catch (err) {
        console.error('[Mail] openEmail error:', err);
        Alpine.store('ui').notify('加载邮件详情失败', 'error');
      } finally {
        this.detailLoading = false;
      }
    },

    // ============ 邮件操作 ============

    async toggleStar() {
      if (!this.selectedId) return;
      const email = this.emails.find(e => e.id === this.selectedId);
      if (!email) return;
      email.starred = !email.starred;
      try {
        await api.patchEmail(this.selectedId, { starred: email.starred });
      } catch {
        email.starred = !email.starred;
      }
    },

    async toggleStarById(id) {
      const email = this.emails.find(e => e.id === id);
      if (!email) return;
      email.starred = !email.starred;
      try {
        await api.patchEmail(id, { starred: email.starred });
      } catch {
        email.starred = !email.starred;
      }
    },
    async toggleArchiveById(id) {
      const email = this.emails.find(e => e.id === id);
      if (!email) return;
      const na = !email.archived;
      email.archived = na;
      try {
        await api.patchEmail(id, { archived: na ? 1 : 0 });
        if (na && (this.view === 'inbox' || this.view === 'unread')) {
          this.emails = this.emails.filter(e => e.id !== id);
        }
      } catch {
        email.archived = !na;
      }
    },
    async toggleTagById(id, tag) {
      const email = this.emails.find(e => e.id === id);
      if (!email) return;
      const tags = Array.isArray(email.tags) ? email.tags.slice() : [];
      const i = tags.indexOf(tag);
      if (i >= 0) tags.splice(i, 1); else tags.push(tag);
      email.tags = tags;
      try { await api.patchEmail(id, { tags: tags }); } catch {}
    },
    setTag(tag) {
      this.currentTag = tag;
      this.view = '';
      this.currentTopic = '';
      this.filter = 'all';
      this.searchQuery = '';
      this.page = 0;
      this.selectedId = null;
      this.currentEmail = null;
      this.fetchEmails();
    },
    tagColor(name) {
      const t = (this.tagPalette || []).find(x => x.name === name);
      return t ? t.color : '#9ca3af';
    },

    async toggleUnread() {
      if (!this.selectedId) return;
      const email = this.emails.find(e => e.id === this.selectedId);
      if (!email) return;
      email.unread = !email.unread;
      this._bumpUnread(email.unread ? +1 : -1);
      try {
        await api.patchEmail(this.selectedId, { unread: email.unread });
      } catch {
        email.unread = !email.unread;
        this._bumpUnread(email.unread ? +1 : -1);
      }
    },

    async deleteSelected() {
      if (!this.selectedId) return;
      const delId = this.selectedId;
      const delEmail = this.emails.find(e => e.id === delId);
      try {
        const res = await api.deleteEmail(delId);
        if (delEmail && delEmail.unread) this._bumpUnread(-1);
        this.emails = this.emails.filter(e => e.id !== delId);
        Alpine.store('ui').notify('邮件已删除', 'success');
        this.trackAction(res && res.action_id, { done: '删除已在 Outlook 生效', error: '删除失败' });
        // 选中下一封
        if (this.emails.length > 0) {
          this.selectedId = this.emails[0].id;
          this.openEmail(this.selectedId);
        } else {
          this.selectedId = null;
          this.currentEmail = null;
        }
      } catch (err) {
        Alpine.store('ui').notify('删除失败', 'error');
      }
    },

    // ============ 通讯录 ============

    async fetchContacts(force = false) {
      if (this.contactsLoaded && !force) return;
      try {
        const rows = await api.getContacts();
        this.contacts = Array.isArray(rows) ? rows : [];
        this.contactsLoaded = true;
      } catch (err) {
        console.error('[Mail] fetchContacts error:', err);
      }
    },

    // 通讯录页的过滤结果。纯本地，0 延迟。
    get filteredContacts() {
      const q = (this.contactQuery || '').trim().toLowerCase();
      if (!q) return this.contacts;
      return this.contacts.filter(c =>
        (c.email || '').toLowerCase().includes(q) ||
        (c.name || '').toLowerCase().includes(q));
    },

    // 写邮件给某位联系人
    composeTo(addr) {
      if (!addr) return;
      this.pendingComposeTo = addr;
      Alpine.store('ui').composeMode = 'new';
      Alpine.store('ui').composeOpen = true;
    },

    // 看与某位联系人的往来邮件：走现有搜索，不新增端点
    viewCorrespondence(addr) {
      if (!addr) return;
      this.view = 'inbox';
      this.currentTopic = '';
      this.currentTag = '';
      this.searchQuery = addr;
      this.page = 0;
      this.fetchEmails();
    },

    // ============ 草稿箱 ============

    async fetchDrafts() {
      try {
        this.drafts = await api.getDrafts();
      } catch (err) {
        console.error('[Mail] fetchDrafts error:', err);
      }
    },

    // 保存草稿：有 id 走 PATCH，无 id 走 POST。回 id 供 compose 继续跟踪。
    async saveDraft(payload, draftId = null) {
      try {
        if (draftId) {
          await api.patchDraft(draftId, payload);
          await this.fetchDrafts();
          Alpine.store('ui').notify('草稿已更新', 'success');
          return draftId;
        }
        const res = await api.createDraft(Object.assign({ source: 'ui' }, payload));
        await this.fetchDrafts();
        Alpine.store('ui').notify('已存入草稿箱', 'success');
        return res && res.id;
      } catch (err) {
        Alpine.store('ui').notify('保存草稿失败', 'error');
        return null;
      }
    },

    // 打开草稿到撰写窗。compose 的 prefill() 会读 editingDraft。
    openDraft(id) {
      const d = this.drafts.find(x => x.id === id);
      if (!d) return;
      this.editingDraft = d;
      Alpine.store('ui').composeMode = 'draft';
      Alpine.store('ui').composeOpen = true;
    },

    async removeDraft(id) {
      try {
        await api.deleteDraft(id);
        this.drafts = this.drafts.filter(d => d.id !== id);
      } catch (err) {
        Alpine.store('ui').notify('删除草稿失败', 'error');
      }
    },

    async sendEmail(data) {
      try {
        const res = await api.sendEmail(data);
        if (res && res.ok === false) {
          Alpine.store('ui').notify(res.error || '发送失败', 'error');
          throw new Error(res.error || 'send failed');
        }
        Alpine.store('ui').notify('已提交发送…', 'info');
        Alpine.store('ui').composeOpen = false;
        // 草稿的使命到此结束；删除失败不影响已提交的发送，只记日志
        if (data && data.draft_id) {
          this.removeDraft(data.draft_id).catch(() => {});
          this.editingDraft = null;
        }
        this.trackAction(res && res.action_id, { done: '邮件已发送', error: '发送失败' });
      } catch (err) {
        Alpine.store('ui').notify('发送失败', 'error');
        throw err;
      }
    },

    async forwardEmail(id, to, note, opts = {}) {
      try {
        const res = await api.forwardEmail(id, {
          to, note,
          cc: opts.cc || '',
          bcc: opts.bcc || '',
          is_html: opts.is_html || false,
          attachments: opts.attachments || [],
        });
        if (res && res.ok === false) {
          Alpine.store('ui').notify(res.error || '转发失败', 'error');
          return;
        }
        Alpine.store('ui').notify('已提交转发…', 'info');
        this.trackAction(res && res.action_id, { done: '邮件已转发', error: '转发失败' });
      } catch (err) {
        Alpine.store('ui').notify('转发失败', 'error');
      }
    },

    // 轮询队列动作（发送/转发/删除）的最终执行结果并给出反馈。
    // 这些动作由 daemon 异步消费 Outlook COM 执行，需回查真实结果，
    // 避免“看似成功实际未执行”（如 daemon 未运行或动作过期）。
    trackAction(actionId, labels = {}) {
      if (!actionId) return;
      let attempts = 0;
      const poll = setInterval(async () => {
        attempts++;
        try {
          const st = await api.getActionStatus(actionId);
          if (st.status === 'done') {
            clearInterval(poll);
            Alpine.store('ui').notify(labels.done || '操作成功', 'success');
          } else if (st.status === 'error' || st.status === 'expired') {
            clearInterval(poll);
            const detail = st.error ? `：${st.error}` : '';
            Alpine.store('ui').notify((labels.error || '操作失败') + detail, 'error');
          } else if (st.status === 'not_found') {
            clearInterval(poll);
          }
        } catch {
          clearInterval(poll);
        }
        if (attempts > 30) clearInterval(poll);  // 最长约 60s 后停止轮询
      }, 2000);
    },

    // ============ 键盘导航 ============

    selectNext() {
      const list = this.emails || [];
      if (!list.length) return;
      const idx = list.findIndex(e => e.id === this.selectedId);
      if (idx < 0) { this.gotoEmail(list[0].id); return; }   // 尚未选中：从第一封开始
      if (idx < list.length - 1) { this.gotoEmail(list[idx + 1].id); return; }
      // 已在当前页末尾：还有下一页就先加载，加载完继续下移（列表是无限滚动分页）
      if (this.hasMore && !this.loading) {
        Promise.resolve(this.loadMore()).then(() => {
          const l2 = this.emails || [];
          const i2 = l2.findIndex(e => e.id === this.selectedId);
          if (i2 >= 0 && i2 < l2.length - 1) this.gotoEmail(l2[i2 + 1].id);
        }).catch(() => {});
      }
    },

    selectPrev() {
      const list = this.emails || [];
      if (!list.length) return;
      const idx = list.findIndex(e => e.id === this.selectedId);
      if (idx < 0) { this.gotoEmail(list[0].id); return; }
      if (idx > 0) this.gotoEmail(list[idx - 1].id);
    },

    // 键盘跳转统一入口：选中 + 打开 + 滚动进视口
    gotoEmail(id) {
      if (!id) return;
      this.selectedId = id;
      this.openEmail(id);
      this.scrollRowIntoView(id);
    },

    scrollRowIntoView(id) {
      try {
        const sel = (window.CSS && CSS.escape) ? CSS.escape(String(id)) : String(id);
        requestAnimationFrame(() => {
          const row = document.querySelector('[data-email-id="' + sel + '"]');
          if (row && row.scrollIntoView) row.scrollIntoView({ block: 'nearest' });
        });
      } catch (e) { /* 定位失败不影响选中 */ }
    },

    // ============ 账户/同步 ============

    async fetchAccounts() {
      try {
        this.accounts = await api.getAccounts();
      } catch (err) {
        console.error('[Mail] fetchAccounts error:', err);
      }
    },

    // ============ 账户显示偏好（别名 / 排序）============

    _ACCT_PREFS_KEY: 'quillbox-account-prefs',

    _loadAccountPrefs() {
      try {
        const raw = localStorage.getItem(this._ACCT_PREFS_KEY);
        if (raw) {
          const p = JSON.parse(raw);
          this.accountAliases = (p && typeof p.aliases === 'object' && p.aliases) || {};
          this.accountOrder = Array.isArray(p && p.order) ? p.order : [];
          this.folderOrders = (p && typeof p.folderOrders === 'object' && p.folderOrders) || {};
        }
      } catch (e) {
        console.warn('[Mail] 读取账户偏好失败:', e);
      }
    },

    _saveAccountPrefs() {
      try {
        localStorage.setItem(this._ACCT_PREFS_KEY, JSON.stringify({
          aliases: this.accountAliases,
          order: this.accountOrder,
          folderOrders: this.folderOrders,
        }));
      } catch (e) {
        console.warn('[Mail] 保存账户偏好失败:', e);
      }
    },

    // 重命名账户（仅显示别名；传空则恢复原始地址）
    renameAccount(email, alias) {
      alias = (alias || '').trim();
      const next = { ...this.accountAliases };
      if (alias && alias !== email) next[email] = alias;
      else delete next[email];
      this.accountAliases = next;   // 替换引用以触发响应式
      this._saveAccountPrefs();
    },

    // 拖拽排序：把 draggedEmail 移动到 targetEmail 之前
    reorderAccount(draggedEmail, targetEmail) {
      if (!draggedEmail || draggedEmail === targetEmail) return;
      let order = this.displayAccounts.map(a => a.name).filter(e => e !== draggedEmail);
      const idx = order.indexOf(targetEmail);
      if (idx === -1) order.push(draggedEmail);
      else order.splice(idx, 0, draggedEmail);
      this.accountOrder = order;
      this._saveAccountPrefs();
    },
    _orderFolders(name, folders) {
      const ord = (this.folderOrders && this.folderOrders[name]) || [];
      const rank = (f) => { const i = ord.indexOf(f); return i === -1 ? Number.MAX_SAFE_INTEGER : i; };
      return (folders || []).slice()
        .map((f, i) => ({ f, i }))
        .sort((x, y) => { const rx = rank(x.f), ry = rank(y.f); return rx !== ry ? rx - ry : x.i - y.i; })
        .map((o) => o.f);
    },
    reorderFolder(account, dragged, target) {
      if (!dragged || dragged === target) return;
      const acct = this.displayAccounts.find(a => a.name === account);
      if (!acct) return;
      let order = (acct.folders || []).filter(f => f !== dragged);
      const idx = order.indexOf(target);
      if (idx === -1) order.push(dragged); else order.splice(idx, 0, dragged);
      this.folderOrders = { ...this.folderOrders, [account]: order };
      this._saveAccountPrefs();
    },

    async fetchStats() {
      try {
        this.stats = await api.getStats();
      } catch (err) {
        console.error('[Mail] fetchStats error:', err);
      }
    },

    async sync() {
      try {
        Alpine.store('ui').syncRunning = true;
        await api.triggerSync();
        Alpine.store('ui').notify('同步已开始', 'info');
        // 轮询同步状态
        const poll = setInterval(async () => {
          await this.fetchStats();
          if (!this.stats.sync_running) {
            clearInterval(poll);
            Alpine.store('ui').syncRunning = false;
            await this.fetchEmails();
            Alpine.store('ui').notify('同步完成', 'success');
          }
        }, 2000);
      } catch (err) {
        Alpine.store('ui').syncRunning = false;
        Alpine.store('ui').notify('同步失败', 'error');
      }
    },

    // ============ 待办 ============

    async fetchTodos() {
      try {
        this.todos = await api.getTodos();
      } catch (err) {
        console.error('[Mail] fetchTodos error:', err);
      }
    },

    async addTodo(text) {
      const content = (text || '').trim();
      if (!content) return;
      try {
        await api.createTodo({ content });
        await this.fetchTodos();
      } catch (err) {
        Alpine.store('ui').notify('创建待办失败', 'error');
      }
    },

    async toggleTodo(id) {
      const todo = this.todos.find(t => t.id === id);
      if (!todo) return;
      const to = (todo.status === 'done') ? 'pending' : 'done';
      todo.status = to;
      try {
        await api.patchTodo(id, { status: to });
      } catch {
        todo.status = (to === 'done') ? 'pending' : 'done';
      }
    },

    async removeTodo(id) {
      try {
        await api.deleteTodo(id);
        this.todos = this.todos.filter(t => t.id !== id);
      } catch (err) {
        Alpine.store('ui').notify('删除待办失败', 'error');
      }
    },
    openTodoSource(id) {
      if (!id) return;
      this.view = 'inbox';
      this.fetchEmails();
      this.openEmail(id);
    },
    openTodoBatch() {
      const b = this.todoBatch;
      b.open = true; b.running = false; b.scanned = 0; b.total = 0; b.added = 0;
    },
    async runTodoBatch() {
      const b = this.todoBatch;
      if (b.running) return;
      b.running = true; b.scanned = 0; b.total = 0; b.added = 0;
      try {
        const r = await api.extractTodosBatch({ days: Number(b.days) || 7, accounts: (b.accounts || []).slice(), use_ai: !!b.useAI, max_emails: 200 });
        if (!r || !r.ok) { b.running = false; try { Alpine.store('ui').notify((r && r.error) || '启动失败', 'error'); } catch (e) {} return; }
        const self = this;
        const poll = async function () {
          try {
            const s = await api.extractTodosBatchStatus();
            b.scanned = s.scanned || 0; b.total = s.total || 0; b.added = s.added || 0;
            if (s.running) { setTimeout(poll, 1500); }
            else {
              b.running = false;
              try { Alpine.store('ui').notify('批量提取完成：扫描 ' + (s.scanned||0) + ' 封，新增 ' + (s.added||0) + ' 条待办', 'success'); } catch (e) {}
              await self.fetchTodos();
            }
          } catch (e) { b.running = false; }
        };
        setTimeout(poll, 1500);
      } catch (e) { b.running = false; try { Alpine.store('ui').notify('启动失败', 'error'); } catch (er) {} }
    },

    // ============ 辅助 ============

    // 本地维护未读计数，保证「打开邮件即已读」时侧边栏立即刷新，
    // 无需等待下一次 fetchStats。delta 为 +1 / -1。
    _bumpUnread(delta) {
      if (!this.stats) return;
      for (const k of ['unread', 'unread_inbox']) {
        if (typeof this.stats[k] === 'number') {
          this.stats[k] = Math.max(0, this.stats[k] + delta);
        }
      }
    },

    // 侧边栏徽标：优先用后端的收件箱口径（未删除+未归档+排除免打扰），
    // 它与收件箱/未读列表能显示的内容一一对应，不会出现点不掉的计数。
    // 老后端没有 unread_inbox 时回退到全库未读，再回退到当前页统计。
    get unreadCount() {
      const s = this.stats || {};
      if (typeof s.unread_inbox === 'number') return s.unread_inbox;
      if (typeof s.unread === 'number') return s.unread;
      return this.emails.filter(e => e.unread).length;
    },

    // 应用别名与自定义排序后的账户列表（供侧边栏渲染）。
    // 未纳入自定义顺序的账户保持后端原有相对顺序，排在已排序账户之后。
    get displayAccounts() {
      const accts = this.accounts || [];
      const order = this.accountOrder || [];
      const rank = (name) => {
        const i = order.indexOf(name);
        return i === -1 ? Number.MAX_SAFE_INTEGER : i;
      };
      return accts
        .map((a, i) => ({ a, i }))
        .sort((x, y) => {
          const rx = rank(x.a.name), ry = rank(y.a.name);
          return rx !== ry ? rx - ry : x.i - y.i;
        })
        .map(({ a }) => ({
          ...a,
          displayName: this.accountAliases[a.name] || a.name,
          folders: this._orderFolders(a.name, a.folders || []),
        }));
    },

    get selectedEmail() {
      return this.emails.find(e => e.id === this.selectedId);
    },
  };
}

// ========== stores/ai.js ==========
/**
 * AI 状态管理
 * 管理AI摘要、聊天对话、草稿生成
 */

function aiStore() {
  return {
    // 摘要
    summary: '',
    summaryLoading: false,
    summaryCollapsed: false,
    summaryEmailId: null,

    // 聊天
    chatMessages: [],
    chatInput: '',
    chatLoading: false,
    chatMode: 'global', // 'global' | 'email'
    chatScrollTick: 0,   // 递增计数：驱动视图自动滚动跟随流式输出

    // 草稿
    draftContent: '',
    draftLoading: false,
    draftTone: 'professional', // 'professional' | 'friendly' | 'formal' | 'brief'

    // 流控制器
    _summarySource: null,
    _chatController: null,
    _draftController: null,

    // 对话历史持久化的 localStorage key
    _CHAT_KEY: 'quillbox-chat-history',

    // Alpine 初始化时自动调用：恢复上次保存的对话
    init() {
      try {
        const saved = localStorage.getItem(this._CHAT_KEY);
        if (saved) {
          const arr = JSON.parse(saved);
          if (Array.isArray(arr)) this.chatMessages = arr;
        }
      } catch (e) {
        console.warn('[AI] 恢复对话历史失败:', e);
      }
    },

    // 将当前对话写入 localStorage（去掉流式临时标记）
    _persistChat() {
      try {
        const data = this.chatMessages.map(m => ({
          role: m.role, content: m.content, time: m.time,
        }));
        localStorage.setItem(this._CHAT_KEY, JSON.stringify(data));
      } catch (e) {
        console.warn('[AI] 保存对话历史失败:', e);
      }
    },

    // 导出当前对话为 Markdown 文件下载
    exportChat() {
      if (!this.chatMessages.length) {
        try { Alpine.store('ui').notify('当前没有可导出的对话', 'info'); } catch {}
        return;
      }
      const lines = ['# Quillbox AI 对话记录', '', `导出时间：${new Date().toLocaleString('zh-CN')}`, ''];
      for (const m of this.chatMessages) {
        const who = m.role === 'user' ? '🧑 我' : '🤖 AI';
        lines.push(`## ${who}  ${m.time || ''}`, '', (m.content || '').trim(), '');
      }
      const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '');
      a.download = `quillbox-chat-${ts}.md`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      try { Alpine.store('ui').notify('对话已导出', 'success'); } catch {}
    },

    // ============ AI 摘要 ============

    /**
     * 获取邮件AI摘要（流式）
     * @param {string} emailId - 邮件ID
     */
    async fetchSummary(emailId) {
      if (!emailId) return;
      // 同一邮件不重复获取
      if (this.summaryEmailId === emailId && this.summary) return;

      // 取消之前的请求
      this.cancelSummary();

      this.summaryEmailId = emailId;
      this.summary = '';
      this.summaryLoading = true;
      this.summaryCollapsed = false;

      this._summarySource = streamSummary(emailId, {
        onMessage: (data) => {
          const text = typeof data === 'string' ? data : (data.content || data.text || data.chunk || '');
          this.summary += text;
        },
        onError: (err) => {
          console.error('[AI] Summary stream error:', err);
          this.summaryLoading = false;
          if (!this.summary) {
            this.summary = '摘要生成失败，请稍后重试。';
          }
        },
        onDone: () => {
          this.summaryLoading = false;
        },
      });
    },

    cancelSummary() {
      if (this._summarySource) {
        this._summarySource.close();
        this._summarySource = null;
      }
    },

    toggleSummary() {
      this.summaryCollapsed = !this.summaryCollapsed;
    },

    // ============ AI 聊天 ============

    /**
     * 发送聊天消息
     */
    async sendMessage() {
      const message = this.chatInput.trim();
      if (!message || this.chatLoading) return;

      // 多轮上下文：发送前捕获既有对话（不含本轮），最多最近 10 条
      const history = this.chatMessages
        .filter(m => (m.role === 'user' || m.role === 'assistant') && m.content)
        .slice(-10)
        .map(m => ({ role: m.role, content: m.content }));

      // 添加用户消息
      this.chatMessages.push({
        role: 'user',
        content: message,
        time: new Date().toLocaleTimeString(),
      });
      this.chatInput = '';
      this.chatLoading = true;

      // 添加AI消息占位
      const aiMessage = {
        role: 'assistant',
        content: '',
        time: new Date().toLocaleTimeString(),
        streaming: true,
      };
      this.chatMessages.push(aiMessage);

      const emailId = Alpine.store('mail').selectedId;
      const lastIdx = this.chatMessages.length - 1;
      const callbacks = {
        onChunk: (text) => {
          // 直接修改最后一条消息内容，用splice(lastIdx,1,...)触发Alpine响应式
          this.chatMessages[lastIdx].content += text;
          // 仅在每10个chunk或内容较短时触发视图更新（节流）
          if (this.chatMessages[lastIdx].content.length % 40 < text.length) {
            this.chatMessages.splice(lastIdx, 1, this.chatMessages[lastIdx]);
            this.chatScrollTick++;
          }
        },
        onError: (err) => {
          console.error('[AI] Chat error:', err);
          this.chatMessages[lastIdx].content += '\n\n[连接中断]';
          this.chatMessages[lastIdx].streaming = false;
          this.chatLoading = false;
          this.chatMessages.splice(lastIdx, 1, this.chatMessages[lastIdx]);
          this.chatScrollTick++;
          this._persistChat();
        },
        onDone: () => {
          this.chatMessages[lastIdx].streaming = false;
          this.chatLoading = false;
          this.chatMessages.splice(lastIdx, 1, this.chatMessages[lastIdx]);
          this.chatScrollTick++;
          this._persistChat();
        },
      };

      if (this.chatMode === 'email' && emailId) {
        this._chatController = streamChat(message, emailId, history, callbacks);
      } else {
        this._chatController = streamGlobalChat(message, history, callbacks);
      }
    },

    cancelChat() {
      if (this._chatController) {
        this._chatController.abort();
        this._chatController = null;
        this.chatLoading = false;
      }
    },

    clearChat() {
      this.chatMessages = [];
      this.cancelChat();
      try { localStorage.removeItem(this._CHAT_KEY); } catch {}
    },

    // ============ AI 草稿 ============

    /**
     * 生成回复草稿
     * @param {string} emailId - 邮件ID
     * @param {string} tone - 语气
     */
    async generateDraft(emailId, tone) {
      if (!emailId) return;
      this.cancelDraft();

      this.draftContent = '';
      this.draftLoading = true;
      this.draftTone = tone || this.draftTone;

      this._draftController = streamDraft(emailId, this.draftTone, {
        onChunk: (text) => {
          this.draftContent += text;
        },
        onError: (err) => {
          console.error('[AI] Draft error:', err);
          this.draftLoading = false;
          if (!this.draftContent) {
            this.draftContent = '草稿生成失败，请重试。';
          }
        },
        onDone: () => {
          this.draftLoading = false;
        },
      });
    },

    cancelDraft() {
      if (this._draftController) {
        this._draftController.abort();
        this._draftController = null;
      }
    },

    // ============ 辅助 ============

    get lastAiMessage() {
      const msgs = this.chatMessages.filter(m => m.role === 'assistant');
      return msgs[msgs.length - 1];
    },

    get isStreaming() {
      return this.summaryLoading || this.chatLoading || this.draftLoading;
    },
  };
}

// ========== stores/settings.js ==========
/**
 * 设置状态管理
 * 字段名与后端 GET/POST /api/settings 完全一致
 */

function settingsStore() {
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

// ========== components/emailList.js ==========
/**
 * 邮件列表组件
 */
const _timeCache = new Map();

function emailListComp() { return ({
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
}); }

// ========== components/emailDetail.js ==========
/**
 * 邮件详情组件
 */

function emailDetailComp() { return ({
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
}); }

// ========== components/sidebar.js ==========
/**
 * 侧边栏组件
 */
function sidebarComp() { return ({
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
}); }

// ========== components/aiChat.js ==========
/**
 * AI聊天面板组件
 */
function aiChatComp() { return ({
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
}); }

// ========== components/compose.js ==========
/**
 * 写邮件组件（富文本编辑 + 附件 + 表格）
 */

// 保存编辑器光标位置（模块级，避免被 Alpine 响应式代理包裹导致 Range 失效）
let _savedRange = null;

function composeComp() { return ({
  to: '',
  cc: '',
  bcc: '',
  showBcc: false,        // 密送默认折叠：低频字段不该占常用路径的地方
  subject: '',
  body: '',              // HTML 正文
  attachments: [],       // [{name, size, path, uploading}]
  sending: false,
  savingDraft: false,
  draftId: null,         // 非空表示当前编辑的是草稿箱里的某一条

  // ---- 发件人 ----
  senders: [],
  fromAccount: '',

  // ---- 富文本工具栏 ----
  fmt: {},                 // queryCommandState 快照，用于按钮高亮
  curBlock: 'p',           // 当前段落样式，用于下拉回显
  curFont: '',
  curSize: '',
  colorPickerOpen: false,
  hilitePickerOpen: false,
  tablePickerHeader: true,

  // ---- 收件人补全 ----
  acField: '',           // 当前正在补全哪个字段：to / cc / bcc
  acList: [],
  acIndex: 0,
  maximized: false,      // 撰写窗口是否最大化
  tablePickerOpen: false,
  tableRows: 3,
  tableCols: 3,

  get mode() { return Alpine.store('ui').composeMode; },

  init() {
    this.$watch(() => Alpine.store('ui').composeOpen, (open) => {
      if (open) this.prefill();
    });
    // AI 草稿流式生成时实时同步到编辑器
    this.$watch(() => Alpine.store('ai').draftContent, (val) => {
      if (Alpine.store('ui').composeOpen && val) {
        this.setEditorHtml(this._toHtml(val));
      }
    });
  },

  prefill() {
    const mode = this.mode;
    const email = Alpine.store('mail').currentEmail;
    const draft = Alpine.store('mail').editingDraft;
    // 从草稿箱打开：原样恢复，不套用 Re:/Fwd: 那套改写
    if (mode === 'draft' && draft) {
      this.draftId = draft.id;
      this.to = draft.to || '';
      this.cc = draft.cc || '';
      // 草稿刻意不保存 bcc：密送名单落库后会随草稿在服务端草稿箱可见，
      // 那等于把"密送"变成"半公开"。每次撰写重新填。
      this.bcc = '';
      this.showBcc = false;
      this.subject = draft.subject || '';
      this.body = draft.is_html ? (draft.body || '') : this._toHtml(draft.body || '');
      this.attachments = [];
      Alpine.store('mail').editingDraft = null;
      this.$nextTick(() => this.setEditorHtml(this.body));
      return;
    }
    this.draftId = null;
    this.bcc = '';
    this.showBcc = false;
    this.acClose();
    // 可发信账户：只有一个时界面上不显示下拉（永远只有一项的下拉是噪音）
    if (!this.senders.length) {
      (async () => {
        try {
          const d = await api.getSenders();
          this.senders = (d && d.senders) || [];
          if (!this.fromAccount) this.fromAccount = (d && d.default) || '';
        } catch (e) { /* 取不到就退回单账户行为 */ }
      })();
    }
    // 收件人补全的数据源。原来只在访问通讯录页时加载，导致冷启动直接
    // 写邮件时一个候选都出不来（实测 contacts=0）。放在这里按需加载，
    // fetchContacts 内部有 contactsLoaded 短路，只会真取一次（6KB）。
    // 刻意不 await：补全是输入时才用，不该拖慢窗口打开。
    try { Alpine.store('mail').fetchContacts(); } catch (e) {}
    // 通讯录页点"写邮件给 TA"时带过来的收件人
    const pending = Alpine.store('mail').pendingComposeTo;
    if (pending) {
      Alpine.store('mail').pendingComposeTo = '';
      this.$nextTick(() => { this.to = pending + '; '; });
    }
    if (mode === 'reply' && email) {
      this.to = email.from_addr || email.from || '';
      this.subject = email.subject?.startsWith('Re:') ? email.subject : `Re: ${email.subject || ''}`;
      this.body = '';
    } else if (mode === 'reply-all' && email) {
      this.to = email.from_addr || email.from || '';
      const self = (email.account || '').toLowerCase();
      const sender = (email.from_addr || email.from || '').toLowerCase();
      const recips = Array.isArray(email.recipients) ? email.recipients : [];
      const ccAddrs = recips
        .map(r => (r.email || '').trim())
        .filter(a => a && a.toLowerCase() !== self && a.toLowerCase() !== sender);
      this.cc = [...new Set(ccAddrs)].join('; ');
      this.subject = email.subject?.startsWith('Re:') ? email.subject : `Re: ${email.subject || ''}`;
      this.body = '';
    } else if (mode === 'forward' && email) {
      this.to = '';
      this.subject = email.subject?.startsWith('Fwd:') ? email.subject : `Fwd: ${email.subject || ''}`;
      this.body = '';
    } else {
      this.to = '';
      this.cc = '';
      this.subject = '';
      this.body = '';
    }
    this.attachments = [];
    this.$nextTick(() => this.setEditorHtml(this._toHtml(this.body)));
  },

  // ============ 富文本编辑器 ============

  // 从 contenteditable 读取最新 HTML 到 body
  syncBody() {
    const el = this.$refs.editor;
    if (el) this.body = el.innerHTML;
  },

  // 设置编辑器内容
  setEditorHtml(html) {
    this.body = html || '';
    const el = this.$refs.editor;
    if (el) el.innerHTML = this.body;
  },

  // 执行格式化命令（execCommand：主流浏览器均支持，无需额外依赖）
  exec(cmd, val = null) {
    const el = this.$refs.editor;
    if (el) el.focus();
    try { document.execCommand(cmd, false, val); } catch (e) {}
    this.syncBody();
  },

  createLink() {
    const url = prompt('请输入链接地址：', 'https://');
    if (url) this.exec('createLink', url);
  },

  // ============ 内联样式（字体 / 字号 / 颜色）============
  // 不走 execCommand：它的 fontSize 只接受 1~7（老式 <font size>），
  // 表达不了真实 px；foreColor 默认还产出 <font color> 这种上古标记。
  // 这四项本质都是"给选中文字加内联 style"，手工包裹反而更简单可控，
  // 且产出纯内联样式——邮件客户端会剥掉 class，内联才靠得住。
  _styleRange(props) {
    const el = this.$refs.editor;
    if (!el) return;
    this._restoreCaret();
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount) return;
    const range = sel.getRangeAt(0);
    if (!el.contains(range.commonAncestorContainer)) return;

    // 没选中文字：插一个带样式的空 span 并把光标放进去，
    // 后续输入就继承该样式。否则"先设字号再打字"这个最常见用法会失效。
    if (range.collapsed) {
      const span = document.createElement('span');
      Object.assign(span.style, props);
      span.appendChild(document.createTextNode('\u200b'));
      range.insertNode(span);
      const r = document.createRange();
      r.setStart(span.firstChild, 1);
      r.collapse(true);
      sel.removeAllRanges();
      sel.addRange(r);
      el.focus();
      this.syncBody();
      return;
    }

    // 逐个文本节点包裹。整块 extractContents 会在跨段落时
    // 产出 <span> 里套 <p> 的坏结构；surroundContents 则直接抛错。
    let root = range.commonAncestorContainer;
    // TreeWalker 的 root 是文本节点时 nextNode() 恒为 null（root 不被访问）
    if (root.nodeType === 3) root = root.parentNode;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: n => (n.nodeValue && n.nodeValue.length && range.intersectsNode(n))
        ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    if (!nodes.length) return;

    const startC = range.startContainer, endC = range.endContainer;
    const startO = range.startOffset, endO = range.endOffset;
    const spans = [];
    nodes.forEach(n => {
      let node = n;
      // 首尾节点可能只选中了一部分，先把选中的那段切出来。
      // 必须先切尾再切首：同一节点时先切首会让 endOffset 失效。
      if (node === endC && endO < node.nodeValue.length) node.splitText(endO);
      if (node === startC && startO > 0) node = node.splitText(startO);
      if (!node.nodeValue.length) return;
      const span = document.createElement('span');
      Object.assign(span.style, props);
      node.parentNode.insertBefore(span, node);
      span.appendChild(node);
      spans.push(span);
    });

    // 保持选中，方便连着改第二个属性（比如先设字号再设颜色）
    if (spans.length) {
      const r = document.createRange();
      r.setStartBefore(spans[0]);
      r.setEndAfter(spans[spans.length - 1]);
      sel.removeAllRanges();
      sel.addRange(r);
      _savedRange = r;
    }
    el.focus();
    this.syncBody();
  },

  setFont(stack) {
    if (!stack) return;
    this.curFont = stack;
    this._styleRange({ fontFamily: stack });
  },

  setSize(px) {
    if (!px) return;
    this.curSize = String(px);
    this._styleRange({ fontSize: px + 'px' });
  },

  setColor(c) {
    this._styleRange({ color: c });
    this.colorPickerOpen = false;
  },

  setHilite(c) {
    // 'transparent' 用于取消高亮
    this._styleRange({ backgroundColor: c });
    this.hilitePickerOpen = false;
  },

  // ============ 段落样式（标题 / 引用 / 代码块）============

  // 块级内联样式表。**必须写内联** —— 邮件客户端对 h1/h2 的默认样式差异极大，
  // 很多还会整段重置（Outlook 会把标题压回正文大小）。
  // 跟表格边框那次是同一个道理：邮件里只有内联样式靠得住。
  _blockStyles(tag) {
    const M = {
      h1: { fontSize: '24px', fontWeight: '700', lineHeight: '1.35',
            margin: '14px 0 8px' },
      h2: { fontSize: '20px', fontWeight: '700', lineHeight: '1.4',
            margin: '12px 0 6px' },
      h3: { fontSize: '17px', fontWeight: '600', lineHeight: '1.45',
            margin: '10px 0 5px' },
      blockquote: { margin: '8px 0', padding: '4px 0 4px 12px',
                    borderLeft: '3px solid #d1d5db', color: '#4b5563' },
      pre: { fontFamily: 'Consolas, Monaco, monospace', fontSize: '13px',
             background: '#f6f8fa', padding: '10px 12px', borderRadius: '4px',
             border: '1px solid #e5e7eb', whiteSpace: 'pre-wrap',
             margin: '8px 0' },
      p: {},
    };
    return M[tag] || {};
  },

  // 这些属性在切换块型时要先清掉，否则"标题1 → 正文"会残留 24px 大字号，
  // 看起来像没切换成功。
  _BLOCK_STYLE_KEYS: ['fontSize', 'fontWeight', 'lineHeight', 'margin', 'padding',
                      'background', 'backgroundColor', 'borderLeft', 'border',
                      'borderRadius', 'whiteSpace', 'color', 'fontFamily'],

  // 找光标所在的块级元素。失焦时退回用保存的 range。
  _blockAtCaret() {
    const el = this.$refs.editor;
    if (!el) return null;
    const sel = window.getSelection();
    let n = (sel && sel.rangeCount) ? sel.getRangeAt(0).startContainer : null;
    if ((!n || !el.contains(n)) && _savedRange) n = _savedRange.startContainer;
    const BLOCKS = ['P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'PRE',
                    'BLOCKQUOTE', 'DIV', 'LI'];
    while (n && n !== el) {
      if (n.nodeType === 1 && BLOCKS.indexOf(n.tagName) >= 0) return n;
      n = n.parentNode;
    }
    return null;
  },

  // 选区跨了多个段落时，逐个块处理（只改一个块会让"全选设行距"看起来只生效一半）
  _eachSelectedBlock(fn) {
    const el = this.$refs.editor;
    if (!el) return;
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount) return;
    const range = sel.getRangeAt(0);
    const blocks = el.querySelectorAll('p, h1, h2, h3, h4, h5, h6, pre, blockquote, li, div');
    let hit = 0;
    blocks.forEach(b => {
      try {
        if (range.intersectsNode(b)) { fn(b); hit++; }
      } catch (e) { /* 忽略无法比较的节点 */ }
    });
    if (!hit) {
      const b = this._blockAtCaret();
      if (b) fn(b);
    }
  },

  setBlock(tag) {
    if (!tag) return;
    const el = this.$refs.editor;
    if (!el) return;
    this._restoreCaret();
    try {
      document.execCommand('formatBlock', false, '<' + tag + '>');
    } catch (e) { /* 个别浏览器对 pre 不支持，下面的内联样式仍会生效 */ }
    const blk = this._blockAtCaret();
    if (blk) {
      // 先清上一次的块级样式，再套新的
      this._BLOCK_STYLE_KEYS.forEach(k => { blk.style[k] = ''; });
      Object.assign(blk.style, this._blockStyles(tag));
    }
    this.curBlock = tag;
    el.focus();
    this.syncBody();
    this.refreshFmt();
  },

  setLineHeight(v) {
    if (!v) return;
    const el = this.$refs.editor;
    if (!el) return;
    this._restoreCaret();
    this._eachSelectedBlock(b => { b.style.lineHeight = v; });
    el.focus();
    this.syncBody();
  },

  // ============ 工具栏按钮高亮 ============
  // 不反映当前状态的话，用户不知道粗体到底开着没有，只能靠试。
  refreshFmt() {
    const cmds = ['bold', 'italic', 'underline', 'strikeThrough', 'subscript',
                  'superscript', 'insertUnorderedList', 'insertOrderedList',
                  'justifyLeft', 'justifyCenter', 'justifyRight', 'justifyFull'];
    const st = {};
    cmds.forEach(c => {
      try { st[c] = document.queryCommandState(c); } catch (e) { st[c] = false; }
    });
    this.fmt = st;
    // 回显当前段落样式，让下拉显示的是光标所在块的实际类型
    try {
      const v = (document.queryCommandValue('formatBlock') || '').toLowerCase();
      this.curBlock = v || 'p';
    } catch (e) { /* 取不到就不动 */ }
  },

  // ============ 表格：插入之后还得能改 ============

  // 找到光标所在的单元格。失焦时退回用保存的 range，
  // 否则点工具栏按钮的那一刻就已经找不到光标了。
  _cell() {
    const el = this.$refs.editor;
    if (!el) return null;
    const sel = window.getSelection();
    let n = (sel && sel.rangeCount) ? sel.getRangeAt(0).startContainer : null;
    if ((!n || !el.contains(n)) && _savedRange) n = _savedRange.startContainer;
    while (n && n !== el) {
      if (n.nodeType === 1 && (n.tagName === 'TD' || n.tagName === 'TH')) return n;
      n = n.parentNode;
    }
    return null;
  },

  tableAddRow(below = true) {
    const td = this._cell();
    if (!td) { Alpine.store('ui').notify('把光标放到表格里，再插入行', 'info'); return; }
    const tr = td.parentNode;
    const n = tr.children.length;
    const row = document.createElement('tr');
    for (let i = 0; i < n; i++) {
      const c = this._styleCell(document.createElement('td'), false);
      c.innerHTML = '&nbsp;';
      row.appendChild(c);
    }
    tr.parentNode.insertBefore(row, below ? tr.nextSibling : tr);
    // 光标跟到新行，连着加多行时不用每次重新点
    this._placeCaret(row.firstChild);
    this.syncBody();
  },

  tableDelRow() {
    const td = this._cell();
    if (!td) { Alpine.store('ui').notify('把光标放到表格里，再删除行', 'info'); return; }
    const tr = td.parentNode;
    const tbody = tr.parentNode;
    if (tbody.children.length <= 1) {
      // 最后一行删掉就只剩空表格骨架了，整表移除更符合预期
      const table = tbody.closest('table');
      if (table) table.remove();
    } else {
      // 先算好接手光标的行，再删。删完光标若无处可去，
      // 后续任何表格操作都会静默失效（实测过）。
      const idx = Array.prototype.indexOf.call(tr.children, td);
      const next = tr.nextElementSibling || tr.previousElementSibling;
      tr.remove();
      if (next) this._placeCaret(next.children[Math.min(idx, next.children.length - 1)]);
    }
    this.syncBody();
  },

  tableAddCol(right = true) {
    const td = this._cell();
    if (!td) { Alpine.store('ui').notify('把光标放到表格里，再插入列', 'info'); return; }
    const idx = Array.prototype.indexOf.call(td.parentNode.children, td);
    const table = td.closest('table');
    if (!table) return;
    let added = null;
    Array.prototype.forEach.call(table.rows, tr => {
      const ref = tr.children[idx];
      const isHead = ref && ref.tagName === 'TH';
      const c = this._styleCell(document.createElement(isHead ? 'th' : 'td'), isHead);
      c.innerHTML = '&nbsp;';
      if (ref) tr.insertBefore(c, right ? ref.nextSibling : ref);
      else tr.appendChild(c);
      if (tr === td.parentNode) added = c;
    });
    if (added) this._placeCaret(added);
    this.syncBody();
  },

  tableDelCol() {
    const td = this._cell();
    if (!td) { Alpine.store('ui').notify('把光标放到表格里，再删除列', 'info'); return; }
    const idx = Array.prototype.indexOf.call(td.parentNode.children, td);
    const table = td.closest('table');
    if (!table) return;
    if (table.rows[0] && table.rows[0].children.length <= 1) {
      table.remove();
    } else {
      const row = td.parentNode;
      Array.prototype.forEach.call(table.rows, tr => {
        if (tr.children[idx]) tr.children[idx].remove();
      });
      // 同理：删掉光标所在的列之后要把光标交给邻列
      if (row.children.length) {
        this._placeCaret(row.children[Math.min(idx, row.children.length - 1)]);
      }
    }
    this.syncBody();
  },

  // 引用块：execCommand 的 formatBlock 在各浏览器行为不一，自己插更可控，
  // 且能带上邮件里能正常显示的内联左边框
  insertQuote() {
    // 同样不走 insertHTML：实测 margin / padding / border-left 会被剥掉，
    // 只剩 color，发出去的邮件里就看不出这是引用。
    const q = document.createElement('blockquote');
    q.style.margin = '8px 0';
    q.style.padding = '4px 0 4px 12px';
    q.style.borderLeft = '3px solid #d1d5db';
    q.style.color = '#4b5563';
    q.textContent = '引用内容';
    const tail = document.createElement('p');
    tail.appendChild(document.createElement('br'));
    this._insertAtCaret([q, tail]);
    this._placeCaret(q);
    this.syncBody();
  },

  // 保存/恢复编辑器光标（工具栏交互会让编辑器失焦，插入表格前需恢复）
  saveCaret() {
    const el = this.$refs.editor;
    const sel = window.getSelection && window.getSelection();
    if (el && sel && sel.rangeCount) {
      const r = sel.getRangeAt(0);
      if (el.contains(r.commonAncestorContainer)) _savedRange = r;
    }
  },

  _restoreCaret() {
    const el = this.$refs.editor;
    if (!el) return;
    el.focus();
    const sel = window.getSelection();
    if (_savedRange && el.contains(_savedRange.commonAncestorContainer)) {
      sel.removeAllRanges();
      sel.addRange(_savedRange);
    }
  },

  // ============ 结构化内容的插入 ============
  // 刻意不用 execCommand('insertHTML')：它会剥掉简写 CSS 属性。
  // 实测 border:1px solid #999 只剩 border-color、padding 直接消失、
  // border-collapse 也没了。本应用里看不出来（app.css 兜着），
  // 但发出去的 HTML 就真没有边框了——只有收件人才会发现。
  // 直接建 DOM 节点 + el.style 赋值 + Range 插入，不经 sanitizer。
  _insertAtCaret(nodes) {
    const el = this.$refs.editor;
    if (!el) return;
    this._restoreCaret();
    const sel = window.getSelection();
    let range = null;
    if (sel && sel.rangeCount && el.contains(sel.getRangeAt(0).commonAncestorContainer)) {
      range = sel.getRangeAt(0);
    } else {
      // 光标不在编辑器里（比如刚从工具栏点过来）：追加到末尾
      range = document.createRange();
      range.selectNodeContents(el);
      range.collapse(false);
    }
    range.deleteContents();
    const frag = document.createDocumentFragment();
    nodes.forEach(n => frag.appendChild(n));
    range.insertNode(frag);
    this.syncBody();
  },

  // 单元格内联样式。用 .style 逐项赋值而不是 setAttribute 字符串，
  // 表意更清楚，且产出的都是完整属性（不会只剩 border-color）。
  _styleCell(c, head) {
    c.style.border = '1px solid #999999';
    c.style.padding = '4px 8px';
    c.style.minWidth = '48px';
    if (head) {
      c.style.backgroundColor = '#f3f4f6';
      c.style.fontWeight = '600';
    }
    return c;
  },

  // 把光标放进指定单元格。表格结构变更后必须调用：
  // 删掉光标所在的行/列会让光标孤立，后续操作全部静默失效。
  _placeCaret(cell) {
    const el = this.$refs.editor;
    if (!cell || !el) return;
    el.focus();
    const sel = window.getSelection();
    const r = document.createRange();
    r.selectNodeContents(cell);
    r.collapse(true);
    sel.removeAllRanges();
    sel.addRange(r);
    _savedRange = r;
  },

  // 插入表格：使用内联边框样式，保证在收件人邮箱与本应用中都能显示边框
  insertTable(rows, cols, header = false) {
    rows = Math.max(1, Math.min(30, parseInt(rows) || 3));
    cols = Math.max(1, Math.min(12, parseInt(cols) || 3));
    const table = document.createElement('table');
    table.setAttribute('border', '1');
    table.setAttribute('cellspacing', '0');
    table.style.borderCollapse = 'collapse';
    table.style.margin = '8px 0';
    const tbody = document.createElement('tbody');
    for (let r = 0; r < rows; r++) {
      const tr = document.createElement('tr');
      // 表头行用 th + 灰底：内联样式，收件人邮箱里也能看出是表头
      const head = header && r === 0;
      for (let c = 0; c < cols; c++) {
        const cell = this._styleCell(document.createElement(head ? 'th' : 'td'), head);
        cell.innerHTML = '&nbsp;';
        tr.appendChild(cell);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    // 表格后面留一个空段落，否则光标会卡在表格后面出不来
    const tail = document.createElement('p');
    tail.appendChild(document.createElement('br'));
    this._insertAtCaret([table, tail]);
    // 光标落到第一个单元格，插完就能直接打字
    const first = table.querySelector('th, td');
    if (first) this._placeCaret(first);
    this.syncBody();
    this.tablePickerOpen = false;
  },

  _escapeHtml(s) {
    return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  },

  // 纯文本 → HTML（已是 HTML 则原样返回）
  _toHtml(text) {
    if (!text) return '';
    if (/<[a-z][\s\S]*>/i.test(text)) return text;
    return this._escapeHtml(text).replace(/\n/g, '<br>');
  },

  // ============ 附件 ============

  async onFilesSelected(e) {
    const files = Array.from(e.target.files || []);
    e.target.value = '';  // 允许再次选择同一文件
    for (const file of files) {
      if (file.size > 25 * 1024 * 1024) {
        Alpine.store('ui').notify(`「${file.name}」超过 25MB，已跳过`, 'error');
        continue;
      }
      const item = { name: file.name, size: file.size, path: null, uploading: true };
      this.attachments.push(item);
      try {
        const b64 = await this._readFileB64(file);
        const res = await api.uploadAttachment(file.name, b64);
        if (res && res.ok) {
          item.path = res.path;
          item.uploading = false;
        } else {
          throw new Error((res && res.error) || '上传失败');
        }
      } catch (err) {
        this.attachments = this.attachments.filter(a => a !== item);
        Alpine.store('ui').notify(`附件「${file.name}」上传失败`, 'error');
      }
    }
  },

  _readFileB64(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => {
        const s = String(r.result || '');
        const comma = s.indexOf(',');
        resolve(comma >= 0 ? s.slice(comma + 1) : s);
      };
      r.onerror = reject;
      r.readAsDataURL(file);
    });
  },

  removeAttachment(i) {
    this.attachments.splice(i, 1);
  },

  humanSize(n) {
    if (n === null || n === undefined) return '';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  },

  // ============ 发送 ============

  async send() {
    if (!this.to.trim()) {
      Alpine.store('ui').notify('请填写收件人', 'error');
      return;
    }
    this.syncBody();
    if (this.attachments.some(a => a.uploading)) {
      Alpine.store('ui').notify('附件仍在上传，请稍候…', 'info');
      return;
    }
    const paths = this.attachments.filter(a => a.path).map(a => a.path);

    if (this.mode === 'forward') {
      const emailId = Alpine.store('mail').selectedId;
      if (!emailId) {
        Alpine.store('ui').notify('请先选择要转发的邮件', 'error');
        return;
      }
      this.sending = true;
      try {
        await Alpine.store('mail').forwardEmail(emailId, this.to, this.body,
          { is_html: true, attachments: paths });
        this.reset();
        Alpine.store('ui').composeOpen = false;
      } catch { /* handled in store */ }
      finally { this.sending = false; }
      return;
    }

    this.sending = true;
    try {
      const payload = {
        from: this.fromAccount,
        to: this.to,
        cc: this.cc,
        // 服务端只把 bcc 放进 RCPT TO，不写进邮件头——写了就等于泄露密送名单
        bcc: this.bcc,
        subject: this.subject,
        body: this.body,
        is_html: true,
        attachments: paths,
      };
      if (this.mode === 'reply' || this.mode === 'reply-all') {
        const emailId = Alpine.store('mail').selectedId;
        if (emailId) payload.reply_to_entry_id = emailId;
      }
      // 带上来源草稿 id，store 在发送成功后据此清理草稿箱
      if (this.draftId) payload.draft_id = this.draftId;
      await Alpine.store('mail').sendEmail(payload);
      this.reset();
      Alpine.store('ui').composeOpen = false;
    } catch { /* handled in store */ }
    finally { this.sending = false; }
  },

  // ============ 收件人补全 ============
  // 数据来自 store 里一次取回的通讯录，纯本地匹配，0 延迟。

  // 取"正在输入的那一段"。收件人框用 ; 分隔多个地址，
  // 不切段的话已填好的地址会被反复当成查询词。
  _acSeg(value) {
    const s = String(value || '');
    const cut = s.lastIndexOf(';');
    return { head: cut >= 0 ? s.slice(0, cut + 1) : '', tail: (cut >= 0 ? s.slice(cut + 1) : s).trim() };
  },

  acInput(field) {
    this.acField = field;
    const { tail } = this._acSeg(this[field]);
    if (tail.length < 1) { this.acList = []; return; }
    const q = tail.toLowerCase();
    const all = Alpine.store('mail').contacts || [];
    this.acList = all.filter(c =>
      (c.email || '').toLowerCase().includes(q) ||
      (c.name || '').toLowerCase().includes(q)).slice(0, 8);
    this.acIndex = 0;
  },

  acPick(c) {
    if (!c) return;
    const f = this.acField || 'to';
    const { head } = this._acSeg(this[f]);
    this[f] = head + (head ? ' ' : '') + c.email + '; ';
    this.acClose();
  },

  acClose() { this.acList = []; this.acIndex = 0; },

  // 键盘优先：填收件人是纯键盘动作，只能鼠标点会很难用
  acKey(e, field) {
    if (!this.acList.length) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); this.acIndex = (this.acIndex + 1) % this.acList.length; }
    else if (e.key === 'ArrowUp') { e.preventDefault(); this.acIndex = (this.acIndex - 1 + this.acList.length) % this.acList.length; }
    else if (e.key === 'Enter') { e.preventDefault(); this.acPick(this.acList[this.acIndex]); }
    else if (e.key === 'Escape') { this.acClose(); }
  },

  acLabel(c) {
    return (c.name ? c.name + ' <' + c.email + '>' : c.email)
      + (c.count ? '  · ' + c.count + ' 封' : '');
  },

  // 存草稿：不发送，只落库。Agent 写的草稿也走同一张表。
  async saveDraft() {
    this.syncBody();
    if (!this.subject.trim() && !this.body.trim()) {
      Alpine.store('ui').notify('主题与正文不能同时为空', 'error');
      return;
    }
    this.savingDraft = true;
    try {
      const id = await Alpine.store('mail').saveDraft({
        to: this.to, cc: this.cc, subject: this.subject,
        body: this.body, is_html: true,
      }, this.draftId);
      if (id) this.draftId = id;
    } finally {
      this.savingDraft = false;
    }
  },

  async generateDraft(tone) {
    const emailId = Alpine.store('mail').selectedId;
    if (!emailId) {
      Alpine.store('ui').notify('请先选择要回复的邮件', 'error');
      return;
    }
    // 流式内容会通过 init() 里的 draftContent watch 实时写入编辑器
    await Alpine.store('ai').generateDraft(emailId, tone);
  },

  close() {
    Alpine.store('ui').composeOpen = false;
  },

  reset() {
    this.to = '';
    this.cc = '';
    this.bcc = '';
    this.showBcc = false;
    this.subject = '';
    this.body = '';
    this.draftId = null;
    this.attachments = [];
    const el = this.$refs.editor;
    if (el) el.innerHTML = '';
  },
}); }

// ========== components/settingsPanel.js ==========
/**
 * 设置面板组件
 */
function settingsPanelComp() { return ({
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
}); }

// ========== components/commandPalette.js ==========
/**
 * 命令面板组件
 */
function commandPaletteComp() { return ({
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
}); }

// ========== components/searchBar.js ==========
/**
 * 搜索栏组件
 */
function searchBarComp() { return ({
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
}); }

// ========== components/topBar.js ==========
/**
 * 顶栏组件
 */
function topBarComp() { return ({
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
}); }


// ========== Alpine Registration ==========
function _registerAll() {
  if (window.__quillboxModuleLoaded) return true;  // 防止重复注册导致 store 实例被替换、响应式失联
  if (typeof Alpine === 'undefined') return false;
  
  try {
    Alpine.store('ui', uiStore());
    Alpine.store('mail', mailStore());
    Alpine.store('ai', aiStore());
    Alpine.store('settings', settingsStore());

    Alpine.data('emailList', emailListComp);
    Alpine.data('emailDetail', emailDetailComp);
    Alpine.data('sidebar', sidebarComp);
    Alpine.data('aiChat', aiChatComp);
    Alpine.data('compose', composeComp);
    Alpine.data('settingsPanel', settingsPanelComp);
    Alpine.data('commandPalette', commandPaletteComp);
    Alpine.data('searchBar', searchBarComp);
    Alpine.data('topBar', topBarComp);

    window.__quillboxModuleLoaded = true;
    console.log('[Quillbox] Bundle: stores and components registered.');
    return true;
  } catch(e) {
    console.error('[Quillbox] Registration error:', e);
    return false;
  }
}

// Method 1: alpine:init event
document.addEventListener('alpine:init', function() {
  _registerAll();
});

// Method 2: poll for Alpine (in case event already fired)
(function() {
  if (_registerAll()) return;
  var attempts = 0;
  var timer = setInterval(function() {
    attempts++;
    if (_registerAll()) {
      clearInterval(timer);
    } else if (attempts > 600) {
      clearInterval(timer);
      console.error('[Quillbox] Alpine.js not found after 3s');
    }
  }, 5);
})();

// Keyboard shortcuts
document.addEventListener('DOMContentLoaded', function() {
  setTimeout(function() {
    if (typeof Alpine !== 'undefined') {
      var getStore = function(name) { return Alpine.store(name); };
      // 新界面 /app2 自带键盘处理（E=归档等，与旧版 E=删除冲突），设置 __QUILLBOX_V2 时不注册旧快捷键
      if (!window.__QUILLBOX_V2) {
        registerDefaultShortcuts(getStore);
        initShortcuts();
      }
    }
  }, 300);
});

})(); // end IIFE
