import argparse
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
# 输出根目录：固定为脚本所在目录下的 output/，不受运行目录影响
OUTPUT_DIR = Path(__file__).resolve().parent / "output"

# 米游社各游戏板块与 game_id 的对应关系
# 程序会从 URL 路径中自动识别板块，从而选择对应的 game_id
GAME_ID_MAP = {
    "bh3": "1",   # 崩坏3
    "ys": "2",    # 原神
    "wd": "3",    # 未定事件簿
    "dby": "4",   # 大别野
    "sr": "6",    # 崩坏：星穹铁道
    "zzz": "8",   # 绝区零
}

DEFAULT_GAME_ID = "2"   # 默认原神

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.miyoushe.com/",
}

TIMEOUT = 30
SLEEP_INTERVAL = 0.3            # 图片/视频下载间隔（秒）
ARTICLE_SLEEP_INTERVAL = 1.0    # 文章之间的请求间隔（秒）


# ==================== 工具函数 ====================
def extract_post_info(url: str) -> tuple[str, str]:
    """从米游社文章 URL 中提取 post_id 与 game_id。"""
    url = url.strip()
    if not url:
        raise ValueError("URL 为空")

    # 提取 post_id
    match = re.search(r"/article/(\d+)", url)
    if not match:
        raise ValueError(f"无法从 URL 中提取文章 ID：{url}")
    post_id = match.group(1)

    # 从路径中的板块名提取 game_id
    parsed = urllib.parse.urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p]
    game_id = DEFAULT_GAME_ID
    for part in path_parts:
        if part in GAME_ID_MAP:
            game_id = GAME_ID_MAP[part]
            break

    return post_id, game_id


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
    name = re.sub(r"[\\/:*?\"<>|\s]+", "_", name).strip("_")
    return name[:max_len] if name else "untitled"


def extract_ext_from_url(url: str, default: str = "bin") -> str:
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path)
    ext = Path(path).suffix.lstrip(".").lower()
    if ext in {
        "jpg", "jpeg", "png", "gif", "webp", "bmp", "svg",
        "mp4", "webm", "mov", "mkv", "avi", "flv", "m4v"
    }:
        return ext
    return default


def download_file(url: str, save_path: Path) -> bool:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
        resp.raise_for_status()
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return True
    except Exception as e:
        print(f"    [失败] {url} -> {e}")
        return False


def fetch_article_data(post_id: str, game_id: str):
    api_url = f"https://bbs-api.miyoushe.com/post/wapi/getPostFull?gids={game_id}&post_id={post_id}"
    print(f"  正在请求 API：{api_url}")
    resp = requests.get(api_url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    if data.get("retcode") != 0:
        raise RuntimeError(f"API 返回错误：{data.get('message')}")

    return data["data"]["post"]


# ==================== 内容处理函数 ====================
def save_text(post: dict, text_dir: Path, article_url: str) -> dict:
    inner = post["post"]
    title = inner.get("subject", "")
    raw_html = inner.get("content", "")
    created_at = inner.get("created_at", "")
    updated_at = inner.get("updated_at", "")
    author_uid = inner.get("uid", "")
    post_id = inner.get("post_id", "")

    soup = BeautifulSoup(raw_html, "html.parser")
    body_text = soup.get_text("\n", strip=True)

    text_data = {
        "title": title,
        "post_id": post_id,
        "url": article_url,
        "author_uid": author_uid,
        "created_at": created_at,
        "updated_at": updated_at,
        "raw_html": raw_html,
        "plain_text": body_text,
    }

    json_path = text_dir / "article.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(text_data, f, ensure_ascii=False, indent=2)

    txt_path = text_dir / "article.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"标题：{title}\n")
        f.write(f"链接：{article_url}\n")
        f.write(f"作者 UID：{author_uid}\n")
        f.write(f"发布时间：{created_at}\n")
        f.write(f"更新时间：{updated_at}\n")
        f.write("=" * 50 + "\n\n")
        f.write(body_text)

    print(f"  [文本] 已保存到 {txt_path} 和 {json_path}")
    return text_data


def save_images(post: dict, image_dir: Path):
    inner = post["post"]
    image_urls = list(inner.get("images") or [])

    soup = BeautifulSoup(inner.get("content", ""), "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if src and src.startswith("http") and src not in image_urls:
            image_urls.append(src)

    if not image_urls:
        print("  [图片] 未发现图片")
        return

    print(f"  [图片] 发现 {len(image_urls)} 张图片，开始下载...")
    downloaded = []
    for idx, url in enumerate(image_urls, start=1):
        ext = extract_ext_from_url(url, default="jpg")
        save_name = f"img_{idx:03d}.{ext}"
        save_path = image_dir / save_name

        print(f"    ({idx}/{len(image_urls)}) {url}")
        if download_file(url, save_path):
            downloaded.append({"index": idx, "url": url, "file": str(save_path)})
        time.sleep(SLEEP_INTERVAL)

    manifest_path = image_dir / "images.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(downloaded, f, ensure_ascii=False, indent=2)

    print(f"  [图片] 成功下载 {len(downloaded)}/{len(image_urls)} 张")


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
        print("  [视频] 未发现视频")
        with open(video_dir / "videos.json", "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)
        return

    print(f"  [视频] 发现 {len(unique_direct)} 个可直接下载视频，{len(unique_embeds)} 个嵌入式视频")

    downloaded = []

    # 下载可直接下载的视频
    for idx, item in enumerate(unique_direct, start=1):
        url = item["url"]
        ext = extract_ext_from_url(url, default="mp4")
        save_name = f"video_{idx:03d}.{ext}"
        save_path = video_dir / save_name

        print(f"    (视频 {idx}/{len(unique_direct)}) {url}")
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
        print(f"    (嵌入 {idx}/{len(unique_embeds)}) {url}")
        try:
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
            print(f"      [失败] 无法保存嵌入页面：{e}")
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

    print(f"  [视频] 处理完成 {len(downloaded)}/{total} 个")


# ==================== 批量主流程 ====================
def load_urls(path: Path) -> list[str]:
    """从文件加载 URL 列表，跳过空行和注释行。"""
    urls = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)
    return urls


def process_article(url: str, force: bool = False) -> str:
    """处理单篇文章，返回状态：success / skipped / failed。"""
    print(f"\n[处理] {url}")
    try:
        post_id, game_id = extract_post_info(url)
        print(f"  post_id={post_id}, game_id={game_id}")
    except ValueError as e:
        print(f"  [跳过] {e}")
        return "failed"

    dirs = get_post_dirs(post_id)
    ensure_dirs(dirs)

    # 若已存在且非强制模式则跳过
    json_path = dirs["text"] / "article.json"
    if json_path.exists() and not force:
        print(f"  [跳过] 该文章已存在：{json_path}")
        print("        如需重新下载，请使用 --force 参数")
        return "skipped"

    try:
        post = fetch_article_data(post_id, game_id)
    except Exception as e:
        print(f"  [失败] 获取文章数据失败：{e}")
        return "failed"

    try:
        text_data = save_text(post, dirs["text"], url)
        print(f"  标题：{text_data['title']}")
        print(f"  纯文本长度：{len(text_data['plain_text'])} 字符")
    except Exception as e:
        print(f"  [失败] 保存文本失败：{e}")
        return "failed"

    try:
        save_images(post, dirs["images"])
    except Exception as e:
        print(f"  [失败] 保存图片失败：{e}")

    try:
        save_videos(post, dirs["videos"])
    except Exception as e:
        print(f"  [失败] 保存视频失败：{e}")

    return "success"


def main():
    parser = argparse.ArgumentParser(description="米游社攻略批量爬取工具")
    script_dir = Path(__file__).resolve().parent
    parser.add_argument(
        "--urls-file",
        type=str,
        default=str(script_dir / "urls.txt"),
        help="存放文章 URL 列表的文件，默认为 urls.txt",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重新下载，即使该文章已存在",
    )
    args = parser.parse_args()

    urls_file = Path(args.urls_file)
    if not urls_file.exists():
        print(f"错误：URL 文件不存在：{urls_file}")
        print("请创建该文件，每行写入一个米游社文章链接。")
        return

    urls = load_urls(urls_file)
    if not urls:
        print("错误：URL 文件为空或全是注释/空行。")
        return

    print(f"共读取到 {len(urls)} 个 URL，开始批量爬取...")

    success = 0
    failed = 0
    skipped = 0

    for idx, url in enumerate(urls, start=1):
        print(f"\n[{idx}/{len(urls)}]")
        status = process_article(url, force=args.force)
        if status == "success":
            success += 1
        elif status == "skipped":
            skipped += 1
        else:
            failed += 1

        if idx < len(urls):
            time.sleep(ARTICLE_SLEEP_INTERVAL)

    print(f"\n批量爬取完成：成功 {success}，失败 {failed}，跳过 {skipped}，总计 {len(urls)}")
    print(f"\n输出根目录：{OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
