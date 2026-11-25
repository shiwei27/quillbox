"""outlook_rules.py — 读取本机 Outlook 收件规则并转换为 Quillbox 规则格式。
遍历所有邮件存储(Stores)尝试 GetRules（Exchange 存储更可能支持规则 API）。尽力而为。"""

def _convert_rule(rule):
    conds = []
    try:
        c = rule.Conditions
        try:
            if c.From and c.From.Enabled:
                for r in c.From.Recipients:
                    v = getattr(r, "Address", "") or getattr(r, "Name", "")
                    if v: conds.append({"field": "from", "op": "contains", "value": str(v)})
        except Exception: pass
        try:
            if c.SenderAddress and c.SenderAddress.Enabled:
                for a in c.SenderAddress.Address:
                    if a: conds.append({"field": "from", "op": "contains", "value": str(a)})
        except Exception: pass
        try:
            if c.Subject and c.Subject.Enabled:
                for w in c.Subject.Text:
                    if w: conds.append({"field": "subject", "op": "contains", "value": str(w)})
        except Exception: pass
        try:
            if c.Body and c.Body.Enabled:
                for w in c.Body.Text:
                    if w: conds.append({"field": "body", "op": "contains", "value": str(w)})
        except Exception: pass
        try:
            if c.SentTo and c.SentTo.Enabled:
                for r in c.SentTo.Recipients:
                    v = getattr(r, "Address", "") or getattr(r, "Name", "")
                    if v: conds.append({"field": "to", "op": "contains", "value": str(v)})
        except Exception: pass
    except Exception:
        pass
    cat = ""
    try:
        mv = rule.Actions.MoveToFolder
        if mv and mv.Enabled and mv.Folder:
            cat = str(mv.Folder.Name)
    except Exception: pass
    mark_read = False
    try:
        mr = rule.Actions.MarkAsRead
        if mr and mr.Enabled:
            mark_read = True
    except Exception: pass
    if not conds:
        return None
    return {
        "name": (str(getattr(rule, "Name", "")) or "Outlook规则"),
        "enabled": bool(getattr(rule, "Enabled", True)),
        "match_mode": "all",
        "conditions": conds,
        "ai_match": "",
        "actions": {"category": cat, "star": False, "mark_read": mark_read,
                    "skip_ai": False, "summary_prompt": "", "draft_prompt": ""},
        "_source": "outlook",
    }


def read_outlook_rules():
    try:
        import pythoncom, win32com.client
    except Exception as e:
        return {"ok": False, "error": f"缺少 win32com: {e}"}
    pythoncom.CoInitialize()
    try:
        try:
            outlook = win32com.client.GetActiveObject("Outlook.Application")
        except Exception:
            outlook = win32com.client.Dispatch("Outlook.Application")
        session = outlook.Session
        out = []
        errors = []
        # 优先遍历所有 Stores
        got_any = False
        try:
            stores = session.Stores
            for i in range(1, stores.Count + 1):
                try:
                    st = stores.Item(i)
                    rules = st.GetRules()
                    got_any = True
                    for rule in rules:
                        try:
                            r = _convert_rule(rule)
                            if r: out.append(r)
                        except Exception:
                            continue
                except Exception as e:
                    errors.append(str(e)[:60]); continue
        except Exception as e:
            errors.append("Stores遍历失败:" + str(e)[:60])
        # 回退：默认存储
        if not got_any:
            try:
                rules = session.DefaultStore.GetRules()
                for rule in rules:
                    try:
                        r = _convert_rule(rule)
                        if r: out.append(r)
                    except Exception:
                        continue
                got_any = True
            except Exception as e:
                errors.append("DefaultStore:" + str(e)[:80])
        if not got_any and not out:
            return {"ok": False, "error": "无法读取 Outlook 规则（该账户类型可能不支持规则API）：" + " | ".join(errors)[:160]}
        return {"ok": True, "rules": out}
    except Exception as e:
        return {"ok": False, "error": f"读取 Outlook 规则失败：{str(e)[:120]}"}
    finally:
        try: pythoncom.CoUninitialize()
        except Exception: pass
