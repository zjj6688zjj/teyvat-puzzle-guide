import json
import os
import re
import time
import urllib.parse
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# 在 Windows 终端下强制使用 UTF-8 输出中文，避免控制台乱码
import sys
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ==================== 配置区 ====================
ARTICLE_URL = "https://www.miyoushe.com/ys/article/76382075"
POST_ID = "76382075"           # 文章 ID，可从 URL 中提取
GAME_ID = "2"                  # 原神对应的 game_id

# 输出根目录：固定为脚本所在目录下的 output/，不受运行目录影响
OUTPUT_DIR = Path(__file__).resolve().parent / "output"

# 请求头（模拟浏览器，降低被拦截概率）
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.miyoushe.com/",
}

# 下载超时（秒）
TIMEOUT = 30
# 下载间隔（秒），防止请求过快
SLEEP_INTERVAL = 0.3


# ==================== 工具函数 ====================
def get_post_dirs(post_id: str):
    """根据文章 ID 生成独立的输出目录，避免不同攻略互相覆盖。"""
    base = OUTPUT_DIR / post_id
    return {
        "text": base / "text",
        "images": base / "images",
        "videos": base / "videos",
    }


def ensure_dirs(dirs: dict):
    """创建输出目录。"""
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """将字符串处理成可作为文件名的安全形式。"""
    name = re.sub(r"[\\/:*?\"<>|\s]+", "_", name).strip("_")
    return name[:max_len] if name else "untitled"


def extract_ext_from_url(url: str, default: str = "bin") -> str:
    """从 URL 中提取文件扩展名。"""
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path)
    ext = Path(path).suffix.lstrip(".").lower()
    # 过滤常见图片/视频扩展名，其他情况使用默认值
    if ext in {
        "jpg", "jpeg", "png", "gif", "webp", "bmp", "svg",
        "mp4", "webm", "mov", "mkv", "avi", "flv", "m4v"
    }:
        return ext
    return default


def download_file(url: str, save_path: Path) -> bool:
    """下载单个文件到指定路径，返回是否成功。"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
        resp.raise_for_status()
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return True
    except Exception as e:
        print(f"  [失败] {url} -> {e}")
        return False


def fetch_article_data(post_id: str):
    """调用米游社 API 获取文章原始数据。"""
    api_url = f"https://bbs-api.miyoushe.com/post/wapi/getPostFull?gids={GAME_ID}&post_id={post_id}"
    print(f"正在请求 API：{api_url}")
    resp = requests.get(api_url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    if data.get("retcode") != 0:
        raise RuntimeError(f"API 返回错误：{data.get('message')}")

    return data["data"]["post"]


# ==================== 内容处理函数 ====================
def save_text(post: dict, text_dir: Path) -> dict:
    """提取并保存文本内容。"""
    inner = post["post"]
    title = inner.get("subject", "")
    raw_html = inner.get("content", "")
    created_at = inner.get("created_at", "")
    updated_at = inner.get("updated_at", "")
    author_uid = inner.get("uid", "")
    post_id = inner.get("post_id", "")

    # 使用 BeautifulSoup 从 HTML 中抽取纯文本
    soup = BeautifulSoup(raw_html, "html.parser")
    body_text = soup.get_text("\n", strip=True)

    # 同时把结构化的文本也保留下来（更利于后续分析）
    text_data = {
        "title": title,
        "post_id": post_id,
        "url": ARTICLE_URL,
        "author_uid": author_uid,
        "created_at": created_at,
        "updated_at": updated_at,
        "raw_html": raw_html,
        "plain_text": body_text,
    }

    # 保存 JSON（含完整元数据）
    json_path = text_dir / "article.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(text_data, f, ensure_ascii=False, indent=2)

    # 保存纯文本阅读版
    txt_path = text_dir / "article.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"标题：{title}\n")
        f.write(f"链接：{ARTICLE_URL}\n")
        f.write(f"作者 UID：{author_uid}\n")
        f.write(f"发布时间：{created_at}\n")
        f.write(f"更新时间：{updated_at}\n")
        f.write("=" * 50 + "\n\n")
        f.write(body_text)

    print(f"[文本] 已保存到 {txt_path} 和 {json_path}")
    return text_data


def save_images(post: dict, image_dir: Path):
    """提取并保存图片。"""
    inner = post["post"]
    # 米游社 API 直接在 post['images'] 中给出所有配图 URL
    image_urls = list(inner.get("images") or [])

    # 兜底：从 content HTML 中再抓取一遍实际有 src 的图片
    soup = BeautifulSoup(inner.get("content", ""), "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if src and src.startswith("http") and src not in image_urls:
            image_urls.append(src)

    if not image_urls:
        print("[图片] 未发现图片")
        return

    print(f"[图片] 发现 {len(image_urls)} 张图片，开始下载...")
    downloaded = []
    for idx, url in enumerate(image_urls, start=1):
        ext = extract_ext_from_url(url, default="jpg")
        save_name = f"img_{idx:03d}.{ext}"
        save_path = image_dir / save_name

        print(f"  ({idx}/{len(image_urls)}) {url}")
        if download_file(url, save_path):
            downloaded.append({"index": idx, "url": url, "file": str(save_path)})
        time.sleep(SLEEP_INTERVAL)

    # 保存图片清单
    manifest_path = image_dir / "images.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(downloaded, f, ensure_ascii=False, indent=2)

    print(f"[图片] 成功下载 {len(downloaded)}/{len(image_urls)} 张，清单：{manifest_path}")


def save_videos(post: dict, video_dir: Path):
    """提取并保存视频（含米游社原生视频、<video> 标签及 iframe 嵌入式视频）。"""
    inner = post["post"]
    content = inner.get("content", "")
    soup = BeautifulSoup(content, "html.parser")

    # 1) 米游社原生视频：在 outer post 的 vod_list 中，取最高清晰度
    direct_videos = []
    for vod in post.get("vod_list") or []:
        if not isinstance(vod, dict):
            continue
        resolutions = vod.get("resolutions") or []
        if not resolutions:
            continue
        best = max(
            resolutions,
            key=lambda r: (r.get("width", 0) * r.get("height", 0)),
        )
        url = best.get("url")
        if url:
            direct_videos.append({
                "type": "vod",
                "url": url,
                "cover": vod.get("cover"),
                "definition": best.get("definition") or best.get("label"),
                "width": best.get("width"),
                "height": best.get("height"),
                "resolutions": resolutions,
            })

    # 2) 兼容旧字段 post.videos（极少出现）
    videos_field = inner.get("videos")
    if isinstance(videos_field, list):
        for item in videos_field:
            if isinstance(item, dict):
                url = item.get("url")
                if url:
                    direct_videos.append({"type": "direct", "url": url})
            elif isinstance(item, str) and item.startswith("http"):
                direct_videos.append({"type": "direct", "url": item})
    elif isinstance(videos_field, str) and videos_field.startswith("http"):
        direct_videos.append({"type": "direct", "url": videos_field})

    # 3) content 中的 <video> / <source> 标签
    for video in soup.find_all("video"):
        src = video.get("src")
        if src:
            direct_videos.append({"type": "direct", "url": src})
        for source in video.find_all("source"):
            src = source.get("src")
            if src:
                direct_videos.append({"type": "direct", "url": src})

    # 4) content 中的 iframe 嵌入式视频（常见为 Bilibili）
    embeds = []
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src")
        if src and src.startswith("http"):
            embeds.append({"type": "embed", "url": src})

    # 去重并保持顺序
    seen_urls = set()
    unique_direct = []
    for item in direct_videos:
        url = item["url"]
        if url not in seen_urls:
            seen_urls.add(url)
            unique_direct.append(item)
    unique_embeds = []
    for item in embeds:
        url = item["url"]
        if url not in seen_urls:
            seen_urls.add(url)
            unique_embeds.append(item)

    total = len(unique_direct) + len(unique_embeds)
    if total == 0:
        print("[视频] 未发现视频")
        with open(video_dir / "videos.json", "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)
        return

    print(f"[视频] 发现 {len(unique_direct)} 个可直接下载视频，{len(unique_embeds)} 个嵌入式视频")

    downloaded = []

    # 下载可直接下载的视频
    for idx, item in enumerate(unique_direct, start=1):
        url = item["url"]
        ext = extract_ext_from_url(url, default="mp4")
        save_name = f"video_{idx:03d}.{ext}"
        save_path = video_dir / save_name

        print(f"  (视频 {idx}/{len(unique_direct)}) {url}")
        if download_file(url, save_path):
            entry = {
                "index": idx,
                "type": item["type"],
                "url": url,
                "file": str(save_path),
            }
            if item.get("definition"):
                entry["definition"] = item["definition"]
            if item.get("cover"):
                entry["cover"] = item["cover"]
            downloaded.append(entry)
        time.sleep(SLEEP_INTERVAL)

    # 保存嵌入式视频引用（iframe 页面，作为离线参考）
    for idx, item in enumerate(unique_embeds, start=1):
        url = item["url"]
        save_name = f"embed_{idx:03d}.html"
        save_path = video_dir / save_name
        print(f"  (嵌入 {idx}/{len(unique_embeds)}) {url}")
        try:
            # iframe 页面通常需要对应 Referer，这里用原链接尝试
            resp = requests.get(url, headers={**HEADERS, "Referer": url}, timeout=TIMEOUT)
            resp.raise_for_status()
            with open(save_path, "wb") as f:
                f.write(resp.content)
            downloaded.append({
                "index": idx,
                "type": "embed",
                "url": url,
                "file": str(save_path),
            })
        except Exception as e:
            print(f"    [失败] 无法保存嵌入页面：{e}")
            downloaded.append({
                "index": idx,
                "type": "embed",
                "url": url,
                "file": None,
            })
        time.sleep(SLEEP_INTERVAL)

    manifest_path = video_dir / "videos.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(downloaded, f, ensure_ascii=False, indent=2)

    print(f"[视频] 处理完成 {len(downloaded)}/{total} 个，清单：{manifest_path}")


# ==================== 主流程 ====================
def main():
    post_id = POST_ID
    dirs = get_post_dirs(post_id)
    ensure_dirs(dirs)
    post = fetch_article_data(post_id)

    # 1. 文本
    text_data = save_text(post, dirs["text"])
    print(f"\n文章标题：{text_data['title']}")
    print(f"纯文本长度：{len(text_data['plain_text'])} 字符\n")

    # 2. 图片
    save_images(post, dirs["images"])
    print()

    # 3. 视频
    save_videos(post, dirs["videos"])
    print()

    print("全部处理完成，输出目录：", dirs["text"].parent.resolve())


if __name__ == "__main__":
    main()
