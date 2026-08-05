"""环境检查：验证两个 API Key 存在且模型名正确（真实调用，各花一次最小请求）。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os


def check_llm(cfg):
    name = cfg["api_key_env"]
    key = os.environ.get(name)
    if not key:
        print(f"[FAIL] LLM 缺少 {name}，请在 config/.env 中配置")
        return False
    from src.rag.llm import DeepSeekLLM

    llm = DeepSeekLLM()
    t0 = time.perf_counter()
    out = llm.generate("你是测试助手，只回复两个字：正常", "1+1等于几？")
    ok = bool(out)
    print(f"[{'OK' if ok else 'FAIL'}] LLM {llm.model}: 返回「{out[:30]}」, 耗时 {time.perf_counter() - t0:.1f}s")
    return ok


def check_embedding(cfg):
    name = cfg["api_key_env"]
    key = os.environ.get(name)
    if not key:
        print(f"[FAIL] Embedding 缺少 {name}，请在 config/.env 中配置")
        return False
    from src.ingest.embedder import Embedder

    emb = Embedder()
    t0 = time.perf_counter()
    vec = emb.embed_query("原神")
    ok = len(vec) == cfg["dimensions"]
    print(
        f"[{'OK' if ok else 'FAIL'}] Embedding {emb.model}: 维度 {len(vec)}（期望 {cfg['dimensions']}）, 耗时 {time.perf_counter() - t0:.1f}s"
    )
    return ok


def main():
    from src.settings import load_settings

    cfg = load_settings()
    ok1 = check_llm(cfg["llm"])
    ok2 = check_embedding(cfg["embedding"])
    print("=" * 40)
    print("全部通过 ✅" if (ok1 and ok2) else "存在问题，请按上方提示修复 ❌")


if __name__ == "__main__":
    main()
