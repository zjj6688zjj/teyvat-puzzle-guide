"""问答管道：检索 → 拼 Prompt → LLM 生成 → 带来源引用返回。"""
import time

from src.rag.llm import DeepSeekLLM, SYSTEM_PROMPT
from src.rag.retriever import Retriever
from src.rag.vector_store import FaissVectorStore, RetrievedChunk, VectorStore
from src.settings import load_settings, resolve_path


class RAGPipeline:
    def __init__(self, store: VectorStore | None = None):
        cfg = load_settings()
        self.cfg = cfg
        index_dir = resolve_path(cfg["paths"]["vector_index"])
        self.store = store or FaissVectorStore(index_dir)
        self.retriever = Retriever(
            self.store, None, cfg["retrieval"]["top_k"]
        )
        # retriever.embedder 与 llm 懒加载，避免缺少 Key 时连管道都建不起来
        self._embedder = None
        self._llm = None

    @property
    def embedder(self):
        if self._embedder is None:
            from src.ingest.embedder import Embedder

            self._embedder = Embedder(self.cfg["embedding"])
        self.retriever.embedder = self._embedder
        return self._embedder

    @property
    def llm(self):
        if self._llm is None:
            self._llm = DeepSeekLLM(self.cfg["llm"])
        return self._llm

    def ask(self, query: str, history: list | None = None, on_stage=None) -> dict:
        """执行完整问答流程。

        history 预留多轮对话（本阶段不参与 prompt，后续迭代接入）。
        on_stage: 可选回调，接收阶段名 ("embed"/"retrieve"/"generate")，供界面显示进度。
        返回 {answer, sources, latency_ms, stages_ms, hit_count, error}
        """
        def stage(name: str):
            if on_stage:
                on_stage(name)

        t0 = time.perf_counter()
        stage("embed")
        self.embedder
        hits = self.retriever.retrieve(query)
        t_retrieve = time.perf_counter()
        if not hits:
            return {
                "answer": "知识库还是空的，请先运行 scripts/build_kb.py 构建知识库。",
                "sources": [],
                "latency_ms": int((t_retrieve - t0) * 1000),
                "stages_ms": {"embed": int((t_retrieve - t0) * 1000)},
                "hit_count": 0,
                "error": "",
            }

        context = "\n\n".join(f"[{i + 1}] {h.text}" for i, h in enumerate(hits))
        user_prompt = f"【参考资料】\n{context}\n\n【用户问题】\n{query}"
        stage("generate")
        try:
            answer = self.llm.generate(SYSTEM_PROMPT, user_prompt)
            error = ""
        except Exception as e:
            answer = f"⚠️ **生成回答失败**：{e}"
            error = str(e)
        t_end = time.perf_counter()
        sources = build_sources(hits)
        return {
            "answer": answer,
            "sources": sources,
            "latency_ms": int((t_end - t0) * 1000),
            "stages_ms": {
                "retrieve": int((t_retrieve - t0) * 1000),
                "generate": int((t_end - t_retrieve) * 1000),
            },
            "hit_count": len(hits),
            "error": error,
        }

    def ask_stream(self, query: str, history: list | None = None):
        """流式版 ask：yield ("stage", 阶段名) / ("text", 文本块) / ("done", 结果dict)。

        界面可据此显示进度条并边生成边渲染，体感更快。
        """
        t0 = time.perf_counter()
        yield ("stage", "embed")
        self.embedder
        hits = self.retriever.retrieve(query)
        t_retrieve = time.perf_counter()
        if not hits:
            yield (
                "done",
                {
                    "answer": "知识库还是空的，请先运行 scripts/build_kb.py 构建知识库。",
                    "sources": [],
                    "latency_ms": int((t_retrieve - t0) * 1000),
                    "stages_ms": {"retrieve": int((t_retrieve - t0) * 1000)},
                    "hit_count": 0,
                    "error": "",
                },
            )
            return

        context = "\n\n".join(f"[{i + 1}] {h.text}" for i, h in enumerate(hits))
        user_prompt = f"【参考资料】\n{context}\n\n【用户问题】\n{query}"
        yield ("stage", "generate")
        sources = build_sources(hits)
        try:
            parts: list[str] = []
            for chunk in self.llm.generate_stream(SYSTEM_PROMPT, user_prompt):
                parts.append(chunk)
                yield ("text", chunk)
            answer = "".join(parts).strip()
            error = ""
        except Exception as e:
            answer = f"⚠️ **生成回答失败**：{e}"
            error = str(e)
        t_end = time.perf_counter()
        yield (
            "done",
            {
                "answer": answer,
                "sources": sources,
                "latency_ms": int((t_end - t0) * 1000),
                "stages_ms": {
                    "retrieve": int((t_retrieve - t0) * 1000),
                    "generate": int((t_end - t_retrieve) * 1000),
                },
                "hit_count": len(hits),
                "error": error,
            },
        )


def build_sources(hits: list[RetrievedChunk]) -> list[dict]:
    """从命中片段提取来源列表，按 source_url 去重。"""
    seen: set[str] = set()
    sources = []
    for h in hits:
        m = h.metadata
        url = m.get("source_url", "local:unknown")
        if url in seen:
            continue
        seen.add(url)
        sources.append(
            {
                "source_url": url,
                "author": m.get("author", "未知"),
                "platform": m.get("platform", "未知"),
                "title": m.get("title", ""),
                "game_version": m.get("game_version", ""),
                "category": m.get("category", ""),
                "snippet": h.text[:120],
                "score": round(h.score, 4),
            }
        )
    return sources
