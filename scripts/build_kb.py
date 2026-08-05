"""一键建库：加载 → 清洗去重 → 分段 → 向量化 → 入库。

增量模式：已入库的 chunk（按 content_hash）自动跳过，不重复调用 embedding。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from src.ingest.chunker import chunk_documents
    from src.ingest.cleaner import dedupe_documents
    from src.ingest.embedder import Embedder
    from src.ingest.loader import load_documents
    from src.rag.vector_store import FaissVectorStore
    from src.settings import load_settings, resolve_path

    cfg = load_settings()
    chunk_cfg = cfg["chunking"]
    t0 = time.perf_counter()

    print("① 加载原始文档...")
    docs = load_documents()
    if not docs:
        print("⚠️ 未找到任何攻略文件。请确认数据在 ../output（米游社爬虫产物，主数据源）"
              "或 data/raw/video_transcripts / data/raw/text_guides")
        return
    print(f"   原始文档 {len(docs)} 篇")

    print("② 清洗去重...")
    docs = dedupe_documents(docs)
    print(f"   清洗后 {len(docs)} 篇")

    print("③ 分段...")
    chunks = chunk_documents(docs, chunk_cfg)
    print(f"   共 {len(chunks)} 个片段")

    print("④ 向量化入库...")
    store = FaissVectorStore(resolve_path(cfg["paths"]["vector_index"]))
    embedder = Embedder(cfg["embedding"])

    new_chunks = [c for c in chunks if not store.has_hash(c["metadata"]["content_hash"])]
    print(f"   其中新增 {len(new_chunks)} 条（跳过已入库 {len(chunks) - len(new_chunks)} 条）")

    if new_chunks:
        vectors = embedder.embed_texts([c["text"] for c in new_chunks])
        store.add(
            vectors,
            [c["text"] for c in new_chunks],
            [c["metadata"] for c in new_chunks],
        )

    elapsed = time.perf_counter() - t0
    print("=" * 40)
    print(f"✅ 建库完成：知识库共 {store.size()} 条，耗时 {elapsed:.1f}s")
    if new_chunks:
        # 打印 3 条示例 chunk 供检查
        for c in new_chunks[:3]:
            print("-" * 40)
            print(f"[{c['metadata'].get('source_url', '')}]")
            print(c["text"][:80] + "...")


if __name__ == "__main__":
    main()
