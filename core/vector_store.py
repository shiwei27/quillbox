import os
# 内网环境无法访问 HuggingFace Hub；强制离线，避免加载本地嵌入模型时
# 因联网校验/下载而长时间挂起（表现为进程卡在“Loading SentenceTransformer”）。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# 允许多个 OpenMP 运行时共存，避免与 win32com/pythoncom 同处一进程时
# torch 加载权重死锁（daemon 进程会同时 import win32com 与 torch）。
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

import logging

from pathlib import Path

# chromadb / sentence-transformers 属可选依赖（向量检索 extra）：在 VectorStore 内部惰性导入，
# 没装时由 open_vector_store() 退化为 NullVectorStore，服务照常启动、检索走关键词。
log = logging.getLogger(__name__)

# 只有这类异常才说明"嵌入函数/维度与已存集合不一致"，此时重建集合是唯一出路。
# 其它异常（文件锁、磁盘忙、sqlite busy…）一律不许触发重建——
# 悄悄清掉 13000 条索引远比起不来严重。
_CONFLICT_HINTS = ("embedding function", "embedding_function", "dimension",
                   "dimensionality", "conflict", "does not match", "mismatch")


def _is_embedding_conflict(exc: Exception) -> bool:
    msg = ("%s %s" % (type(exc).__name__, exc)).lower()
    return any(h in msg for h in _CONFLICT_HINTS)


def _unusable_summary(summary: str) -> bool:
    """摘要是否不可用于语义索引（AI 故障文本或失败哨兵）。"""
    s = (summary or "").strip()
    if not s:
        return True
    try:
        from .ai_engine import generation_failed
        if generation_failed(s):
            return True
    except Exception:
        # 循环导入或引擎缺失时退化为本地判据，绝不因此拖垮索引
        if s.startswith(("[API 异常", "[Ollama 异常", '{"error"')):
            return True
    return s in ("AI 分析失败", "摘要生成失败")


class VectorStore:
    """向量检索层 — 合并原 VectorStore + RAGEngine，统一 id 字段"""

    enabled = True

    def __init__(self, chroma_path: str, model_path: str):
        import chromadb
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
        self.client = chromadb.PersistentClient(path=chroma_path)
        embed_fn = SentenceTransformerEmbeddingFunction(
            model_name=model_path,
            device="cpu",
        )
        try:
            self.collection = self.client.get_or_create_collection(
                name="emails",
                embedding_function=embed_fn,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as e:
            # 改前这里是 `except Exception: self.client.delete_collection("emails")`，
            # 也就是**任何**异常都会删掉整个集合。注释写的是"embedding function
            # conflict"，但捕获范围远大于此：一次文件锁或 sqlite busy 就足以
            # 静默清掉 13000 条 / 140 MB 索引，而按自愈 200 封每 5 分钟一轮算，
            # 重建要 5 个多小时。所以只在确实是嵌入函数/维度冲突时才重建。
            if not _is_embedding_conflict(e):
                log.error("打开向量集合失败，且不像嵌入函数冲突，"
                          "拒绝重建集合（宁可起不来也不清库）: %r", e)
                raise
            log.warning("检测到嵌入函数/维度冲突，将重建 emails 集合（原有向量会丢失）: %r", e)
            self.client.delete_collection("emails")
            self.collection = self.client.create_collection(
                name="emails",
                embedding_function=embed_fn,
                metadata={"hnsw:space": "cosine"},
            )
            log.warning("emails 集合已重建，需等自愈逐轮补齐（每轮上限 200 封）")

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #

    def add_email(self, email: dict):
        """索引单封邮件（若已存在则更新）"""
        email_id = email["id"]
        text = self._build_index_text(email)
        meta = {
            "subject":    email.get("subject", ""),
            "sender":     email.get("sender", ""),
            "sender_email": email.get("sender_email", ""),
            "time":       email.get("time", ""),
            "account":    email.get("account", ""),
            "folder":     email.get("folder", ""),
            "summary":    email.get("summary", ""),
            "category":   email.get("category", ""),
        }
        self.collection.upsert(
            ids=[email_id],
            documents=[text],
            metadatas=[meta],
        )

    def add_emails_batch(self, emails: list[dict]):
        if not emails:
            return
        ids, docs, metas = [], [], []
        for e in emails:
            ids.append(e["id"])
            docs.append(self._build_index_text(e))
            metas.append({
                "subject":      e.get("subject", ""),
                "sender":       e.get("sender", ""),
                "sender_email": e.get("sender_email", ""),
                "time":         e.get("time", ""),
                "account":      e.get("account", ""),
                "folder":       e.get("folder", ""),
                "summary":      e.get("summary", ""),
                "category":     e.get("category", ""),
            })
        self.collection.upsert(ids=ids, documents=docs, metadatas=metas)

    # ------------------------------------------------------------------ #
    # 检索
    # ------------------------------------------------------------------ #

    def search(self, query: str, n_results: int = 10,
               account: str | None = None) -> list[dict]:
        """语义搜索，返回带 id 和 score 的结果列表"""
        where = {"account": account} if account else None
        kwargs = {"query_texts": [query], "n_results": n_results, "include": ["metadatas", "distances"]}
        if where:
            kwargs["where"] = where
        try:
            res = self.collection.query(**kwargs)
        except Exception as e:
            # 改前静默返回 []，于是"检索故障"与"没有匹配"在调用方看来一模一样。
            # chat_search 是 AI 问答的 RAG 入口，索引出问题时表现为
            # "AI 突然什么都不知道"，却查不到原因。返回类型不变，但必须留痕。
            log.warning("向量检索失败，本次按空结果处理 query=%r: %r", (query or "")[:60], e)
            return []

        results = []
        ids       = res["ids"][0]
        metas     = res["metadatas"][0]
        distances = res["distances"][0]
        for eid, meta, dist in zip(ids, metas, distances):
            item = dict(meta)
            item["id"] = eid
            item["score"] = max(0.0, 1.0 - dist)
            results.append(item)
        return results

    def chat_search(self, query: str, n_results: int = 10) -> list[dict]:
        """语义搜索别名，供 AI 聊天接口调用"""
        return self.search(query, n_results=n_results)

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #

    def count(self) -> int:
        return self.collection.count()

    def get_indexed_ids(self) -> set:
        result = self.collection.get(include=[])
        return set(result["ids"])

    def _build_index_text(self, email: dict) -> str:
        """构建用于向量索引的文本。
        优先使用摘要（AI生成的高质量文本），其次是主题+正文片段。
        不混入发件人地址等噪声字段（这些用关键词搜索更合适）。
        """
        subject = email.get("subject", "")
        summary = email.get("summary", "")
        body = email.get("body", "")

        # 改前判据是 `not summary.startswith("[Ollama")`——Ollama 时代写的，
        # 加 API 后端后没跟着改：当前错误字符串是 "[API 异常: ...]"，挡不住；
        # "AI 分析失败" 这类失败哨兵也挡不住，会被当作邮件语义内容送去 embedding。
        # 上游已改成"失败不落库"，这里是第二道闸。
        if summary and not _unusable_summary(summary):
            # 有有效的AI摘要时，用 主题+摘要（高质量短文本，embedding效果最好）
            return f"{subject} {summary}"
        else:
            # 无摘要时，用 主题+正文前800字（短一些，embedding质量更高）
            return f"{subject} {body[:800]}"


class NullVectorStore:
    """未安装向量检索依赖时的替身：接口与 VectorStore 一致，检索恒为空，写入为空操作。

    调用方据 `enabled` 区分"没装"与"没命中"；关键词检索（SQLite FTS）不受影响。
    """

    enabled = False

    def __init__(self, reason: str = ""):
        self.reason = reason

    def add_email(self, email: dict):
        return None

    def add_emails_batch(self, emails: list[dict]):
        return 0

    def search(self, query: str, n_results: int = 10, account: str | None = None) -> list[dict]:
        return []

    def chat_search(self, query: str, n_results: int = 10) -> list[dict]:
        return []

    def count(self) -> int:
        return 0

    def get_indexed_ids(self) -> set:
        return set()


def open_vector_store(chroma_path: str, model_path: str):
    """打开向量库；仅当可选依赖缺失（ImportError）时退化为 NullVectorStore。

    其它异常照旧抛出——沿用"宁可起不来也不清库"的原则，不把真实故障伪装成"未启用"。
    """
    try:
        return VectorStore(chroma_path, model_path)
    except ImportError as e:
        log.warning("未安装向量检索依赖（chromadb / sentence-transformers），语义检索已关闭，"
                    "仅用关键词检索。安装：pip install -e '.[vector]'。原因: %s", e)
        return NullVectorStore(str(e))
