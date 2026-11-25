/**
 * AI 状态管理
 * 管理AI摘要、聊天对话、草稿生成
 */
import { streamSummary, streamChat, streamGlobalChat, streamDraft } from '../utils/sse.js';

export default function aiStore() {
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
