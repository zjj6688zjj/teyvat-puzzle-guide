"""多源数据加载：把原始数据转成 Document 列表。

支持三种来源：
1. 米游社爬虫产物（主数据源）：output/{post_id}/text/article.json
2. 视频转写目录：data/raw/video_transcripts（_text.txt / .srt）
3. 文本攻略目录：data/raw/text_guides（.md/.txt，文件头带来源注释）
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.settings import load_settings, resolve_path

# 文本攻略文件头的元数据注释格式：
# <!-- source_url: xxx \n author: xxx \n platform: xxx \n ... -->
_HEADER_RE = re.compile(r"<!--(.*?)-->", re.S)


@dataclass
class Document:
    content: str
    metadata: dict = field(default_factory=dict)


# ============================================================
# 米游社爬虫产物（主数据源）
# ============================================================
def _load_miyoushe_output(root: Path) -> list[Document]:
    """解析 output/{post_id}/text/article.json。"""
    if not root.is_dir():
        return []
    docs: list[Document] = []
    for post_dir in sorted(root.iterdir()):
        if not post_dir.is_dir():
            continue
        article = post_dir / "text" / "article.json"
        if not article.is_file():
            continue
        try:
            data = json.loads(article.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        plain = (data.get("plain_text") or "").strip()
        if not plain:
            continue
        post_id = str(data.get("post_id", post_dir.name))
        meta = {
            "platform": "米游社",
            "category": guess_category(data.get("title", "")),
            "title": data.get("title", ""),
            "source_url": data.get(
                "url", f"https://www.miyoushe.com/ys/article/{post_id}"
            ),
            "author": str(data.get("author_uid", "未知")),
            "post_id": post_id,
            "created_at": fmt_ts(data.get("created_at")),
            "image_count": _count_json(post_dir / "images" / "images.json"),
            "video_count": _count_json(post_dir / "videos" / "videos.json"),
        }
        docs.append(Document(content=plain, metadata=meta))
    return docs


def guess_category(title: str) -> str:
    """按标题关键词粗分类（后续可替换为更精细的分类）。"""
    if any(k in title for k in ("宝箱", "神瞳", "挑战", "解密", "机关", "地灵龛", "印磐", "食谱", "收集")):
        return "解密收集"
    if "任务" in title:
        return "世界任务"
    return "攻略杂谈"


def _count_json(path: Path) -> int:
    try:
        return len(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return 0


def fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except Exception:
        return ""


# ============================================================
# 兼容旧格式：视频转写 / 文本攻略
# ============================================================
def _parse_header(text: str) -> dict:
    """解析 md 文件头部的 HTML 注释元数据。"""
    meta = {}
    m = _HEADER_RE.search(text)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip()
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
    return meta


def _load_text_file(path: Path, base_meta: dict) -> Document:
    text = path.read_text(encoding="utf-8", errors="ignore")
    header_meta = _parse_header(text)
    body = _HEADER_RE.sub("", text)
    meta = {**base_meta, **header_meta}
    meta.setdefault("title", path.stem)
    return Document(content=body, metadata=meta)


def _load_srt_file(path: Path, base_meta: dict) -> Document:
    """解析 SRT 字幕：去掉序号和时间轴，只保留文本行。"""
    lines = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.isdigit():  # 序号行
            continue
        if "-->" in line or re.fullmatch(r"[\d:,\.\s]+", line):  # 时间轴行
            continue
        lines.append(line)
    meta = {**base_meta, "title": path.stem}
    return Document(content="\n".join(lines), metadata=meta)


def _infer_bv_url(filename: str) -> str:
    """从形如 `标题-BV1xxxx.txt` 的文件名里提取 B 站链接。"""
    m = re.search(r"(BV[0-9A-Za-z]+)", filename)
    return f"https://www.bilibili.com/video/{m.group(1)}" if m else f"local:{filename}"


def load_documents() -> list[Document]:
    """扫描全部配置的原始目录，返回 Document 列表。目录不存在时静默跳过。"""
    cfg = load_settings()["paths"]
    docs: list[Document] = []

    # ① 米游社爬虫产物（主数据源）
    docs.extend(_load_miyoushe_output(resolve_path(cfg["raw_output"])))

    # ② 视频转写目录
    video_dir = resolve_path(cfg["raw_video"])
    if video_dir.is_dir():
        for p in sorted(video_dir.iterdir()):
            if not p.is_file():
                continue
            meta = {"platform": "bilibili", "category": "video_transcript"}
            if p.suffix.lower() == ".srt":
                docs.append(_load_srt_file(p, meta))
            elif p.suffix.lower() in (".txt", ".md"):
                docs.append(_load_text_file(p, meta))
            else:
                continue
            docs[-1].metadata.setdefault("source_url", _infer_bv_url(p.name))

    # ③ 文本攻略目录
    text_dir = resolve_path(cfg["raw_text"])
    if text_dir.is_dir():
        for p in sorted(text_dir.iterdir()):
            if not p.is_file() or p.suffix.lower() not in (".txt", ".md"):
                continue
            meta = {"platform": "unknown"}
            docs.append(_load_text_file(p, meta))
            docs[-1].metadata.setdefault("source_url", f"local:{p.name}")

    return docs
