"""语义检索：查询向量化 → 相似度召回 Top-K。"""
from src.ingest.embedder import Embedder
from src.rag.vector_store import RetrievedChunk, VectorStore
from src.settings import load_settings


class Retriever:
    def __init__(self, store: VectorStore, embedder: Embedder, top_k: int = 5):
        self.store = store
        self.embedder = embedder
        self.top_k = top_k

    def retrieve(self, query: str, top_k: int | None = None) -> list[RetrievedChunk]:
        k = top_k or self.top_k
        query_vec = self.embedder.embed_query(query)
        return self.store.search(query_vec, k)


def default_retriever(store: VectorStore) -> Retriever:
    cfg = load_settings()
    embedder = Embedder(cfg["embedding"])
    return Retriever(store, embedder, cfg["retrieval"]["top_k"])
