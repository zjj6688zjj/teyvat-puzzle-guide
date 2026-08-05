"""文本分段：按 token 估算切块，尽量在换行处切断，带重叠。"""
from src.ingest.cleaner import content_hash
from src.ingest.loader import Document


def est_tokens(text: str, token_per_char: float) -> int:
    """粗略估算 token 数：1 token ≈ 0.6 个汉字。"""
    return int(len(text) * token_per_char)


def chunk_text(text: str, chunk_size_tokens: int, overlap_tokens: int,
               token_per_char: float, doc_meta: dict) -> list[dict]:
    """把长文本切成若干 chunk。

    返回 [{text, metadata}]，metadata 继承文档元数据并附加 chunk_index。
    """
    max_chars = max(int(chunk_size_tokens / token_per_char), 100)
    overlap_chars = max(int(overlap_tokens / token_per_char), 0)
    chunks = []
    i, n = 0, len(text)
    idx = 0
    while i < n:
        end = min(i + max_chars, n)
        if end < n:
            # 尝试回退到上一个换行处，避免句子被腰斩
            nl = text.rfind("\n", i, end)
            if nl != -1 and nl > i + max_chars * 0.5:
                end = nl
        piece = text[i:end].strip()
        if piece:
            meta = dict(doc_meta)
            meta["chunk_index"] = idx
            meta["content_hash"] = content_hash(piece)
            chunks.append({"text": piece, "metadata": meta})
            idx += 1
        if end >= n:
            break
        i = max(end - overlap_chars, i + 1)
    return chunks


def chunk_documents(docs: list[Document], cfg: dict) -> list[dict]:
    """批量分段。cfg 取 settings.yaml 的 chunking 节。"""
    out = []
    for doc in docs:
        chunks = chunk_text(
            doc.content,
            cfg["chunk_size_tokens"],
            cfg["overlap_tokens"],
            cfg["token_per_chinese_char"],
            doc.metadata,
        )
        out.extend(chunks)
    return out
