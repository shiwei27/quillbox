import logging
import requests
import json

log = logging.getLogger(__name__)

# 两条 _generate 在后端不可达/超时/非 2xx 时并不抛异常，而是把错误包成字符串返回
# （流式版本则 yield 同样的字符串）。这是既有约定，改成抛异常会波及全部调用方，
# 所以这里把它**显式化**：任何拿到 _generate 结果的地方都可以用
# generation_failed() 判断这到底是模型输出还是故障。
AI_ERROR_PREFIXES = ("[API 异常", "[Ollama 异常")


def generation_failed(raw) -> bool:
    """判断 _generate / _stream 的产出是否其实是一次失败。"""
    if not isinstance(raw, str):
        return False
    s = raw.strip()
    return s.startswith(AI_ERROR_PREFIXES) or s.startswith('{"error"')


class AIEngine:
    """AI 认知层 — 支持 Ollama 原生接口 和 OpenAI 兼容接口双后端"""

    # ── 内置强化 Prompt（用户未自定义时使用）───────────────────────────
    DEFAULT_SUMMARY_PROMPT = """你是资深邮件分析助手。请深入分析下面这封邮件，输出结构化摘要，帮助收件人快速抓住要点并判断如何处理。

【输出格式】禁止使用 Markdown 记号（不要出现 #、*、` 等符号）。用下列中文小标题分段，每个小标题单独成行、各段之间空一行；列举项用“· ”开头：
【核心】一句话说清这封邮件到底想说什么、要什么。
【关键信息】提取具体的人、时间/截止日期、金额/数字、地点、涉及的项目或系统等硬信息（有则分条列出，没有不编造）。
【需要我做什么】明确列出需要收件人采取的行动或决策；若仅为知会，则写“无需行动，仅知会”。
【历史提醒】结合下方“同类历史邮件”判断这封邮件是否属于周期性/重复出现的类型：若是，提醒其规律（多久一次、通常关注什么、可与哪一期对比）；若无明显规律，写“暂无同类历史规律”。
【背景/影响】用一两句补充上下文或潜在影响，避免流于表面。

同类历史邮件（同一发件人近期往来，供“历史提醒”参考）：
{history}

主题：{subject}
正文：{body}"""

    DEFAULT_DRAFT_PROMPT = """你是专业的邮件撰写助手。请基于下面的原始邮件起草一封得体的回复。要求：
- 语气：{tone}
- 【回复语言必须与原邮件正文的主要语言一致】：原文是英文就用英文回复，中文就用中文回复，其他语种同理，不要擅自改变语言。
- 结构完整：含合适的称呼、正文、结尾问候与署名占位（如“此致 敬礼”或“Best regards,”）。
- 针对原文的具体问题/请求逐一回应；需要用户补充处用 [占位] 标注。
- 只输出回复正文本身，不加解释、不加“以下是回复”之类前言。

原始邮件主题：{subject}
原始邮件正文：{body}"""

    DEFAULT_CHAT_SYSTEM_GLOBAL = """你是 Quillbox 智能邮件助手，服务于一位需要高效处理大量邮件的专业人士。你能基于检索到的邮件内容回答问题、做跨邮件的归纳分析、梳理待办与时间线。要求：
- 优先依据提供的邮件上下文作答；上下文不足时明确说明，绝不编造事实或杜撰邮件内容。
- 有条理、抓重点：能下结论就先给结论，再给依据。
- 结合完整对话历史理解用户的追问，保持上下文连贯。
- 除非用户另有要求，用简洁专业的中文回答。"""

    DEFAULT_CHAT_SYSTEM_EMAIL = """你是 Quillbox 智能邮件助手。用户正在阅读一封特定邮件并就这封邮件向你提问。要求：
- 严格基于【当前邮件】的内容作答，不要引用或混入其他邮件的内容。
- 可做解读、总结、要点提取、回应思路草拟等；信息不足时如实说明。
- 结合完整对话历史理解用户的追问，保持连贯。
- 用简洁专业的中文（若邮件为外文且用户要求，可用相应语言）。"""

    TONE_MAP = {
        "professional": "专业、稳重", "friendly": "友好、亲和",
        "formal": "正式、礼貌", "brief": "简洁、干练", "casual": "轻松、口语化",
        "正式": "正式、礼貌", "友好": "友好、亲和", "简洁": "简洁、干练", "专业": "专业、稳重",
    }

    def __init__(self,
                 base_url: str = "http://127.0.0.1:11434",
                 model: str = "qwen2.5:7b",
                 provider: str = "ollama",
                 api_base_url: str = "",
                 api_key: str = "",
                 api_model: str = "",
                 api_timeout: int = 120,
                 prompts: dict | None = None,
                 signature: str = ""):
        self.provider     = provider          # "ollama" | "api"
        # Ollama 参数
        self.ollama_url   = base_url.rstrip("/")
        self.ollama_model = model
        # API 参数
        self.api_url      = (api_base_url or base_url).rstrip("/")
        self.api_key      = api_key
        self.api_model    = api_model or model
        self.api_timeout  = api_timeout
        # 用户自定义 prompt 覆盖（留空则用内置强化版）
        self.prompts      = prompts or {}
        self.signature    = signature or ""

    def _custom(self, key: str) -> str:
        v = self.prompts.get(key)
        return v.strip() if isinstance(v, str) else ""

    @staticmethod
    def _fill(tpl: str, **kw) -> str:
        for k, val in kw.items():
            tpl = tpl.replace("{" + k + "}", str(val))
        return tpl

    # ── 邮件规则引擎（Outlook/Foxmail 式：条件 + AI条件 + 动作）──────────

    def _ai_classify(self, subject, body, criteria) -> bool:
        """用 LLM 判断邮件是否符合某自然语言分类标准（返回 True/False）。"""
        criteria = (criteria or "").strip()
        if not criteria:
            return False
        prompt = (
            "你是邮件分类器。判断下面这封邮件是否符合给定的分类标准。"
            "只回答一个字：符合回答“是”，不符合回答“否”，不要任何解释。\n\n"
            f"分类标准：{criteria}\n\n"
            f"邮件主题：{subject or ''}\n"
            f"邮件正文：{(body or '')[:1500]}\n\n"
            "是否符合（是/否）："
        )
        try:
            raw = self._generate(prompt, timeout=30) or ""
            if generation_failed(raw):
                # 故障返回 False 会让"AI 匹配"类规则静默不生效——
                # 表现是规则"有时候不管用"，最难排查的那种。至少要留痕。
                log.warning("_ai_classify 生成失败，本次判定为不匹配: %s", raw[:160])
                return False
            r = raw.strip().lower()
            return r.startswith("是") or r.startswith("y") or "是" in r[:3] or "yes" in r[:5]
        except Exception as e:
            log.warning("_ai_classify 异常，本次判定为不匹配: %s", e)
            return False

    @staticmethod
    def _email_match_fields(email: dict) -> dict:
        rec = email.get("recipients", "")
        if isinstance(rec, (list, tuple)):
            rec = " ".join(str(x) for x in rec)
        subject = str(email.get("subject", "") or "")
        body = str(email.get("body", "") or "")
        return {
            "subject": subject,
            "body": body,
            "subject_or_body": subject + " " + body,
            "from": str(email.get("sender", "") or "") + " " + str(email.get("sender_email", "") or ""),
            "to": str(rec or ""),
            "account": str(email.get("account", "") or ""),
            "folder": str(email.get("folder", "") or ""),
            "has_attachment": email.get("has_attachment"),
        }

    @staticmethod
    def _rule_conditions(rule: dict) -> list:
        """返回规则的条件列表；兼容旧格式 match:{field:[kw,...]}。"""
        if isinstance(rule.get("conditions"), list):
            return rule["conditions"]
        m = rule.get("match") or {}
        fmap = {"subject": "subject", "sender": "from", "recipient": "to", "body": "body"}
        conds = []
        for k, dst in fmap.items():
            for kw in (m.get(k) or []):
                if kw:
                    conds.append({"field": dst, "op": "contains", "value": kw})
        return conds

    @staticmethod
    def _eval_condition(c: dict, fields: dict):
        """返回 True/False；条件为空(无字段或空值)时返回 None 表示忽略。"""
        field = c.get("field", "")
        op = (c.get("op") or "contains")
        val = str(c.get("value", "") or "")
        if field == "has_attachment":
            has = bool(fields.get("has_attachment"))
            want = val.strip().lower() in ("1", "true", "yes", "是", "有", "y")
            return has == want
        if not field or not val.strip():
            return None
        import re as _re
        fv_full = str(fields.get(field, "") or "").lower()
        v = val.lower()

        # `from` 是「显示名 + 邮箱」拼串，`to` 是 recipients 的字符串化；
        # 用户在规则里填的是纯地址，直接拿整串比 is/starts_with/ends_with
        # 永远不相等（同一类系统通知实际一封都命中不了）。因此把整串与串中
        # 出现的每个邮箱地址都作为候选值参与比较。
        cands = [fv_full]
        for _a in _re.findall(r"[\w.+%-]+@[\w.-]+\.[a-z]{2,}", fv_full):
            if _a not in cands:
                cands.append(_a)

        if op == "pattern_is":
            # 同类主题：去掉 Re/Fw 前缀、数字串归一成 #，再整串比较（「巡检报告 - 2026-10-09」≈「巡检报告 - 2026-10-10」）
            return AIEngine.subject_pattern(fields.get(field, "")) == AIEngine.subject_pattern(val)

        def _one(fv: str):
            if op == "contains":     return v in fv
            if op == "not_contains": return v not in fv
            if op == "is":           return fv.strip() == v.strip()
            if op == "not_is":       return fv.strip() != v.strip()
            if op == "starts_with":  return fv.strip().startswith(v.strip())
            if op == "ends_with":    return fv.strip().endswith(v.strip())
            return v in fv

        # 否定型算符必须对所有候选都成立；否则「不等于」会被任一候选轻易满足
        if op in ("not_contains", "not_is"):
            return all(_one(c) for c in cands)
        return any(_one(c) for c in cands)

    @staticmethod
    def _rule_prompt(rule, kind: str) -> str:
        """取规则的专用 prompt：actions.{kind}_prompt 优先，回退旧 rule.{kind}。"""
        if not rule:
            return ""
        acts = rule.get("actions") or {}
        return (acts.get(kind + "_prompt") or rule.get(kind) or "").strip()

    NO_ACTION_TAG = "无需处理"

    @staticmethod
    def subject_pattern(s) -> str:
        """主题模板：用于「同类邮件」判定。去掉回复/转发前缀，连续数字归一为 #，压缩空白，小写。"""
        import re as _re
        s = str(s or "").strip()
        s = _re.sub(r'^((re|fw|fwd|回复|答复|回覆|转发)\s*[:：]\s*)+', '', s, flags=_re.I)
        s = _re.sub(r'\d+', '#', s)
        return " ".join(s.split()).lower()

    def match_no_action(self, email) -> dict | None:
        """「无需处理」规则独立判定，不参与首个命中链——否则会把同一发件人的移动/分类规则挡掉。"""
        rules = self.prompts.get("rules") if isinstance(self.prompts, dict) else None
        if not rules or not isinstance(email, dict):
            return None
        fields = self._email_match_fields(email)
        for rule in rules:
            if not isinstance(rule, dict) or rule.get("enabled") is False:
                continue
            if not (rule.get("actions") or {}).get("no_action"):
                continue
            res = [r for r in (self._eval_condition(c, fields) for c in self._rule_conditions(rule)) if r is not None]
            if not res:
                continue
            if (any(res) if (rule.get("match_mode") or "all").lower() == "any" else all(res)):
                return rule
        return None

    def _match_rule(self, email):
        """按 prompts.rules 逐条匹配，返回首个命中的规则（含动作），否则 None。
        条件：conditions=[{field,op,value}]（字段内部按 op；条件之间 match_mode=all/any）
             + ai_match（自然语言，LLM 判断，作为一条附加条件）。
        空条件(空值)自动忽略；无任何有效条件且无 AI 判断的规则不命中。"""
        rules = self.prompts.get("rules") if isinstance(self.prompts, dict) else None
        if not rules or not isinstance(email, dict):
            return None
        fields = self._email_match_fields(email)
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            if rule.get("enabled") is False:
                continue
            if (rule.get("actions") or {}).get("no_action"):
                continue   # 由 match_no_action 单独处理
            mode = (rule.get("match_mode") or "all").lower()
            ai_criteria = (rule.get("ai_match") or "").strip()
            results = []
            for c in self._rule_conditions(rule):
                r = self._eval_condition(c, fields)
                if r is None:
                    continue
                results.append(r)
            has_cond = len(results) > 0
            has_ai = bool(ai_criteria)
            if not has_cond and not has_ai:
                continue
            if mode == "any":
                if has_cond and any(results):
                    return rule
                if has_ai and self._ai_classify(fields["subject"], fields["body"], ai_criteria):
                    return rule
                continue
            else:  # all（AND）
                if has_cond and not all(results):
                    continue
                if has_ai and not self._ai_classify(fields["subject"], fields["body"], ai_criteria):
                    continue
                return rule
        return None

    # 规则生成/评估用的允许值
    RULE_FIELDS = {"subject", "from", "to", "body", "subject_or_body", "account", "folder", "has_attachment"}
    RULE_OPS = {"contains", "not_contains", "is", "not_is", "starts_with", "ends_with", "pattern_is"}
    RULE_CATS = {"", "🚨重要行动", "📢内部通知", "📰订阅"}

    def _sanitize_rule(self, rule: dict) -> dict:
        """校验并归一化（由 AI 生成的）规则，剔除非法字段/运算符/分类。"""
        rule = rule if isinstance(rule, dict) else {}
        conds = []
        for c in (rule.get("conditions") or []):
            if not isinstance(c, dict):
                continue
            f = c.get("field", "")
            if f not in self.RULE_FIELDS:
                continue
            op = c.get("op", "contains")
            if op not in self.RULE_OPS:
                op = "contains"
            conds.append({"field": f, "op": op, "value": str(c.get("value", "") or "")})
        acts = rule.get("actions") or {}
        cat = acts.get("category", "")
        if cat not in self.RULE_CATS:
            cat = ""
        return {
            "name": (str(rule.get("name", "") or "新规则")[:40]),
            "enabled": True,
            "match_mode": "any" if str(rule.get("match_mode", "all")).lower() == "any" else "all",
            "conditions": conds,
            "ai_match": str(rule.get("ai_match", "") or "").strip(),
            "actions": {
                "category": cat,
                "star": bool(acts.get("star")),
                "mark_read": bool(acts.get("mark_read")),
                "skip_ai": bool(acts.get("skip_ai")),
                "summary_prompt": str(acts.get("summary_prompt", "") or ""),
                "draft_prompt": str(acts.get("draft_prompt", "") or ""),
            },
        }

    def generate_rule(self, description: str) -> dict:
        """功能1：自然语言描述 → 结构化邮件规则。"""
        description = (description or "").strip()
        if not description:
            return {"ok": False, "error": "描述为空"}
        prompt = (
            (self._custom("rule_gen") or "你是邮件规则生成器。根据用户的自然语言描述，输出一条严格 JSON 邮件规则，不要任何解释、不要 markdown、只输出 JSON。") + "\n"
            "可用字段 field：subject(主题) from(发件人) to(收件人) body(正文) "
            "subject_or_body(主题或正文) account(账户) folder(文件夹) has_attachment(有附件)。\n"
            "可用运算符 op：contains not_contains is not_is starts_with ends_with。\n"
            "match_mode：all(全部满足) 或 any(任一满足)。\n"
            'actions.category 只能是 "" 或 "🚨重要行动" 或 "📢内部通知" 或 "📰订阅"。\n'
            "若某判断难以用关键词表达(需要语义理解)，放到 ai_match 字段（自然语言）；能用关键词就不要用 ai_match。\n"
            "has_attachment 的 value 用 是 或 否。\n"
            "输出结构：\n"
            '{"name":"简短规则名","match_mode":"all","conditions":[{"field":"from","op":"contains","value":"noc@"}],'
            '"ai_match":"","actions":{"category":"🚨重要行动","star":true,"mark_read":false,"skip_ai":false,"summary_prompt":"","draft_prompt":""}}\n\n'
            f"用户描述：{description}\nJSON："
        )
        raw = self._generate(prompt, fmt="json" if self.provider == "ollama" else None, timeout=60)
        try:
            cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            rule = json.loads(cleaned)
        except Exception as e:
            return {"ok": False, "error": f"AI 未返回合法 JSON：{str(e)[:80]}"}
        return {"ok": True, "rule": self._sanitize_rule(rule)}

    def evaluate_rule(self, rule: dict) -> str:
        """功能2：评估一条（手动或生成的）规则的质量并给建议。"""
        try:
            rj = json.dumps(rule, ensure_ascii=False, indent=2)
        except Exception:
            rj = str(rule)
        prompt = (
            "你是邮件规则审查专家。请分析下面这条邮件规则，用简洁中文分点回答：\n"
            "1.【会匹配什么】举 1-2 个会被这条规则命中的邮件例子。\n"
            "2.【潜在问题】是否过于宽泛(命中过多)或过于狭窄(几乎不命中)？字段/运算符/AND-OR/动作是否合理？"
            "有无冲突或风险(如误删、误分类、跳过重要邮件的AI摘要)？\n"
            "3.【改进建议】如有，给出更合适的条件或设置。\n"
            "不要复述 JSON，直接给分析。\n\n"
            f"规则：\n{rj}"
        )
        return self._generate(prompt, timeout=60)

    # ── 内部：路由到对应后端 ───────────────────────────────────────────

    def _generate(self, prompt: str, fmt: str | None = None, timeout: int = 60) -> str:
        if self.provider == "api":
            return self._api_generate(prompt, timeout=timeout)
        return self._ollama_generate(prompt, fmt=fmt, timeout=timeout)

    def _stream(self, prompt: str, timeout: int = 120):
        if self.provider == "api":
            yield from self._api_stream(prompt, timeout=timeout)
        else:
            yield from self._ollama_stream(prompt, timeout=timeout)

    # ── Ollama 后端 ───────────────────────────────────────────────────

    def _ollama_generate(self, prompt: str, fmt: str | None = None, timeout: int = 90) -> str:
        payload = {"model": self.ollama_model, "prompt": prompt, "stream": False}
        if fmt:
            payload["format"] = fmt
        try:
            r = requests.post(f"{self.ollama_url}/api/generate", json=payload, timeout=timeout)
            r.raise_for_status()
            return r.json().get("response", "")
        except Exception as e:
            # 此前这里静默返回错误字符串，调用方无从区分故障与模型输出，
            # 而引擎内又没有任何日志——AI 挂掉可以做到零痕迹。
            log.warning("Ollama 生成失败 model=%s: %s", self.ollama_model, e)
            return f'{{"error": "{e}"}}' if fmt == "json" else f"[Ollama 异常: {e}]"

    def _ollama_stream(self, prompt: str, timeout: int = 120):
        payload = {"model": self.ollama_model, "prompt": prompt, "stream": True}
        try:
            with requests.post(f"{self.ollama_url}/api/generate", json=payload,
                               timeout=timeout, stream=True) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if line:
                        data = json.loads(line)
                        if not data.get("done"):
                            chunk = data.get("response", "")
                            if chunk:
                                yield chunk
        except Exception as e:
            yield f"[Ollama 异常: {e}]"

    # ── OpenAI 兼容后端 ───────────────────────────────────────────────

    def _api_headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _api_generate(self, prompt: str, timeout: int = 60) -> str:
        payload = {
            "model": self.api_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        try:
            r = requests.post(f"{self.api_url}/chat/completions",
                              headers=self._api_headers(), json=payload, timeout=timeout)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            log.warning("API 生成失败 model=%s url=%s: %s",
                        self.api_model, self.api_url, e)
            return f"[API 异常: {e}]"

    def _api_stream(self, prompt: str, timeout: int = 120):
        payload = {
            "model": self.api_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }
        try:
            with requests.post(f"{self.api_url}/chat/completions",
                               headers=self._api_headers(), json=payload,
                               timeout=timeout, stream=True) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    text = line.decode("utf-8") if isinstance(line, bytes) else line
                    if text.startswith("data: "):
                        text = text[6:]
                    if text == "[DONE]":
                        break
                    try:
                        data = json.loads(text)
                        chunk = data["choices"][0]["delta"].get("content", "")
                        if chunk:
                            yield chunk
                    except Exception:
                        continue
        except Exception as e:
            yield f"[API 异常: {e}]"

    # ── 多轮消息流式（用于带上下文的聊天）──────────────────────────────

    def _stream_messages(self, messages: list[dict], timeout: int = 120):
        if self.provider == "api":
            yield from self._api_stream_messages(messages, timeout=timeout)
        else:
            yield from self._ollama_stream_messages(messages, timeout=timeout)

    def _api_stream_messages(self, messages, timeout=120):
        payload = {"model": self.api_model, "messages": messages, "stream": True}
        try:
            with requests.post(f"{self.api_url}/chat/completions",
                               headers=self._api_headers(), json=payload,
                               timeout=timeout, stream=True) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    text = line.decode("utf-8") if isinstance(line, bytes) else line
                    if text.startswith("data: "):
                        text = text[6:]
                    if text == "[DONE]":
                        break
                    try:
                        data = json.loads(text)
                        chunk = data["choices"][0]["delta"].get("content", "")
                        if chunk:
                            yield chunk
                    except Exception:
                        continue
        except Exception as e:
            yield f"[API 异常: {e}]"

    def _ollama_stream_messages(self, messages, timeout=120):
        payload = {"model": self.ollama_model, "messages": messages, "stream": True}
        try:
            with requests.post(f"{self.ollama_url}/api/chat", json=payload,
                               timeout=timeout, stream=True) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if line:
                        data = json.loads(line)
                        if not data.get("done"):
                            chunk = (data.get("message") or {}).get("content", "")
                            if chunk:
                                yield chunk
        except Exception as e:
            yield f"[Ollama 异常: {e}]"

    # ── 对外接口（与原版完全相同，上层代码不需改动）─────────────────────

    def stream_summary(self, subject: str, body: str, history_hint: str = "", email=None):
        # 截断正文防止 prompt 过长导致超时（强化摘要保留更多上下文）
        truncated_body = body[:2500] if body else ""
        rule = self._match_rule(email if isinstance(email, dict) else {"subject": subject, "body": body})
        tpl = (self._rule_prompt(rule, "summary") or self._custom("summary")
               or self.DEFAULT_SUMMARY_PROMPT)
        prompt = self._fill(tpl, subject=subject, body=truncated_body,
                            history=(history_hint or "（无同类历史邮件记录）"))
        yield from self._stream(prompt, timeout=120)

    def stream_chat(self, user_message: str,
                    search_results: list[dict] | None = None,
                    current_email: dict | None = None,
                    history: list[dict] | None = None):
        # 系统提示（可被用户自定义 prompt 覆盖）
        if current_email:
            system = self._custom("chat") or self.DEFAULT_CHAT_SYSTEM_EMAIL
        else:
            system = self._custom("chat") or self.DEFAULT_CHAT_SYSTEM_GLOBAL

        # 邮件上下文
        parts = []
        if current_email:
            parts.append(
                f"【当前邮件（用户正在阅读）】\n"
                f"主题：{current_email.get('subject','')}\n"
                f"发件人：{current_email.get('sender','')} <{current_email.get('sender_email','')}>\n"
                f"时间：{current_email.get('time','')}\n"
                f"正文：{current_email.get('body','')[:2000]}"
            )
        if search_results:
            items = []
            for i, r in enumerate(search_results[:8], 1):
                items.append(f"{i}. [{r.get('time','')[:10]}] {r.get('subject','')} | {r.get('sender','')} | {r.get('summary','')[:60]}")
            parts.append("【检索到的相关邮件（供参考，可能含无关项）】\n" + "\n".join(items))
        context = "\n\n".join(parts)

        # 组装 messages：system(+邮件上下文) → 历史多轮 → 当前问题
        sys_content = system if not context else f"{system}\n\n{context}"
        messages = [{"role": "system", "content": sys_content}]
        for h in (history or [])[-10:]:
            role = h.get("role")
            content = (h.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_message})

        yield from self._stream_messages(messages, timeout=120)

    def stream_draft(self, subject: str, body: str, tone: str = "正式", email=None):
        tone_desc = self.TONE_MAP.get(tone, tone or "正式、礼貌")
        rule = self._match_rule(email if isinstance(email, dict) else {"subject": subject, "body": body})
        tpl = (self._rule_prompt(rule, "draft") or self._custom("draft")
               or self.DEFAULT_DRAFT_PROMPT)
        who = self._reply_identity(email)
        prompt = self._fill(tpl, subject=subject, body=(body[:2500] if body else ""), tone=tone_desc,
                            me=who["me"], sender=who["sender"], sender_email=who["sender_email"])
        # 视角块对内置 / 自定义 / 规则 prompt 一律追加。改前只给主题+正文，模型不知道谁是「我」，
        # 把原文「张工，各位好」照抄成回信抬头，变成替发件人回复邮箱主人。
        prompt += self._reply_perspective_block(who)
        if getattr(self, "signature", ""):
            prompt += ("\n\n【签名】请在回复正文结尾另起一行，原样附上以下签名"
                       "（不要改写、不要翻译、不要加引号）：\n" + self.signature)
        # 只靠提示词不够：实测正文视角已改对，抬头仍照抄「张工，您好」，所以首行再做一次确定性兜底
        yield from self._guard_salutation(self._stream(prompt, timeout=90), who)

    @staticmethod
    def _clean_display_name(name) -> str:
        """显示名归一：「张三27/EXAMPLE」→「张三」；只有地址（邮件头没有显示名）时返回空。"""
        import re as _re
        s = str(name or "").strip().strip("\"'")
        if not s or "@" in s:
            return ""
        return _re.sub(r"[\d\s]*(/.*)?$", "", s).strip()

    @staticmethod
    def _cn_name(name: str) -> bool:
        return 2 <= len(name) <= 4 and all("\u4e00" <= c <= "\u9fff" for c in name)

    @staticmethod
    def _reply_identity(email) -> dict:
        """起草回信的双方：me = 邮箱主人（代其执笔），sender = 原邮件发件人（回信对象）。"""
        e = email if isinstance(email, dict) else {}
        me, role = "", ""
        try:
            from core import insight_v2 as _iv2
            r = _iv2.load_reader()
            me, role = str(r.get("name") or "").strip(), str(r.get("role") or "").strip()
        except Exception as ex:
            log.warning("draft: 读取 reader 身份失败，退回收件人显示名: %s", ex)
        acct = str(e.get("account") or "").strip()
        if not me and acct:
            rc = e.get("recipients")
            if isinstance(rc, str):
                try:
                    rc = json.loads(rc)
                except Exception:
                    rc = []
            for x in rc if isinstance(rc, list) else []:
                if isinstance(x, dict) and str(x.get("email") or "").strip().lower() == acct.lower():
                    me = AIEngine._clean_display_name(x.get("name"))
                    break
        sender = AIEngine._clean_display_name(e.get("sender"))
        hon = (sender[0] + "工") if AIEngine._cn_name(sender) else sender
        return {"me": me, "role": role, "account": acct, "sender": sender, "sender_hon": hon,
                "sender_email": str(e.get("sender_email") or "").strip()}

    def _reply_perspective_block(self, who: dict) -> str:
        me = who.get("me") or ""
        me_ref = me or "邮箱主人"
        intro = "你是在替「%s」执笔回信" % me if me else "你是在替这封邮件的收件人（邮箱主人）执笔回信"
        if who.get("role"):
            intro += "（%s）" % who["role"]
        if who.get("sender"):
            to = "%s <%s>" % (who["sender"], who.get("sender_email") or "")
            name_hint = "抬头称呼对方，例如「%s，您好：」" % who["sender_hon"]
        else:
            to = "<%s>" % (who.get("sender_email") or "未知地址")
            name_hint = ("邮件头没有对方姓名，请从原文末尾的署名识别对方是谁并称呼对方"
                         "（如署名「张三」可称「张工」或「张三」）；识别不出时只写「您好」")
        # 中文姓名给一个具体的称呼样例，模型对「王工」这种简称更敏感
        hon = ("「%s工」「%s总」" % (me[0], me[0])) if self._cn_name(me) else ""
        lines = [
            "",
            "",
            "【身份与视角（必须遵守，优先于上文）】",
            "- %s。回信的发件人就是%s，用%s的第一人称（我/我们）写。" % (intro, me_ref, me_ref),
            "- 回信对象是原邮件的发件人：%s。%s。" % (to, name_hint),
            "- 原文里对%s的称呼%s是对方写给%s的，不要照抄成回信抬头；回信中不得称呼%s本人，"
            "也不要写「感谢您的邮件」「关于您提出的需求」这类把%s当成来信人的话。"
            % (me_ref, (hon + "等") if hon else "", me_ref, me_ref, me_ref),
            "- 对方请%s做的事，以%s的口吻答复（如“收到，我来跟进/协调，有结果同步你”），"
            "不要写成对方在答复%s，也不要大段复述对方自己的需求。" % (me_ref, me_ref, me_ref),
        ]
        if me and not getattr(self, "signature", ""):
            lines.append("- 结尾署名写「%s」，不要写成对方的名字，也不要留署名占位。" % me)
        return "\n".join(lines)

    @staticmethod
    def _guard_salutation(chunks, who):
        """首行兜底：抬头仍在称呼邮箱主人本人（「张工，您好」「张三：」）时，改成称呼发件人。

        只缓冲到第一个非空行结束，其余 chunk 原样透传，流式体验不变；首行认不出时一个字不动。
        """
        import re as _re
        me = who.get("me") or ""
        if not me:
            yield from chunks
            return
        alts = [_re.escape(me)]
        if AIEngine._cn_name(me):
            alts.append(_re.escape(me[0]) + "(?:工|总|老师|经理|主任|哥|姐)")
        pat = _re.compile(r"(?:尊敬的)?(?:%s)(?:[、，,\s]*(?:各位|大家))?[、，,\s]*(?:您好|你好|好)?[！!：:，,\s]*"
                          % "|".join(alts))
        hon = who.get("sender_hon") or ""

        def fix(buf):
            lead = len(buf) - len(buf.lstrip())
            head = buf[lead:]
            nl = head.find("\n")
            line, rest = (head, "") if nl < 0 else (head[:nl], head[nl:])
            m = pat.match(line)
            if not m:
                return buf
            tail = line[m.end():]
            if tail.strip():        # 「王工，收到……」：只换掉称呼，正文保留
                line = ("%s，" % hon if hon else "") + tail.lstrip()
            else:
                line = ("%s，您好：" % hon) if hon else "您好："
            return buf[:lead] + line + rest

        buf, done = "", False
        for c in chunks:
            if done:
                yield c
                continue
            buf += c
            head = buf.lstrip()
            if "\n" not in head and len(head) < 60:
                continue
            done = True
            yield fix(buf)
        if not done and buf:
            yield fix(buf)

    def quick_todos(self, summary_text: str) -> list[str]:
        prompt = f"""从以下邮件摘要中提取需要跟进的待办事项，每项一行，动词开头，不超过5项。如无则输出空。

{summary_text}

待办："""
        raw = self._generate(prompt, timeout=30)
        todos = [line.strip().lstrip("-•·1234567890. ") for line in raw.splitlines() if line.strip()]
        return [t for t in todos if len(t) > 3][:5]

    def analyze_email(self, subject: str, body: str) -> dict:
        prompt = f"""分析这封邮件，严格输出 JSON，包含三个字段：
"category": 从 ["🚨重要行动","📢内部通知","📰订阅"] 中选一个，不要使用其他emoji或分类
"summary": 50字以内总结
"action_items": 待办事项数组（无则为[]）

主题：{subject}
正文：{body[:1500]}"""
        raw = self._generate(prompt, fmt="json" if self.provider == "ollama" else None, timeout=90)

        # 改前这里对"生成失败"和"模型返回了非 JSON"一视同仁，都回一个
        # {"category": "📢内部通知", "summary": "AI 分析失败", ...}
        # —— 结构完整、看起来正常，连 category 都是编的，于是调用方照常落库。
        # 而摘要端点是"有摘要就直接返回缓存"，那封邮件就永久停在这句话上。
        # 现在：失败一律 ok=False + summary 留空。空摘要会被
        # get_emails_without_ai（判据 summary IS NULL OR summary=''）重新捞起，
        # 等于自带重试；写假摘要则是永久损坏。
        if generation_failed(raw):
            log.warning("analyze_email 生成失败，跳过落库: %s", (raw or "")[:160])
            return {"ok": False, "error": "generation_failed",
                    "category": "", "summary": "", "action_items": []}
        try:
            cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            result = json.loads(cleaned)
        except Exception as e:
            log.warning("analyze_email 解析失败(%s)，原始返回前 200 字: %r",
                        e, (raw or "")[:200])
            return {"ok": False, "error": "parse_failed",
                    "category": "", "summary": "", "action_items": []}

        # 后处理：强制归一化 category，防止模型自创emoji/分类
        result["category"] = self._normalize_category(result.get("category", ""))
        result["ok"] = True
        return result

    # ── 结构化分析（新界面 AI 侧栏 / 列表优先级）──────────────────────
    INSIGHT_PRIORITIES = ("act", "reply", "notice", "fyi")
    DEFAULT_INSIGHT_PROMPT = """你是邮件分诊助手。阅读下面这封邮件，只输出一个 JSON 对象，不要输出任何其他文字。字段：
"priority": 只能是下列之一——
  "act"：需要收件人采取行动、做决定或处理事务；
  "reply"：主要需要收件人回复对方（确认、答复问题），没有其他实质动作；
  "notice"：系统/自动发出的通知、告警、账单、提醒，无需回复；
  "fyi"：仅供阅读的资讯、订阅、抄送知会。
  判定要点：邮件里只要提出了需要收件人或收件人所在团队确认、执行的具体行动（例如会议纪要里的行动项、审批、需要回复的问题），priority 至少为 "act"（只需回复的为 "reply"），不能判为 "fyi"。
"summary": 内容摘要，1~3 句话（40~120 字）：先给结论或核心事项，再带上关键事实——数字、金额、日期、对象、原因、决定。读者不看原文，就能知道这封邮件到底说了什么。
  不合格的写法：只描述邮件类型（“某某答复了某某的问题”“系统提醒有会议”），或只写“涉及多项行动项，需确认/需跟进”这类空话。合格写法必须写出答复、通知或请求的具体内容。邮件很短且内容单一时，一句话即可。
"points": 3~5 条实质内容，按重要性排序，每条不超过 40 字：邮件里的事实、结论、决定、数字、风险、待回答的问题；比 summary 更具体，不要重复 summary 的原句。
  不要列收件人、发件人、电话、邮箱、系统免责声明、“登录控制台查看”“点击查看详情”这类样板，也不要写“发件人为系统自动通知”这类关于邮件本身的描述。正文是系统通知且没有更多信息时给 []。
"todos": 收件人需要做的事，动词开头，最多 3 条；没有就 []。
"drafts": 仅当 priority 为 act 或 reply 且发件人是真人时，给出 2~3 个立场不同的回复草稿，格式 [{"tone": "不超过 6 个字的立场名", "text": "完整回复正文"}]；否则 []。
"headline": 与收件人（我）相关的一句结论，不超过 60 字：说清发生了什么、对我意味着什么或我要做什么，并带出具体内容（对象、数字、结论），不能只写“需确认”“需跟进”；不写时间、地点、参会人等元信息（例：“供应商把单价上调到 3.2 万/月，请你批示是否接受”）。
"meta": 时间、地点、参会人、单号等元信息压成一行，不超过 50 字；没有就 "".
"actions": 邮件里提出的具体行动项，最多 6 条，格式 [{"text": "动词开头不超过 40 字", "owner": "邮件里写明的负责人姓名，没写明就留空"}]；不是给我的也列出（由我确认）；系统通知里“登录控制台查看”“点击查看详情”这类通用操作提示不算行动项；没有就 []。
"due": 邮件里明确要求收件人完成某事的截止日期，格式 YYYY-MM-DD；相对说法（如“本周五”“明天下班前”）按邮件日期换算；没有明确期限就 ""。
"due_src": 截止日期依据的原文片段，不超过 20 字；没有就 ""。
"next": 收件人的下一步，一句话、动词开头、不超过 24 字；无需任何动作就 ""。
"link": 正文里与这件事最相关的一个链接，必须原样出现在正文中；没有就 ""。
要求：只依据邮件内容，不编造；草稿语言与原邮件主要语言一致，含称呼，需要用户补充处用 [占位]。
以下样板话一律不要写进 headline / summary / points：“此邮件为系统通知/自动发送”“无需回复/请勿回复”“登录控制台查看/管理”“点击查看详情”；points 也不要重复 headline 已说过的内容，没有新增信息就给 []。
正文若分为【最新回复】与【引用的历史往来】两段：summary、points、headline 以【最新回复】为准，历史往来只用来理解背景；若只有【最新回复】很短（如“好的”“收到”），则结合历史往来说明这个回复在回应什么。
正文很长时中间可能被省略（标有“…（中间省略）…”），只总结你看到的部分，不要猜测省略的内容。
正文后面如果有“【附件：文件名】”段落，是附件的文字内容（表格用“ | ”分隔单元格、“；”分隔行），要和正文同等对待，把附件里的关键内容（金额、型号、日期、结论）概括进摘要和要点。

邮件日期：{date}
收件人（我）：{account}
发件人：{sender} <{sender_email}>
主题：{subject}
正文：{body}"""

    COMPARE_PROMPT = """你在比较同一系列的两期邮件（例如两次周会纪要）。只输出一个 JSON 对象，不要其他文字。字段：
"new": 本期新出现、上期没有的议题，最多 6 条，[{"text": "不超过 30 字", "quote": "本期正文里原样出现的一段文字，8~40 字"}]；
"continued": 两期都出现的议题，最多 6 条，[{"text": "不超过 30 字", "quote": "本期原文 8~40 字", "prev_quote": "上期原文 8~40 字"}]；
"followups": 对下面「上期行动」逐条判断本期是否有进展（若写着“上期没有记录行动项”，就从上期正文里找出明确提出的行动项再判断，最多 6 条），[{"text": "上期行动原文", "prev_quote": "上期正文里提出这项行动的原文 8~40 字", "quote": "本期正文里说明进展的原文 8~40 字；本期没提到就 \"\"", "progress": "进展一句话，不超过 24 字；没提到就 \"\""}]。
要求：quote / prev_quote 必须一字不差地从对应正文复制；正文没写明进展的，quote 与 progress 都留空——不要推断为已完成。

上期行动：
{actions}

上期正文（{prev_date}）：
{prev_body}

本期正文（{cur_date}）：
{cur_body}"""

    def compare_issues(self, cur: dict, prev: dict, prev_actions: list) -> dict:
        """失败返回 ok=False（调用方不缓存）。只做生成与解析；可追溯性校验由调用方按原文做。"""
        def body(e):
            b = (e.get("body") or "").strip()
            if not b and e.get("html_body"):
                import re as _re
                b = _re.sub(r"<[^>]+>", " ", e["html_body"])
            return " ".join(b.split())[:3500]
        prompt = (self.COMPARE_PROMPT.replace("{actions}", "\n".join("- " + a for a in prev_actions) or "（上期没有记录行动项，请从上期正文里找）")
                  .replace("{prev_date}", str(prev.get("time") or "")[:10]).replace("{prev_body}", body(prev))
                  .replace("{cur_date}", str(cur.get("time") or "")[:10]).replace("{cur_body}", body(cur)))
        raw = self._generate(prompt, fmt="json" if self.provider == "ollama" else None, timeout=120)
        if generation_failed(raw):
            return {"ok": False, "error": "generation_failed"}
        try:
            s = (raw or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            i, j = s.find("{"), s.rfind("}")
            d = json.loads(s[i:j + 1] if i >= 0 and j > i else s)
            if not isinstance(d, dict):
                raise ValueError("not an object")
        except Exception as e:
            log.warning("compare_issues 解析失败(%s): %r", e, (raw or "")[:200])
            return {"ok": False, "error": "parse_failed"}
        return {"ok": True, "data": d, "cur_body": body(cur), "prev_body": body(prev)}

    # 正文预算：回复邮件 = 最新回复(≤2500) + 引用历史(≤3500)；普通长邮件 = 开头 4300 + 结尾 1500。
    _INSIGHT_BUDGET = 6000
    _QUOTE_RE = None

    @classmethod
    def _insight_input(cls, email: dict, budget=None) -> str:
        """给 analyze_insight 的正文：折叠空白；回复串把「最新回复」与「引用历史」分开标注；超长取头尾。"""
        import re as _re
        body = (email.get("body") or "").strip()
        if not body and email.get("html_body"):
            body = _re.sub(r"<[^>]+>", " ", email["html_body"])
        body = " ".join(body.split())
        if cls._QUOTE_RE is None:
            cls._QUOTE_RE = _re.compile(
                r"(?:(?:发件人|From)\s*[:：].{0,120}?(?:发送时间|发送日期|Sent|Date|日期)\s*[:：])"
                r"|(?:-{2,}\s*(?:原始邮件|原邮件|Original Message|Forwarded message|转发邮件)\s*-{2,})"
                r"|(?:On .{5,80}wrote:)|(?:在 ?\d{4}.{0,40}写道)", _re.I)
        m = cls._QUOTE_RE.search(body)
        total = int(budget or cls._INSIGHT_BUDGET)
        if m and m.start() >= 15:   # 开头就是头信息的是转发，正文本身就是内容，不分段
            new, hist = body[:m.start()].strip(), body[m.start():].strip()
            new = new[:int(total * 0.42)]
            hist = hist[:max(500, total - len(new) - 300)]
            return "【最新回复】" + new + "\n【引用的历史往来（仅作背景）】" + hist
        if len(body) > total:
            return body[:int(total * 0.72)] + " …（中间省略）… " + body[-int(total * 0.25):]
        return body

    # ── 摘要后处理：模型总会夹带样板话，靠提示词拦不住，这里确定性兜底 ──────────────
    _BOILER = None

    @classmethod
    def _strip_boilerplate(cls, out: dict, keep_thin: bool = False) -> dict:
        import re as _re
        if cls._BOILER is None:
            cls._BOILER = [_re.compile(x) for x in (
                r"(此|本)邮件(为|由)?系统(自动)?(通知|发送|发出)", r"(无需|请勿|不要)(直接)?回复",
                r"无需.{0,8}(行动|操作|处理|回复|响应)", r"仅(为|是)?系统(自动)?(发送|发出|通知|提醒)",
                r"系统(自动)?(通知|发送|发出).{0,6}(无需|请勿)",
                r"(登录|通过|进入|前往|访问).{0,12}(控制台|官网|链接|平台).{0,12}(查看|管理|处理|了解|详情)",
                r"控制台.{0,12}(查看|管理)", r"点击.{0,8}(查看|了解).{0,6}(详情|更多)",
                r"可通过.{0,20}(查看|管理|进行管理)", r"免责声明|版权所有|保密(声明|提示)")]
            cls._META_PT = _re.compile(
                r"^((操作|收件|发件|登录)?(账户|账号|用户|邮箱)|收件人|发件人|联系人|联系电话|电话)\s*(为|是|:|：)"
                r"|(账号|账户|邮箱).{0,8}[\w.\-]+@[\w.\-]+|^[\w.\-]+@[\w.\-]+$|^\+?[\d\- ]{7,}$|联系.{0,8}\d{7,}|发件人为.{0,12}(系统|自动)")
        def boiler(t):
            return any(r.search(t) for r in cls._BOILER)
        def clean_text(t):
            t = str(t or "")
            parts = _re.split(r"([，,；;。.！!]\s*)", t)          # 保留分隔符，按分句处理
            keep, i = [], 0
            while i < len(parts):
                seg = parts[i]; sep = parts[i + 1] if i + 1 < len(parts) else ""
                if seg.strip() and not (len(seg) <= 40 and boiler(seg)):
                    keep.append(seg + sep)
                i += 2
            r = "".join(keep).strip()
            r = _re.sub(r"[，,；;]\s*$", "", r)
            if r and not _re.search(r"[。.！!？?]$", r) and len(r) > 24:
                r += "。"
            return r
        def bg(x):
            x = _re.sub(r"\W+", "", x)
            return {x[i:i + 2] for i in range(len(x) - 1)}
        try:                                    # 元话（读者 / 被抄送 / 与你无关）：旧路径的输出也过一遍
            from core import insight_v2 as _iv2
            for _k in ("headline", "summary"):
                out[_k] = _iv2.clean_meta(out.get(_k)) or out.get(_k) or ""
            out["points"] = [p for p in (_iv2.clean_meta(p) for p in (out.get("points") or [])) if p]
        except Exception:
            pass
        out["headline"] = clean_text(out.get("headline"))
        sm = clean_text(out.get("summary")) or out.get("summary", "")
        if len(sm) > 200:   # 在句 / 分句边界截断，不要停在半个词上
            cut = max(sm.rfind(c, 0, 200) for c in "。；;，,")
            sm = (sm[:cut] if cut >= 80 else sm[:200]).rstrip("，,；;") + "。"
        out["summary"] = sm
        ref = bg(out["headline"] or out["summary"])   # 界面显示的是 headline，没有才显示 summary
        pts = []
        for p in out.get("points") or []:
            if boiler(p) or cls._META_PT.search(p):
                continue
            b = bg(p)
            if ref and len(b) >= 3 and len(b & ref) / len(b) >= 0.8:
                continue
            pts.append(p)
        # 通知 / 知会且没有行动项：剩下 ≤2 条几乎一定是在复述标题，直接不显示（_thin_notice）
        if not keep_thin and out.get("priority") in ("notice", "fyi") and not out.get("actions") and len(pts) <= 2:
            pts = []
        out["points"] = pts
        return out

    # ── 按邮件类型选模式（规则路由，不额外调用 AI）──────────────────────────────
    _MODE_RE = {}
    _MODE_BLOCKS = {
        "machine": (
            '"summary": 一句话事实（不超过 40 字）：对象 + 发生了什么/当前状态 + 关键数值或时间，例：“账户 acme_newbusiness 可用金余额 2875 元，预计 15 天内欠费”。只写邮件里的事实，不写邮件类型和样板（“系统通知”“自动发送”“无需回复”“登录控制台查看”）。\n'
            '"points": 默认 []。只有邮件里有多个独立对象或数值（如多台机器的告警、多个指标）时才逐条列出，格式“对象：状态/数值”，最多 5 条，每条不超过 40 字；不要列账户、收件人、邮箱、电话，不要重复 summary。\n',
            '"headline": 与收件人（我）相关的一句话，不超过 40 字：告警未恢复、欠费、即将到期、需要处理时，写明对象和该做什么；纯知会就写事实本身，不要加“无需操作”“仅供知悉”。\n'),
        "request": (
            '"summary": 内容摘要，2~3 句（60~140 字）：谁向谁提出什么请求或给出什么答复；依据、金额、条件等关键事实；需要收件人做出的决定或下一步。以最新回复为准。\n'
            '"points": 3~5 条，每条不超过 40 字：对方的诉求、依据与数字、风险或争议点、时间要求；不要重复 summary 的原句，不要列收件人、发件人、电话。\n',
            '"headline": 与收件人（我）相关的一句结论，不超过 60 字：我要做的决定 + 关键数字或条件（例：“供应商把单价上调到 3.2 万/月，请你批示是否接受”）。\n'),
        "doc": (
            '"summary": 内容摘要，3~5 句（100~200 字）：先给结论、决议或总体状况，再给关键数字及变化，最后是风险、遗留问题或待决事项。读者不看原文就能掌握全貌。\n'
            '"points": 4~6 条，每条不超过 50 字，按“决议/结论 → 数字与变化 → 风险与问题 → 下一步”排序；带上负责人、日期、金额、对象；不要写“详见附件”这类空话，不要重复 summary 的原句。\n',
            '"headline": 与收件人（我）相关的一句结论，不超过 60 字：这份材料里最需要我知道或处理的一件事，带具体内容（对象、数字、结论）。\n'),
    }
    _MODE_BUDGET = {"machine": 3000, "doc": 9000}

    @classmethod
    def _insight_mode(cls, email: dict) -> str:
        """machine 机器通知/告警(≤1500 字) · doc 纪要/周报/报告/长机器报告 · request 请求审批/回复转发 · general 兜底。"""
        import re as _re
        P = cls._MODE_RE
        if not P:
            P.update(
                auto_sender=_re.compile(r"^(no-?reply|noreply|do-?not-?reply|notifications?|notify|alerts?|noc|monitor(ing)?|mailer-daemon|postmaster|system|billing|bounce|newsletter|news)\b", _re.I),
                auto_body=_re.compile(r"请勿(直接)?回复|系统自动(发出|发送)|此邮件由系统|由系统自动|do[ -]?not reply|no-?reply|自动发出|unsubscribe|退订", _re.I),
                alert_subj=_re.compile(r"告警|报警|\balert\b|PROBLEM|RESOLVED|已恢复|欠费|到期|续费|账单|验证码|创建成功|提醒|宕机", _re.I),
                reply=_re.compile(r"^\s*((re|回复|答复|fw|fwd|转发)\s*[:：]\s*)+", _re.I),
                doc_subj=_re.compile(r"纪要|周报|月报|日报|季报|简报|报告|巡检|复盘|总结|汇报|白皮书"),
                req_subj=_re.compile(r"审批|报备|申请|采购|合同|批示|请示|签批|请确认|评审|议价|报价|付款|预算"))
        subj = str(email.get("subject") or "")
        body = (email.get("body") or "").strip()
        if not body and email.get("html_body"):
            body = _re.sub(r"<[^>]+>", " ", email["html_body"])
        body = " ".join(body.split())
        n = len(body)
        local = str(email.get("sender_email") or "").lower().split("@")[0]
        is_reply = bool(P["reply"].match(subj))
        auto = bool(P["auto_sender"].match(local)) or bool(P["auto_body"].search(body[:1500] + " " + body[-600:])) \
            or bool(P["alert_subj"].search(subj) and not P["req_subj"].search(subj) and not is_reply)
        if auto and not is_reply:
            return "machine" if n <= 1500 else "doc"
        if P["doc_subj"].search(subj) and not is_reply:
            return "doc"
        if is_reply or P["req_subj"].search(subj):
            return "request"
        return "doc" if n >= 3000 else "general"

    @classmethod
    def _prompt_for_mode(cls, mode: str) -> str:
        tpl = cls.DEFAULT_INSIGHT_PROMPT
        blk = cls._MODE_BLOCKS.get(mode)
        if not blk:
            return tpl
        i, j = tpl.index('"summary":'), tpl.index('"todos":')
        k, l = tpl.index('"headline":'), tpl.index('"meta":')
        return tpl[:i] + blk[0] + tpl[j:k] + blk[1] + tpl[l:]

    def _attachment_context(self, email: dict, total_chars: int = 12000):
        """附件文字块 + info。任何异常都按「没有附件」处理，不影响邮件分析。"""
        try:
            from core import insight_v2 as _iv2, attachment_text as _at
            return _at.read_for_analysis(_iv2.load_cfg(), email, total_chars)
        except Exception as _e:
            log.warning("附件读取异常（忽略，按无附件分析）: %s", _e)
            return "", []

    @classmethod
    def _is_auto_sender(cls, email: dict) -> bool:
        cls._insight_mode({})      # 确保路由正则已编译
        local = str(email.get("sender_email") or "").lower().split("@")[0]
        return bool(cls._MODE_RE["auto_sender"].match(local))

    def _gen_retry(self, prompt: str, fmt, timeout: int):
        """网关偶发超时 / 抖动：失败立刻再试一次（第二次超时短一点，总时长有界）。"""
        raw = self._generate(prompt, fmt=fmt, timeout=timeout)
        if generation_failed(raw):
            log.warning("生成失败，重试一次: %s", (raw or "")[:100])
            raw = self._generate(prompt, fmt=fmt, timeout=min(timeout, 90))
        return raw

    def _analyze_insight_v2(self, email: dict, reader=None):
        """insight.v2（判断 + 证据）。返回 {"ok":…, 旧字段…, "v2":…, "level":…}；
        生成失败返回 ok=False；两次输出都不合格返回 None（调用方降级到旧路径）。"""
        from core import insight_v2 as iv2
        import re as _re
        reader = iv2.load_reader() if reader is None else reader
        body_full = (email.get("body") or "").strip()
        if not body_full and email.get("html_body"):
            body_full = _re.sub(r"<[^>]+>", " ", email["html_body"])
        body_full = " ".join(body_full.split())
        budget = 9000
        relation = iv2.resolve_relation(email, reader, system=self._is_auto_sender(email))
        att_block, att_info = self._attachment_context(email)
        evid_text = body_full + (" " + " ".join(att_block.split()) if att_block else "")     # 证据核对的范围 = 正文 + 附件文字
        body_in = self._insight_input(email, budget) + (("\n\n" + att_block) if att_block else "")
        prompt = iv2.build_prompt(email, reader, relation, body_in, truncated=len(body_full) > budget)
        reason = ""
        for attempt in (0, 1):
            p = prompt if attempt == 0 else prompt + "\n\n（上一次输出不合格：%s。请严格按要求只输出一个 JSON 对象。）" % reason
            raw = self._gen_retry(p, "json" if self.provider == "ollama" else None, 150 if att_block else 120)
            if generation_failed(raw):
                log.warning("analyze_insight(v2) 生成失败: %s", (raw or "")[:160])
                return {"ok": False, "error": "generation_failed"}
            try:
                s = (raw or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                i, j = s.find("{"), s.rfind("}")
                data = json.loads(s[i:j + 1] if i >= 0 and j > i else s)
            except Exception as e:
                reason = "无法解析为 JSON"
                log.warning("analyze_insight(v2) 解析失败(%s): %r", e, (raw or "")[:200])
                continue
            v2, why, _st = iv2.normalize(data, evid_text, relation, subject=str(email.get("subject") or ""))
            if v2:
                break
            reason = why
        else:
            log.warning("analyze_insight(v2) 两次输出都不合格（%s），降级到旧路径", reason)
            return None
        out = self._strip_boilerplate(self.normalize_insight(iv2.to_legacy(v2)), keep_thin=True)
        if out.get("link") and out["link"] not in ((email.get("body") or "") + (email.get("html_body") or "")):
            out["link"] = ""
        out["v2"] = v2
        out["level"] = v2["level"]
        if att_info:
            v2["attachments"] = att_info
            out["att_info"] = att_info
            unread = [a for a in att_info if a.get("status") != "read"]
            if unread:
                note = "附件未读取：" + "；".join("%s（%s）" % (a["name"][:28], a.get("reason") or "未读取") for a in unread[:2])
                v2["notes"] = (v2.get("notes") or [])[:3] + [note[:80]]
                out["points"] = (out.get("points") or [])[:4] + [note[:120]]
        return {"ok": True, **out}

    def analyze_insight(self, email: dict) -> dict:
        """入口：先走原有分析，再对「正文只是链接 / 附件 / 空」的邮件把没有信息量的摘要换成如实说明。"""
        r = self._analyze_insight_inner(email)
        try:
            if isinstance(r, dict) and r.get("ok") and r.get("priority") == "reply" and not r.get("v2") and self._insight_mode(email) == "machine":
                r = dict(r, priority="notice")      # 系统自动发出的通知（请勿回复）不会「等你回复」
            return self._mark_pointer(email, r)
        except Exception as _e:
            log.warning("pointer 标注异常（忽略）: %s", _e)
            return r

    def _mark_pointer(self, email: dict, r):
        if not isinstance(r, dict) or not r.get("ok"):
            return r
        from core import insight_v2 as iv2
        info = iv2.pointer_info(email, r.get("att_info"))
        if not info:
            return r
        if r.get("priority") in ("act", "reply"):           # 需要行动的保留原提示（截止日 / 动作都在里面），只清掉复述主题的要点
            r = dict(r)
            r["points"] = []                                # 正文没有内容，要点只能是复述主题 / 猜测
            if isinstance(r.get("v2"), dict):
                r["v2"] = dict(r["v2"], points=list(r["points"]))
            return r
        # 通知 / 仅供阅读：按定义，正文几乎没有主题以外的信息，模型写出来的「新内容」只能是复述或猜测——如实说明
        msg = info["message"]
        r = dict(r)
        r["headline"], r["summary"], r["points"] = msg, msg, []
        if isinstance(r.get("v2"), dict):
            r["v2"] = dict(r["v2"], headline=msg, digest=msg, points=[])
        return r

    def _analyze_insight_inner(self, email: dict) -> dict:
        """一次调用产出 priority/summary/points/todos/drafts。失败返回 ok=False（调用方不得缓存）。"""
        mode = self._insight_mode(email)
        if mode != "machine" and not self._custom("insight"):    # 机器通知保留快速通道；自定义提示词优先于 v2
            try:
                from core import insight_v2 as _iv2
                if _iv2.v2_enabled():
                    _r2 = self._analyze_insight_v2(email)
                    if _r2 is not None:
                        return _r2
            except Exception as _e:                                 # v2 任何异常都降级到旧路径
                log.warning("analyze_insight(v2) 异常，降级: %s", _e)
        body = self._insight_input(email, self._MODE_BUDGET.get(mode))
        att_block, att_info = self._attachment_context(email, 4000)
        if att_block:
            body += "\n\n" + att_block
        tpl = self._custom("insight") or self._prompt_for_mode(mode)
        prompt = (tpl.replace("{account}", str(email.get("account") or ""))
                     .replace("{sender}", str(email.get("sender") or ""))
                     .replace("{sender_email}", str(email.get("sender_email") or ""))
                     .replace("{subject}", str(email.get("subject") or ""))
                     .replace("{date}", str(email.get("time") or "")[:10])
                     .replace("{body}", body))
        if getattr(self, "signature", ""):
            prompt += "\n\n回复草稿正文结尾另起一行原样附上签名：\n" + self.signature
        raw = self._gen_retry(prompt, "json" if self.provider == "ollama" else None, 90)
        if generation_failed(raw):
            log.warning("analyze_insight 生成失败: %s", (raw or "")[:160])
            return {"ok": False, "error": "generation_failed"}
        try:
            s = (raw or "").strip()
            s = s.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            i, j = s.find("{"), s.rfind("}")
            data = json.loads(s[i:j + 1] if i >= 0 and j > i else s)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except Exception as e:
            log.warning("analyze_insight 解析失败(%s): %r", e, (raw or "")[:200])
            return {"ok": False, "error": "parse_failed"}
        out = self._strip_boilerplate(self.normalize_insight(data))
        # 链接必须真的出现在邮件里（含 HTML 源码里的 href），模型编的一律丢掉
        if out.get("link") and out["link"] not in ((email.get("body") or "") + (email.get("html_body") or "")):
            out["link"] = ""
        return {"ok": True, **out, **({"att_info": att_info} if att_info else {})}

    @classmethod
    def normalize_insight(cls, d: dict) -> dict:
        """把模型输出收敛成固定结构；任何字段不合规都退化为空，不抛异常。"""
        def strs(v, n, w):
            if not isinstance(v, list):
                return []
            out = []
            for x in v:
                x = " ".join(str(x or "").split())
                if x and x not in out:
                    out.append(x[:w])
            return out[:n]
        pri = str(d.get("priority") or "").strip().lower()
        if pri not in cls.INSIGHT_PRIORITIES:
            pri = "fyi"
        drafts = []
        if pri in ("act", "reply") and isinstance(d.get("drafts"), list):
            for x in d["drafts"]:
                if isinstance(x, dict):
                    tone = " ".join(str(x.get("tone") or "").split())[:12]
                    text = str(x.get("text") or "").strip()[:4000]
                    if tone and text:
                        drafts.append({"tone": tone, "text": text})
        import re as _re, datetime as _dt
        due = str(d.get("due") or "").strip()[:10]
        try:
            due = _dt.date.fromisoformat(due).isoformat() if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", due) else ""
        except ValueError:
            due = ""
        link = str(d.get("link") or "").strip()
        if not _re.fullmatch(r"https?://\S{3,2000}", link):
            link = ""
        acts = []
        for x in (d.get("actions") or []) if isinstance(d.get("actions"), list) else []:
            if isinstance(x, dict):
                t = " ".join(str(x.get("text") or "").split())[:80]
                o = " ".join(str(x.get("owner") or "").split())[:20]
                if t and t not in [a["text"] for a in acts]:
                    acts.append({"text": t, "owner": o})
            elif isinstance(x, str) and x.strip():
                acts.append({"text": " ".join(x.split())[:80], "owner": ""})
        return {"priority": pri,
                "headline": " ".join(str(d.get("headline") or "").split())[:90],
                "meta": " ".join(str(d.get("meta") or "").split())[:100],
                "actions": acts[:6],
                "due": due,
                "due_src": " ".join(str(d.get("due_src") or "").split())[:30] if due else "",
                "next": " ".join(str(d.get("next") or "").split())[:40],
                "link": link,
                "summary": " ".join(str(d.get("summary") or "").split())[:260],
                "points": strs(d.get("points"), 5, 80),
                "todos": strs(d.get("todos"), 3, 80),
                "drafts": drafts[:3]}

    @staticmethod
    def _normalize_category(raw_category: str) -> str:
        """将非标准分类映射到三个标准分类之一"""
        VALID = {"🚨重要行动", "📢内部通知", "📰订阅"}
        if raw_category in VALID:
            return raw_category

        # 模糊匹配
        lower = raw_category.lower()
        if "重要" in lower or "行动" in lower or "紧急" in lower or "待办" in lower or "🚨" in lower:
            return "🚨重要行动"
        if "订阅" in lower or "新闻" in lower or "推送" in lower or "📰" in lower:
            return "📰订阅"
        # 默认归到内部通知
        return "📢内部通知"

    def draft_reply(self, subject: str, body: str, tone: str = "正式") -> str:
        prompt = f"基于以下邮件，起草语气【{tone}】的回复正文：\n主题：{subject}\n原文：{body[:1500]}\n\n回复："
        return self._generate(prompt, timeout=60)

    def chat(self, user_message: str, search_results=None, current_email=None) -> str:
        return "".join(self.stream_chat(user_message, search_results, current_email))

    def generate_person_report(self, person_name: str, emails: list[dict],
                                start_label: str = "", end_label: str = "") -> str:
        if not emails:
            return "没有找到相关邮件记录。"
        period = f"{start_label} 至 {end_label}" if start_label and end_label else "指定时间段"
        lines = [f"[{e.get('time','')[:10]}] 主题:{e.get('subject','')[:50]} 摘要:{(e.get('summary') or e.get('body',''))[:60]}"
                 for e in emails[:80]]
        prompt = f"""为"{person_name}"生成{period}工作报告（Markdown）：

{chr(10).join(lines)}

## 工作总结
### 主要工作事项
### 重要推进与成果
### 待跟进事项
### 综合评价"""
        return self._generate(prompt, timeout=120)

    def classify_intent(self, user_message: str) -> dict:
        prompt = f"""判断邮件查询意图，输出JSON：
{{"intent": "search|person_report|approval_track|summary|chat", "params": {{}}}}
用户：{user_message}"""
        raw = self._generate(prompt, timeout=30)
        try:
            cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            return json.loads(cleaned)
        except Exception:
            return {"intent": "chat", "params": {}}

    def extract_todos(self, subject: str, body: str) -> list[dict]:
        prompt = (self._custom("todo_extract") or "从邮件中提取待办事项。") + f"""
输出JSON数组，每项含 content/due_date/priority；无待办则输出 []。
主题：{subject}
正文：{body[:2000]}"""
        raw = self._generate(prompt, timeout=60)
        if generation_failed(raw):
            # 返回 None 表示**失败**，与"真的没有待办"（返回 []）区分开。
            # 只写日志是不够的 —— 用户看不到日志，界面上两种情况长得一样，
            # 就不知道该不该重试。这和之前修摘要"失败不许伪装成正常结果"同一个道理。
            log.warning("extract_todos 生成失败: %s", (raw or "")[:160])
            return None
        try:
            cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            result = json.loads(cleaned)
            return result if isinstance(result, list) else result.get("items", [])
        except Exception as e:
            log.warning("extract_todos 解析失败(%s)，原始返回前 200 字: %r",
                        e, (raw or "")[:200])
            return None

    def is_running(self) -> bool:
        """检查当前后端是否可达"""
        if self.provider == "api":
            # 优先探测 /models；部分网关（如内网网关）未实现该端点会返回
            # 404/405，但 chat/completions 可用。只要服务器有响应（非连接错误、
            # 非 5xx），即视为可达；否则再尝试一次极小的 chat 请求兜底。
            try:
                r = requests.get(f"{self.api_url}/models",
                                 headers=self._api_headers(), timeout=5)
                if r.status_code < 500:
                    return True
            except Exception:
                pass
            try:
                r = requests.post(
                    f"{self.api_url}/chat/completions",
                    headers=self._api_headers(),
                    json={"model": self.api_model,
                          "messages": [{"role": "user", "content": "ping"}],
                          "max_tokens": 1, "stream": False},
                    timeout=8,
                )
                return r.status_code < 500
            except Exception:
                return False
        try:
            r = requests.get(f"{self.ollama_url}/api/tags", timeout=5)
            return r.status_code == 200
        except Exception:
            return False
