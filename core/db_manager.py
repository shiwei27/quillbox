import sqlite3
import json
import threading
from pathlib import Path
from contextlib import contextmanager


class DBManager:
    """SQLite 持久层，统一字段标准，支持分页、FTS5全文搜索"""

    _TABLE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS emails (
        id                TEXT PRIMARY KEY,
        subject           TEXT,
        body              TEXT,
        html_body         TEXT,
        sender            TEXT,
        sender_email      TEXT,
        recipients        TEXT,
        time              TEXT,
        received_timestamp REAL,
        unread            INTEGER DEFAULT 0,
        starred           INTEGER DEFAULT 0,
        deleted           INTEGER DEFAULT 0,
        folder            TEXT,
        account           TEXT,
        has_attachment    INTEGER DEFAULT 0,
        attachments       TEXT,
        category          TEXT,
        summary           TEXT,
        action_items      TEXT
    );
    CREATE TABLE IF NOT EXISTS sync_status (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS people (
        email       TEXT PRIMARY KEY,
        name        TEXT,
        last_seen   REAL,
        email_count INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS email_people (
        email_id    TEXT,
        person_email TEXT,
        role        TEXT,
        PRIMARY KEY (email_id, person_email, role)
    );
    CREATE TABLE IF NOT EXISTS todos (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        content     TEXT NOT NULL,
        source_email_id TEXT,
        status      TEXT DEFAULT 'pending',
        priority    TEXT DEFAULT 'normal',
        due_date    TEXT,
        note        TEXT,
        created_at  REAL,
        updated_at  REAL
    );
    CREATE TABLE IF NOT EXISTS drafts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        to_addr     TEXT,
        cc_addr     TEXT,
        subject     TEXT,
        body        TEXT,
        is_html     INTEGER DEFAULT 0,
        account     TEXT,
        reply_to_id TEXT,
        source      TEXT DEFAULT 'ui',
        created_at  REAL,
        updated_at  REAL
    );
    -- 新界面 AI 侧栏的结构化分析缓存。与 emails 分表：生成失败不写，
    -- 邮件被覆盖更新时也不受影响；body_hash 用于判定正文变了需要重算。
    CREATE TABLE IF NOT EXISTS email_insights (
        email_id    TEXT PRIMARY KEY,
        priority    TEXT,
        summary     TEXT,
        points      TEXT,
        todos       TEXT,
        drafts      TEXT,
        body_hash   TEXT,
        model       TEXT,
        created_at  REAL
    );
    -- 稍后提醒：到 until_ts 之前不出现在「今日分拣」；到期后回到原分组最前并标「提醒」。只影响分拣显示。
    CREATE TABLE IF NOT EXISTS snoozes (
        email_id    TEXT PRIMARY KEY,
        until_ts    REAL NOT NULL,
        created_at  REAL
    );
    -- 完成说明 / 处理记录：完成并归档时留下的结果、富文本说明、附件清单、关联邮件。
    -- deleted=1 为软删（撤销「完成并归档」时），附件文件 7 天后才清理。一封邮件可有多条。
    CREATE TABLE IF NOT EXISTS archive_notes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        email_id    TEXT NOT NULL,
        outcome     TEXT,
        note_html   TEXT,
        note_text   TEXT,
        attachments TEXT,
        links       TEXT,
        deleted     INTEGER DEFAULT 0,
        created_at  REAL,
        updated_at  REAL
    );
    CREATE INDEX IF NOT EXISTS idx_anote_email ON archive_notes(email_id);
    -- 行动项状态 / 纠正 / 系列
    -- 行动项状态：akey = 行动项文字归一（去空白、小写）。state 目前只有 not_mine（「已加入」由待办表判定，不重复存）。
    CREATE TABLE IF NOT EXISTS action_states (
        email_id TEXT NOT NULL, akey TEXT NOT NULL, text TEXT, state TEXT, created_at REAL,
        PRIMARY KEY (email_id, akey)
    );
    -- 「判断有误」纠正：同一发件人 + 同一主题模板之后按 to_priority 处理（AI 结果被覆盖）。
    CREATE TABLE IF NOT EXISTS ai_corrections (
        id INTEGER PRIMARY KEY AUTOINCREMENT, email_id TEXT, sender TEXT NOT NULL, pattern TEXT NOT NULL, label TEXT,
        to_priority TEXT NOT NULL, created_at REAL, UNIQUE(sender, pattern)
    );
    -- 系列拆分：用户说「这不是一个系列」的 key，不再折叠。
    CREATE TABLE IF NOT EXISTS series_splits (skey TEXT PRIMARY KEY, label TEXT, created_at REAL);
    -- 与上期对比缓存：两期正文任一变化（hash）就重算。
    CREATE TABLE IF NOT EXISTS series_compare (email_id TEXT PRIMARY KEY, prev_id TEXT, hash TEXT, data TEXT, created_at REAL);
    -- 静音告警：scope=object 时 key 为 server._alert_key（发件人 + 告警对象），scope=sender 时 key 为发件人地址。
    -- 跨账户生效；只影响「今日分拣」的告警与通知组，不影响同步与规则。
    CREATE TABLE IF NOT EXISTS alert_mutes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        scope       TEXT NOT NULL,
        key         TEXT NOT NULL,
        label       TEXT,
        until_ts    REAL NOT NULL,
        created_at  REAL,
        UNIQUE(scope, key)
    );
    """

    _MIGRATIONS = [
        ("starred",    "INTEGER DEFAULT 0"),
        ("deleted",    "INTEGER DEFAULT 0"),
        ("recipients", "TEXT"),
        ("html_body",  "TEXT"),
        ("tags",       "TEXT"),
        ("archived",   "INTEGER DEFAULT 0"),
        # 跨后端去重：Outlook(EntryID) / POP3 / IMAP 三条取件路径的 id 体系不同，
        # 唯一可靠的同一性判据是邮件头 Message-ID。归一化后（去尖括号、小写）存这里。
        ("message_id", "TEXT"),
    ]

    # drafts 表的增量列。与 _MIGRATIONS（emails 表）分开，避免误加到 emails 上。
    _DRAFT_MIGRATIONS = [
        ("imap_folder", "TEXT"),     # 回写到服务端草稿箱时的原始文件夹名
        ("imap_uid", "TEXT"),        # 该副本的 UID，重推时据此删旧副本
    ]

    _INDEXES = [
        "CREATE INDEX IF NOT EXISTS idx_received    ON emails(received_timestamp DESC)",
        # 列表主路径 WHERE deleted=? ORDER BY received_timestamp DESC 的复合索引。
        # 只有 idx_deleted 时要把全部行整行读出再临时排序（万封级邮箱一页约 300ms），
        # 有它则按索引顺序取前 N 行即停（<1ms），结果逐行一致。
        "CREATE INDEX IF NOT EXISTS idx_deleted_received ON emails(deleted, received_timestamp DESC)",
        # 计数类（/api/stats 每次都算、列表每页都算 total）。万封级邮箱上：
        # 未读计数约 20ms→0ms、待 AI 计数约 100ms→0.2ms（部分索引只收录满足条件的行，
        # 体积很小）；(deleted, archived) 让收件箱 total 走覆盖索引。
        "CREATE INDEX IF NOT EXISTS idx_unread_live ON emails(deleted, archived) WHERE unread=1",
        "CREATE INDEX IF NOT EXISTS idx_no_summary ON emails(deleted, account, folder)"
        " WHERE (summary IS NULL OR summary='')",
        "CREATE INDEX IF NOT EXISTS idx_deleted_archived ON emails(deleted, archived)",
        # 星标 / 归档视图：命中行通常极少，部分索引让它们不再逐行扫全部
        # 未删除邮件（约 20ms → <0.1ms）。
        "CREATE INDEX IF NOT EXISTS idx_starred_live ON emails(deleted, received_timestamp DESC) WHERE starred=1",
        "CREATE INDEX IF NOT EXISTS idx_archived_live ON emails(deleted, received_timestamp DESC) WHERE archived=1",
        # 全文搜索两段式的第一段：按 id 查 deleted/received_timestamp 走覆盖索引，不读整行。
        "CREATE INDEX IF NOT EXISTS idx_id_live ON emails(id, deleted, received_timestamp)",
        # 覆盖索引。emails 表 body 排在 folder/account/unread 等列之前，
        # 正文长的行溢出到 overflow 页，任何要读这些列的整行访问都得顺着正文走一遍：
        # 按文件夹列表 120–330ms、/api/folders 与 /api/accounts 约 100ms、分类 80ms、
        # 会话往来 85–140ms。下面四个索引让这些查询不碰整行。在万封级邮箱上，224 个列表 / 计数 /
        # 聚合组合合计约 3.5s → 0.3s，结果逐项一致；代价是「文件夹 + 星标 / 已归档」
        # 这类极少用的组合从 <1ms 变为 5–16ms（规划器改走文件夹索引）。
        "CREATE INDEX IF NOT EXISTS idx_acc_folder_recv ON emails(account, folder, deleted, received_timestamp DESC, unread)",
        "CREATE INDEX IF NOT EXISTS idx_folder_recv ON emails(folder, deleted, received_timestamp DESC, unread)",
        "CREATE INDEX IF NOT EXISTS idx_cov_category ON emails(category, deleted)",
        # 只给 /api/emails/{id}/thread 用（INDEXED BY 强制、整索引扫描约 5ms）。
        # subject 打头是故意的：列表查询从不按 subject 等值过滤，规划器不会拿它去抢别的查询。
        "CREATE INDEX IF NOT EXISTS idx_thread_subj ON emails(subject, account, deleted, received_timestamp, id)",
        # 只给话题筛选（server.py::_topic_ids，INDEXED BY 强制）用：话题规则按发件人 / 收件人 / 主题 LIKE 过滤，
        # 只能逐行判断；在这个窄索引里判断不碰正文（整索引扫描约 4–9ms），原来顺着整行走 25–90ms。
        # sender 打头同样是故意的：列表查询不按 sender 等值过滤，规划器不会拿它抢别的查询。
        "CREATE INDEX IF NOT EXISTS idx_topic_meta ON emails(sender, sender_email, subject, recipients, deleted,"
        " unread, received_timestamp, account, folder, category, has_attachment, starred, id)",
        "CREATE INDEX IF NOT EXISTS idx_account     ON emails(account)",
        "CREATE INDEX IF NOT EXISTS idx_folder      ON emails(folder)",
        "CREATE INDEX IF NOT EXISTS idx_starred     ON emails(starred)",
        "CREATE INDEX IF NOT EXISTS idx_deleted     ON emails(deleted)",
        "CREATE INDEX IF NOT EXISTS idx_message_id  ON emails(message_id)",
        "CREATE INDEX IF NOT EXISTS idx_ep_person   ON email_people(person_email)",
        "CREATE INDEX IF NOT EXISTS idx_ep_email    ON email_people(email_id)",
        "CREATE INDEX IF NOT EXISTS idx_todo_status ON todos(status)",
        "CREATE INDEX IF NOT EXISTS idx_todo_email  ON todos(source_email_id)",
        "CREATE INDEX IF NOT EXISTS idx_draft_updated ON drafts(updated_at DESC)",
    ]

    _FTS_SCHEMA = """
    -- 旧 emails_fts（unicode61 整段分词）及其三个同步触发器已删除，由下面的 emails_fts2 取代。
    -- 中文按单字索引的影子表。unicode61 把连续汉字整段当一个词，搜「会议」只能命中
    -- 恰好单独出现「会议」的邮件（只占实际包含该词邮件的极少数）。这里入库前在每个汉字两侧插空格，查询时用相邻字短语。
    -- 分字只能在 Python 做，所以触发器只把变动的 id 记进待处理队列（任何写库方都会被记录），
    -- 由 DBManager._fts2_sync() 在搜索前增量刷新。fts2_ids 记 id→FTS rowid，删除走 rowid 而不扫全表。
    CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts2 USING fts5(
        id UNINDEXED, subject, sender, sender_email, body,
        tokenize='unicode61 remove_diacritics 1'
    );
    CREATE TABLE IF NOT EXISTS emails_fts2_ids (id TEXT PRIMARY KEY, rid INTEGER);
    CREATE TABLE IF NOT EXISTS emails_fts2_pending (id TEXT PRIMARY KEY);
    CREATE TRIGGER IF NOT EXISTS emails_fts2_q2_ins AFTER INSERT ON emails BEGIN
        INSERT INTO emails_fts2_pending(id) SELECT new.id WHERE NOT EXISTS (SELECT 1 FROM emails_fts2_pending WHERE id = new.id);
    END;
    CREATE TRIGGER IF NOT EXISTS emails_fts2_q2_upd AFTER UPDATE OF subject,sender,sender_email,body ON emails
    WHEN old.subject IS NOT new.subject OR old.sender IS NOT new.sender
      OR old.sender_email IS NOT new.sender_email OR old.body IS NOT new.body BEGIN
        INSERT INTO emails_fts2_pending(id) SELECT new.id WHERE NOT EXISTS (SELECT 1 FROM emails_fts2_pending WHERE id = new.id);
    END;
    CREATE TRIGGER IF NOT EXISTS emails_fts2_q2_del AFTER DELETE ON emails BEGIN
        INSERT INTO emails_fts2_pending(id) SELECT old.id WHERE NOT EXISTS (SELECT 1 FROM emails_fts2_pending WHERE id = old.id);
    END;
    """

    def __init__(self, db_path: str):
        # 每实例独立的线程本地连接缓存。写成类属性会让同进程的第二个实例
        # 复用第一个实例的连接，从而静默忽略这里传入的 db_path。
        self._local = threading.local()
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """获取线程本地连接（复用，避免频繁 open/close）"""
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    @contextmanager
    def _conn(self):
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(self._TABLE_SCHEMA)
            existing = {row[1] for row in conn.execute("PRAGMA table_info(emails)")}
            for col, ddl in self._MIGRATIONS:
                if col not in existing:
                    conn.execute(f"ALTER TABLE emails ADD COLUMN {col} {ddl}")
            d_existing = {row[1] for row in conn.execute("PRAGMA table_info(drafts)")}
            for col, ddl in self._DRAFT_MIGRATIONS:
                if col not in d_existing:
                    conn.execute(f"ALTER TABLE drafts ADD COLUMN {col} {ddl}")
            # email_insights 增量列：截止日 / 依据 / 下一步 / 主链接，JSON 存一列，加字段不再改表
            i_existing = {row[1] for row in conn.execute("PRAGMA table_info(email_insights)")}
            if "extra" not in i_existing:
                conn.execute("ALTER TABLE email_insights ADD COLUMN extra TEXT")
            for idx_sql in self._INDEXES:
                conn.execute(idx_sql)
            # 从未 ANALYZE 过的库补一次统计信息（约 0.1s，只在缺 sqlite_stat1 时做）。
            # 没有统计时规划器按文件夹查也会走 idx_deleted_received 全扫（19ms），
            # 有统计后改走 idx_folder（2.4ms）。统计持久化在库里，之后不再重复。
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'").fetchone():
                conn.execute("ANALYZE")
            else:
                # 已有统计的库里新加的索引没有统计行，规划器会按默认估算选错索引；只补这几个。
                _have = {r[0] for r in conn.execute("SELECT DISTINCT idx FROM sqlite_stat1")}
                for _ix in ("idx_acc_folder_recv", "idx_folder_recv", "idx_cov_category", "idx_thread_subj", "idx_topic_meta"):
                    if _ix not in _have:
                        conn.execute("ANALYZE " + _ix)
            # FTS5 初始化
            # 第一版队列触发器用 INSERT OR IGNORE：外层是 UPSERT 时 SQLite 用外层的冲突策略覆盖触发器里的
            # OR IGNORE，同一封邮件在刷队列前被第二次改写就会报 UNIQUE 失败、整条写入回滚（test_message_id 覆盖）。
            # 换成 WHERE NOT EXISTS 写法并改名，这里删掉旧触发器。
            for _old in ("emails_fts2_q_ins", "emails_fts2_q_upd", "emails_fts2_q_del"):
                conn.execute("DROP TRIGGER IF EXISTS %s" % _old)
            conn.executescript(self._FTS_SCHEMA)
            built = conn.execute("SELECT value FROM sync_status WHERE key='fts2_built'").fetchone()
            if not built or built[0] != "1":
                self._fts2_rebuild(conn)
            # 删除旧 emails_fts（大邮箱上可达数百 MB）。触发器必须一起删：表没了触发器还在，
            # 任何写邮件都会报 no such table 而回滚。幂等：每个进程首启执行一次，已删则无事。
            # 旧代码的进程（未重启的 daemon 等）若重新 new DBManager 会把空表建回来并全量回填——部署后须重启 server + daemon。
            for _old in ("emails_fts_insert", "emails_fts_update", "emails_fts_delete"):
                conn.execute("DROP TRIGGER IF EXISTS %s" % _old)
            conn.execute("DROP TABLE IF EXISTS emails_fts")

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #

    @staticmethod
    def _norm_time_field(email: dict) -> str:
        """入库前统一 time 的时区口径，以 received_timestamp 为权威源。

        为什么需要它：三个 extractor 拿到的原始时间都带**错误的**时区标签 ——
        pywin32 的 msg.ReceivedTime 字面是本地时间却标 tzinfo=UTC，str() 出来
        就是 '2026-09-24 11:30:38+00:00'。前端 new Date() 按 UTC 解析再转本地，
        在 UTC+8 下列表时间因此晚 8 小时（权威 11:30，页面渲染 19:30）。

        received_timestamp 走的是 .replace(tzinfo=None).timestamp()，按本地时区
        解释那个字面值，得到的是**正确**的 UTC epoch —— 所以它是权威源
        （在 UTC+8 的库里，全部行与 time 的差值恒为 +8h，零例外）。

        为什么在写入入口统一，而不是在读取出口修：返回邮件的出口至少 5 处，
        其中话题聚合 _rows_from、api_thread、topic_emails、export_emails_csv
        都绕过 _row_to_dict，逐个打补丁会漏，且将来新增查询会再次出错。

        刻意保持「本地偏移」而不转成真 UTC：server.py L919/L2010 与
        core/ai_engine.py 有三处 (time)[:10] 截日期喂 AI，转 UTC 会让本地
        00:00-08:00 收到的邮件日期倒退一天。用本地偏移则字面时间零漂移。

        偏移不硬编码 +08:00，由 astimezone() 让系统给出，跨时区部署自动正确。
        received_timestamp 无效（<=0，如退信）时原样返回，不制造 1970 假时间。
        """
        import datetime as _dt
        raw = email.get("time", "") or ""
        try:
            rts = float(email.get("received_timestamp") or 0)
        except (TypeError, ValueError):
            return raw
        if rts <= 0:
            return raw
        try:
            return (_dt.datetime.fromtimestamp(rts)
                    .astimezone()
                    .isoformat(sep=" ", timespec="seconds"))
        except (OSError, OverflowError, ValueError):
            # Windows 上 fromtimestamp(极小值).astimezone() 会抛
            # OSError(EINVAL)：系统无法给出该时刻的时区偏移（越界同理）。
            # 这个函数在入库主路径上，**绝不能因单封邮件的畸形时间戳
            # 让整轮同步失败** —— 原样返回，把它留作数据质量问题。
            return raw

    def upsert_email(self, email: dict):
        recipients_json = json.dumps(email.get("recipients", []), ensure_ascii=False)
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO emails
                    (id, subject, body, html_body, sender, sender_email, recipients, time,
                     received_timestamp, unread, folder, account,
                     has_attachment, attachments, message_id)
                VALUES
                    (:id, :subject, :body, :html_body, :sender, :sender_email, :recipients, :time,
                     :received_timestamp, :unread, :folder, :account,
                     :has_attachment, :attachments, :message_id)
                ON CONFLICT(id) DO UPDATE SET
                    -- UPSERT-KEEP-LOCAL-20260930：unread/folder 是本地状态（已读、移动都不回写服务器），
                    -- 冲突时保留本地值，只刷新服务器权威的内容字段。
                    subject=excluded.subject,
                    recipients=excluded.recipients,
                    html_body=COALESCE(excluded.html_body, html_body),
                    message_id=COALESCE(NULLIF(excluded.message_id,''), message_id)
            """, {
                "id":                 email["id"],
                "subject":            email.get("subject", ""),
                "body":               email.get("body", ""),
                "html_body":          email.get("html_body", ""),
                "sender":             email.get("sender", ""),
                "sender_email":       email.get("sender_email", ""),
                "recipients":         recipients_json,
                "time":               self._norm_time_field(email),
                "received_timestamp": email.get("received_timestamp", 0.0),
                "unread":             1 if email.get("unread") else 0,
                "folder":             email.get("folder", ""),
                "account":            email.get("account", ""),
                "has_attachment":     1 if email.get("has_attachment") else 0,
                "attachments":        json.dumps(email.get("attachments", []), ensure_ascii=False),
                "message_id":         self.norm_message_id(email.get("message_id", "")),
            })

    def insert_email_if_new(self, email: dict) -> bool:
        """仅在邮件不存在时插入，返回 True 表示新插入，False 表示已存在（被忽略）。
        不做 UPDATE，比 upsert_email 更轻量，适用于同步时快速去重入库。"""
        recipients_json = json.dumps(email.get("recipients", []), ensure_ascii=False)
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT OR IGNORE INTO emails
                    (id, subject, body, html_body, sender, sender_email, recipients, time,
                     received_timestamp, unread, folder, account,
                     has_attachment, attachments, message_id)
                VALUES
                    (:id, :subject, :body, :html_body, :sender, :sender_email, :recipients, :time,
                     :received_timestamp, :unread, :folder, :account,
                     :has_attachment, :attachments, :message_id)
            """, {
                "id":                 email["id"],
                "subject":            email.get("subject", ""),
                "body":               email.get("body", ""),
                "html_body":          email.get("html_body", ""),
                "sender":             email.get("sender", ""),
                "sender_email":       email.get("sender_email", ""),
                "recipients":         recipients_json,
                "time":               self._norm_time_field(email),
                "received_timestamp": email.get("received_timestamp", 0.0),
                "unread":             1 if email.get("unread") else 0,
                "folder":             email.get("folder", ""),
                "account":            email.get("account", ""),
                "has_attachment":     1 if email.get("has_attachment") else 0,
                "attachments":        json.dumps(email.get("attachments", []), ensure_ascii=False),
                "message_id":         self.norm_message_id(email.get("message_id", "")),
            })
            return cur.rowcount > 0

    # ------------------------------------------------------------------ #
    #  message_id：跨后端（Outlook / POP3 / IMAP）去重依据
    # ------------------------------------------------------------------ #

    @staticmethod
    def norm_message_id(value: str) -> str:
        """归一化 Message-ID：去尖括号、去空白、转小写；缺失返回空串。

        必须与 core.pop3_extractor 的口径一致，否则同一封信在两条取件路径下
        算出的键不同，去重就会漏。
        """
        mid = (value or "").strip()
        if mid.startswith("<") and mid.endswith(">"):
            mid = mid[1:-1]
        return mid.strip().lower()

    def set_message_id(self, email_id: str, message_id: str) -> bool:
        """回填单行的 message_id；写入空值视为无操作。"""
        mid = self.norm_message_id(message_id)
        if not mid:
            return False
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE emails SET message_id=? WHERE id=?", (mid, email_id))
            return cur.rowcount > 0

    def find_id_by_message_id(self, message_id: str, account: str | None = None) -> str | None:
        """按 message_id 找已存在的行 id（用于新后端入库前查重）。"""
        mid = self.norm_message_id(message_id)
        if not mid:
            return None
        sql = "SELECT id FROM emails WHERE message_id=?"
        args: list = [mid]
        if account:
            sql += " AND account=?"
            args.append(account)
        with self._conn() as conn:
            row = conn.execute(sql + " LIMIT 1", args).fetchone()
        return row["id"] if row else None

    def count_missing_message_id(self, account: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM emails WHERE (message_id IS NULL OR message_id='')"
        args: list = []
        if account:
            sql += " AND account=?"
            args.append(account)
        with self._conn() as conn:
            return conn.execute(sql, args).fetchone()[0]

    def ids_missing_message_id(self, account: str | None = None, limit: int = 500,
                               offset: int = 0) -> list[str]:
        """取一批缺 message_id 的行 id（按收信时间新→旧，先补最可能与增量重叠的）。

        offset 用于顺序扫描：取不到值的行会一直留在结果集里，调用方必须靠偏移
        往后走，否则每轮都在同一段打转。
        """
        sql = "SELECT id FROM emails WHERE (message_id IS NULL OR message_id='')"
        args: list = []
        if account:
            sql += " AND account=?"
            args.append(account)
        sql += " ORDER BY received_timestamp DESC LIMIT ? OFFSET ?"
        args.extend([int(limit), int(offset)])
        with self._conn() as conn:
            return [r["id"] for r in conn.execute(sql, args)]

    def upsert_people(self, email: dict):
        ts = email.get("received_timestamp", 0.0)
        email_id = email["id"]
        participants: list[tuple[str, str, str]] = []

        sender_email = (email.get("sender_email") or "").strip()
        sender_name  = (email.get("sender") or "").strip()
        if sender_email:
            participants.append((sender_name, sender_email, "sender"))

        for r in email.get("recipients") or []:
            addr = (r.get("email") or "").strip()
            name = (r.get("name") or "").strip()
            role = r.get("type", "to")
            if addr:
                participants.append((name, addr, role))

        if not participants:
            return

        with self._conn() as conn:
            for name, addr, role in participants:
                conn.execute("""
                    INSERT INTO people(email, name, last_seen, email_count)
                    VALUES(?, ?, ?, 1)
                    ON CONFLICT(email) DO UPDATE SET
                        name       = CASE WHEN excluded.name != '' THEN excluded.name ELSE name END,
                        last_seen  = MAX(last_seen, excluded.last_seen),
                        email_count = email_count + 1
                """, (addr, name, ts))
                conn.execute("""
                    INSERT OR IGNORE INTO email_people(email_id, person_email, role)
                    VALUES(?, ?, ?)
                """, (email_id, addr, role))

    def bulk_update_category(self, email_ids: list[str], category: str):
        if not email_ids:
            return 0
        with self._conn() as conn:
            conn.executemany(
                "UPDATE emails SET category=? WHERE id=?",
                [(category, eid) for eid in email_ids],
            )
        return len(email_ids)

    def star_email(self, email_id: str, starred: bool):
        with self._conn() as conn:
            conn.execute("UPDATE emails SET starred=? WHERE id=?", (1 if starred else 0, email_id))

    def mark_read(self, email_id: str, read: bool):
        with self._conn() as conn:
            conn.execute("UPDATE emails SET unread=? WHERE id=?", (0 if read else 1, email_id))

    def update_html_body(self, email_id: str, html_body: str):
        """回填/更新富文本正文（供 backfill 脚本或重新提取使用）"""
        with self._conn() as conn:
            conn.execute("UPDATE emails SET html_body=? WHERE id=?", (html_body, email_id))

    def soft_delete(self, email_id: str, deleted: bool = True):
        with self._conn() as conn:
            conn.execute("UPDATE emails SET deleted=? WHERE id=?", (1 if deleted else 0, email_id))

    def move_folder(self, email_id: str, folder: str):
        with self._conn() as conn:
            conn.execute("UPDATE emails SET folder=? WHERE id=?", (folder, email_id))

    def update_ai_fields(self, email_id: str, category: str, summary: str, action_items):
        with self._conn() as conn:
            conn.execute("""
                UPDATE emails SET category=?, summary=?, action_items=?
                WHERE id=?
            """, (
                category,
                summary,
                json.dumps(action_items, ensure_ascii=False) if isinstance(action_items, list) else action_items,
                email_id,
            ))

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def exists(self, email_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM emails WHERE id=?", (email_id,)).fetchone()
            return row is not None

    # ---- 结构化 AI 分析缓存（email_insights）----
    def get_insight(self, email_id: str) -> dict | None:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM email_insights WHERE email_id=?", (email_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("points", "todos", "drafts"):
            try:
                d[k] = json.loads(d.get(k) or "[]")
            except Exception:
                d[k] = []
        try:
            ex = json.loads(d.pop("extra", None) or "{}")
        except Exception:
            ex = {}
        for k in self.INSIGHT_EXTRA:
            d[k] = str((ex or {}).get(k) or "")
        a = (ex or {}).get("actions")
        d["actions"] = [x for x in a if isinstance(x, dict) and x.get("text")] if isinstance(a, list) else []
        _v = (ex or {}).get("v2")
        d["v2"] = _v if isinstance(_v, dict) else None
        d["level"] = str((ex or {}).get("level") or "")
        return d

    INSIGHT_EXTRA = ("due", "due_src", "next", "link", "headline", "meta")

    # ---- §10：行动项状态 / 纠正 / 系列拆分 ----
    @staticmethod
    def akey(text) -> str:
        return "".join(str(text or "").split()).lower()[:160]

    def set_action_state(self, email_id: str, text: str, state):
        import time as _t
        k = self.akey(text)
        with self._conn() as conn:
            if not state:
                conn.execute("DELETE FROM action_states WHERE email_id=? AND akey=?", (email_id, k))
            else:
                conn.execute("INSERT INTO action_states(email_id, akey, text, state, created_at) VALUES (?,?,?,?,?)"
                             " ON CONFLICT(email_id, akey) DO UPDATE SET state=excluded.state, created_at=excluded.created_at",
                             (email_id, k, text, state, _t.time()))

    def action_states(self, ids) -> dict:
        ids = [i for i in ([ids] if isinstance(ids, str) else ids or []) if i]
        out = {}
        with self._conn() as conn:
            for k in range(0, len(ids), 500):
                part = ids[k:k + 500]
                for r in conn.execute("SELECT email_id, akey, state FROM action_states WHERE email_id IN (%s)" % ",".join("?" * len(part)), part):
                    out.setdefault(r[0], {})[r[1]] = r[2]
        return out

    def add_correction(self, email_id, sender, pattern, label, to_priority) -> int:
        import time as _t
        with self._conn() as conn:
            conn.execute("INSERT INTO ai_corrections(email_id, sender, pattern, label, to_priority, created_at) VALUES (?,?,?,?,?,?)"
                         " ON CONFLICT(sender, pattern) DO UPDATE SET to_priority=excluded.to_priority, email_id=excluded.email_id, label=excluded.label, created_at=excluded.created_at",
                         (email_id, sender, pattern, label, to_priority, _t.time()))
            return conn.execute("SELECT id FROM ai_corrections WHERE sender=? AND pattern=?", (sender, pattern)).fetchone()[0]

    def list_corrections(self) -> list:
        with self._conn() as conn:
            return [dict(zip(("id", "email_id", "sender", "pattern", "label", "to_priority", "created_at"), r))
                    for r in conn.execute("SELECT id, email_id, sender, pattern, label, to_priority, created_at FROM ai_corrections ORDER BY created_at DESC")]

    def find_correction(self, sender, pattern):
        with self._conn() as conn:
            r = conn.execute("SELECT id, to_priority FROM ai_corrections WHERE sender=? AND pattern=?", (sender, pattern)).fetchone()
        return {"id": r[0], "to_priority": r[1]} if r else None

    def remove_correction(self, cid: int) -> bool:
        with self._conn() as conn:
            return conn.execute("DELETE FROM ai_corrections WHERE id=?", (int(cid),)).rowcount > 0

    def set_insight_priority(self, email_id: str, priority: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE email_insights SET priority=? WHERE email_id=?", (priority, email_id))

    def get_compare(self, email_id: str):
        with self._conn() as conn:
            r = conn.execute("SELECT prev_id, hash, data, created_at FROM series_compare WHERE email_id=?", (email_id,)).fetchone()
        if not r:
            return None
        try:
            d = json.loads(r[2] or "{}")
        except Exception:
            d = {}
        return {"prev_id": r[0], "hash": r[1], "data": d, "created_at": r[3]}

    def save_compare(self, email_id, prev_id, h, data) -> None:
        import time as _t
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO series_compare(email_id, prev_id, hash, data, created_at) VALUES (?,?,?,?,?)",
                         (email_id, prev_id, h, json.dumps(data, ensure_ascii=False), _t.time()))

    def add_split(self, skey: str, label: str = "") -> None:
        import time as _t
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO series_splits(skey, label, created_at) VALUES (?,?,?)", (skey, label, _t.time()))

    def remove_split(self, skey: str) -> bool:
        with self._conn() as conn:
            return conn.execute("DELETE FROM series_splits WHERE skey=?", (skey,)).rowcount > 0

    def list_splits(self) -> list:
        with self._conn() as conn:
            return [dict(zip(("skey", "label", "created_at"), r)) for r in conn.execute("SELECT skey, label, created_at FROM series_splits ORDER BY created_at DESC")]

    def series_rows(self) -> list:
        """系列识别用的轻量全表：未删除邮件的 id / 发件人 / 主题 / 时间 / 文件夹。"""
        with self._conn() as conn:
            return conn.execute("SELECT id, LOWER(COALESCE(sender_email,'')), COALESCE(subject,''), COALESCE(received_timestamp,0), COALESCE(folder,''), COALESCE(account,''), COALESCE(unread,0)"
                                " FROM emails WHERE deleted=0").fetchall()

    # ---- 完成说明 ----
    _NOTE_COLS = ("id", "email_id", "outcome", "note_html", "note_text", "attachments", "links", "deleted", "created_at", "updated_at")

    def _note_row(self, r) -> dict:
        d = dict(zip(self._NOTE_COLS, r))
        for k in ("attachments", "links"):
            try:
                d[k] = json.loads(d.get(k) or "[]")
            except Exception:
                d[k] = []
        return d

    def add_note(self, email_id, outcome, note_html, note_text, attachments, links) -> int:
        import time as _t
        now = _t.time()
        with self._conn() as conn:
            cur = conn.execute("INSERT INTO archive_notes(email_id, outcome, note_html, note_text, attachments, links, deleted, created_at, updated_at)"
                               " VALUES (?,?,?,?,?,?,0,?,?)",
                               (email_id, outcome, note_html, note_text, json.dumps(attachments or [], ensure_ascii=False),
                                json.dumps(links or [], ensure_ascii=False), now, now))
            return cur.lastrowid

    def update_note(self, note_id: int, **kw) -> bool:
        import time as _t
        f = {}
        for k in ("outcome", "note_html", "note_text", "deleted"):
            if k in kw: f[k] = kw[k]
        for k in ("attachments", "links"):
            if k in kw: f[k] = json.dumps(kw[k] or [], ensure_ascii=False)
        if not f:
            return False
        f["updated_at"] = _t.time()
        with self._conn() as conn:
            return conn.execute("UPDATE archive_notes SET %s WHERE id=?" % ", ".join(k + "=?" for k in f), list(f.values()) + [int(note_id)]).rowcount > 0

    def get_note(self, note_id: int) -> dict | None:
        with self._conn() as conn:
            r = conn.execute("SELECT %s FROM archive_notes WHERE id=?" % ",".join(self._NOTE_COLS), (int(note_id),)).fetchone()
        return self._note_row(r) if r else None

    def notes_for(self, email_id: str) -> list:
        with self._conn() as conn:
            rows = conn.execute("SELECT %s FROM archive_notes WHERE email_id=? AND deleted=0 ORDER BY created_at" % ",".join(self._NOTE_COLS), (email_id,)).fetchall()
        return [self._note_row(r) for r in rows]

    def notes_linking(self, email_id: str) -> list:
        """把这封邮件列为「关联邮件」的处理记录（反向关联）。"""
        with self._conn() as conn:
            rows = conn.execute("SELECT %s FROM archive_notes WHERE deleted=0 AND links LIKE ? ORDER BY created_at" % ",".join(self._NOTE_COLS),
                                ('%' + json.dumps(email_id, ensure_ascii=False) + '%',)).fetchall()
        return [n for n in (self._note_row(r) for r in rows) if email_id in (n.get("links") or [])]

    def search_notes(self, q: str, limit: int = 50) -> list:
        """处理记录全文（结果 + 说明文字 + 附件名），返回 email_id 列表（新到旧）。所有词都要命中。"""
        words = [w for w in str(q or "").split() if w][:6]
        if not words:
            return []
        sql = "SELECT email_id FROM archive_notes WHERE deleted=0" + "".join(" AND (COALESCE(note_text,'') || ' ' || COALESCE(outcome,'') || ' ' || COALESCE(attachments,'')) LIKE ?" for _ in words)
        with self._conn() as conn:
            rows = conn.execute(sql + " ORDER BY updated_at DESC LIMIT ?", ["%" + w + "%" for w in words] + [limit]).fetchall()
        out = []
        for (i,) in rows:
            if i not in out: out.append(i)
        return out

    def note_counts(self, ids) -> dict:
        ids = [i for i in (ids or []) if i]
        out = {}
        with self._conn() as conn:
            for k in range(0, len(ids), 500):
                part = ids[k:k + 500]
                for r in conn.execute("SELECT email_id, COUNT(*) FROM archive_notes WHERE deleted=0 AND email_id IN (%s) GROUP BY email_id" % ",".join("?" * len(part)), part):
                    out[r[0]] = r[1]
        return out

    def purge_deleted_notes(self, older_than_s: float = 7 * 86400) -> list:
        """返回被物理删除的 note id（调用方据此删附件目录）。"""
        import time as _t
        with self._conn() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM archive_notes WHERE deleted=1 AND updated_at < ?", (_t.time() - older_than_s,))]
            if ids:
                conn.execute("DELETE FROM archive_notes WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
        return ids

    # ---- 稍后提醒 / 静音告警 ----
    def set_snooze(self, email_id: str, until_ts: float) -> None:
        import time as _t
        with self._conn() as conn:
            conn.execute("INSERT INTO snoozes(email_id, until_ts, created_at) VALUES (?,?,?)"
                         " ON CONFLICT(email_id) DO UPDATE SET until_ts=excluded.until_ts, created_at=excluded.created_at",
                         (email_id, float(until_ts), _t.time()))

    def clear_snooze(self, email_id: str) -> bool:
        with self._conn() as conn:
            return conn.execute("DELETE FROM snoozes WHERE email_id=?", (email_id,)).rowcount > 0

    def get_snoozes(self, ids=None) -> dict:
        """email_id → until_ts。ids 为空时返回全部。"""
        with self._conn() as conn:
            if ids is None:
                return {r[0]: r[1] for r in conn.execute("SELECT email_id, until_ts FROM snoozes")}
            ids = [i for i in ids if i]
            out = {}
            for k in range(0, len(ids), 500):
                part = ids[k:k + 500]
                for r in conn.execute("SELECT email_id, until_ts FROM snoozes WHERE email_id IN (%s)" % ",".join("?" * len(part)), part):
                    out[r[0]] = r[1]
            return out

    def add_mute(self, scope: str, key: str, label: str, until_ts: float) -> int:
        import time as _t
        with self._conn() as conn:
            conn.execute("INSERT INTO alert_mutes(scope, key, label, until_ts, created_at) VALUES (?,?,?,?,?)"
                         " ON CONFLICT(scope, key) DO UPDATE SET label=excluded.label, until_ts=excluded.until_ts, created_at=excluded.created_at",
                         (scope, key, label or "", float(until_ts), _t.time()))
            return conn.execute("SELECT id FROM alert_mutes WHERE scope=? AND key=?", (scope, key)).fetchone()[0]

    def remove_mute(self, mute_id: int) -> bool:
        with self._conn() as conn:
            return conn.execute("DELETE FROM alert_mutes WHERE id=?", (int(mute_id),)).rowcount > 0

    def list_mutes(self, active_only: bool = True) -> list:
        import time as _t
        sql = "SELECT id, scope, key, label, until_ts, created_at FROM alert_mutes"
        args = ()
        if active_only:
            sql += " WHERE until_ts > ?"; args = (_t.time(),)
        with self._conn() as conn:
            return [dict(zip(("id", "scope", "key", "label", "until_ts", "created_at"), r)) for r in conn.execute(sql + " ORDER BY until_ts", args)]

    def get_insights_brief(self, ids) -> dict:
        """今日分拣用：一次取多封的 priority / summary / due / next / link，不解析 points/drafts。"""
        ids = [i for i in (ids or []) if i]
        out = {}
        if not ids:
            return out
        with self._conn() as conn:
            for k in range(0, len(ids), 500):
                part = ids[k:k + 500]
                for r in conn.execute("SELECT email_id, priority, summary, extra FROM email_insights WHERE email_id IN (%s)"
                                      % ",".join("?" * len(part)), part):
                    try:
                        ex = json.loads(r[3] or "{}")
                    except Exception:
                        ex = {}
                    acts = (ex or {}).get("actions")
                    out[r[0]] = {"priority": r[1], "summary": r[2] or "", **{x: str((ex or {}).get(x) or "") for x in self.INSIGHT_EXTRA},
                                 "actions": [a for a in acts if isinstance(a, dict) and a.get("text")] if isinstance(acts, list) else [],
                                 "level": str((ex or {}).get("level") or "")}
        return out

    def save_insight(self, email_id: str, ins: dict, body_hash: str = "", model: str = "") -> None:
        import time as _t
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO email_insights(email_id, priority, summary, points, todos, drafts, body_hash, model, created_at, extra)"
                " VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(email_id) DO UPDATE SET priority=excluded.priority,"
                " summary=excluded.summary, points=excluded.points, todos=excluded.todos, drafts=excluded.drafts,"
                " body_hash=excluded.body_hash, model=excluded.model, created_at=excluded.created_at, extra=excluded.extra",
                (email_id, ins.get("priority"), ins.get("summary"),
                 json.dumps(ins.get("points") or [], ensure_ascii=False),
                 json.dumps(ins.get("todos") or [], ensure_ascii=False),
                 json.dumps(ins.get("drafts") or [], ensure_ascii=False),
                 body_hash, model, _t.time(),
                 json.dumps(dict({k: ins.get(k) or "" for k in self.INSIGHT_EXTRA}, actions=ins.get("actions") or [],
                                  level=ins.get("level") or "", v2=ins.get("v2") or None), ensure_ascii=False)))

    def get_insight_priorities(self, ids) -> dict:
        ids = [i for i in (ids or []) if i]
        if not ids:
            return {}
        out = {}
        with self._conn() as conn:
            for k in range(0, len(ids), 500):
                part = ids[k:k + 500]
                for r in conn.execute("SELECT email_id, priority FROM email_insights WHERE email_id IN (%s)"
                                      % ",".join("?" * len(part)), part):
                    out[r[0]] = r[1]
        return out

    def get_email_by_id(self, email_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
            return self._row_to_dict(row) if row else None

    def get_unread_count_by_folder(self, account: str | None = None) -> dict:
        sql = "SELECT folder, COUNT(*) FROM emails WHERE unread=1 AND deleted=0"
        params = []
        if account:
            # account 可以是单个账户（老调用方）或账户列表（按邮箱标签筛选时）
            if isinstance(account, (list, tuple, set)):
                _accs = [a for a in account if a]
                if _accs:
                    sql += " AND account IN (%s)" % ",".join("?" * len(_accs))
                    params.extend(_accs)
            else:
                sql += " AND account=?"
                params.append(account)
        sql += " GROUP BY folder"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return {r[0]: r[1] for r in rows if r[0]}

    def get_emails_paginated(self, page: int = 0, page_size: int = 50,
                              account: str | None = None,
                              folder: str | None = None,
                              starred: bool | None = None,
                              unread_only: bool = False,
                              include_deleted: bool = False,
                              deleted_only: bool = False,
                              exclude_ids=None,
                              archived: bool | None = None, tag: str | None = None) -> list[dict]:
        sql = "SELECT * FROM emails WHERE 1=1"
        params: list = []
        if deleted_only:
            # 只看回收站。include_deleted 的语义是"也包含已删除"，
            # 不能用它表达"只要已删除"。
            sql += " AND deleted=1"
        elif not include_deleted:
            sql += " AND deleted=0"
        if archived is True:
            sql += " AND archived=1"
        elif archived is False:
            sql += " AND (archived=0 OR archived IS NULL)"
        if tag:
            sql += " AND tags LIKE ?"
            params.append('%"' + tag + '"%')
        if account:
            # account 可以是单个账户（老调用方）或账户列表（按邮箱标签筛选时）
            if isinstance(account, (list, tuple, set)):
                _accs = [a for a in account if a]
                if _accs:
                    sql += " AND account IN (%s)" % ",".join("?" * len(_accs))
                    params.extend(_accs)
            else:
                sql += " AND account=?"
                params.append(account)
        if folder:
            sql += " AND folder=?"
            params.append(folder)
        if starred is True:
            sql += " AND starred=1"
        if unread_only:
            sql += " AND unread=1"
        if exclude_ids:
            sql += " AND id NOT IN (%s)" % ",".join("?" * len(exclude_ids))
            params += list(exclude_ids)
        sql += " ORDER BY received_timestamp DESC LIMIT ? OFFSET ?"
        params += [page_size, page * page_size]
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_dict(r) for r in rows]

    # ---- 中文按单字 FTS（emails_fts2）----
    _CJK_RE = __import__("re").compile(r"([\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])")
    _TOKEN_RE = __import__("re").compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+|[A-Za-z0-9_.@-]+")

    @classmethod
    def _cjk_seg(cls, text) -> str:
        return cls._CJK_RE.sub(r" \1 ", text or "")

    def _fts2_index_rows(self, conn, rows) -> None:
        for r in rows:
            cur = conn.execute("INSERT INTO emails_fts2(id, subject, sender, sender_email, body) VALUES (?,?,?,?,?)",
                               (r[0], self._cjk_seg(r[1]), self._cjk_seg(r[2]), r[3] or "", self._cjk_seg(r[4])))
            conn.execute("INSERT OR REPLACE INTO emails_fts2_ids(id, rid) VALUES (?,?)", (r[0], cur.lastrowid))

    def _fts2_rebuild(self, conn) -> int:
        """整表重建（首次部署 / 新库）。约 8s / 1.5 万封，写锁期间其他写入会等待（busy_timeout）。"""
        conn.execute("DELETE FROM emails_fts2")
        conn.execute("DELETE FROM emails_fts2_ids")
        conn.execute("DELETE FROM emails_fts2_pending")
        n = 0
        cur = conn.execute("SELECT id, subject, sender, sender_email, body FROM emails")
        while True:
            rows = cur.fetchmany(500)
            if not rows:
                break
            self._fts2_index_rows(conn, rows); n += len(rows)
        conn.execute("INSERT OR REPLACE INTO sync_status(key, value) VALUES ('fts2_built', '1')")
        return n

    def _fts2_sync(self, max_rows: int = 5000) -> int:
        """把待处理队列里的邮件刷进 emails_fts2；每次最多 max_rows 封。失败不影响调用方。"""
        try:
            with self._conn() as conn:
                ids = [r[0] for r in conn.execute("SELECT id FROM emails_fts2_pending LIMIT ?", (max_rows,))]
                for k in range(0, len(ids), 300):
                    part = ids[k:k + 300]; qm = ",".join("?" * len(part))
                    for (rid,) in conn.execute("SELECT rid FROM emails_fts2_ids WHERE id IN (%s)" % qm, part).fetchall():
                        conn.execute("DELETE FROM emails_fts2 WHERE rowid=?", (rid,))
                    conn.execute("DELETE FROM emails_fts2_ids WHERE id IN (%s)" % qm, part)
                    rows = conn.execute("SELECT id, subject, sender, sender_email, body FROM emails WHERE id IN (%s)" % qm, part).fetchall()
                    self._fts2_index_rows(conn, rows)
                    conn.execute("DELETE FROM emails_fts2_pending WHERE id IN (%s)" % qm, part)
                return len(ids)
        except Exception:
            return -1

    @classmethod
    def _fts2_query(cls, kw: str, joiner: str = " AND ") -> str:
        parts = []
        for t in cls._TOKEN_RE.findall(kw or ""):
            parts.append('"%s"' % (" ".join(t) if cls._CJK_RE.match(t) else t))
        return joiner.join(parts)

    def _fts_rows(self, table: str, fts_query: str, limit: int):
        with self._conn() as conn:
            return conn.execute("""
                SELECT e.* FROM (
                    SELECT e2.rowid AS rid, f.rank AS r, e2.received_timestamp AS ts
                    FROM %s f JOIN emails e2 ON e2.id = f.id
                    WHERE %s MATCH ? AND e2.deleted = 0
                    ORDER BY f.rank, e2.received_timestamp DESC
                    LIMIT ?
                ) t JOIN emails e ON e.rowid = t.rid
                ORDER BY t.r, t.ts DESC
            """ % (table, table), (fts_query, limit)).fetchall()

    def search_by_keyword(self, kw: str, limit: int = 100) -> list[dict]:  #fts-legacy-removed
        # 中文按单字（emails_fts2）：多个词之间 AND（都要出现）；AND 无结果再放宽成 OR；仍无结果走 LIKE 回退。
        # 旧 emails_fts 引擎与 sync_status.fts_engine=legacy 回退开关已删除。
        self._fts2_sync()
        try:
            q_and = self._fts2_query(kw)
            if q_and:
                rows = self._fts_rows("emails_fts2", q_and, limit)
                if not rows and " AND " in q_and:
                    rows = self._fts_rows("emails_fts2", self._fts2_query(kw, " OR "), limit)
                if rows:
                    return [self._row_to_dict(r) for r in rows]
        except Exception:
            pass
        return self._search_like(kw, limit)

    def _search_like(self, kw: str, limit: int = 100) -> list[dict]:
        # 回退：LIKE 搜索
        like = f"%{kw}%"
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM emails
                WHERE (subject LIKE ? OR sender LIKE ? OR sender_email LIKE ?)
                  AND deleted = 0
                ORDER BY received_timestamp DESC LIMIT ?
            """, (like, like, like, limit)).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def search_advanced(self, ops: dict, text: str = "", limit: int = 100) -> list[dict]:
        """搜索操作符：from/to/subject/body/has/before/after + 剩余自由文本。"""
        import datetime
        sql = "SELECT * FROM emails WHERE deleted=0"
        params: list = []
        for kw in ops.get("from", []):
            sql += " AND (sender LIKE ? OR sender_email LIKE ?)"; params += [f"%{kw}%", f"%{kw}%"]
        for kw in ops.get("to", []):
            sql += " AND recipients LIKE ?"; params.append(f"%{kw}%")
        for kw in ops.get("subject", []):
            sql += " AND subject LIKE ?"; params.append(f"%{kw}%")
        for kw in ops.get("body", []):
            sql += " AND body LIKE ?"; params.append(f"%{kw}%")
        for kw in ops.get("has", []):
            if str(kw).lower() in ("attachment", "attachments", "att", "附件", "yes", "1", "true"):
                sql += " AND has_attachment=1"
        def _ts(d):
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
                try: return datetime.datetime.strptime(d.strip(), fmt).timestamp()
                except Exception: pass
            return None
        for d in ops.get("after", []):
            ts = _ts(d)
            if ts: sql += " AND received_timestamp>=?"; params.append(ts)
        for d in ops.get("before", []):
            ts = _ts(d)
            if ts: sql += " AND received_timestamp<?"; params.append(ts)
        if text:
            sql += " AND (subject LIKE ? OR body LIKE ? OR sender LIKE ?)"; params += [f"%{text}%"] * 3
        sql += " ORDER BY received_timestamp DESC LIMIT ?"; params.append(limit)
        with self._conn() as conn:
            return [self._row_to_dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_emails_without_ai(self, limit: int = 200,
                               skip_accounts: list | None = None,
                               skip_folders: list | None = None) -> list[dict]:
        """返回尚未有 AI 摘要的真实邮件（排除自动化账户/文件夹）"""
        sql = "SELECT * FROM emails WHERE (summary IS NULL OR summary='') AND deleted=0"
        params: list = []

        if skip_accounts:
            placeholders = ",".join("?" * len(skip_accounts))
            sql += f" AND account NOT IN ({placeholders})"
            params.extend(skip_accounts)

        if skip_folders:
            for f in skip_folders:
                sql += " AND folder NOT LIKE ?"
                params.append(f"%{f}%")

        sql += " ORDER BY received_timestamp DESC LIMIT ?"
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 人物索引查询
    # ------------------------------------------------------------------ #

    def get_display_name(self, addr: str) -> str:
        """按地址找对方的真实显示名。同一地址有的邮件头带名字、有的只有地址，
        起草回信时要靠它称呼对方（people.name 多数也只是地址，指望不上）。"""
        addr = (addr or "").strip()
        if not addr:
            return ""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT sender FROM emails WHERE sender_email=? COLLATE NOCASE AND sender<>'' "
                "AND sender NOT LIKE '%@%' ORDER BY received_timestamp DESC LIMIT 1", (addr,)).fetchone()
            if row:
                return row[0]
            # 对方没发过带名字的邮件：再看别人（含我）发给他的邮件收件人列表
            for (rc,) in conn.execute(
                    "SELECT recipients FROM emails WHERE recipients LIKE ? "
                    "ORDER BY received_timestamp DESC LIMIT 20", ("%" + addr + "%",)):
                try:
                    for x in json.loads(rc or "[]"):
                        n = str(x.get("name") or "").strip()
                        if str(x.get("email") or "").strip().lower() == addr.lower() and n and "@" not in n:
                            return n
                except Exception:
                    continue
        return ""

    def get_people(self, kw: str = "", limit: int = 200) -> list[dict]:
        if kw:
            like = f"%{kw}%"
            sql = "SELECT * FROM people WHERE name LIKE ? OR email LIKE ? ORDER BY email_count DESC LIMIT ?"
            params = (like, like, limit)
        else:
            sql = "SELECT * FROM people ORDER BY email_count DESC LIMIT ?"
            params = (limit,)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_emails_by_person(self, person_email: str,
                              start_ts: float = 0, end_ts: float = 0,
                              limit: int = 500) -> list[dict]:
        sql = """
            SELECT DISTINCT e.*
            FROM emails e
            JOIN email_people ep ON ep.email_id = e.id
            WHERE ep.person_email = ?
              AND e.deleted = 0
        """
        params: list = [person_email]
        if start_ts:
            sql += " AND e.received_timestamp >= ?"
            params.append(start_ts)
        if end_ts:
            sql += " AND e.received_timestamp <= ?"
            params.append(end_ts)
        sql += " ORDER BY e.received_timestamp ASC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def get_all_ids(self) -> set:
        with self._conn() as conn:
            rows = conn.execute("SELECT id FROM emails").fetchall()
            return {r[0] for r in rows}

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]

    def count_filtered(self, account: str | None = None,
                       folder: str | None = None,
                       starred: bool | None = None,
                       unread_only: bool = False,
                       include_deleted: bool = False,
                       deleted_only: bool = False,
                       archived: bool | None = None, tag: str | None = None,
                       exclude_ids=None) -> int:
        """按与 get_emails_paginated 相同的过滤条件统计总数，供分页使用。

        exclude_ids 必须与 get_emails_paginated 保持同一语义：列表排除了哪些 id，
        总数就得排除同一批。少了这个参数，免打扰（muted）场景下 total 会比实际
        可见条数多，分页和"还有几封"的提示都会骗人。
        """
        sql = "SELECT COUNT(*) FROM emails WHERE 1=1"
        params: list = []
        if deleted_only:
            # 只看回收站。include_deleted 的语义是"也包含已删除"，
            # 不能用它表达"只要已删除"。
            sql += " AND deleted=1"
        elif not include_deleted:
            sql += " AND deleted=0"
        if archived is True:
            sql += " AND archived=1"
        elif archived is False:
            sql += " AND (archived=0 OR archived IS NULL)"
        if tag:
            sql += " AND tags LIKE ?"
            params.append('%"' + tag + '"%')
        if account:
            # account 可以是单个账户（老调用方）或账户列表（按邮箱标签筛选时）
            if isinstance(account, (list, tuple, set)):
                _accs = [a for a in account if a]
                if _accs:
                    sql += " AND account IN (%s)" % ",".join("?" * len(_accs))
                    params.extend(_accs)
            else:
                sql += " AND account=?"
                params.append(account)
        if folder:
            sql += " AND folder=?"
            params.append(folder)
        if starred is True:
            sql += " AND starred=1"
        if unread_only:
            sql += " AND unread=1"
        if exclude_ids:
            _ex = list(exclude_ids)
            sql += " AND id NOT IN (%s)" % ",".join("?" * len(_ex))
            params += _ex
        with self._conn() as conn:
            return conn.execute(sql, params).fetchone()[0]

    def get_distinct_categories(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT DISTINCT category FROM emails WHERE category IS NOT NULL AND category!='' AND deleted=0").fetchall()
            return [r[0] for r in rows if r[0]]

    def get_accounts(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT DISTINCT account FROM emails ORDER BY account").fetchall()
            return [r[0] for r in rows if r[0]]

    def get_folders(self, account: str | None = None) -> list[str]:
        if account:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT folder FROM emails WHERE account=? ORDER BY folder",
                    (account,)
                ).fetchall()
        else:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT folder FROM emails ORDER BY folder"
                ).fetchall()
        return [r[0] for r in rows if r[0]]

    # ------------------------------------------------------------------ #
    # 同步状态
    # ------------------------------------------------------------------ #

    def set_sync_status(self, key: str, value: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sync_status(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value)
            )

    def get_sync_status(self, key: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM sync_status WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    # ------------------------------------------------------------------ #
    # 待办管理
    # ------------------------------------------------------------------ #

    def add_todo(self, content: str, source_email_id: str = None,
                 priority: str = "normal", due_date: str = None, note: str = None) -> int:
        now = __import__("time").time()
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO todos(content, source_email_id, status, priority, due_date, note, created_at, updated_at)
                VALUES(?, ?, 'pending', ?, ?, ?, ?, ?)
            """, (content, source_email_id, self.norm_priority(priority), due_date, note, now, now))
            return cur.lastrowid

    # ---- TODO-QUALITY-20260930：AI 来源待办的质量闸 ----
    _TODO_NON_ITEM = __import__("re").compile(
        r"(无需(采取)?(任何)?行动|仅(作)?知会|无(需要跟进的)?待办|没有(需要)?(跟进的)?待办|"
        r"无需提取|不需要(采取)?(任何)?行动|no action (is )?(required|needed)|^根据邮件(摘要|内容)[，,：:])",
        __import__("re").I)
    _TODO_ABS_DATE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{1,2}:\d{2}(:\d{2})?)?$")

    @staticmethod
    def norm_priority(p) -> str:
        s = str(p or "").strip().lower()
        if s in ("high", "h", "高", "紧急", "urgent", "重要", "p0", "p1", "1"):
            return "high"
        if s in ("low", "l", "低", "p3", "3"):
            return "low"
        return "normal"

    @classmethod
    def _todo_key(cls, content: str) -> str:
        import re as _re
        return _re.sub(r"[\s\W_]+", "", (content or "").lower())

    def todo_exists(self, content: str, source_email_id: str = None) -> bool:
        """同一封邮件下是否已有「归一化后相同或互相包含」的待办（空白/标点差异视为同一条）。"""
        key = self._todo_key(content)
        if not key:
            return True
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT content FROM todos WHERE IFNULL(source_email_id,'')=IFNULL(?,'')",
                (source_email_id,)).fetchall()
        for (c,) in rows:
            k = self._todo_key(c)
            if k == key or (len(key) >= 8 and len(k) >= 8 and (key in k or k in key)):
                return True
        return False

    def add_todo_checked(self, content: str, source_email_id: str = None,
                         priority: str = "normal", due_date: str = None):
        """AI 来源待办的唯一写入口。返回新 id；被过滤或重复时返回 None。"""
        content = (content or "").strip()
        if len(content) < 2 or self._TODO_NON_ITEM.search(content):
            return None
        if self.todo_exists(content, source_email_id):
            return None
        note = None
        d = (str(due_date).strip() if due_date else "")
        if d and not self._TODO_ABS_DATE.match(d):
            note, d = "截止（原文，未换算）：" + d, ""
        return self.add_todo(content, source_email_id=source_email_id,
                             priority=self.norm_priority(priority), due_date=d or None, note=note)

    def import_action_items(self, email_id: str) -> int:
        email = self.get_email_by_id(email_id)
        if not email:
            return 0
        items = email.get("action_items") or []
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except Exception:
                items = []
        count = 0
        for item in items:
            content = str(item).strip()
            if not content:
                continue
            if self.add_todo_checked(content, source_email_id=email_id):   # TODO-QUALITY-20260930
                count += 1
        return count

    def get_emails_for_batch(self, days: int = 7, accounts: list = None, limit: int = 200) -> list:
        import time as _t
        cutoff = _t.time() - max(1, int(days)) * 86400
        sql = "SELECT id, subject, body, account, action_items, received_timestamp FROM emails WHERE deleted=0 AND received_timestamp >= ?"
        params = [cutoff]
        if accounts:
            ph = ",".join("?" * len(accounts))
            sql += " AND account IN (%s)" % ph
            params.extend(accounts)
        sql += " ORDER BY received_timestamp DESC LIMIT ?"
        params.append(int(limit))
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def add_todos_dedup(self, contents, source_email_id: str = None) -> int:
        added = 0
        for content in contents:
            content = (content or "").strip()
            if not content:
                continue
            if self.add_todo_checked(content, source_email_id=source_email_id):   # TODO-QUALITY-20260930
                added += 1
        return added

    def update_todo(self, todo_id: int, **kwargs):
        allowed = {"content", "status", "priority", "due_date", "note"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        fields["updated_at"] = __import__("time").time()
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [todo_id]
        with self._conn() as conn:
            conn.execute(f"UPDATE todos SET {set_clause} WHERE id=?", values)

    def delete_todo(self, todo_id: int):
        with self._conn() as conn:
            conn.execute("DELETE FROM todos WHERE id=?", (todo_id,))

    def get_todos(self, status: str = None, limit: int = 500) -> list[dict]:
        if status:
            sql = "SELECT * FROM todos WHERE status=? ORDER BY created_at DESC LIMIT ?"
            params = (status, limit)
        else:
            sql = "SELECT * FROM todos ORDER BY created_at DESC LIMIT ?"
            params = (limit,)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 草稿箱
    # ------------------------------------------------------------------ #
    # 只存待发内容，不参与同步、去重与向量索引。发送动作不在本层，
    # 由前端调用 /api/emails/send 后再删草稿——保证「发送」始终是人的动作。

    _DRAFT_FIELDS = ("to_addr", "cc_addr", "subject", "body",
                     "is_html", "account", "reply_to_id", "source",
                     "imap_folder", "imap_uid")

    def add_draft(self, subject: str = "", body: str = "", to_addr: str = "",
                  cc_addr: str = "", is_html: bool = False, account: str = None,
                  reply_to_id: str = None, source: str = "ui") -> int:
        now = __import__("time").time()
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO drafts(to_addr, cc_addr, subject, body, is_html,
                                   account, reply_to_id, source, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (to_addr or "", cc_addr or "", subject or "", body or "",
                  1 if is_html else 0, account, reply_to_id, source or "ui", now, now))
            return cur.lastrowid

    def get_drafts(self, limit: int = 200) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM drafts ORDER BY updated_at DESC LIMIT ?",
                (int(limit),)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_draft(self, draft_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM drafts WHERE id=?",
                               (int(draft_id),)).fetchone()
            return dict(row) if row else None

    def update_draft(self, draft_id: int, **kwargs) -> bool:
        """只允许改白名单字段；id/created_at 不可改，updated_at 自动刷新。"""
        fields = {k: v for k, v in kwargs.items() if k in self._DRAFT_FIELDS}
        if "is_html" in fields:
            fields["is_html"] = 1 if fields["is_html"] else 0
        if not fields:
            return False
        fields["updated_at"] = __import__("time").time()
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [int(draft_id)]
        with self._conn() as conn:
            cur = conn.execute(f"UPDATE drafts SET {set_clause} WHERE id=?", values)
            return cur.rowcount > 0

    def delete_draft(self, draft_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM drafts WHERE id=?", (int(draft_id),))
            return cur.rowcount > 0

    def count_drafts(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]

    def count_todos_by_status(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM todos GROUP BY status"
            ).fetchall()
            return {r[0]: r[1] for r in rows}

    # ------------------------------------------------------------------ #
    # 导出
    # ------------------------------------------------------------------ #

    def export_todos_csv(self) -> str:
        import csv, io
        todos = self.get_todos()
        if not todos:
            return ""
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=todos[0].keys())
        writer.writeheader()
        for row in todos:
            if row.get("source_email_id"):
                email = self.get_email_by_id(row["source_email_id"])
                row["source_subject"] = email.get("subject", "") if email else ""
            writer.writerow(row)
        return out.getvalue()

    def export_todos_with_emails(self, todo_ids: list[int] = None) -> list[dict]:
        if todo_ids:
            placeholders = ",".join("?" * len(todo_ids))
            with self._conn() as conn:
                rows = conn.execute(
                    f"SELECT * FROM todos WHERE id IN ({placeholders})", todo_ids
                ).fetchall()
            todos = [dict(r) for r in rows]
        else:
            todos = self.get_todos()
        result = []
        for todo in todos:
            entry = dict(todo)
            if todo.get("source_email_id"):
                email = self.get_email_by_id(todo["source_email_id"])
                entry["source_email"] = email or {}
            result.append(entry)
        return result

    def export_emails_csv(self, email_ids: list[str] = None, limit: int = 5000) -> str:
        import csv, io
        if email_ids:
            placeholders = ",".join("?" * len(email_ids))
            with self._conn() as conn:
                rows = conn.execute(
                    f"SELECT id,subject,sender,sender_email,time,folder,account,"
                    f"category,summary,action_items,has_attachment FROM emails WHERE id IN ({placeholders})",
                    email_ids
                ).fetchall()
        else:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT id,subject,sender,sender_email,time,folder,account,"
                    "category,summary,action_items,has_attachment FROM emails "
                    "ORDER BY received_timestamp DESC LIMIT ?", (limit,)
                ).fetchall()
        if not rows:
            return ""
        out = io.StringIO()
        cols = ["id","subject","sender","sender_email","time","folder","account",
                "category","summary","action_items","has_attachment"]
        writer = csv.writer(out)
        writer.writerow(cols)
        for r in rows:
            writer.writerow(list(r))
        return out.getvalue()

    def count_no_ai(self, skip_accounts: list = None, skip_folders: list = None) -> int:
        """返回尚无 AI 摘要且不在跳过列表中的邮件数"""
        skip_accounts = skip_accounts or []
        skip_folders  = skip_folders  or []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM emails WHERE (summary IS NULL OR summary='') AND deleted=0"
            ).fetchone()[0]
            if skip_accounts or skip_folders:
                excl = conn.execute(
                    "SELECT COUNT(*) FROM emails WHERE (summary IS NULL OR summary='') AND deleted=0"
                    " AND (account IN ({}) OR folder IN ({}))".format(
                        ",".join("?" * len(skip_accounts)) if skip_accounts else "'__never__'",
                        ",".join("?" * len(skip_folders))  if skip_folders  else "'__never__'",
                    ),
                    skip_accounts + skip_folders
                ).fetchone()[0]
                rows -= excl
        return max(rows, 0)

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    def add_tag(self, email_id: str, tag: str) -> bool:
        """幂等追加标签；已有则不写。返回是否新加。"""
        with self._conn() as conn:
            r = conn.execute("SELECT tags FROM emails WHERE id=?", (email_id,)).fetchone()
            if not r:
                return False
            try:
                tags = json.loads(r[0] or "[]")
            except Exception:
                tags = []
            if not isinstance(tags, list):
                tags = []
            if tag in tags:
                return False
            tags.append(tag)
            conn.execute("UPDATE emails SET tags=? WHERE id=?", (json.dumps(tags, ensure_ascii=False), email_id))
            return True

    def has_tag(self, email_id: str, tag: str) -> bool:
        with self._conn() as conn:
            r = conn.execute("SELECT tags FROM emails WHERE id=?", (email_id,)).fetchone()
        try:
            return bool(r) and tag in (json.loads(r[0] or "[]") or [])
        except Exception:
            return False

    def set_tags(self, email_id: str, tags) -> None:
        val = tags if isinstance(tags, str) else json.dumps(tags, ensure_ascii=False)
        with self._conn() as conn:
            conn.execute("UPDATE emails SET tags=? WHERE id=?", (val, email_id))

    def set_archived(self, email_id: str, archived: bool = True) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE emails SET archived=? WHERE id=?", (1 if archived else 0, email_id))

    def _row_to_dict(self, row) -> dict:
        d = dict(row)
        try:
            d["tags"] = json.loads(d.get("tags") or "[]")
        except Exception:
            d["tags"] = []
        d["unread"] = bool(d.get("unread"))
        d["has_attachment"] = bool(d.get("has_attachment"))
        try:
            d["attachments"] = json.loads(d.get("attachments") or "[]")
        except Exception:
            d["attachments"] = []
        try:
            d["recipients"] = json.loads(d.get("recipients") or "[]")
        except Exception:
            d["recipients"] = []
        try:
            ai = d.get("action_items")
            if ai and ai.startswith("["):
                d["action_items"] = json.loads(ai)
        except Exception:
            pass
        return d
