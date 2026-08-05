"""数据清洗：去标签、去噪声行、文档级去重。"""
import hashlib
import re

from src.ingest.loader import Document

# 时间轴行：如 00:12 / 01:02:03
_TIMESTAMP_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
# 纯数字/符号行
_NOISE_RE = re.compile(r"^[\d\s:,.，。、\-–—·]+$")


def clean_text(text: str) -> str:
    """清洗单段文本：去 HTML 注释/标签、去时间轴与噪声行、折叠连续重复行。"""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\r\n", "\n")

    lines = []
    for ln in text.split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        if _TIMESTAMP_RE.match(ln) or _NOISE_RE.match(ln):
            continue
        # 折叠连续重复行（ASR 常见的重复输出）
        if lines and ln == lines[-1]:
            continue
        lines.append(ln)
    return "\n".join(lines)


def content_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def dedupe_documents(docs: list[Document]) -> list[Document]:
    """按清洗后内容的 MD5 去重，保留先出现者。"""
    seen: set[str] = set()
    out: list[Document] = []
    for doc in docs:
        cleaned = clean_text(doc.content)
        if not cleaned:
            continue
        h = content_hash(cleaned)
        if h in seen:
            continue
        seen.add(h)
        doc.content = cleaned
        out.append(doc)
    return out
