# -*- coding: utf-8 -*-
"""
B站下载 + 语音转写 流水线 CLI
===============================
用法: python video_fetching_and_audio_textualizing_cli.py

流程:
  搜索 -> 下载（自动提取音频）-> 转写（讯飞ASR）
  一条命令走完，不用切换工具。

依赖: video_fetching_and_audio_textualizing_common.py（公共逻辑模块）
"""

import sys
import os
import time
import re

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ── 核心逻辑全部来自 video_fetching_and_audio_textualizing_common ──────────────────────────────────
from video_fetching_and_audio_textualizing_common import (
    # 常量
    DEFAULT_VIDEOS_DIR, DEFAULT_AUDIO_DIR, DEFAULT_OUTPUT_DIR,
    QUALITY_VALID,

    # 搜索
    search_bilibili,

    # 下载
    download_video, format_download_text, detect_parts, get_part_list,

    # 转写
    ensure_xfyun_config,
    resolve_audio_path,
    transcribe_file,

    # 辅助
    format_file_list, parse_selection, SUPPORTED_EXTS, AUDIO_EXTS,
    parse_hotwords_string,
    check_dependencies, format_deps_text,
)

PAGE_SIZE = 10

# ============================================================
# 输出样式
# ============================================================


class QuitProgram(Exception):
    """用户请求退出程序：q/quit 触发或 Ctrl+C / EOF 中断触发。

    message: 退出时打印的文案；为 None 时顶层处理打印默认「再见!」。
    """

    def __init__(self, message=None):
        self.message = message
        super().__init__(message)


def _info(msg: str):
    print(f"[INFO]  {msg}")


def _success(msg: str):
    print(f"[OK]   {msg}")


def _warn(msg: str):
    print(f"[WARN]  {msg}")


def _error(msg: str):
    print(f"[ERROR] {msg}")


def _progress_bar(percent: float, width: int = 40) -> str:
    filled = int(width * min(percent, 1.0))
    bar = "=" * filled + "-" * (width - filled)
    return f"[{bar}]"


def _format_elapsed(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}时{m}分{s}秒"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"

# ============================================================
# 视频列表格式化（CLI 详细版）
# ============================================================


def _fmt_videos(videos, start_index=1):
    """格式化视频列表（CLI 详细展示：含标题/UP主/分区/日期/时长/播放/点赞/收藏/弹幕/链接）"""
    lines = []
    for i, v in enumerate(videos, start_index):
        title = v.get("title", "")
        author = v.get("author", "-")
        typename = v.get("typename", "")
        pubdate = v.get("pubdate", "-")
        duration = v.get("duration", "-")
        play = v.get("play", "0")
        like = v.get("like", "0")
        favorites = v.get("favorites", "0")
        video_review = v.get("video_review", "0")
        url = v.get("url", "")
        lines.append(f"  [{i}] {title}")
        if typename:
            lines.append(f"      UP: {author}  |  分区: {typename}  |  日期: {pubdate}  |  时长: {duration}")
        else:
            lines.append(f"      UP: {author}  |  日期: {pubdate}  |  时长: {duration}")
        lines.append(f"      播放 {play}  |  点赞 {like}  |  收藏 {favorites}  |  弹幕 {video_review}")
        lines.append(f"      -> {url}")
        lines.append("")
    return "\n".join(lines)

# ============================================================
# 搜索
# ============================================================


def _do_search(keyword, page=1):
    """执行搜索并打印结果，返回完整 data（含 videos / page_count）"""
    print(f"\n搜索: {keyword} ...")
    data = search_bilibili(keyword, page, PAGE_SIZE)
    if not data.get("success"):
        print(f"[ERROR] 搜索失败: {data.get('error', 'unknown')}")
        return data
    videos = data.get("videos", [])
    total = data.get("total", 0)
    page_count = data.get("page_count", 1)
    cur_page = data.get("page", page)
    print(f"共 {total} 个结果，第 {cur_page}/{page_count} 页：\n")
    if videos:
        print(_fmt_videos(videos, start_index=(cur_page - 1) * PAGE_SIZE + 1))
    else:
        print("未找到结果")
    return data

# ============================================================
# 画质交互
# ============================================================


def _prompt_quality(prompt_text):
    """询问画质，无效输入循环重试"""
    while True:
        try:
            q = input(prompt_text).strip()
        except KeyboardInterrupt:
            print()  # Ctrl+C 不回显、无回车换行，补一行使其与 Ctrl+Z（EOF）退出的空行一致
            raise QuitProgram("检测到用户输入了 Ctrl+C，系统自动退出。再见！")
        except EOFError:
            raise QuitProgram("检测到用户输入了 Ctrl+Z（EOF），系统自动退出。再见！")
        if q == "" or q in QUALITY_VALID:
            return q or "best"
        print(f"  [!] 无效画质: '{q}'，可选: {', '.join(QUALITY_VALID)} 或直接回车默认 best")

# ============================================================
# 下载
# ============================================================

_parts_cache = {}


def _prompt_select_parts(bvid, part_count):
    """让用户选择要下载的多个分P：列出各P标题（带 [pN] 标记）、支持范围/多选、下载前确认。
    返回 list[int]（选中序号）或 None（用户取消）。"""
    # 获取各分P标题（失败则降级为仅显示序号）
    parts_info = get_part_list(bvid)
    title_map = {}
    if parts_info.get("success") and parts_info.get("parts"):
        print(f"    各分P标题：")
        for p in parts_info["parts"]:
            t = p.get("title") or f"第{p['index']}P"
            print(f"      [p{p['index']}] {t}")
            title_map[p["index"]] = t
    else:
        print(f"    （无法获取分P标题，将仅显示序号）")

    while True:
        try:
            body = input(
                f"  请输入要下载的分P序号（如 1-{part_count}、1,3,5、all; 0 取消）: "
            ).strip()
        except KeyboardInterrupt:
            # Ctrl+C 按全局约定退出程序，不应被当作 0 取消后回到上级菜单
            print()  # Ctrl+C 不回显、无回车换行，补一行使其与 Ctrl+Z（EOF）退出的空行一致
            raise QuitProgram("检测到用户输入了 Ctrl+C，系统自动退出。再见！")
        except EOFError:
            raise QuitProgram("检测到用户输入了 Ctrl+Z（EOF），系统自动退出。再见！")
        if body == "0":
            return None

        # 与「选择下载视频 / 选择转写视频」完全一致的校验与解析逻辑：
        # 仅接受 a/all 或「纯数字/空格/逗号/区间」形式，其余（含字母 p 等）一律视为无效指令
        body_l = body.lower()
        is_selection = body_l in ("all", "a") or bool(re.fullmatch(r'[\d\s,\-]+', body_l))
        if not is_selection:
            print("[!] 未识别到有效指令")
            continue

        # 复用 parse_selection：单个 / 空格或逗号分隔多个 / 区间 / 混合 / all
        chosen = parse_selection(body, part_count, page_start=1)
        if not chosen:
            if re.search(r'\d', body):
                print(f"  [!] 序号超出范围（1-{part_count}），请重新输入（输入 0 可取消）")
            else:
                print("[!] 未识别到有效指令")
            continue

        # 展示选中项（带标题）并确认
        preview = []
        for idx in chosen:
            t = title_map.get(idx, f"第{idx}P")
            preview.append(f"      [p{idx}] {t}")
        print(f"  将下载以下 {len(chosen)} 个分P：")
        print("\n".join(preview))
        try:
            confirm = input("  确定下载以上分P吗？(y 确定，n 重选，0 取消, 默认为 y): ").strip().lower()
        except KeyboardInterrupt:
            # Ctrl+C 按全局约定退出程序，不应与「0 取消」的 return None 撞成同一哨兵
            print()  # Ctrl+C 不回显、无回车换行，补一行使其与 Ctrl+Z（EOF）退出的空行一致
            raise QuitProgram("检测到用户输入了 Ctrl+C，系统自动退出。再见！")
        except EOFError:
            raise QuitProgram("检测到用户输入了 Ctrl+Z（EOF），系统自动退出。再见！")
        if confirm in ("", "y", "yes"):
            return chosen
        if confirm == "0":
            return None
        if confirm in ("n", "no"):
            continue  # 回到输入分P序号，重新选择
        print("  [!] 请输入 y（确定）/ n（重选）/ 0（取消）, 或直接回车默认 y")


def _prompt_parts(bvid, part_count):
    """多P视频下载方式选择，返回 (parts, all_parts)。
    parts: None=只下第1P / list[int]=下载指定分P；all_parts=True 时 parts 为 None。"""
    print(f"    该视频共 {part_count} 个分P，如何下载？")
    print(f"    1) 下载全部P（共 {part_count} 个文件）")
    print(f"    2) 下载部分P（后面会问具体下载哪些分P）")
    print(f"    3) 仅下载第1P")
    while True:
        try:
            sel = input("  请选择 (1/2/3, 默认为 3): ").strip()
        except KeyboardInterrupt:
            # Ctrl+C 不应被误判为「只下第1P」(None, False)，按全局约定转成 QuitProgram 退出
            print()  # Ctrl+C 不回显、无回车换行，补一行使其与 Ctrl+Z（EOF）退出的空行一致
            raise QuitProgram("检测到用户输入了 Ctrl+C，系统自动退出。再见！")
        except EOFError:
            raise QuitProgram("检测到用户输入了 Ctrl+Z（EOF），系统自动退出。再见！")
        if sel == "" or sel == "3":
            return None, False
        if sel == "1":
            return None, True
        if sel == "2":
            selected = _prompt_select_parts(bvid, part_count)
            if selected is None:
                continue  # 用户取消，回到主菜单重选
            return selected, False
        print(f"  [!] 无效选择: '{sel}'，可选: 1/2/3，或直接回车默认 3")


def _do_download_batch(videos, indices, page_start=1):
    """批量下载指定序号（indices 为页面内 1-based，page_start 用于显示全局序号）"""
    print()
    print(f"即将下载以下 {len(indices)} 个视频:")
    for local_num in indices:
        idx = local_num - 1
        v = videos[idx]
        global_num = page_start + idx
        print(f"  [{global_num}] {v['title']}")
    print()
    quality = _prompt_quality("请输入画质 (best/1080p/720p/480p/360p, 默认为 best): ")

    for local_num in indices:
        idx = local_num - 1
        v = videos[idx]
        global_num = page_start + idx
        print(f"\n准备下载视频[{global_num}]...")
        print(f"- 视频标题: {v['title']}")
        q = quality
        print(f"- 观看网址: {v['url']}")
        print(f"- 目标画质: {q}")

        if not v.get("bvid"):
            print(f"  [!] 下载 {v['title']} 失败: 缺少 BV 号，数据不完整")
            continue

        # 分P探测 + 交互选择
        parts, all_parts = None, False
        bvid = v["bvid"]
        if bvid not in _parts_cache:
            _parts_cache[bvid] = detect_parts(bvid)
        det = _parts_cache[bvid]
        if det.get("is_multi"):
            parts, all_parts = _prompt_parts(bvid, det.get("part_count", 1))
            if all_parts:
                print(f"- 模式: 下载全部 {det.get('part_count')} 个分P")
            elif parts:
                print(f"- 模式: 下载指定 {len(parts)} 个分P: [{', '.join(f'p{n}' for n in parts)}]")
            else:
                print(f"- 模式: 仅下载第1P")
        else:
            print(f"  [发现只有 1 个分P，跳过分P 选择，直接下载第1P]")

        try:
            result = download_video(bvid, quality=q, part=parts, all_parts=all_parts, echo=False)
        except KeyboardInterrupt:
            # yt-dlp 下载（subprocess.Popen + proc.wait，最长 1 小时）过程中按 Ctrl+C：
            # KeyboardInterrupt 不是 Exception 子类，不会被 download_video 内的 except Exception 捕获，
            # 需转成 QuitProgram 优雅退出，避免抛 traceback（与转写执行阶段 560/605 同源处理）
            print()
            raise QuitProgram("检测到用户输入了 Ctrl+C，系统自动退出。再见！")
        print()
        if result.get("success"):
            files = result.get("files") or ([result.get("file")] if result.get("file") else [])
            audios = result.get("audio_files") or ([result.get("audio_file")] if result.get("audio_file") else [])
            print(f"视频[{global_num}]下载完成！（共 {len(files)} 个分P文件）")
            qw = result.get("quality_warning")
            if qw:
                print(f"  [!] {qw}")
            if files:
                print(f"- 实际画质: {result.get('quality_actual')}")
                print(f"- 本地视频 ({len(files)} 个):")
                for i, f in enumerate(files, 1):
                    print(f"    [{i}] {f}")
                print(f"- 本地音频 ({len(audios)} 个):")
                for i, a in enumerate(audios, 1):
                    print(f"    [{i}] {a or '提取失败（视频已保存）'}")
        else:
            text = format_download_text(result)
            print(text)
            print(f"  [!] 下载 {v['title']} 失败")

    print()

# ============================================================
# 文件列表表格
# ============================================================


def _disp_width(s) -> int:
    """按终端显示宽度计算字符串宽度（东亚全角/宽字符按 2 计）。"""
    import unicodedata
    w = 0
    for ch in str(s):
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _pad(s, width, align="left") -> str:
    """按显示宽度对齐填充（正确处理中文全角字符）。"""
    s = str(s)
    pad = width - _disp_width(s)
    if pad <= 0:
        return s
    if align == "right":
        return " " * pad + s
    if align == "center":
        left = pad // 2
        return " " * left + s + " " * (pad - left)
    return s + " " * pad


def _truncate_disp(s, max_width) -> str:
    """按显示宽度截断字符串，超出部分以 '..' 结尾。"""
    s = str(s)
    if _disp_width(s) <= max_width:
        return s
    import unicodedata
    out, w = "", 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > max_width - 2:
            break
        out += ch
        w += cw
    return out + ".."


def _print_file_table(files, indices=None):
    """打印文件列表表格：序号 / 文件名 / 类型 / 大小 / 时长 / 画质 / 修改日期。"""
    # (标题, 列宽, body对齐)
    cols = [
        ("序号", 4, "right"),
        ("文件名", 40, "left"),
        ("类型", 11, "left"),
        ("大小", 11, "right"),
        ("时长", 9, "right"),
        ("画质", 7, "center"),
        ("修改日期", 16, "left"),
    ]
    header = " | ".join(_pad(name, w, "center") for name, w, _a in cols)
    line_w = _disp_width(header)
    print()
    print("=" * line_w)
    print(header)
    print("-" * line_w)
    for i, f in enumerate(files, 1):
        mark = " <--" if indices and i in indices else ""
        row = [
            _pad(i, cols[0][1], cols[0][2]),
            _pad(_truncate_disp(f["name"], cols[1][1]), cols[1][1], cols[1][2]),
            _pad(f.get("type_str", "-"), cols[2][1], cols[2][2]),
            _pad(f["size_str"], cols[3][1], cols[3][2]),
            _pad(f.get("duration_str", "-"), cols[4][1], cols[4][2]),
            _pad(f.get("quality_str", "-"), cols[5][1], cols[5][2]),
            _pad(f.get("mtime_str", ""), cols[6][1], cols[6][2]),
        ]
        print(" | ".join(row) + mark)
    print("=" * line_w)

# ============================================================
# 热词输入（CLI 交互）
# ============================================================


def _input_hotwords():
    """让用户输入热词（仅保留 2-16 字符的词，不合规的会被静默丢弃）"""
    print("\n" + "-" * 50)
    print("  热词设置（提升游戏术语识别率）")
    print("-" * 50)
    print("  输入示例: 渊下宫, 七圣召唤, 雷电将军, 元素爆发")
    print("  留空回车跳过，词之间用逗号/分号/顿号（中英文均可）或空格分隔均可")
    print("  每个热词需 2-16 个字符；少于2字或超过16字的热词将自动忽略")
    print("-" * 50)
    try:
        raw = input("热词 (可选输入, 直接回车则默认没有热词): ").strip()
    except KeyboardInterrupt:
        # Ctrl+C 不应被当成「留空跳过热词」(return []) 而继续转写，按全局约定退出程序
        print()  # Ctrl+C 不回显、无回车换行，补一行使其与 Ctrl+Z（EOF）退出的空行一致
        raise QuitProgram("检测到用户输入了 Ctrl+C，系统自动退出。再见！")
    except EOFError:
        raise QuitProgram("检测到用户输入了 Ctrl+Z（EOF），系统自动退出。再见！")
    if not raw:
        return []
    valid, invalid = parse_hotwords_string(raw)
    if invalid:
        print(f"  以下热词因字数不符（需2-16字）已忽略: {', '.join(invalid)}")
    return valid

# ============================================================
# 讯飞凭证检查（CLI UI 版）
# ============================================================


def _check_xfyun_config():
    """检查讯飞凭证是否就绪（CLI 交互提示版）"""
    is_ready, err_msg = ensure_xfyun_config()
    if not is_ready:
        print("\n[WARN] 讯飞凭证未配置，请先按 README.md 配置讯飞凭证后再尝试进入转写模式")
        return False
    return True

# ============================================================
# 进度回调
# ============================================================


class ProgressTracker:
    def __init__(self, name: str):
        self.name     = name
        self.start    = time.time()
        self.last_dot = 0

    def __call__(self, order_id: str, status: str, elapsed: int):
        if status == "uploading":
            print(f"\r  上传中... {'.' * (elapsed % 4)}", end="", flush=True)
        elif status == "polling":
            dot = elapsed // 5
            if dot > self.last_dot:
                self.last_dot = dot
                bar = _progress_bar(elapsed / 300)
                eta = _format_elapsed(elapsed)
                print(f"\r  转写中 {bar}  已用 {eta}...", end="", flush=True)

# ============================================================
# 转写流程（CLI 交互版）
# ============================================================


def _do_transcribe():
    """转写 videos/ 中已下载的视频（CLI 交互版）。

    返回 "search" 表示用户在转写模式中选择 s(earch) 进入搜索模式；
    其余情况（配置缺失 / 无视频 / 正常结束）返回 None。
    """
    if not _check_xfyun_config():
        return

    print()  # 进入转写模式时与上一行输入之间留一个空行，保持与搜索模式一致

    video_folder = DEFAULT_VIDEOS_DIR
    _info("正在读取videos/ 中的（不会读取子目录里面的）音视频文件信息，请稍候...")
    files = format_file_list(video_folder, probe=True)

    if not files:
        _warn("videos/ 中没有音视频文件，请先下载")
        return

    n_video = sum(1 for f in files if not f.get("is_audio"))
    n_audio = len(files) - n_video
    _info(f"共 {len(files)} 个音视频文件（{n_video} 个视频和{n_audio} 个音频，已按修改日期降序排列）")
    _info("支持的音频后缀: " + "/".join(sorted(AUDIO_EXTS)))
    _info("支持的视频后缀: " + "/".join(sorted(SUPPORTED_EXTS - AUDIO_EXTS)))
    _print_file_table(files)

    # 转写模式主循环：留在转写模式继续接收操作指令（不回主菜单）。
    # 操作指令说明仅在进入转写模式时打印一次，之后像下载模式那样，
    # 每轮转写完成后直接回到"请输入操作指令>"，不再重复打印指令提示。
    print()
    print("[转写模式]")
    print("  可用操作指令:")
    ops = [
        ("1", "转写单个文件（第 1 个）"),
        ("1-10", "转写连续区间（第 1~10 个）"),
        ("2,5", "转写多个离散序号（空格 / 逗号分隔皆可）"),
        ("1,3-5,7", "混合转写（第 1、3、4、5、7 个）"),
        ("a(ll)", "转写全部文件"),
        ("s(earch)", "进入搜索模式"),
        ("q(uit)", "退出整个程序"),
    ]
    for _cmd, _desc in ops:
        print(f"    {_cmd:<20} -> {_desc}")
    print()

    while True:
        # 选择文件（直接请求操作指令，不再重复打印操作说明）
        while True:
            try:
                sel = input("请输入操作指令> ").strip()
            except KeyboardInterrupt:
                print()  # Ctrl+C 不回显、无回车换行，补一行使其与 Ctrl+Z（EOF）退出的空行一致
                raise QuitProgram("检测到用户输入了 Ctrl+C，系统自动退出。再见！")
            except EOFError:
                raise QuitProgram("检测到用户输入了 Ctrl+Z（EOF），系统自动退出。再见！")

            if not sel:
                continue
            if sel.lower() in ("s", "search"):
                return "search"
            if sel.lower() in ("q", "quit"):
                raise QuitProgram()

            # 选择文件：仅接受 a / all 或「纯数字/空格/逗号/区间」形式，其余（含字母前缀的输入）
            # 一律视为无效指令，与下载模式校验逻辑统一
            sel_l = sel.lower()
            is_selection = sel_l in ("all", "a") or bool(re.fullmatch(r'[\d\s,\-]+', sel_l))
            if not is_selection:
                print("[!] 未识别到有效指令")
                continue

            # 复用 parse_selection：单个 / 空格或逗号分隔多个 / 区间 / 混合 / all
            indices = parse_selection(sel, len(files))
            if not indices:
                if re.search(r'\d', sel):
                    print(f"[!] 序号超出本页范围（1-{len(files)}），请重新输入")
                else:
                    print("[!] 未识别到有效指令")
                continue

            selected = [files[i - 1] for i in indices]
            print(f"\n即将转写以下 {len(selected)} 个视频:")
            # 显示 parse_selection 返回的真实序号（保留输入顺序、不排序、不重新编号），
            # 与下载模式的呈现方式统一
            for idx in indices:
                print(f"  [{idx}] {files[idx - 1]['name']}")
            break  # 列表末尾不额外空行；热词块开头自带的 "\n" 恰好提供 1 行空行

        # 热词
        hot_words = _input_hotwords()

        # 执行前再次校验凭证：避免选完文件、输完热词后才发现凭证失效，白跑一遍
        if not _check_xfyun_config():
            return

        # 执行转写
        print("\n开始转写音视频...")
        results = []
        audio_dir = DEFAULT_AUDIO_DIR

        for i, f in enumerate(selected, 1):
            print(f"\n{'='*60}")
            print(f"  [{i}/{len(selected)}] 处理: {f['name']}")
            print("=" * 60)

            video_path = f["path"]
            try:
                audio_path, src = resolve_audio_path(video_path, audio_dir)
            except KeyboardInterrupt:
                # 音频提取（ffmpeg）过程中按 Ctrl+C，同样转成 QuitProgram 优雅退出，避免抛 traceback
                print()
                raise QuitProgram("检测到用户输入了 Ctrl+C，系统自动退出。再见！")
            except Exception as e:
                _error(f"音频提取失败: {e}")
                results.append({"name": f["name"], "success": False, "error": str(e)})
                continue

            if src == "original":
                _info(f"音频文件，直接使用: {video_path}")
            elif src == "cached":
                _success(f"已有音频（下载时提取）: {audio_path}")
            else:
                size_mb = os.path.getsize(audio_path) / 1024**2
                _success(f"音频提取完成: {audio_path} ({size_mb:.1f} MB)")

            # 转写
            tracker = ProgressTracker(f["name"])
            try:
                _info(f"上传并转写（热词={hot_words or '无'}）...")
                res = transcribe_file(video_path, hot_words=hot_words,
                                      audio_dir=audio_dir, progress_callback=tracker)
                print()  # 换行

                saved = res["saved"]
                char_count = res["chars"]
                speed = char_count / max(res["duration"], 1)
                print(f"\n  [OK] 转写成功！")
                print(f"     时长: {res['duration']}秒")
                print(f"     字数: {char_count}字")
                print(f"     耗时: {_format_elapsed(res['elapsed'])}")
                print(f"     速度: ~{speed:.0f}字/秒")
                print(f"\n  TXT纯文本: {saved['text_path']}")
                print(f"  SRT字幕: {saved['srt_path']}")
                print(f"  JSON分段: {saved['segments_path']}")

                results.append({
                    "name": f["name"],
                    "success": True,
                    "duration": res["duration"],
                    "chars": char_count,
                    "elapsed": res["elapsed"],
                })

            except KeyboardInterrupt:
                # 上传/转写过程中按 Ctrl+C：KeyboardInterrupt 不是 Exception 子类，
                # 不会被下面的 except Exception 捕获，需仿照其它地方转成 QuitProgram 优雅退出
                print()
                raise QuitProgram("检测到用户输入了 Ctrl+C，系统自动退出。再见！")
            except TimeoutError as e:
                print()
                _error(f"转写超时: {e}")
                results.append({"name": f["name"], "success": False, "error": str(e)})
            except Exception as e:
                print()
                _error(f"转写失败: {e}")
                results.append({"name": f["name"], "success": False, "error": str(e)})

        # 转写完成：提示保存位置，随后外层 while 循环继续，重新显示操作指令（不回主菜单）
        print()  # 与上面每个文件的结果块之间留一个空行
        ok = sum(1 for r in results if r.get("success"))
        if ok > 0:
            _info(f"所有结果已保存到: {DEFAULT_OUTPUT_DIR}")
        else:
            _info("没有成功转写的文件，未生成任何输出")
        print()  # 空一行，再回到"请输入操作指令>"


# ============================================================
# 搜索 + 下载模式
# ============================================================


def _print_download_help(page_start: int, count: int) -> None:
    """打印下载模式操作说明；示例序号按当前页实际全局范围自适应。"""
    end = page_start + max(count - 1, 0)

    def rel(k: int) -> int:
        return min(page_start + k - 1, end)

    one = rel(1)
    three = rel(3)
    four = rel(4)
    five = rel(5)
    seven = rel(7)
    print("[下载模式]")
    print("  可用操作指令:")
    ops = [
        (str(one), f"下载单个视频及其音频（第 {one} 个）"),
        (f"{one}-{end}", f"下载连续区间（第 {one}~{end} 个）"),
        (f"{one} {three} {rel(6)} {rel(4)}", "下载多个离散序号（空格 / 逗号分隔皆可）"),
        (f"{one},{three}-{five},{seven}", f"混合下载（第 {one}、{three}、{four}、{five}、{seven} 个）"),
        ("a(ll)", "下载本页全部视频"),
        ("n(ext)", "下一页搜索结果"),
        ("p(revious)", "上一页搜索结果"),
        ("t(ranscribe)", "进入转写模式"),
        ("b(ack)", "返回搜索模式"),
        ("q(uit)", "退出整个程序"),
    ]
    for _cmd, _desc in ops:
        print(f"    {_cmd:<20} -> {_desc}")
    print()


def _search_download_mode():
    """搜索 + 下载模式：搜索模式输入关键词 -> 下载模式；

    下载模式中 b(ack) 返回搜索模式、t(ranscribe) 进入转写模式、q(uit) 退出整个程序。
    搜索模式仅接受关键词（包括 q/quit 也会作为关键词搜索），不使用任何模式切换指令。
    """
    # 进入搜索模式时与上一行输入之间留一个空行。
    # 注意：input 回车已产生一个换行，故用一个 print()（自带一个换行）即可得到恰好 1 行空行；
    # 若用 print("\n") 会输出“字符串内\n + print自带\n”共 2 个换行，导致空两行。
    print()

    last_keyword = None
    last_page = 1
    last_videos = []

    while True:
        print("[搜索模式] 请输入搜索关键词> ", end="")
        try:
            keyword = input().strip()
        except KeyboardInterrupt:
            print()  # Ctrl+C 不回显、无回车换行，补一行使其与 Ctrl+Z（EOF）退出的空行一致
            raise QuitProgram("检测到用户输入了 Ctrl+C，系统自动退出。再见！")
        except EOFError:
            raise QuitProgram("检测到用户输入了 Ctrl+Z（EOF），系统自动退出。再见！")

        if not keyword:
            continue

        last_page = 1
        last_keyword = keyword

        last_data = _do_search(keyword, last_page)
        last_videos = last_data.get("videos", [])
        last_page_count = last_data.get("page_count", 1)

        if not last_videos:
            continue

        page_start = (last_page - 1) * PAGE_SIZE + 1
        _print_download_help(page_start, len(last_videos))
        while True:
            try:
                cmd = input("请输入操作指令> ").strip()
            except KeyboardInterrupt:
                print()  # Ctrl+C 不回显、无回车换行，补一行使其与 Ctrl+Z（EOF）退出的空行一致
                raise QuitProgram("检测到用户输入了 Ctrl+C，系统自动退出。再见！")
            except EOFError:
                raise QuitProgram("检测到用户输入了 Ctrl+Z（EOF），系统自动退出。再见！")

            if not cmd:
                continue
            if cmd.lower() in ("b", "back"):
                print()
                break
            if cmd.lower() in ("q", "quit"):
                raise QuitProgram()

            # 翻页
            if cmd.lower() in ("n", "next"):
                if not last_keyword:
                    print("[!] 没有搜索记录")
                    continue
                if last_page >= last_page_count:
                    print(f"[!] 已经是最后一页（共 {last_page_count} 页）")
                    continue
                last_page += 1
                last_data = _do_search(last_keyword, last_page)
                last_videos = last_data.get("videos", [])
                last_page_count = last_data.get("page_count", last_page_count)
                if not last_videos:
                    last_page -= 1
                page_start = (last_page - 1) * PAGE_SIZE + 1
                _print_download_help(page_start, len(last_videos))
                continue
            if cmd.lower() in ("p", "previous"):
                if not last_keyword:
                    print("[!] 没有搜索记录")
                    continue
                if last_page <= 1:
                    print("[!] 已经是第一页")
                    continue
                last_page -= 1
                last_data = _do_search(last_keyword, last_page)
                last_videos = last_data.get("videos", [])
                last_page_count = last_data.get("page_count", last_page_count)
                if not last_videos:
                    last_page += 1
                page_start = (last_page - 1) * PAGE_SIZE + 1
                _print_download_help(page_start, len(last_videos))
                continue

            # ASR 转写（t -> 转写模式）
            if cmd.lower().startswith("t") or cmd.lower() == "transcribe":
                if _do_transcribe() == "search":
                    print()  # 转写模式下选择 s(earch) -> 返回搜索模式，与上一行输入留一个空行
                    break
                print()
                continue

            # 下载选择：仅接受 a / all 或「纯数字/空格/逗号/区间」形式，其余（含 d/download
            # 等带字母前缀的输入）一律视为无效指令，无需为每个无效前缀单独写判断
            cmd_l = cmd.lower()
            is_selection = cmd_l in ("all", "a") or bool(re.fullmatch(r'[\d\s,\-]+', cmd_l))
            if not is_selection:
                print("[!] 未识别到有效指令")
                continue

            # 下载（不再需要 d 前缀，与转写选择方式统一，复用 parse_selection）
            # 支持：单个 / 空格或逗号分隔多个 / 区间 / 混合 / all
            indices = parse_selection(cmd, len(last_videos), page_start)
            if indices:
                _do_download_batch(last_videos, indices, page_start)
                continue
            if re.search(r'\d', cmd):
                print(f"[!] 序号超出本页范围（{page_start}-{page_start + len(last_videos) - 1}），请重新输入")
                continue

            # 其余未识别（字母指令或无效输入）一律视为无效指令
            print("[!] 未识别到有效指令")

# ============================================================
# 主入口
# ============================================================


def main():
    print("=" * 60)
    print("  B站视频下载 + 语音转写 流水线")
    print("=" * 60)

    # 第一步：检查环境依赖
    deps_data = check_dependencies()
    print(format_deps_text(deps_data))
    print()

    # 缺少必要依赖（yt-dlp + ffmpeg/ffprobe）时，禁止进入程序
    if not deps_data.get("all_ready"):
        sys.exit(1)

    print("[OK] 环境检查完成，bilibili-pipeline 可进入交互")
    print()

    try:
        # 进入程序后只打印一次模式菜单，避免空回车 / 无效模式时反复刷屏
        print("[成功进入程序]")
        print("  可用模式:")
        print("    s(earch)     -> 搜索模式")
        print("    t(ranscribe) -> 转写模式")
        print("    q(uit)       -> 退出整个程序")
        print()
        while True:
            try:
                choice = input("请输入模式> ").strip().lower()
            except KeyboardInterrupt:
                print()  # Ctrl+C 不回显、无回车换行，补一行使其与 Ctrl+Z（EOF）退出的空行一致
                raise QuitProgram("检测到用户输入了 Ctrl+C，系统自动退出。再见！")
            except EOFError:
                raise QuitProgram("检测到用户输入了 Ctrl+Z（EOF），系统自动退出。再见！")

            if not choice:
                continue
            if choice in ("s", "search"):
                _search_download_mode()
            elif choice in ("t", "transcribe"):
                if _do_transcribe() == "search":
                    _search_download_mode()
            elif choice in ("q", "quit"):
                print()
                print("再见!")
                break
            else:
                print("[!] 未识别到有效模式")
    except QuitProgram as e:
        print()
        print(e.message if e.message else "再见!")

if __name__ == "__main__":
    main()
