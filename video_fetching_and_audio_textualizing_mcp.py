# -*- coding: utf-8 -*-
"""
B站视频 + 语音转写 流水线 MCP Server
=====================================
一站式 MCP 服务：搜索 B站视频 → yt-dlp 下载 → 讯飞 ASR 语音转写。

暴露的 MCP 工具（共 13 个）:

  搜索与下载:
    search_video          搜索 Bilibili 视频
    download_video        下载视频（自动提取音频）
    get_video_info        获取视频详情（画质/大小/可用高度等）
    get_part_list         获取视频分P列表（各分P序号与标题）
    check_deps            检查依赖（yt-dlp + ffmpeg + ffprobe + 讯飞凭证）
    check_xfyun_config    主动预检讯飞凭证是否配置就绪（转写前可用）
    get_quality_summary   批量查询多视频画质支持情况

  跟下载和转写相关的配置:
    get_user_prefs        读取 config.json 偏好（画质/降级/自动转写/热词）
    init_user_prefs       首次初始化 / 重新配置 偏好并写入 config.json
    update_user_prefs     单项修改 / 清空 偏好（不覆盖其它字段）

  语音转写:
    list_videos           列出音视频文件（用于转写）
    transcribe            转写为文字 + SRT（自动识别单文件/批量）
    validate_hotwords     校验热词合规性（transcribe 前预检）

使用方法:
  python video_fetching_and_audio_textualizing_mcp.py

依赖: video_fetching_and_audio_textualizing_common.py（公共逻辑模块）

设计说明（务必阅读）:
  ★ 本 server 无「模式切换」概念。它只是一组【无状态】的 MCP 工具，由 agent 按需调用，
    不存在 CLI 那样的「搜索模式 / 下载模式 / 转写模式」交互子菜单或状态机。
    agent 听到的「进入转写模式」等自然语言，应理解为「接下来要调用 list_videos + transcribe
    的组合流程」，而非切换到某个持久状态；server 也不会保存「当前处于某模式」。
  ★ 本 MCP server 虽与 CLI（video_fetching_and_audio_textualizing_cli.py）同源、共用
    common 模块的核心逻辑与排版函数（format_file_table / format_search_results 等），
    但二者形态存在本质区别：
      - CLI 是有状态的终端交互程序，可反复进入某模式、循环操作
        （如转写模式可反复选文件、反复转写，并维护选择状态）；
      - MCP 是无状态的工具服务，每次「转写 / 下载」都是 agent 临场决定调用哪些工具的一次性流程，
        不保持「当前处于某模式」的状态。
    因此不要把 CLI 的「模式」概念套用到本 server；凡涉及流程编排，由 agent 按各工具的
    description 中写明的铁律逐步调用对应工具即可。
"""

import sys
import io
import json
import os
import subprocess
import unicodedata
import contextlib

# ── 核心逻辑全部来自 video_fetching_and_audio_textualizing_common ──────────────────────────────────
from video_fetching_and_audio_textualizing_common import (
    # 常量
    DEFAULT_VIDEOS_DIR,
    DEFAULT_OUTPUT_DIR,
    QUALITY_VALID,
    QUALITY_HEIGHT,

    # 搜索
    search_bilibili,
    format_search_results,

    # 下载
    download_video, format_download_text, detect_parts, get_part_list, check_quality_available,
    preflight_quality_ok, post_download_quality_check, detect_parts_cached,

    # 转写
    ensure_xfyun_config,
    transcribe_file,
    parse_hotwords_string,
    validate_hotwords,

    # 辅助
    format_file_list,
    format_file_table,
    parse_selection,
    check_dependencies, format_deps_text,
    PROBE_HINT,

    # 内部私有辅助（被本文件内移入的函数复用）
    XFYunConfig,
    _format_bytes,
    _get_ytdlp_cmd,
    _parse_bvid_or_url,
    _run_capture,
    _run_batch,
)


# ============================================================
# MCP 协议处理
# ============================================================

# 在其它任何重定向发生前捕获真实 stdout，作为 JSON-RPC 通道的永久出口。
# 这样即便 _dispatch_tool_call 内部被 redirect_stdout 临时换成黑洞，
# 下列三个发送函数依然写到真正的 stdout，协议不被吞掉。
_REAL_STDOUT = sys.stdout


def _send_response(req_id, result):
    payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}, ensure_ascii=False) + "\n"
    _REAL_STDOUT.buffer.write(payload.encode("utf-8"))
    _REAL_STDOUT.buffer.flush()


def _send_error(req_id, code, message):
    payload = json.dumps({
        "jsonrpc": "2.0", "id": req_id,
        "error": {"code": code, "message": message}
    }, ensure_ascii=False) + "\n"
    _REAL_STDOUT.buffer.write(payload.encode("utf-8"))
    _REAL_STDOUT.buffer.flush()


def _send_notification(method, params):
    """发送 JSON-RPC 通知（无 id，客户端不应回复）。用于转写进度推送。"""
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, ensure_ascii=False) + "\n"
    _REAL_STDOUT.buffer.write(payload.encode("utf-8"))
    _REAL_STDOUT.buffer.flush()


class _ProgressSender:
    """transcribe_file 的进度回调，转成 MCP notifications/progress。

    progress_token 为 None（客户端未提供）时不发通知，但仍把阶段信息累积在
    last_stage() 中，供最终响应文本展示（长任务不黑屏兜底）。
    """

    def __init__(self, progress_token):
        self.token = progress_token
        self._last = "等待开始..."

    def __call__(self, order_id, status, elapsed):
        if status == "uploading":
            stage = "上传中..."
            pct = 0
        else:  # polling
            pct = int(min(elapsed, 300) / 300 * 100)
            stage = f"转写中 {pct}% (已用 {elapsed}s)"
        self._last = stage
        if self.token is not None:
            try:
                _send_notification("notifications/progress", {
                    "progressToken": self.token,
                    "progress": pct,
                    "total": 100,
                    "message": stage,
                })
            except Exception:
                pass

    def last_stage(self):
        return self._last


def _make_progress_sender(progress_token):
    return _ProgressSender(progress_token)


def _safe_int(v, default=1):
    """安全地把参数转成 int；非数字 / None / 空字符串时回退 default。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _ensure_deps_ready(req_id, required=("yt-dlp", "ffmpeg", "ffprobe")):
    """下载类操作前的依赖预检兜底。

    缺失必需依赖时，直接发送错误响应并返回 False（调用方应 return）；
    否则返回 True。仅作用于 mcp 层，不改动 common / CLI。
    """
    data = check_dependencies()
    deps = data.get("dependencies", {})
    missing = [n for n in required if not deps.get(n, {}).get("available", False)]
    if missing:
        text = format_deps_text(data)
        detail = f"{text}\n\n本次操作缺失依赖: {', '.join(missing)}，无法继续执行。"
        _send_error(req_id, -31003, detail)
        return False
    return True


def _resolve_allow_fallback(args, prefs, quality):
    """解析'不降级下载'策略（allow_quality_fallback）。与 CLI(_do_download_batch)完全对齐。

    优先级：
      ① config.json 已设定（非 null）-> 直接用，不再询问；
      ② 配置未设定 且 本次 quality == "best" -> 静默返回 False（best 即最高画质，
         不存在降级场景，CLI 在此情形连问都不问、直接按「拒不降级」处理，
         且无视 agent 本次传入的 allow_quality_fallback，绝不报错）；
      ③ 配置未设定 且 quality != "best" -> 看 agent 本次是否显式传入（agent 已按铁律
         询问用户），有则采用；
      ④ 都无 -> 返回 None，由调用方报错要求 agent 先询问再传参（MCP 无法交互，
         以报错代替 CLI 的交互阻塞询问）。
    """
    cfg = prefs.get("allow_quality_fallback")
    if cfg is not None:
        return bool(cfg)
    if quality == "best":
        # 与 CLI 完全一致：best 即最高画质，无降级场景，静默取 False，且无视 agent 传入
        return False
    arg = args.get("allow_quality_fallback", None)
    if isinstance(arg, bool):
        return arg
    return None


def _resolve_auto_transcribe(args, prefs):
    """解析'下载后自动转写'策略，配置优先铁律。

    ① config.json 的 auto_transcribe_after_download 已设定（非 null）-> 直接用；
    ② 配置未设定 -> 看 agent 本次是否显式传入 auto_transcribe 参数；
    ③ 都无（config 未设且本次未传参）-> 返回 None，由调用方报错要求先询问
        （与 CLI 统一口径：未配置时 CLI 会交互询问，MCP 无法交互故报错让 agent 去问，
        绝不静默取 False）。
    """
    cfg = prefs.get("auto_transcribe_after_download")
    if cfg is not None:
        return bool(cfg)
    arg = args.get("auto_transcribe")
    if isinstance(arg, bool):
        return arg
    return None


def _build_parts_preview(bvid_or_url, all_parts, norm_part):
    """构造已下载分P的可读预览（多P时含标题），单P视频返回空串（#7）。"""
    try:
        det = detect_parts_cached(bvid_or_url)
    except Exception:
        return ""
    if not det.get("success"):
        return ""
    pc = det.get("part_count", 0)
    if pc <= 1:
        return ""  # 单P 视频，无需标注分P
    parts = det.get("parts", []) or []
    if all_parts:
        indices = list(range(1, pc + 1))
    elif isinstance(norm_part, list):
        indices = norm_part
    elif isinstance(norm_part, int):
        indices = [norm_part]
    else:
        indices = [1]
    chunks = []
    for i in indices:
        if 1 <= i <= pc:
            title = parts[i - 1].get("title", "") if i - 1 < len(parts) else ""
            chunks.append(f"[p{i}] {title}" if title else f"[p{i}]")
    if not chunks:
        return ""
    return "下载分P: " + " | ".join(chunks)


# ============================================================
# 用户偏好（画质 / 画质降级 / 自动转写 / 热词）— 配置优先于"每次询问"
# ============================================================
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

_PREFS_KEYS = ("default_quality", "allow_quality_fallback",
               "auto_transcribe_after_download", "hot_words")


def _load_user_prefs():
    """读取 config.json 中的用户偏好。与 CLI(_load_prefs) 完全一致。

    当且仅当 default_quality / allow_quality_fallback /
    auto_transcribe_after_download / hot_words 四个偏好字段都存在于
    config.json 时，返回含这四个字段的 dict（字段值允许为 None / [] /
    具体列表，逐字段取 config 中的真实值，不做额外默认值兜底）；
    否则（文件不存在 / JSON 解析失败 / 四字段未全部存在）返回 None，
    即视为「未初始化」。
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not all(k in cfg for k in _PREFS_KEYS):
        return None
    return {
        "default_quality": cfg.get("default_quality"),
        "allow_quality_fallback": cfg.get("allow_quality_fallback"),
        "auto_transcribe_after_download": cfg.get("auto_transcribe_after_download"),
        "hot_words": cfg.get("hot_words"),
    }


def _save_user_prefs(prefs):
    """把四个偏好字段写回 config.json，保留其它字段（讯飞凭证等）。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {}
    for k in _PREFS_KEYS:
        if k in prefs:
            cfg[k] = prefs[k]
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def _format_user_prefs_text(prefs):
    """把偏好渲染成 agent 易读的文本，强调"配置优先级高于询问用户"。

    与 CLI(_print_prefs_status / _ensure_user_prefs) 的初始化粒度一致：
    prefs 为 None 表示四偏好字段未齐全（未初始化），此时只提示先 init；
    prefs 为非 None dict 表示已初始化（四字段均存在，值可为 None/[]/列表）。
    """
    if prefs is None:
        return ("【当前用户偏好（来自 config.json）】\n"
                "  [未初始化] 四偏好字段未齐全。请先按 画质/降级/自动转写/热词 四问依次"
                "【询问用户】，再调 init_user_prefs 一次性写入；写入后这四个字段即直接采用，"
                "不再询问。（已设定字段必须直接使用，严禁就该项再询问用户）")
    q = prefs.get("default_quality")
    fb = prefs.get("allow_quality_fallback")
    auto = prefs.get("auto_transcribe_after_download")
    hw = prefs.get("hot_words")
    lines = ["【当前用户偏好（来自 config.json，优先级最高）】"]
    lines.append(f"  画质 default_quality: {q if q is not None else '未设定 → 需按铁律询问用户'}")
    lines.append(f"  画质降级 allow_quality_fallback: {fb if fb is not None else '未设定 → 需按铁律询问用户'}（True=目标画质不支持时自动降级）")
    lines.append(f"  自动转写 auto_transcribe_after_download: {auto if auto is not None else '未设定 → 需询问用户'}")
    if hw is None:
        lines.append("  热词 hot_words: 未设定 → 每次需询问用户")
    elif len(hw) == 0:
        lines.append("  热词 hot_words: 空表 []（已决定不加热词，静默使用空表）")
    else:
        lines.append(f"  热词 hot_words: {hw}（已决定沿用，静默代入）")
    lines.append("")
    lines.append("  [已初始化] 上述非 null 字段必须直接使用，严禁就该项再询问用户；"
                 "只有标为'需询问用户'的 null 字段才按铁律询问。")
    lines.append("【铁律】配置文件即用户意志，优先级高于任何'先问用户'的提示。")
    return "\n".join(lines)


# ============================================================
# MCP 工具定义
# ============================================================

TOOLS = [
    # ==================== 搜索与下载 ====================
    {
        "name": "search_video",
        "description": ("搜索 Bilibili 视频。按关键词搜索，【返回查到的全部信息】供用户挑选下载：序号、标题、UP主、分区、上传日期、时长、"
                        "播放、点赞、收藏、弹幕、简介、标签，以及每条视频的完整 URL 链接（务必带 URL，便于用户定位/直接下载）。支持分页(page)。\n"
                        "【流程】搜到结果后，把返回的【块式列表】（含全部字段与完整 URL）完整呈现给用户，"
                        "【不要只摘几条】，也不要强行改排成对齐表格（块式即原始返回格式）；"
                        "并【明确询问】想下载哪些（如'第1、3个''前5个''全部'或具体标题），"
                        "以及是否要翻页看更多（再调本工具 page+1）；不要自作主张直接下载。"
                        "用户选定后，再去调 download_video（并严格按铁律先询问画质/分P）。"
                        "若用户想要'搜索→下载→转写'一站式，下载完成后继续调 transcribe 转写刚下载的文件。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词，如：Python教程、初音ミク、考研计算机",
                },
                "page": {
                    "type": "integer",
                    "description": "页码，默认第1页",
                    "default": 1,
                },
                "page_size": {
                    "type": "integer",
                    "description": "每页数量，默认10",
                    "default": 10,
                },
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "download_video",
        "description": ("使用 yt-dlp 下载 Bilibili 视频到本地并提取音频到 videos/audios/，支持 BV 号/URL 与 画质(quality)/分P(part)。\n"
                        "【配置优先·铁律】调用前先调 get_user_prefs；已设字段直接采用、不追问，仅 config=null 才按下列询问并显式传参；"
                        "未设且未传必填项→本工具直接报错代问。\n"
                        "【下载前必须先问】凡未由 config 决定的选择处你必须先问：\n"
                        "  1) 画质：仅 default_quality=null 时先查支持画质并【问】用户选哪个（best 可推但不默认）；已设直接用。批量统一问一次。\n"
                        "  2) 分P：多P 先 get_part_list 展示各分P并【问】全部(all_parts=true)/仅第1P(part=1)/指定(part=[1,3,5]或'1-5')，绝不默认；单P直接下；批量各视频独立 part。\n"
                        "  3) 降级：config 已设直接用；未设先问(True/False)并传入；都无时 quality=best 静默取 False，否则报错代问。\n"
                        "  4) 自动转写：config 已设直接用；未设先问并传入，都无则报错；为 true 下载后自动讯飞转写、结果附响应。\n"
                        "  5) 热词：仅 auto_transcribe=true 且 config 的 hot_words=null 才传；用户无则 ''（省略报错）；已设([]/列表)忽略传入。每词 2-16 字、仅空格分隔。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "bvid_or_url": {
                    "type": ["string", "array"],
                    "description": "视频BV号或完整B站URL；也可传字符串数组批量下载。如：BV1xx5411W2 或 ['BV1..','BV2..']",
                },
                "quality": {
                    "type": "string",
                    "enum": list(QUALITY_VALID),
                    "description": ("【配置优先】config 已设 default_quality 时无需传本参数（自动采用，已设项严禁再问）；"
                                    "仅当 config 该字段为 null（每次询问）时，你(agent)才必须先问用户再显式传入 quality，省略且 config 未设会报错。\n"
                                    "画质: best/1080p/720p/480p/360p。是否降级取决于 allow_quality_fallback"
                                    "（false=源端无目标画质则跳过/删除；true=自动降级到更低可用画质）。"
                                    "best 仅可在用户明确选择或 config 已设 default_quality=best 时使用，不得未经询问默认 best。"),
                },
                "allow_quality_fallback": {
                    "type": ["boolean", "null"],
                    "description": ("【配置优先】config 已设本字段时无需传（自动采用）；仅当 config 为 null（每次询问）时你必须先问用户再显式传入，省略且 config 未设会报错。\n"
                                    "目标画质不可用时是否接受降级下载。config 已设则直接用；否则你必须先问用户(True=接受降级/False=不降级)并显式传入，不得省略（省略报错）。例外：quality=best 时未传则静默取 False（best 即最高画质、无降级场景、无需问也不报错）。"),
                },
                "auto_transcribe": {
                    "type": "boolean",
                    "description": ("【配置优先】config 已设 auto_transcribe_after_download 时无需传（自动采用）；仅当 config 为 null（每次询问）时你必须先问用户再显式传入，省略且 config 未设会报错。\n"
                                    "下载完成后是否自动对提取音频调讯飞转写。config 已设则直接用；否则先问用户并显式传入，不得省略（省略报错）。"),
                },
                "hot_words": {
                    "type": "string",
                    "description": ("【配置优先】config 已设 hot_words（[]或具体列表）时无需传（自动采用、忽略本参数）；仅当 config 为 null（每次询问）且 auto_transcribe=true 时才必传，省略且 config 未设会报错。\n"
                                    "热词列表，词间【仅空格分隔】（如: 渊下宫 七圣召唤 雷电将军；逗号/分号/顿号等标点视为词的一部分、不作分隔）。"
                                    "仅当 auto_transcribe=true 且 config 的 hot_words=null 时才必传：用户说没有就传空串 ''（不能省略，省略报错）；config 已设则忽略本参数、直接采用。每个热词 2-16 字。"),
                },
                "download_dir": {
                    "type": "string",
                    "description": "下载目录路径，默认 videos/",
                },
                "part": {
                    "type": ["integer", "array", "string"],
                    "description": ("指定下载的分P（1-based）。多P视频【必须】先询问用户要下哪些并把答案作为本参数传入（如 '1,3' 或 'all'），省略报错；单P视频忽略。"
                                    "支持: 整数(3)、数组([1,3,5])、字符串('1,3,5'/'1-5'/'all')。与 all_parts 等价：字符串 'all'（或 'a'）会被归一为 all_parts=true，二选一即可（传 part='all' 时 all_parts 自动视为 true）。"
                                    "批量时对各条目统一生效；任一目标为多P且未指定 part/all_parts 则报错，建议逐视频调用。下载前可用 get_part_list 看分P标题。"),
                },
                "all_parts": {
                    "type": "boolean",
                    "description": "是否下载该视频的全部分P。true 时每个分P独立成文件并分别提取音频。",
                    "default": False,
                },
                "concurrency": {
                    "type": "integer",
                    "description": "批量下载时同时进行的任务数（仅当 bvid_or_url 为数组时生效），默认1（串行）。",
                    "default": 1,
                },
            },
            "required": ["bvid_or_url"],
        },
    },
    {
        "name": "get_video_info",
        "description": ("使用 yt-dlp 获取视频详情（不下载）：可用画质格式、大小、标题等。"
                        "仅当 default_quality 为 null 时，先本工具(或 get_quality_summary)查清支持的画质，"
                        "【展示并明确询问】用户选哪个，再把答案作为 quality 传给 download_video；不得默认 best。"
                        "config 已设画质则直接用、不须问。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "bvid_or_url": {
                    "type": "string",
                    "description": "视频BV号或完整B站URL",
                },
            },
            "required": ["bvid_or_url"],
        },
    },
    {
        "name": "get_part_list",
        "description": ("获取 B站视频的分P列表（各分P序号与标题）。在下载多P视频前调用。"
                        "【流程】先用本工具拿到各分P标题，然后【向用户展示并询问】要下载哪些分P："
                        "全部(all_parts=true) / 仅第1P(part=1) / 指定若干(part=[1,3,5] 或 '1-5')；"
                        "把用户选择作为 download_video 的 part 或 all_parts 传入：多P视频必须显式传 part(如 part=1 表示仅第1P) 或 all_parts=true，省略 part 会报错；不要替用户默认全下或不下。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "bvid_or_url": {
                    "type": "string",
                    "description": "视频BV号或完整B站URL",
                },
            },
            "required": ["bvid_or_url"],
        },
    },
    {
        "name": "check_deps",
        "description": "检查所有依赖是否安装：yt-dlp、ffmpeg、ffprobe、讯飞 ASR 凭证。返回各项状态。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_xfyun_config",
        "description": ("主动预检讯飞语音转写凭证（APPID / APISecret）是否已配置就绪。"
                        "转写前（或排查转写失败时）可先调本工具，避免等到 transcribe 真正调用时才被动失败。"
                        "返回 is_ready 布尔与原因；未配置时提示按 README 配置环境变量 XF_APP_ID / XF_SECRET_KEY 或创建 config.json。"
                        "本工具不读取偏好、不触发转写，仅做凭证就绪检查。"),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_quality_summary",
        "description": ("批量查询多个视频支持的画质，返回可读摘要（每个画质支持哪些序号）。输入 search_video 返回的 videos 列表。"
                        "仅当 default_quality 为 null 时，先本工具查清支持的画质，"
                        "【展示并明确询问】本次统一选哪个（批量问一次即可，不得默认 best），再把答案作为 quality 传入；"
                        "config 已设画质则统一用该值、不须问。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "videos": {
                    "type": "array",
                    "description": "search_video 返回的 videos 列表（含 bvid/title）",
                },
            },
            "required": ["videos"],
        },
    },

    # ==================== 用户偏好（配置优先） ====================
    {
        "name": "get_user_prefs",
        "description": ("读取 config.json 用户偏好：画质(default_quality)、降级(allow_quality_fallback)、"
                        "自动转写(auto_transcribe_after_download)、热词(hot_words)；四字段齐全即视为已初始化。\n"
                        "【配置优先·铁律】涉及这四项前先调 get_user_prefs；已设字段直接采用、不追问，仅 config=null 才询问并显式传参。"
                        "首次四字段未齐全：四问后 init_user_prefs 一次性写入，之后直接使用不再询问。"),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "init_user_prefs",
        "description": ("首次初始化 / 重新配置 用户偏好并写入 config.json（写入后四字段即齐全，视为已初始化）。"
                        "四字段：\n"
                        "  - quality: 默认画质 'best'/'1080p'/'720p'/'480p'/'360p'，或 null=每次询问。\n"
                        "  - allow_quality_fallback: 布尔 或 null（null=每次询问），目标画质不支持时是否自动降级。\n"
                        "  - auto_transcribe_after_download: 布尔/null，下载后是否自动转写；null=每次询问。\n"
                        "  - hot_words: 热词列表(数组 或 仅空格分隔字符串)或 null；标点视为词的一部分。[]=决定不加热词，null=每次询问，非空列表=沿用此表。\n"
                        "调用前应已按四问询问用户并拿到答案；写入后即成为用户意志、后续直接采用不再询问。\n"
                        "【推荐默认】询问时呈现确认：画质=best、降级=False（固定画质则推荐 True）、自动转写=False、热词=[]（[]≠null）。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "quality": {
                    "type": ["string", "null"],
                    "enum": [None, "best", "1080p", "720p", "480p", "360p"],
                    "description": "默认画质；传 null 表示'每次询问用户'。",
                },
                "allow_quality_fallback": {
                    "type": ["boolean", "null"],
                    "description": "目标画质不支持时是否自动降级；传 null 表示'每次询问用户'。",
                },
                "auto_transcribe_after_download": {
                    "type": ["boolean", "null"],
                    "description": "下载完成后是否自动转写；传 null 表示'每次询问用户'。",
                },
                "hot_words": {
                    "type": ["array", "string", "null"],
                    "items": {"type": "string"},
                    "description": "热词列表；字符串时仅以空格分隔（标点视为词的一部分）。非空=沿用此表，[]/空串=决定不加，null=每次询问。",
                },
            },
            "required": ["quality", "allow_quality_fallback", "auto_transcribe_after_download", "hot_words"],
        },
    },
    {
        "name": "update_user_prefs",
        "description": ("单项修改 / 清空 用户偏好（不覆盖未传入的字段，区别于 init_user_prefs 的全量写入）。"
                        "所有字段均可选，仅传入的字段会被更新，其余字段保持原值。\n"
                        "  - quality / auto_transcribe_after_download: 传 null 表示清回'每次询问'。\n"
                        "  - allow_quality_fallback: 布尔 或 null（传 null 清回每次询问），重新设定是否允许降级。\n"
                        "  - hot_words: 热词列表(数组)或字符串(字符串时仅以空格分隔，标点视为词的一部分)，或 null(清回每次询问)，或空数组[](决定不加热词)；"
                        "长度不符(2~16字)的热词会被忽略并在返回中提示。\n"
                        "  - remember: 布尔，默认 true 表示写入 config.json 并持久化；false 仅返回预览不落盘。\n"
                        "典型用法：用户只想改画质 -> 只传 quality；想恢复某字段的'每次询问' -> 传对应字段为 null。\n"
                        "【推荐默认】同 init_user_prefs：画质=best、降级=False、自动转写=False、热词=[]（[]≠null）。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "quality": {
                    "type": ["string", "null"],
                    "enum": [None, "best", "1080p", "720p", "480p", "360p"],
                    "description": "默认画质；传 null 清回'每次询问用户'。",
                },
                "allow_quality_fallback": {
                    "type": ["boolean", "null"],
                    "description": "目标画质不支持时是否自动降级；传 null 清回'每次询问用户'。",
                },
                "auto_transcribe_after_download": {
                    "type": ["boolean", "null"],
                    "description": "下载完成后是否自动转写；传 null 清回'每次询问用户'。",
                },
                "hot_words": {
                    "type": ["array", "string", "null"],
                    "items": {"type": "string"},
                    "description": "热词；字符串时仅以空格分隔（标点视为词的一部分）。非空=沿用此表，[]/空串=决定不加，null=清回每次询问。",
                },
                "remember": {
                    "type": "boolean",
                    "description": "是否写入 config.json 持久化，默认 true；false 仅返回预览。",
                },
            },
            "required": [],
        },
    },

    # ==================== 语音转写 ====================
    {
        "name": "list_videos",
        "description": ("列出 videos/ 文件夹中的【全部】音视频文件（mp4/mkv/avi/mp3/m4a/wav 等），返回序号、文件名、类型、"
                        "大小、时长、画质、是否已转写、修改时间。用于选择待转写文件（CLI 版会用 ffprobe 探测时长/画质，本工具默认同样探测）。"
                        "注意：本工具返回的是【全部】文件，agent 必须将完整列表以【对齐表格】（序号/文件名/类型/大小/时长/画质/是否已转写/修改日期）"
                        "完整展示给用户，不要只举数例或自行筛选。"
                        "在'下载后转写'流程中，下载完成调用本工具即可拿到刚下载的文件列表，"
                        "再传给 transcribe（并先按铁律询问转哪些文件；热词与自动转写须先调 get_user_prefs 按配置优先处理）。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "视频文件夹路径，默认使用 videos/ 目录",
                },
                "probe": {
                    "type": "boolean",
                    "description": ("是否用 ffprobe 探测每个文件的时长与画质（高度）。默认 true（与 CLI 对齐，"
                                    "可拿到 时长/画质 字段）；文件很多时若嫌慢可传 false，则只返回名字/大小/时间。"),
                    "default": True,
                },
            },
        },
    },
    {
        "name": "transcribe",
        "description": ("将视频/音频文件转写为文字。支持单文件/数组/文件夹扫描/indices 区间；自动提取音频+讯飞API，"
                        "输出 纯文本+SRT字幕+分段JSON 存 videos/transcripts/。\n"
                        "【配置优先·铁律】涉及 画质/降级/自动转写/热词 前先调 get_user_prefs；已设字段直接采用、不追问，仅 config=null 才询问并显式传参。"
                        "可选预检：check_xfyun_config 凭证 / validate_hotwords 热词合规；auto_transcribe_after_download 本工具不读。\n"
                        "【流程·必须先问再转写】\n"
                        "  1) 转哪些：先 list_videos 列出【全部】本地音视频，以【对齐表格】(序号/文件名/类型/大小/时长/画质/是否已转写/修改日期)【完整呈现】用户、不得只举例或筛选；再【问】转哪些(全部/序号/区间)。\n"
                        "  2) 热词(仅当 hot_words=null)：【问】有无专有名词热词(如游戏术语)；用户无则传 ''（省略报错）。每词 2-16 字。\n"
                        "  3) 确认：展示'将转写 N 个文件 + 热词=xxx'给用户确认后执行。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "video_path": {
                    "type": ["string", "array"],
                    "description": "视频/音频文件路径（绝对路径或相对于 videos/ 的文件名）。也可传字符串数组批量转写；留空则自动扫描 folder 下全部文件。可传文件夹路径自动扫描。",
                },
                "hot_words": {
                    "type": "string",
                    "description": ("【配置优先】config 已设 hot_words（[]或具体列表）时无需传（自动采用、忽略本参数）；仅当 config 为 null（每次询问）时才必须显式传入，省略且 config 未设会报错。\n"
                                    "热词列表，词间【仅空格分隔】（逗号/分号/顿号等标点视为词的一部分、不作分隔）。"
                                    "调用前（仅当 config 为 null 时）先问用户是否需要；若 config 的 hot_words=null 则必须显式传入——"
                                    "用户说没有就传空串 ''（不能省略，省略报错）。每个热词 2-16 字。"),
                },
                "folder": {
                    "type": "string",
                    "description": "当 video_path 为空或为文件夹路径时，扫描此目录。默认 videos/。",
                },
                "indices": {
                    "type": "string",
                    "description": "当扫描文件夹时，按序号选择子集（如 '1'、'1,3,5'、'1-5'、'all'）。仅当 video_path 为空/为文件夹时生效。",
                },
                "concurrency": {
                    "type": "integer",
                    "description": "批量转写时同时进行的任务数（默认1，即串行）。讯飞有频率限制，建议不超过2。",
                    "default": 1,
                },
            },
        },
    },
    {
        "name": "validate_hotwords",
        "description": ("校验热词字符串的合规性，供调用 transcribe(hot_words=) 前预检，避免不合规热词被静默忽略。"
                        "规则：单个热词需 2~16 字符、最多 200 个；词之间【仅以空白字符（空格/制表符等）分隔】，"
                        "逗号/分号/顿号等标点一律视为热词的一部分、不作为分隔符。"
                        "返回 valid（合规清单，超过 200 个仅保留前 200）与 invalid（不合规清单，多为不满足 2~16 字符者），并给出可读报告文本。"
                        "本工具不写盘、不转写，仅做校验反馈。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hot_words": {
                    "type": "string",
                    "description": "待校验的热词字符串，多个热词之间仅以空白字符（空格/制表符等）分隔；逗号/分号/顿号等标点视为词的一部分，不作分隔。",
                },
            },
            "required": ["hot_words"],
        },
    },
]


# ============================================================
# MCP 请求处理（统一入口）
# ============================================================

def _handle_request(msg: dict):
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params", {})

    # ---- 握手 ----
    if method == "initialize":
        _send_response(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": "bilibili-pipeline",
                "version": "",
                "description": ("B站视频 搜索+下载+讯飞语音转写 一站式 MCP Server。\n"
                                "【重要·本 server 无模式切换】这不是有状态的 CLI 程序，没有「进入某模式」的子菜单或状态机；"
                                "所谓「三种使用模式」只是对工具组合方式的分类说明。agent 听到的「进入转写模式」等自然语言，"
                                "应理解为「接下来调用 list_videos + transcribe 的组合流程」，而非切换到持久状态，server 不保存任何模式状态。\n"
                                "使用流程由你(agent)按各工具描述逐步调用（如 search_video → download_video → transcribe），无需让用户先「选择模式」。\n"
                                "首次使用前必须先调 init_user_prefs 完成 画质/降级/自动转写/热词 四问配置；写入后下载/转写直接采用 config，已设项不再询问。详见各工具描述。\n"
                                "【配置优先·铁律】涉及 画质/降级/自动转写/热词 前先调 get_user_prefs；已设字段直接采用、不追问，仅 config=null 才询问并显式传参；首次四问后 init_user_prefs 写入。详见 get_user_prefs 工具描述。\n"
                                "【每调用必问项】分P（哪几P）、转写哪些文件、是否继续 等不在 config 的每调用决策，必须先把决策展示用户并【明确询问】，绝不替用户默认；批量中多P视频须逐视频单独问分P。\n"),
            },
        })
        return

    # ---- 工具列表 ----
    if method == "tools/list":
        _send_response(req_id, {"tools": TOOLS})
        return

    # ---- 工具调用 ----
    if method == "tools/call":
        try:
            # stdout 兜底：在执行业务逻辑期间，将 sys.stdout 临时重定向到黑洞。
            # 目的：common 模块里的进度/日志本应输出到 stderr（见 _print_part_result /
            # _stream_output），但为防御任何意外的裸 print()，这里统一吞掉 stdout，
            # 确保 JSON-RPC 通道不被污染。协议发送走 _REAL_STDOUT，不受此处重定向影响；
            # 转写进度通知（_send_notification）同样走 _REAL_STDOUT，照常推送。
            with contextlib.redirect_stdout(io.StringIO()):
                _dispatch_tool_call(req_id, params)
        except Exception as e:
            _debug_log(f"[tools/call] {e}")
            _send_error(req_id, -32603, f"工具执行内部错误: {e}")
        return
    # ---- notifications/initialized ----
    if method == "notifications/initialized":
        return

    # ---- 未知方法 ----
    _send_error(req_id, -32601, f"未知方法: {method}")


# ============================================================
# 主循环
# ============================================================

def _reconfigure_stream(stream):
    fn = getattr(stream, "reconfigure", None)
    if callable(fn):
        try:
            fn(encoding="utf-8")
        except Exception:
            pass


def _debug_log(msg):
    try:
        sys.stderr.buffer.write(f"[bilibili-pipeline] {msg}\n".encode("utf-8"))
        sys.stderr.buffer.flush()
    except Exception:
        pass


def _dispatch_tool_call(req_id, params):
    name = params.get("name")
    args = params.get("arguments") or {}

    # ── search_video ──
    if name == "search_video":
        keyword = args.get("keyword", "")
        page = _safe_int(args.get("page", 1), 1)
        page_size = _safe_int(args.get("page_size", 10), 10)
        if not keyword:
            _send_error(req_id, -32602, "缺少必填参数: keyword")
            return
        data = search_bilibili(keyword, page, page_size)
        text = format_search_results(data, show_details=True)
        _send_response(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": not data.get("success"),
            "_raw_data": data,
        })
        return

    # ── download_video ──
    if name == "download_video":
        raw = args.get("bvid_or_url", "")
        if isinstance(raw, list):
            items = [str(x).strip() for x in raw if str(x).strip()]
        else:
            items = [str(raw).strip()] if raw and str(raw).strip() else []
        if not items:
            _send_error(req_id, -32602, "缺少必填参数: bvid_or_url")
            return
        # 依赖预检兜底：缺 yt-dlp / ffmpeg / ffprobe 时直接报错返回
        if not _ensure_deps_ready(req_id):
            return
        # 配置优先：config.json 已设定默认画质则强制采用（高于任何询问/默认 best）
        _prefs = _load_user_prefs()
        if _prefs is None:
            _send_error(req_id, -32602,
                        "用户偏好尚未初始化：config.json 的四偏好字段（画质/降级/自动转写/热词）未齐全。"
                        "请先调 get_user_prefs 查看当前状态，按铁律【询问用户】这四项后，"
                        "调 init_user_prefs 一次性写入，再调用本下载工具。")
            return
        _cfg_quality = _prefs.get("default_quality")
        quality = args.get("quality")
        if _cfg_quality is not None:
            quality = _cfg_quality
        elif quality is None:
            _send_error(req_id, -32602,
                        "缺少必填参数 quality：MCP 无法交互询问，请先按铁律向用户确认画质"
                        "（best/1080p/720p/480p/360p）后，显式传入 quality 参数再调用，"
                        "不得未经询问就默认 best 下载。")
            return
        if not isinstance(quality, str):
            _send_error(req_id, -32602,
                        f"画质参数 quality 必须是字符串，但收到 {type(quality).__name__}：{quality!r}。"
                        "请检查 config.json 的 default_quality 是否为合法取值（best/1080p/720p/480p/360p）。")
            return
        # 画质降级策略：config.json 配置优先 > agent 显式传入；二者皆无时按 CLI 口径处理（best 静默 False，否则报错要求先问）
        allow_fb = _resolve_allow_fallback(args, _prefs, quality)
        if allow_fb is None:
            _send_error(req_id, -32602,
                        "缺少必填参数 allow_quality_fallback：MCP 无法交互询问，请先按铁律向用户确认降级策略"
                        "（True=接受降级 / False=不降级）后，显式传入 allow_quality_fallback 参数再调用；"
                        "不得省略（未配置时必须先问再传参）。")
            return
        # 下载后自动转写策略：config.json 配置优先 > agent 显式传入；二者皆无则报错要求先问
        auto_transcribe = _resolve_auto_transcribe(args, _prefs)
        if auto_transcribe is None:
            _send_error(req_id, -32602,
                        "缺少必填参数 auto_transcribe：请先按铁律询问用户下载后是否自动转写，"
                        "再把答案显式传入 auto_transcribe 参数再调用；不得省略。")
            return
        # 自动转写热词：配置优先（与 transcribe 工具一致）
        #   - config 已设定（[] 或具体列表）→ 强制采用，高于 agent 传入
        #   - config 为 null（每次询问）→ 与 CLI 完全一致：CLI 在 auto_transcribe 且 hot_words=null 时
        #     会交互询问热词；MCP 无法交互，故未配置且 agent 未传入 hot_words 参数时直接报错，
        #     要求 agent 先询问再传入（绝不能静默无热词；用户说没有就传空字符串 ''）
        _cfg_hw = _prefs.get("hot_words")
        # A4：静默采用/忽略 回显（纯提示，不改运行逻辑）
        _adopt_notes = []
        if quality == "best":
            _adopt_notes.append("[画质] 采用 best（最高画质），降级参数已忽略")
        if _cfg_quality is not None and args.get("quality") is not None and args.get("quality") != _cfg_quality:
            _adopt_notes.append(f"[画质] 已采用 config 值 {_cfg_quality}，忽略传入的 {args.get('quality')}")
        _cfg_fb = _prefs.get("allow_quality_fallback")
        if _cfg_fb is not None and args.get("allow_quality_fallback") is not None and bool(args.get("allow_quality_fallback")) != bool(_cfg_fb):
            _adopt_notes.append(f"[降级] 已采用 config 值 {_cfg_fb}，忽略传入的 {args.get('allow_quality_fallback')}")
        _cfg_auto = _prefs.get("auto_transcribe_after_download")
        if _cfg_auto is not None and args.get("auto_transcribe") is not None and bool(args.get("auto_transcribe")) != bool(_cfg_auto):
            _adopt_notes.append(f"[自动转写] 已采用 config 值 {_cfg_auto}，忽略传入的 {args.get('auto_transcribe')}")
        if _cfg_hw is not None and "hot_words" in args:
            _adopt_notes.append("[热词] 已采用 config 非 null 值，忽略本次传入的 hot_words")
        if _cfg_hw is not None:
            _resolved_hw = _cfg_hw            # 配置优先：忽略 agent 传入
        elif not auto_transcribe:
            # auto=False 时不触发转写，_resolved_hw 不被消费，占位即可（与 CLI 此分支同为非业务占位）
            _resolved_hw = None
        elif "hot_words" not in args:
            _send_error(req_id, -32602,
                        "下载后需自动转写，但热词未确定：config.json 的 hot_words 为 null（每次询问）"
                        "且本次未传入 hot_words 参数。请先按铁律【明确询问用户】热词"
                        "（用户说没有就传空字符串 ''），再把答案作为 hot_words 参数显式传入再调用。")
            return
        else:
            _hw_raw = args.get("hot_words", "")
            _resolved_hw, _ = parse_hotwords_string(_hw_raw)
            # '' → ([], []) 即用户放弃热词，与 CLI 回车得 [] 一致；非空则采用用户输入
        download_dir = args.get("download_dir")
        all_parts = bool(args.get("all_parts", False))
        concurrency = _safe_int(args.get("concurrency", 1), 1)

        # 归一化 part 参数：支持 整数 / 数组 / 字符串(区间, 'all')
        raw_part = args.get("part")
        if isinstance(raw_part, str) and raw_part.strip().lower() in ("all", "a"):
            all_parts = True
            norm_part = None
        else:
            # 单条时先探测分P数，避免子集选择越界
            pc = None
            if len(items) == 1:
                det = detect_parts_cached(items[0])
                if det.get("success") and det.get("is_multi"):
                    pc = det.get("part_count")
            # 与 CLI 完全一致：多P视频 CLI 会交互询问「下载哪些分P」；MCP 无法交互，
            # 故未指定 part 且未选 all_parts 时直接报错，要求 agent 先询问用户再传入
            if pc is not None and raw_part is None:
                _send_error(req_id, -32602,
                            "目标为多P视频（共 %d 个分P），但本次未指定 part 也未选 all_parts。"
                            "请先【明确询问用户】要下载哪些分P（或全部），再把答案作为 part 参数"
                            "（如 '1,3' 或 'all'）显式传入再调用，不得省略。" % pc)
                return
            norm_part = _parse_part_to_list(raw_part, pc)
        # 批量且未指定分P：若任一目标为多P视频则报错（CLI 会逐视频询问分P，MCP 要求先问）
        if len(items) > 1 and raw_part is None and not all_parts:
            _mp = []
            for _it in items:
                _d = detect_parts_cached(_it)
                if _d.get("success") and _d.get("is_multi"):
                    _mp.append(_it)
            if _mp:
                _send_error(req_id, -32602,
                            "批量下载中含 %d 个多P视频，但本次未指定 part 也未选 all_parts。"
                            "请先【明确询问用户】每个多P视频要下载哪些分P，再逐视频调用本工具传入对应 part（或全部 all_parts）。"
                            % len(_mp))
                return

        # 单条
        if len(items) == 1:
            # 下载前画质预检：不降级且目标画质源端不可用则直接跳过（不下载）
            ok, reason = preflight_quality_ok(items[0], quality, allow_fb)
            if not ok:
                _send_response(req_id, {
                    "content": [{"type": "text", "text": f"[跳过] {reason}"}],
                    "isError": False,
                    "_raw_data": {"success": False, "status": "skipped_quality_unavailable", "reason": reason},
                })
                return
            result = download_video(items[0], download_dir, quality, part=norm_part, all_parts=all_parts)
            # 下载后画质降级兜底：仍下载了低画质则删除文件放弃
            result, _, note = post_download_quality_check(result, allow_fb)
            text = format_download_text(result)
            if note:
                text = text + "\n" + note
            # 分P预览：标明实际下载的分P（多P场景）（#7）
            _pp = _build_parts_preview(items[0], all_parts, norm_part)
            if _pp:
                text = text + "\n" + _pp
            # 下载后自动转写（配置优先）：仅下载成功且讯飞凭证就绪时触发
            # 与 CLI(_auto_transcribe_files) 一致：凭证缺失时静默跳过，不尝试、不报错
            if auto_transcribe and result.get("success"):
                _a = result.get("audio_file")
                if _a and os.path.exists(_a):
                    _xf_ready, _xf_err = ensure_xfyun_config()
                    if not _xf_ready:
                        text = text + f"\n[自动转写] 跳过：讯飞凭证未配置（{_xf_err}）"
                    else:
                        try:
                            _tr = transcribe_file(_a, hot_words=_resolved_hw)
                            result["transcription"] = _tr
                            _saved = _tr.get("saved", {})
                            text = text + f"\n[自动转写] 完成：{_saved.get('text_path', '')}"
                        except Exception as e:
                            result["transcription_error"] = str(e)
                            text = text + f"\n[自动转写] 失败：{e}"
            if _adopt_notes:
                text = text + "\n" + "\n".join(_adopt_notes)
            _send_response(req_id, {
                "content": [{"type": "text", "text": text}],
                "isError": not result.get("success"),
                "_raw_data": result,
            })
            return

        # 批量
        results, ok, total = _batch_download_videos(items, quality, download_dir, norm_part, all_parts, concurrency, allow_fb, auto_transcribe, _resolved_hw)
        # 聚合摘要：区分成功 / 失败 / 跳过（画质不可用跳过）（#6）
        _skip = sum(1 for r in results if r.get("status") == "skipped_quality_unavailable")
        _fail = sum(1 for r in results if not r["success"] and r.get("status") != "skipped_quality_unavailable")
        lines = [f"批量下载完成：共 {total} 个，成功 {ok}，失败 {_fail}，跳过 {_skip}", "=" * 40]
        for r in results:
            if r["success"]:
                lines.append(f"  [OK] {r['input']}\n      {r['result'].get('message', 'OK')}")
                if r.get("note"):
                    lines.append(f"      {r['note']}")
                # 分P预览：标明实际下载的分P（多P场景）（#7）
                if r.get("parts_preview"):
                    lines.append(f"      {r['parts_preview']}")
            else:
                lines.append(f"  [X] {r['input']}: {r['result'].get('error', 'unknown')}")
        if _adopt_notes:
            lines.append("\n".join(_adopt_notes))
        _send_response(req_id, {
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "isError": ok < total,
            "_raw": {"total": total, "ok": ok, "failed": _fail, "skipped": _skip, "results": results},
        })
        return

    # ── get_video_info ──
    if name == "get_video_info":
        bvid_or_url = args.get("bvid_or_url", "")
        if not bvid_or_url:
            _send_error(req_id, -32602, "缺少必填参数: bvid_or_url")
            return
        data = _get_video_info(bvid_or_url)
        text = _format_video_info_text(data)
        _send_response(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": not data.get("success"),
            "_raw_data": data,
        })
        return

    # ── get_part_list ──
    if name == "get_part_list":
        bvid_or_url = args.get("bvid_or_url", "")
        if not bvid_or_url:
            _send_error(req_id, -32602, "缺少必填参数: bvid_or_url")
            return
        data = get_part_list(bvid_or_url)
        text = _format_part_list_text(data)
        _send_response(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": not data.get("success"),
            "_raw_data": data,
        })
        return

    # ── check_deps ──
    if name == "check_deps":
        data = check_dependencies()
        text = format_deps_text(data)
        _send_response(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": False,
            "_raw_data": data,
        })
        return

    # ── check_xfyun_config ──
    if name == "check_xfyun_config":
        is_ready, err_msg = ensure_xfyun_config()
        if is_ready:
            text = "✅ 讯飞凭证已配置就绪，可以正常转写。"
            _send_response(req_id, {
                "content": [{"type": "text", "text": text}],
                "isError": False,
                "_raw": {"is_ready": True},
            })
        else:
            text = f"❌ 讯飞凭证未配置：{err_msg}\n请按 README.md 配置后再调用 transcribe。"
            _send_response(req_id, {
                "content": [{"type": "text", "text": text}],
                "isError": True,
                "_raw": {"is_ready": False, "error": err_msg},
            })
        return

    # ── get_quality_summary ──
    if name == "get_quality_summary":
        videos = args.get("videos", [])
        text = _format_quality_summary(videos)
        _send_response(req_id, {
            "content": [{"type": "text", "text": text or "无视频数据"}],
            "isError": False,
            "_raw_data": {"success": True, "videos_count": len(videos)},
        })
        return

    # ── list_videos ──
    if name == "list_videos":
        folder = args.get("folder") or DEFAULT_VIDEOS_DIR
        probe = args.get("probe", True)
        if isinstance(probe, str):
            probe = probe.strip().lower() not in ("false", "0", "no")
        else:
            probe = bool(probe)
        # probe=True 时用 ffprobe 探测时长/画质（与 CLI 的 _do_transcribe 对齐），
        # 这样返回的文件信息包含 时长(duration_str) 与 画质(quality_str)，便于用户决定转哪些。
        files = format_file_list(folder, probe=probe)
        if not files:
            _send_response(req_id, {
                "content": [{"type": "text", "text": f"文件夹为空或不存在: {folder}"}],
                "isError": True,
                "_raw": {"files": []},
            })
            return
        # 表格渲染与 CLI 共用 format_file_table（common），确保两边输出一致。
        text = format_file_table(files, folder=folder, probe=probe)
        # ffprobe 提示须在探测「前」给出；MCP 同步返回文本，故把提示置于返回文本开头。
        if probe:
            text = f"正在列出文件，{PROBE_HINT}\n\n" + text
        _send_response(req_id, {
            "content": [{"type": "text", "text": text}],
            "_raw": {"files": files, "folder": folder, "probed": probe},
        })
        return

    # ── transcribe ──
    if name == "transcribe":
        raw = args.get("video_path", "")
        # 热词解析：配置优先（与 CLI 的 _do_transcribe 完全一致）
        #   - config 已设定（[] 或具体列表）→ 强制采用，高于 agent 传入
        #   - config 为 null（每次询问）→ 与 CLI 一致：CLI 会交互询问热词；MCP 无法交互，
        #     故 agent 未传入 hot_words 参数时直接报错，要求先询问再传入（绝不能静默无热词）
        _prefs = _load_user_prefs()
        if _prefs is None:
            _send_error(req_id, -32602,
                        "用户偏好尚未初始化：config.json 的四偏好字段未齐全。请先调 init_user_prefs 写入"
                        "（尤其 hot_words 的「每次询问 / 空表 / 具体列表」取值）后再转写。")
            return
        _cfg_hw = _prefs.get("hot_words")
        if _cfg_hw is not None:
            hot_words = _cfg_hw            # 配置优先：忽略 agent 传入
            rejected_hw = []
        elif "hot_words" not in args:
            _send_error(req_id, -32602,
                        "热词未确定：config.json 的 hot_words 为 null（每次询问），本次也未传入 hot_words 参数。"
                        "请先按铁律【明确询问用户】有无热词（用户说没有就传空字符串 ''），再把答案作为 hot_words 参数显式传入再调用。")
            return
        else:
            hot_raw = args.get("hot_words", "")
            hot_words, rejected_hw = parse_hotwords_string(hot_raw)
            # '' → ([], []) 即用户放弃热词，与 CLI 回车得 [] 一致；非空则采用用户输入
        folder = args.get("folder")
        indices_raw = args.get("indices")
        concurrency = _safe_int(args.get("concurrency", 1), 1)

        # 转写进度：客户端通过 tools/call 的 progressToken 订阅进度通知；
        # 未提供 token 时，阶段信息累积在 _ts.last_stage() 供响应文本兜底展示（长任务不黑屏）
        progress_token = params.get("progressToken")
        _ts = _make_progress_sender(progress_token)

        paths, _scanned = _normalize_transcribe_inputs(raw, folder, indices_raw)

        # A6：单文件模式下 indices 被忽略提示
        _indices_ignored = bool(indices_raw is not None and isinstance(raw, str) and raw and os.path.isfile(raw))

        if not paths:
            _send_error(req_id, -32602, "未找到可转写的文件")
            return

        # 讯飞凭证校验
        is_ready, err_msg = ensure_xfyun_config()
        if not is_ready:
            _send_error(req_id, -31002, err_msg)
            return

        # 单条
        if len(paths) == 1:
            video_path = paths[0]
            if not os.path.exists(video_path):
                _send_error(req_id, -32602, f"文件不存在: {video_path}")
                return
            try:
                res = transcribe_file(video_path, hot_words=hot_words, progress_callback=_ts)
            except ValueError as e:
                _send_error(req_id, -31002, str(e))
                return
            except Exception as e:
                _send_error(req_id, -31003, f"转写失败: {e}")
                return

            saved = res["saved"]
            dur = res["duration"]
            chars = res["chars"]
            preview = res["result"].get("plain_text", "")[:300]
            if len(res["result"].get("plain_text", "")) > 300:
                preview += "..."
            text = (
                f"\u2705 转写成功！\n"
                f"视频: {res['video_name']}\n"
                f"时长: {dur}秒 | 字数: {chars}字\n"
                f"热词: {hot_words or '无'}\n\n"
                f"【文字预览（前300字）】\n{preview}\n\n"
                f"TXT纯文本: {saved['text_path']}\n"
                f"SRT字幕: {saved['srt_path']}\n"
                f"JSON分段: {saved['segments_path']}"
            )
            # 进度兜底：客户端未订阅通知时，在文本末尾展示最终阶段
            _final_stage = _ts.last_stage()
            if _final_stage and progress_token is None:
                text = text + f"\n\n[进度] {_final_stage}"
            # 热词校验反馈：被忽略的不合规热词（配置优先时 rejected_hw 已清空）
            if rejected_hw:
                text = text + f"\n\n[注意] {len(rejected_hw)} 个热词因长度不符(2~16字)被忽略: {rejected_hw}"
            if _indices_ignored:
                text = text + "\n\n[注意] indices 已忽略（单文件模式仅按 video_path 转写）"
            _send_response(req_id, {
                "content": [{"type": "text", "text": text}],
                "_raw": {"result": res["result"], "saved": saved, "rejected_hot_words": rejected_hw},
            })
            return

        # 批量
        results, ok, total = _batch_transcribe_files(paths, hot_words, concurrency, progress_callback=_ts)
        lines = [f"批量转写完成：共 {total} 个文件，成功 {ok}，失败 {total - ok}", "=" * 40]
        for r in results:
            if r.get("success"):
                s = r.get("saved", {})
                name = r.get("video_name", os.path.basename(r.get("input", "")))
                lines.append(f"  [OK] {name} ({r.get('chars', 0)}字, {r.get('duration', 0)}秒)")
                if s:
                    lines.append(f"      TXT: {s.get('text_path', '')}")
                    lines.append(f"      SRT: {s.get('srt_path', '')}")
            else:
                name = r.get("video_name", os.path.basename(r.get("input", "")))
                lines.append(f"  [X] {name}: {r.get('error', 'unknown')[:80]}")
        # 进度兜底
        _final_stage = _ts.last_stage()
        if _final_stage and progress_token is None:
            lines.append(f"[进度] {_final_stage}")
        # 热词校验反馈：被忽略的不合规热词（配置优先时 rejected_hw 已清空）
        if rejected_hw:
            lines.append(f"[注意] {len(rejected_hw)} 个热词因长度不符(2~16字)被忽略: {rejected_hw}")
        if _indices_ignored:
            lines.append("[注意] indices 已忽略（单文件模式仅按 video_path 转写）")
        _send_response(req_id, {
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "isError": ok < total,
            "_raw": {"total": total, "ok": ok, "failed": total - ok, "results": results, "rejected_hot_words": rejected_hw},
        })
        return

    # ── validate_hotwords ──
    if name == "validate_hotwords":
        raw = args.get("hot_words", "")
        if not raw or not str(raw).strip():
            _send_response(req_id, {
                "content": [{"type": "text", "text": "未提供热词，无可校验内容。"}],
                "isError": False,
                "_raw": {"valid": [], "invalid": []},
            })
            return
        valid, invalid = parse_hotwords_string(raw)
        lines = []
        if valid:
            lines.append(f"合规热词（{len(valid)} 个）：")
            for i, w in enumerate(valid, 1):
                lines.append(f"  {i}. {w}")
        else:
            lines.append("无合规热词。")
        if invalid:
            lines.append(f"\n不合规热词（{len(invalid)} 个，需 2~16 字符、最多 200 个，将被忽略）：")
            for i, w in enumerate(invalid, 1):
                lines.append(f"  {i}. {w}")
        else:
            lines.append("\n无不合规热词。")
        text = "\n".join(lines)
        _send_response(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": False,
            "_raw": {"valid": valid, "invalid": invalid},
        })
        return

    # ── get_user_prefs ──
    if name == "get_user_prefs":
        prefs = _load_user_prefs()
        text = _format_user_prefs_text(prefs)
        _send_response(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": False,
            "_raw_data": prefs,
        })
        return

    # ── init_user_prefs ──
    if name == "init_user_prefs":
        quality = args.get("quality", None)
        allow_fb = args.get("allow_quality_fallback", None)
        auto = args.get("auto_transcribe_after_download", None)
        hot_raw = args.get("hot_words", None)
        # 归一化 hot_words：字符串/列表均走统一校验，返回被忽略的不合规项；null 保持
        rejected_hw = []
        if isinstance(hot_raw, str):
            hot_words, rejected_hw = parse_hotwords_string(hot_raw)
        elif isinstance(hot_raw, list):
            hot_words, rejected_hw = validate_hotwords(hot_raw)
        else:
            hot_words = None  # null = 每次询问
        prefs = {
            "default_quality": quality,
            "allow_quality_fallback": (None if allow_fb is None else bool(allow_fb)),
            "auto_transcribe_after_download": auto,
            "hot_words": hot_words,
        }
        _save_user_prefs(prefs)
        text = ("已写入 config.json（四个偏好字段已齐全，视为已初始化）：\n"
                f"  画质: {quality}\n"
                f"  画质降级: {allow_fb}\n"
                f"  自动转写: {auto}\n"
                f"  热词: {hot_words}\n")
        if rejected_hw:
            text += (f"  [注意] {len(rejected_hw)} 个热词因长度不符(2~16字)被忽略: {rejected_hw}\n")
        text += "此后涉及这四项的操作将直接使用本配置，不再询问用户。"
        _raw = dict(prefs)
        _raw["rejected_hot_words"] = rejected_hw
        _send_response(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": False,
            "_raw_data": _raw,
        })
        return

    # ── update_user_prefs（单项改/清，保留未传字段）（#3）──
    if name == "update_user_prefs":
        prefs = _load_user_prefs()
        if prefs is None:
            _send_error(req_id, -32602,
                        "用户偏好尚未初始化：四偏好字段未齐全。请先调 init_user_prefs 一次性写入，"
                        "之后才能用 update_user_prefs 单项修改。")
            return
        rejected_hw = []
        changed = []
        # quality（传 null 清回每次询问）
        if "quality" in args:
            prefs["default_quality"] = args.get("quality")
            changed.append("quality")
        # allow_quality_fallback（支持 null=清回每次询问）
        if "allow_quality_fallback" in args:
            v = args.get("allow_quality_fallback")
            prefs["allow_quality_fallback"] = None if v is None else bool(v)
            changed.append("allow_quality_fallback")
        # auto_transcribe_after_download（传 null 清回每次询问）
        if "auto_transcribe_after_download" in args:
            prefs["auto_transcribe_after_download"] = args.get("auto_transcribe_after_download")
            changed.append("auto_transcribe_after_download")
        # hot_words（null=清回询问，[]/=决定不加，str/list=解析校验）
        if "hot_words" in args:
            hw = args.get("hot_words")
            if hw is None:
                prefs["hot_words"] = None
            elif isinstance(hw, str):
                prefs["hot_words"], rejected_hw = parse_hotwords_string(hw)
            elif isinstance(hw, list):
                prefs["hot_words"], rejected_hw = validate_hotwords(hw)
            else:
                prefs["hot_words"] = None
            changed.append("hot_words")
        remember = args.get("remember", True)
        if remember:
            _save_user_prefs(prefs)
            _changed_note = "已写入 config.json：\n"
        else:
            _changed_note = "（remember=false，仅预览未落盘）：\n"
        text = "已更新用户偏好" + _changed_note + _format_user_prefs_text(prefs)
        if changed:
            text += "\n本次变更字段: " + ", ".join(changed)
        else:
            text += "\n（未传入任何字段，配置保持不变）"
        if rejected_hw:
            text += f"\n[注意] {len(rejected_hw)} 个热词因长度不符(2~16字)被忽略: {rejected_hw}"
        _raw = dict(prefs)
        _raw["rejected_hot_words"] = rejected_hw
        _raw["changed_fields"] = changed
        _raw["persisted"] = bool(remember)
        _send_response(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": False,
            "_raw_data": _raw,
        })
        return

    # ── 未知工具 ──
    _send_error(req_id, -32601, f"未知工具: {name}")
    return


def main():
    _reconfigure_stream(sys.stdin)

    # 启动时检查环境
    deps = check_dependencies()
    _debug_log("Server started. Env check:")
    for name, info in deps.get("dependencies", {}).items():
        status = "OK" if info.get("available") else "MISSING"
        _debug_log(f"  dep [{status}] {name}")
    _debug_log(f"  all_ready={deps.get('all_ready')}")
    _debug_log("Waiting for JSON-RPC requests...")

    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            msg = json.loads(line.strip())
            _handle_request(msg)
        except KeyboardInterrupt:
            break
        except Exception as e:
            _debug_log(f"Error: {e}")
            try:
                rid = msg.get("id") if isinstance(msg, dict) else None
            except Exception:
                rid = None
            try:
                _send_error(rid, -32603, f"内部错误: {e}")
            except Exception:
                pass


# ============================================================
# 以下函数原属 common（仅 mcp 使用），已移入并加 _ 前缀
# ============================================================

def _normalize_transcribe_inputs(raw, folder=None, indices_raw=None):
    """统一转写输入归一化：字符串/数组/文件夹路径 → 路径列表。
    仅 MCP 使用（由 _dispatch_tool_call 调用），消除 MCP 侧的输入判断逻辑。

    返回 (paths: list, scanned_files: list|None)。

    支持:
      - 字符串：文件路径 → [路径]
      - 字符串：文件夹路径 → 扫描目录下所有文件
      - 列表：逐个处理字符串路径
      - 留空 raw：自动扫描 folder（默认 videos/）
      - indices：从扫描结果中按区间选择
    """
    folder = folder or DEFAULT_VIDEOS_DIR
    scanned_files = None

    if isinstance(raw, list):
        paths = []
        for p in raw:
            p = str(p).strip()
            if not p:
                continue
            if not os.path.isabs(p):
                cand = os.path.join(DEFAULT_VIDEOS_DIR, p)
                if os.path.exists(cand):
                    p = cand
            paths.append(p)
    else:
        rp = (raw or "").strip()
        if rp:
            if not os.path.isabs(rp):
                cand = os.path.join(DEFAULT_VIDEOS_DIR, rp)
                rp = cand if os.path.exists(cand) else rp
            if os.path.isdir(rp):
                scanned_files = format_file_list(rp)
                paths = [f["path"] for f in scanned_files]
            else:
                paths = [rp]
        else:
            scanned_files = format_file_list(folder)
            paths = [f["path"] for f in scanned_files]

    # indices 区间过滤
    if indices_raw and scanned_files is not None:
        idx_list = parse_selection(str(indices_raw), len(scanned_files))
        paths = [scanned_files[i - 1]["path"] for i in idx_list]

    return paths, scanned_files


def _get_video_info(bvid_or_url):
    """使用 yt-dlp --dump-json 获取视频详细信息"""
    bvid, url = _parse_bvid_or_url(bvid_or_url)
    if not bvid:
        return {"success": False, "error": "无效的视频标识"}

    ytdlp = _get_ytdlp_cmd()
    cmd = [*ytdlp, "--dump-json", "--no-playlist", url]
    try:
        proc = _run_capture(cmd)
        if proc.returncode != 0:
            return {"success": False, "error": proc.stderr.strip()[:300]}
        info = json.loads(proc.stdout.strip())
        formats = []
        for fmt in info.get("formats", []):
            vcodec = fmt.get("vcodec", "none")
            height = fmt.get("height") or 0
            formats.append({
                "format_id": fmt.get("format_id", ""),
                "ext": fmt.get("ext", ""),
                "height": height,
                "resolution": fmt.get("resolution", f"{height}p" if height else ""),
                "filesize": _format_bytes(fmt.get("filesize", 0) or 0),
                "vcodec": vcodec,
                "acodec": fmt.get("acodec", "none"),
                "fps": fmt.get("fps"),
            })
        # 提取可用画质高度（仅视频格式）
        durations_raw = info.get("duration", 0) or 0
        dur = int(durations_raw) if isinstance(durations_raw, (int, float)) else 0
        heights = set()
        for fmt in info.get("formats", []):
            if fmt.get("vcodec", "none") == "none":
                continue
            h = fmt.get("height") or 0
            if h > 0:
                heights.add(h)
        sorted_heights = sorted(heights, reverse=True)
        max_h = sorted_heights[0] if sorted_heights else 0

        return {
            "success": True,
            "title": info.get("title", ""),
            "bvid": bvid,
            "url": info.get("webpage_url", url),
            "duration": dur,
            "duration_str": f"{dur // 60}:{dur % 60:02d}",
            "view_count": info.get("view_count", 0),
            "like_count": info.get("like_count", 0),
            "uploader": info.get("uploader", ""),
            "upload_date": info.get("upload_date", ""),
            "description": (info.get("description", "") or "")[:200],
            "thumbnail": info.get("thumbnail", ""),
            "available_formats": formats,
            "format_count": len(formats),
            "available_heights": sorted_heights,
            "max_height": max_h,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "获取视频信息超时"}
    except FileNotFoundError:
        return {"success": False, "error": "yt-dlp 未安装"}
    except json.JSONDecodeError:
        return {"success": False, "error": "解析视频信息失败"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _format_video_info_text(data):
    if not data.get("success"):
        return f"[X] 获取失败: {data.get('error')}"
    heights = data.get("available_heights", [])
    height_str = f"（{', '.join(str(h) + 'p' for h in heights)}）" if heights else ""
    return "\n".join([
        f"标题: {data['title']}",
        f"UP主: {data['uploader']}",
        f"时长: {data['duration_str']}  |  播放: {data['view_count']}  |  点赞: {data['like_count']}",
        f"发布日期: {data['upload_date']}",
        f"URL: {data['url']}",
        f"可用画质: {data.get('max_height', 0)}p {height_str}（共 {data['format_count']} 种格式）",
    ] + ([f"简介: {data['description']}"] if data.get("description") else []))


def _format_quality_summary(videos):
    """批量查询视频可用画质，返回可读摘要"""
    if not videos:
        return ""
    height_map = {}
    unknown_count = 0
    for v in videos:
        bvid = v.get("bvid", "")
        info = _get_video_info(bvid)
        if not info.get("success") or info.get("available_heights") is None:
            height_map[bvid] = None
            unknown_count += 1
        else:
            height_map[bvid] = info.get("available_heights", [])

    def supports(heights, q):
        if heights is None:
            return None
        if not heights:
            return False
        return QUALITY_HEIGHT[q] <= max(heights)

    supported = {"1080p": [], "720p": [], "480p": [], "360p": []}
    for i, v in enumerate(videos, start=1):
        bvid = v.get("bvid", "")
        heights = height_map.get(bvid)
        for q in ["1080p", "720p", "480p", "360p"]:
            if supports(heights, q):
                supported[q].append(i)

    lines = ["--- 画质支持情况 ---"]
    for q in ["1080p", "720p", "480p", "360p"]:
        nums = supported[q]
        if nums:
            lines.append(f"  支持下载 {q} 的: {nums}")
    if unknown_count:
        lines.append(f"  画质待查（B站接口慢）: {unknown_count} 个视频")
    lines.append("  注: 下载时若目标画质不支持，按 allow_quality_fallback 决定——false（默认）则跳过/删除该视频，true 则降级到更低可用画质")
    return "\n".join(lines)


def _parse_part_to_list(part, part_count=None):
    """把 download_video 的 part 参数规范化为 list[int] 或 None（与 CLI 的 parse_selection 对齐）。

    支持:
      - None / '' / False / 0        -> None（只下第1P）
      - int (如 3)                   -> [3]
      - list (如 [1,3,5])            -> [1,3,5]
      - str (如 '1,3,5' / '1-5')     -> 用 parse_selection 解析为 list[int]
      - str 'all'                    -> 由调用方转成 all_parts=True（本函数返回 None）
    part_count: 探测到的真实分P数，用于越界裁剪（可选）。
    """
    if part is None or part == "" or part is False:
        return None
    if isinstance(part, bool):
        return None
    if isinstance(part, int):
        return [part] if part > 0 else None
    if isinstance(part, list):
        nums = []
        for x in part:
            try:
                n = int(x)
                if n > 0:
                    nums.append(n)
            except (TypeError, ValueError):
                continue
        return nums or None
    if isinstance(part, str):
        s = part.strip()
        if not s:
            return None
        max_n = part_count or 9999
        return parse_selection(s, max_n) or None
    return None


def _format_part_list_text(data):
    """将分P列表格式化为可读字符串（MCP 响应文本）。"""
    if not data.get("success"):
        return f"[X] 获取分P列表失败: {data.get('error')}"
    pc = data.get("part_count", 0)
    is_multi = pc > 1
    lines = [
        f"BV号: {data.get('bvid')}",
        f"是否多P: {'是' if is_multi else '否'}  |  分P数: {pc}",
    ]
    if is_multi:
        lines.append("各分P标题:")
        for p in data.get("parts", []):
            lines.append(f"  [p{p['index']}] {p['title']}")
    return "\n".join(lines)


def _batch_download_videos(items, quality="best", download_dir=None,
                           part=None, all_parts=False, concurrency=1, allow_fb=False,
                           auto_transcribe=False, hot_words=None):
    """对 bvid_or_url 列表批量下载，返回 (results, ok, total)。

    results 每项: {"input": bvid_or_url, "success": bool, "result": download_result_dict,
                   "note": str|None}
    当某个视频为多P但本次未要求 all_parts（默认只下第1P）时，note 提示调用方
    实际只下载了第1P，避免静默丢P。
    """
    def work(bvid_or_url):
        p = part
        # 子集选择时，按该视频真实分P数裁剪，避免越界（与 CLI 探测分P一致）
        if isinstance(p, list) and p and not all_parts:
            det = detect_parts_cached(bvid_or_url)
            if det.get("success") and det.get("is_multi"):
                pc = det.get("part_count", 1)
                p = [x for x in p if 1 <= x <= pc] or None
        # 下载前画质预检：不降级且目标画质源端不可用时直接跳过（不下载）
        ok, reason = preflight_quality_ok(bvid_or_url, quality, allow_fb)
        if not ok:
            return {"input": bvid_or_url, "success": False,
                    "status": "skipped_quality_unavailable",
                    "result": {"success": False, "status": "skipped", "reason": reason}}
        try:
            res = download_video(bvid_or_url, download_dir, quality,
                                 part=p, all_parts=all_parts)
        except Exception as e:
            res = {"success": False, "error": str(e)}
        # 下载后画质降级兜底：仍下载了低画质则删除文件放弃
        res, _, note = post_download_quality_check(res, allow_fb)
        result = {"input": bvid_or_url, "success": bool(res.get("success")), "result": res}
        # 分P预览：标明实际下载的分P（多P场景，单P为空串）（#7）
        if res.get("success"):
            result["parts_preview"] = _build_parts_preview(bvid_or_url, all_parts, p)
        if note:
            result["note"] = note
        # 下载后自动转写（配置优先）：仅下载成功且讯飞凭证就绪时触发
        # 与 CLI(_auto_transcribe_files) 一致：凭证缺失时静默跳过，不尝试、不报错
        if auto_transcribe and res.get("success"):
            _xf_ready, _xf_err = ensure_xfyun_config()
            if not _xf_ready:
                res["transcription_skipped"] = f"讯飞凭证未配置，自动转写已跳过（{_xf_err}）"
            else:
                _audios = res.get("audio_files") or ([res.get("audio_file")] if res.get("audio_file") else [])
                _trans = []
                for _a in _audios:
                    if _a and os.path.exists(_a):
                        try:
                            _trans.append({"audio": _a, "result": transcribe_file(_a, hot_words=hot_words)})
                        except Exception as e:
                            _trans.append({"audio": _a, "success": False, "error": str(e)})
                if _trans:
                    res["transcription"] = _trans
        # 多P 静默截断提示：未要求 all_parts 且下载成功时，探测是否其实为多P
        if not all_parts and res.get("success"):
            det = detect_parts_cached(bvid_or_url)
            if det.get("success") and det.get("is_multi") and det.get("part_count", 1) > 1:
                multi_note = (
                    f"[注] 该视频含 {det['part_count']} 个分P，"
                    f"仅下载第1P；传 all_parts=true 可下载全部"
                )
                result["note"] = (result.get("note") + "  " + multi_note) if result.get("note") else multi_note
        return result
    return _run_batch(items, work, concurrency)


def _batch_transcribe_files(paths, hot_words=None, concurrency=1, progress_callback=None):
    """对文件列表批量转写，返回 (results, ok, total)。

    先验证讯飞凭证；若缺失则所有项标记为失败。
    results 每项: transcribe_file 返回的 dict，或 {"success":False, "error":..., "video_name":...}
    progress_callback: 透传给 transcribe_file 的进度回调（可选）。
    """
    paths = [p for p in paths if p]
    if not paths:
        return [], 0, 0

    # 提前校验讯飞凭证
    cfg = XFYunConfig()
    cfg.load()
    if not cfg.is_ready:
        return [{"input": p, "success": False, "video_name": os.path.basename(p),
                 "error": "讯飞凭证未配置"} for p in paths], 0, len(paths)

    def work(path):
        try:
            return transcribe_file(path, hot_words=hot_words, progress_callback=progress_callback)
        except Exception as e:
            return {"success": False, "error": str(e), "video_name": os.path.basename(path)}

    # 按文件名保持输入顺序（同名文件排到末尾，保持原行为）
    path_map = {p: i for i, p in enumerate(paths)}

    def sort_key(r):
        return path_map.get(os.path.basename(r.get("input", r.get("video_name", ""))), 9999)
    return _run_batch(paths, work, concurrency, sort_key=sort_key)


if __name__ == "__main__":
    main()
