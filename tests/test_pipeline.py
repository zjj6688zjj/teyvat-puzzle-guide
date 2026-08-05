"""管道冒烟测试：不依赖任何 API Key，只验证本地逻辑（分段/清洗/向量库）。"""
import sys
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest.chunker import chunk_text
from src.ingest.cleaner import clean_text, dedupe_documents
from src.ingest.loader import Document, _load_text_file
from src.rag.pipeline import RAGPipeline
from src.rag.vector_store import FaissVectorStore

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def test_cleaner():
    print("[cleaner]")
    raw = "<!-- source_url: https://x.com -->\n<p>胡桃的圣遗物搭配</p>\n\n00:12\n圣遗物\n圣遗物\n"
    out = clean_text(raw)
    check("去注释标签", "<!--" not in out and "<p>" not in out)
    check("去时间轴", "00:12" not in out)
    check("去重复行", out == "胡桃的圣遗物搭配\n圣遗物")


def test_chunker():
    print("[chunker]")
    text = "\n".join(f"第{i}行 胡桃圣遗物配置说明内容，需要足够长度来测试分段逻辑。" for i in range(200))
    chunks = chunk_text(text, chunk_size_tokens=512, overlap_tokens=50,
                        token_per_char=0.6, doc_meta={"source_url": "u"})
    check("产出多个chunk", len(chunks) > 1)
    check("chunk_index递增", chunks[0]["metadata"]["chunk_index"] == 0)
    check("含content_hash", "content_hash" in chunks[0]["metadata"])
    joined = "".join(c["text"] for c in chunks)
    check("内容无丢失", "第0行" in joined and "第199行" in joined)


def test_miyoushe_loader():
    print("[miyoushe_loader]")
    from src.ingest.loader import load_documents

    docs = load_documents()
    check("output/ 数据已加载", len(docs) >= 1)
    if docs:
        d = docs[0]
        meta = d.metadata
        check("平台为米游社", meta.get("platform") == "米游社")
        check("含 source_url", bool(meta.get("source_url")))
        check("含 title", bool(meta.get("title")))
        check("含 author", bool(meta.get("author")))
        check("含 post_id", bool(meta.get("post_id")))
        check("正文非空且>20字", len(d.content) > 20)
        check("正文不含html标签", "<p" not in d.content and "<div" not in d.content)


def test_dedupe():
    print("[dedupe]")
    docs = [Document("一样的攻略内容，重复出现。"),
            Document("一样的攻略内容，重复出现。"),
            Document("另一篇不一样的攻略内容。")]
    out = dedupe_documents(docs)
    check("去重后剩2篇", len(out) == 2)


def test_store():
    print("[vector_store]")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        index_dir = Path(td) / "\u4e2d\u6587\u7d22\u5f15"
        store = FaissVectorStore(index_dir)
        n = 5
        dim = 8
        import numpy as np
        vecs = np.random.rand(n, dim).astype("float32")
        store.add(vecs, [f"文本{i}" for i in range(n)],
                  [{"content_hash": f"h{i}"} for i in range(n)])
        check("size正确", store.size() == n)
        check("增量去重命中", store.has_hash("h2") and not store.has_hash("h9"))
        hits = store.search(vecs[3].tolist(), top_k=3)
        check("自相似命中第一", hits and hits[0].metadata["content_hash"] == "h3")
        check("相似度在合理范围", all(-1.01 < h.score <= 1.01 for h in hits))
        # 重新加载持久化
        store2 = FaissVectorStore(index_dir)
        check("落盘重载", store2.size() == n)

def test_chinese_filename_roundtrip():
    print("[chinese_filename]")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        guide_path = Path(td) / "\u80e1\u6843\u5723\u9057\u7269\u653b\u7565.md"
        guide_path.write_text(
            "\u80e1\u6843\u9002\u5408\u4f7f\u7528\u751f\u547d\u6c99\u548c\u706b\u4f24\u676f",
            encoding="utf-8",
        )
        document = _load_text_file(guide_path, {"platform": "local"})
        store = FaissVectorStore(Path(td) / "\u4e2d\u6587\u77e5\u8bc6\u5e93")
        store.add(
            [[1.0, 0.0]],
            [document.content],
            [{**document.metadata, "content_hash": "chinese-filename"}],
        )
        reloaded = FaissVectorStore(store.index_dir)
        hits = reloaded.search([1.0, 0.0], top_k=1)
        check("Chinese source filename", document.metadata["title"] == guide_path.stem)
        check(
            "Chinese filename metadata roundtrip",
            bool(hits) and hits[0].metadata["title"] == guide_path.stem,
        )


def test_pipeline_lazy_embedder():
    print("[pipeline]")

    class FakeEmbedder:
        def embed_query(self, _query):
            return [1.0, 0.0]

    class FakeLLM:
        def generate(self, _system_prompt, _user_prompt):
            return "test answer"

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        store = FaissVectorStore(Path(td) / "index")
        store.add(
            [[1.0, 0.0]],
            ["test guide"],
            [{"content_hash": "pipeline-test", "source_url": "local:test"}],
        )
        pipeline = RAGPipeline(store)
        pipeline._embedder = FakeEmbedder()
        pipeline._llm = FakeLLM()
        result = pipeline.ask("test question")
        check("lazy embedder retrieval", result["hit_count"] == 1)
        check("full pipeline", result["answer"] == "test answer")


if __name__ == "__main__":
    test_cleaner()
    test_chunker()
    test_miyoushe_loader()
    test_dedupe()
    test_store()
    test_chinese_filename_roundtrip()
    test_pipeline_lazy_embedder()
    print("=" * 40)
    print(f"通过 {PASS} / 失败 {FAIL}")
    sys.exit(1 if FAIL else 0)
