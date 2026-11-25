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
export function sanitizeHTML(html, opts) {
  return _sanRun(html, opts).html;
}

/** 同一输入下被拦截的外部资源数（图片 + CSS url()） */
export function sanitizeRemoteCount(html, opts) {
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
export function sanitizeEmailDoc(html, opts) {
  return _frameRun(html, opts).html;
}

/** sanitizeEmailDoc 同一输入下被拦截的外部资源数（图片、背景图、样式表里的 url()、外部字体） */
export function sanitizeEmailDocRemoteCount(html, opts) {
  return _frameRun(html, opts).remote;
}

// ---- 外部图片的信任域名（按发件人域名，存 localStorage） ----
const _SAN_TRUST_KEY = 'quillbox.trustedImageDomains';

function _sanDomainOf(addr) {
  const s = String(addr || '').trim().toLowerCase();
  const i = s.lastIndexOf('@');
  return i >= 0 ? s.slice(i + 1) : '';
}

export function imgTrustedDomains() {
  try {
    const v = JSON.parse(localStorage.getItem(_SAN_TRUST_KEY) || '[]');
    return Array.isArray(v) ? v : [];
  } catch (e) { return []; }
}

export function senderDomainOf(addr) { return _sanDomainOf(addr); }

export function isImgTrusted(senderAddr) {
  const d = _sanDomainOf(senderAddr);
  return !!d && imgTrustedDomains().indexOf(d) >= 0;
}

export function trustImgDomain(senderAddr) {
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
