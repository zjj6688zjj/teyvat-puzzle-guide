"""Embedding 封装：阿里百炼 qwen3.7-text-embedding（OpenAI 兼容）。"""
import os

from openai import OpenAI

from src.settings import load_settings


class Embedder:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or load_settings()["embedding"]
        self.model = cfg["model"]
        self.dimensions = cfg["dimensions"]
        self.batch_size = cfg["batch_size"]
        api_key = os.environ.get(cfg["api_key_env"])
        if not api_key:
            raise ValueError(
                f"缺少环境变量 {cfg['api_key_env']}，请复制 .env.example 为 config/.env 并填入 Key"
            )
        self.client = OpenAI(api_key=api_key, base_url=cfg["base_url"])

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量向量化，返回与输入等长的向量列表。"""
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            resp = self.client.embeddings.create(
                model=self.model,
                input=batch,
                dimensions=self.dimensions,
            )
            # resp.data 与输入顺序一致
            vectors.extend([d.embedding for d in sorted(resp.data, key=lambda x: x.index)])
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]
