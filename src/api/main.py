"""FastAPI 服务：/api/chat 问答、/api/health 健康检查。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.rag.pipeline import RAGPipeline

app = FastAPI(title="原神AI攻略助手")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 管道惰性初始化：首次请求时才加载（依赖 config/.env 中的 Key）
_pipeline: RAGPipeline | None = None


def get_pipeline() -> RAGPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = RAGPipeline()
    return _pipeline


class ChatRequest(BaseModel):
    query: str
    history: list | None = None


@app.get("/api/health")
def health():
    return {"status": "ok", "kb_size": get_pipeline().store.size()}


@app.post("/api/chat")
def chat(req: ChatRequest):
    return get_pipeline().ask(req.query, req.history)
