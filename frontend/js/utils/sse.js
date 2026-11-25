/**
 * SSE (Server-Sent Events) 流式读取工具
 * 支持 GET 和 POST 请求的 SSE 流
 */

const BASE_URL = '';  // 前端和API由同一个server提供，使用相对路径

/**
 * 创建 GET SSE 连接
 * @param {string} path - API路径
 * @param {object} options - 配置选项
 * @param {function} options.onMessage - 收到消息回调
 * @param {function} options.onError - 错误回调
 * @param {function} options.onDone - 完成回调
 * @returns {EventSource} EventSource实例（可用于关闭连接）
 */
export function createSSE(path, { onMessage, onError, onDone }) {
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
export function createPostSSE(path, body, { onChunk, onError, onDone }) {
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
export function streamSummary(emailId, callbacks) {
  return createSSE(`/api/stream/summary/${emailId}`, callbacks);
}

/**
 * 流式AI聊天（针对特定邮件）
 * @param {string} message - 用户消息
 * @param {string} emailId - 邮件ID
 * @param {object} callbacks - 回调函数
 * @returns {AbortController}
 */
export function streamChat(message, emailId, history, callbacks) {
  return createPostSSE('/api/stream/chat', { message, email_id: emailId, history: history || [] }, callbacks);
}

/**
 * 全局AI聊天
 * @param {string} message - 用户消息
 * @param {object} callbacks - 回调函数
 * @returns {AbortController}
 */
export function streamGlobalChat(message, history, callbacks) {
  return createPostSSE('/api/stream/global-chat', { message, history: history || [] }, callbacks);
}

/**
 * 流式生成草稿
 * @param {string} emailId - 邮件ID
 * @param {string} tone - 语气
 * @param {object} callbacks - 回调函数
 * @returns {AbortController}
 */
export function streamDraft(emailId, tone, callbacks) {
  return createPostSSE('/api/stream/draft', { email_id: emailId, tone }, callbacks);
}
