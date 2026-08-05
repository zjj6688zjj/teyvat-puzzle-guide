"""向量库抽象接口 + 本地 FAISS 实现。

后续接入阿里云数据库时，实现一个 PgVectorStore（pgvector）并替换工厂即可。
"""
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import faiss
import numpy as np


@dataclass
class RetrievedChunk:
    text: str
    metadata: dict = field(default_factory=dict)
    score: float = 0.0


class VectorStore(ABC):
    """统一接口：开发期 FAISS，上线期可换 pgvector/Milvus。"""

    @abstractmethod
    def add(self, vectors, texts, metadatas): ...

    @abstractmethod
    def search(self, query_vec, top_k): ...

    @abstractmethod
    def size(self): ...

    def has_hash(self, content_hash: str) -> bool:
        """是否已存在该 chunk（用于增量建库去重）。"""
        return False


class FaissVectorStore(VectorStore):
    """FAISS 余弦相似度（内积 + 向量归一化）。索引与元数据落盘到 index_dir。"""

    def __init__(self, index_dir):
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.index_dir / "index.bin"
        self.meta_file = self.index_dir / "metadata.json"
        self.index = None
        self.texts: list[str] = []
        self.metadatas: list[dict] = []
        self.hashes: set[str] = set()
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        if self.index_file.exists() and self.meta_file.exists():
            serialized = np.frombuffer(self.index_file.read_bytes(), dtype=np.uint8)
            self.index = faiss.deserialize_index(serialized)
            meta = json.loads(self.meta_file.read_text(encoding="utf-8"))
            self.texts = meta["texts"]
            self.metadatas = meta["metadatas"]
            self.hashes = {m.get("content_hash", "") for m in self.metadatas}

    def _save(self):
        if self.index is not None:
            serialized = faiss.serialize_index(self.index)
            self.index_file.write_bytes(serialized.tobytes())
        payload = {"texts": self.texts, "metadatas": self.metadatas}
        self.meta_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    # ---------- 接口 ----------
    def add(self, vectors, texts, metadatas):
        v = np.asarray(vectors, dtype=np.float32)
        # 归一化为单位向量 → 内积即余弦相似度
        v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-9
        dim = v.shape[1]
        if self.index is None:
            self.index = faiss.IndexFlatIP(dim)
        self.index.add(v)
        self.texts.extend(texts)
        self.metadatas.extend(metadatas)
        self.hashes.update(m.get("content_hash", "") for m in metadatas)
        self._save()

    def search(self, query_vec, top_k):
        if self.index is None or self.size() == 0:
            return []
        q = np.asarray([query_vec], dtype=np.float32)
        q /= np.linalg.norm(q) + 1e-9
        k = min(int(top_k), self.size())
        scores, ids = self.index.search(q, k)
        return [
            RetrievedChunk(
                text=self.texts[i],
                metadata=self.metadatas[i],
                score=float(scores[0][j]),
            )
            for j, i in enumerate(ids[0])
        ]

    def size(self):
        return len(self.texts)

    def has_hash(self, content_hash: str) -> bool:
        return content_hash in self.hashes
