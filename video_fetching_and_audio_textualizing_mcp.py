# -*- coding: utf-8 -*-
"""
B站视频 + 语音转写 流水线 MCP Server
=====================================
一站式 MCP 服务：搜索 B站视频 → yt-dlp 下载 → 讯飞 ASR 语音转写。

暴露的 MCP 工具（共 8 个）:

  搜索与下载:
    search_video          搜索 Bilibili 视频
    download_video        下载视频（自动提取音频）
    get_video_info        获取视频详情（画质/大小/可用高度等）
    get_part_list         获取视频分P列表（各分P序号与标题）
    check_deps            检查依赖（yt-dlp + ffmpeg + ffprobe + 讯飞凭证）
    get_quality_summary   批量查询多视频画质支持情况

  语音转写:
    list_videos           列出音视频文件（用于转写）
    transcribe            转写为文字 + SRT（自动识别单文件/批量）

使用方法:
  python video_fetching_and_audio_textualizing_mcp.py

依赖: video_fetching_and_audio_textualizing_common.py（公共逻辑模块）
"""

import sys
import json
import os
import subprocess

# ── 核心逻辑全部来自 video_fetching_and_audio_textualizing_common ──────────────────────────────────
from video_fetching_and_audio_textualizing_common import (
    # 常量
    DEFAULT_VIDEOS_DIR,
    QUALITY_VALID,
    QUALITY_HEIGHT,

    # 搜索
    search_bilibili,

    # 下载
    download_video, format_download_text, detect_parts, get_part_list,

    # 转写
    ensure_xfyun_config,
    transcribe_file,
    parse_hotwords_string,

    # 辅助
    format_file_list,
    parse_selection,
    check_dependencies, format_deps_text,

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

def _send_response(req_id, result):
    payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}, ensure_ascii=False) + "\n"
    sys.stdout.buffer.write(payload.encode("utf-8"))
    sys.stdout.buffer.flush()


def _send_error(req_id, code, message):
    payload = json.dumps({
        "jsonrpc": "2.0", "id": req_id,
        "error": {"code": code, "message": message}
    }, ensure_ascii=False) + "\n"
    sys.stdout.buffer.write(payload.encode("utf-8"))
    sys.stdout.buffer.flush()


# ============================================================
# MCP 工具定义
# ============================================================

TOOLS = [
    # ==================== 搜索与下载 ====================
    {
        "name": "search_video",
        "description": ("搜索 Bilibili 视频。按关键词搜索，【返回查到的全部信息】供用户挑选下载：序号、标题、UP主、分区、上传日期、时长、"
                        "播放、点赞、收藏、弹幕、简介、标签，以及每条视频的完整 URL 链接（务必带 URL，便于用户定位/直接下载）。支持分页(page)。\n"
                        "【流程】搜到结果后，把列表（含全部字段与 URL）以【对齐表格】形式呈现给用户"
                        "（列：序号/标题/UP主/分区/时长/播放/点赞/收藏/弹幕/URL，对齐风格参考 CLI 的 _print_file_table），【不要逐条块列】；"
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
        "description": ("使用 yt-dlp 下载 Bilibili 视频到本地并提取音频到 videos/audios/。支持 BV 号或完整 URL，"
                        "可指定画质(quality)与分P(part)。\n"
                        "【铁律·必须先问用户再下载】本工具无法在下载过程中反问用户，所有有选择处都必须先问：\n"
                        "  1) 画质：先调 get_quality_summary(批量) 或 get_video_info(单个) 查清支持的画质，"
                        "再【向用户展示并明确询问】选哪个（best 可作为推荐项呈现，但不得未经询问就直接下载）。\n"
                        "  2) 分P（最关键）：对每个候选视频先调 get_part_list 判断是否多P。"
                        "单P视频直接下；多P视频【必须展示各分P标题并明确询问】要下全部(all_parts=true)/仅第1P/指定哪些(part=[1,3,5]或'1-5')，"
                        "绝不能对多P视频默认只下第1P或默认全下，也绝不能不问用户就下载。\n"
                        "  3) 批量且各视频分P选择不同时：不要传 bvid_or_url 数组+单一全局 part（那会对所有视频套用同一选择）。"
                        "应逐视频调用本工具，每个多P视频带上该视频用户指定的 part/all_parts；单P视频可合并批量。\n"
                        "画质不支持时自动降级。"),
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
                    "description": "画质选择: best(最佳)/1080p/720p/480p/360p。下载后自动提取音频。如目标画质不可用，自动回退到更低的画质。",
                    "default": "best",
                },
                "download_dir": {
                    "type": "string",
                    "description": "下载目录路径，默认 videos/",
                },
                "part": {
                    "type": ["integer", "array", "string"],
                    "description": ("指定下载的分P（1-based）。支持三种形式: 单个序号(整数, 如 3); "
                                    "数组(如 [1,3,5])下载多个指定分P; 字符串(如 '1,3,5'、'1-5'、'all')。 "
                                    "不填则默认只下载第1P。传 'all' 或数组/区间=shell 全部或某几个分P，"
                                    "等价于下载对应的分P。与 all_parts 互斥（传 all_parts=true 时本参数被忽略）。 "
                                    "批量下载时对所有条目统一生效。下载前可用 get_part_list 查看各分P标题再决定。"),
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
        "description": ("使用 yt-dlp 获取 Bilibili 视频详细信息（不下载），包括可用画质格式、大小、标题等。"
                        "【流程】在调用 download_video 前，先本工具(或 get_quality_summary)查清该视频支持哪些画质，"
                        "然后【必须向用户展示并明确询问】选哪个画质，把用户选择传给 download_video 的 quality 参数；"
                        "不得未经询问就默认 best 下载。"),
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
                        "全部(all_parts=true) / 仅第1P(默认) / 指定若干(part=[1,3,5] 或 '1-5')，"
                        "再把用户选择作为 download_video 的 part 或 all_parts 参数传入；不要替用户默认全下或不下。"),
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
        "name": "get_quality_summary",
        "description": ("批量查询多个视频支持的画质，返回可读摘要（每个画质支持哪些序号）。输入 search_video 返回的 videos 列表。"
                        "【流程】调用 download_video 前，先用本工具查清这些视频都支持哪些画质，"
                        "然后【必须向用户展示可选画质并明确询问】每个视频下哪个画质（不得默认 best），"
                        "再把用户的选择作为 quality 传给对应的 download_video 调用。"),
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

    # ==================== 语音转写 ====================
    {
        "name": "list_videos",
        "description": ("列出 videos/ 文件夹中的【全部】音视频文件（mp4/mkv/avi/mp3/m4a/wav 等），返回序号、文件名、类型、"
                        "大小、时长、画质、修改时间。用于选择待转写文件（CLI 版会用 ffprobe 探测时长/画质，本工具默认同样探测）。"
                        "注意：本工具返回的是【全部】文件，agent 必须将完整列表以【对齐表格】（序号/文件名/类型/大小/时长/画质/修改日期）"
                        "完整展示给用户，不要只举数例或自行筛选。"
                        "在'下载后转写'流程中，下载完成调用本工具即可拿到刚下载的文件列表，"
                        "再传给 transcribe（并先按铁律询问转哪些文件、是否加热词）。"),
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
        "description": ("将视频/音频文件转写为文字。支持单文件、文件数组、文件夹扫描、indices 区间选择；自动提取音频、调讯飞API，"
                        "输出 纯文本+SRT字幕+分段JSON 存到 videos/transcripts/。\n"
                        "【流程·必须先问用户再转写】\n"
                        "  1) 转哪些：先调 list_videos 列出本地【全部】音视频文件；你必须把返回的完整列表以【对齐表格】形式"
                        "（列：序号/文件名/类型/大小/时长/画质/修改日期，对齐风格参考 CLI 的 _print_file_table）"
                        "【完整呈现】给用户，【不得只举数例或自行筛选】；再【明确询问】转哪些（可'全部'或指定序号/区间）。"
                        "也可转写刚下载的文件（配合下载流程）。不得不经询问就转写全部。\n"
                        "  2) 热词（点9）：【明确询问用户】有无要加的专有名词热词（如游戏术语，可显著提升识别率），"
                        "示例: 渊下宫,七圣召唤,雷电将军；用户说没有就传空或不传。每个热词需 2-16 字，不符的会被忽略。\n"
                        "  3) 确认：把'将转写 N 个文件 + 热词=xxx'展示给用户确认后再执行。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "video_path": {
                    "type": ["string", "array"],
                    "description": "视频/音频文件路径（绝对路径或相对于 videos/ 的文件名）。也可传字符串数组批量转写；留空则自动扫描 folder 下全部文件。可传文件夹路径自动扫描。",
                },
                "hot_words": {
                    "type": "string",
                    "description": ("热词列表，词之间用逗号/分号/顿号/空格分隔（如: 渊下宫,七圣召唤,雷电将军）。"
                                    "热词可显著提升游戏术语识别率。调用前请先【询问用户】是否需要，用户说没有则不要传此参数。每个热词需 2-16 字。"),
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
                                "支持三种使用模式，由你(agent)先【询问用户】选择：\n"
                                "  1) 仅搜索（search_video）；\n"
                                "  2) 搜索并下载（搜索后 download_video，下载前必须逐项询问画质/分P）；\n"
                                "  3) 搜索→下载→转写（下载完成后自动转写刚下载的全部文件：list_videos + transcribe）。\n"
                                "【铁律】凡有选择的地方（画质、分P、转写哪些文件、是否加热词、是否继续转写），"
                                "你必须先把决策信息展示给用户，并【明确询问】其意向，绝不能自行默认或替用户决定。"
                                "批量下载时若其中某些视频是多P，必须对每个多P视频单独询问要下哪些分P。\n"
                                "【标准对话流示例·务必照此节奏】\n"
                                "用户：「搜下原神攻略，挑几个有意思的下」\n"
                                "→ 你：search_video(keyword=\"原神攻略\")，把列表(标题/UP主/分区/时长/播放等)展示给用户，"
                                "并【问】：想下哪几个？要不要翻页看更多？\n"
                                "用户：「下第1和第3个」\n"
                                "→ 你：先 get_quality_summary 或逐个 get_video_info 查清这俩支持的画质，"
                                "展示后【问每个视频选哪个画质】(不得默认 best)。再对第3个调 get_part_list，"
                                "发现它是多P，展示各分P标题后【问】：下全部/仅第1P/指定哪些？第1个若是单P就不必问分P。\n"
                                "用户：「第1个1080p，第3个下第1、2P」\n"
                                "→ 你：逐视频调用 download_video——第1个 quality='1080p'；第3个 quality='1080p' 且 part=[1,2]。"
                                "（多P视频各自带自己的 part，绝不用单一全局 part 套所有视频）\n"
                                "用户若选模式3(下载后转写)：\n"
                                "→ 你：list_videos 列出【刚下载】的文件（表格呈现），【问】转哪些；再【问】是否加专有名词热词(示例:渊下宫,七圣召唤)；"
                                "展示「将转写 N 个文件 + 热词=xxx」让用户确认后，再 transcribe。\n"
                                "若用户要转写【已有本地库】中的视频（非刚下载）：先 list_videos 列出【全部】本地音视频文件，"
                                "以【对齐表格】（序号/文件名/类型/大小/时长/画质/修改日期）完整呈现，【不得只举数例】；"
                                "再【问】转哪些 + 是否加热词，确认后 transcribe。"),
            },
        })
        return

    # ---- 工具列表 ----
    if method == "tools/list":
        _send_response(req_id, {"tools": TOOLS})
        return

    # ---- 工具调用 ----
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}

        # ── search_video ──
        if name == "search_video":
            keyword = args.get("keyword", "")
            page = int(args.get("page", 1))
            page_size = int(args.get("page_size", 10))
            if not keyword:
                _send_error(req_id, -32602, "缺少必填参数: keyword")
                return
            data = search_bilibili(keyword, page, page_size)
            text = _format_search_results(data)
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
            quality = args.get("quality", "best")
            download_dir = args.get("download_dir")
            all_parts = bool(args.get("all_parts", False))
            concurrency = int(args.get("concurrency", 1))

            # 归一化 part 参数：支持 整数 / 数组 / 字符串(区间, 'all')
            raw_part = args.get("part")
            if isinstance(raw_part, str) and raw_part.strip().lower() in ("all", "a"):
                all_parts = True
                norm_part = None
            else:
                # 单条时先探测分P数，避免子集选择越界
                pc = None
                if len(items) == 1:
                    det = detect_parts(items[0])
                    if det.get("success") and det.get("is_multi"):
                        pc = det.get("part_count")
                norm_part = _parse_part_to_list(raw_part, pc)

            # 单条
            if len(items) == 1:
                result = download_video(items[0], download_dir, quality, part=norm_part, all_parts=all_parts)
                text = format_download_text(result)
                _send_response(req_id, {
                    "content": [{"type": "text", "text": text}],
                    "isError": not result.get("success"),
                    "_raw_data": result,
                })
                return

            # 批量
            results, ok, total = _batch_download_videos(items, quality, download_dir, norm_part, all_parts, concurrency)
            lines = [f"批量下载完成：共 {total} 个，成功 {ok}，失败 {total - ok}", "=" * 40]
            for r in results:
                if r["success"]:
                    lines.append(f"  [OK] {r['input']}\n      {r['result'].get('message', 'OK')}")
                    if r.get("note"):
                        lines.append(f"      {r['note']}")
                else:
                    lines.append(f"  [X] {r['input']}: {r['result'].get('error', 'unknown')}")
            _send_response(req_id, {
                "content": [{"type": "text", "text": "\n".join(lines)}],
                "isError": ok < total,
                "_raw": {"total": total, "ok": ok, "failed": total - ok, "results": results},
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
            n_video = sum(1 for f in files if not f.get("is_audio"))
            n_audio = len(files) - n_video
            rows = []
            for i, f in enumerate(files, 1):
                dur = f.get("duration_str", "-")
                q = f.get("quality_str", "-")
                rows.append(
                    f"  [{i}] {f['name']}  ({f.get('type_str', '-')}, {f['size_str']}, "
                    f"时长 {dur}, {q}, {f['mtime_str']})"
                )
            text = (f"共 {len(files)} 个文件（{n_video} 视频 / {n_audio} 音频，文件夹: {folder}）:\n"
                    + "\n".join(rows)
                    + "\n（仅扫描根目录，不读取子目录"
                    + ("；时长/画质经 ffprobe 探测，文件多时可能稍慢" if probe else "")
                    + "）")
            _send_response(req_id, {
                "content": [{"type": "text", "text": text}],
                "_raw": {"files": files, "folder": folder, "probed": probe},
            })
            return

        # ── transcribe ──
        if name == "transcribe":
            raw = args.get("video_path", "")
            hot_raw = args.get("hot_words", "")
            hot_words, _ = parse_hotwords_string(hot_raw) if hot_raw else ([], [])
            if not hot_words:
                hot_words = None
            folder = args.get("folder")
            indices_raw = args.get("indices")
            concurrency = int(args.get("concurrency", 1))

            paths, _scanned = _normalize_transcribe_inputs(raw, folder, indices_raw)

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
                    res = transcribe_file(video_path, hot_words=hot_words)
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
                _send_response(req_id, {
                    "content": [{"type": "text", "text": text}],
                    "_raw": {"result": res["result"], "saved": saved},
                })
                return

            # 批量
            results, ok, total = _batch_transcribe_files(paths, hot_words, concurrency)
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
            _send_response(req_id, {
                "content": [{"type": "text", "text": "\n".join(lines)}],
                "isError": ok < total,
                "_raw": {"total": total, "ok": ok, "failed": total - ok, "results": results},
            })
            return

        # ── 未知工具 ──
        _send_error(req_id, -32601, f"未知工具: {name}")
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


# ============================================================
# 以下函数原属 common（仅 mcp 使用），已移入并加 _ 前缀
# ============================================================

def _normalize_transcribe_inputs(raw, folder=None, indices_raw=None):
    """统一转写输入归一化：字符串/数组/文件夹路径 → 路径列表。
    CLI 和 MCP 共用此函数，消除各自的输入判断逻辑。

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


def _format_search_results(data: dict) -> str:
    """将搜索结果格式化为可读字符串（MCP 响应文本）。"""
    if not data.get("success"):
        return f"❌ 搜索失败：{data.get('error', '未知错误')}"

    keyword = data.get("keyword", "")
    page = data.get("page", 1)
    total = data.get("total", 0)
    page_count = data.get("page_count", 1)
    videos = data.get("videos", [])

    header = (
        f"🔍 关键词：{keyword}  |  "
        f"第 {page}/{page_count} 页  |  "
        f"共 {total} 个视频\n"
    )
    if not videos:
        return header + "⚠️  未找到相关视频"

    lines = [header, f"{'─' * 60}"]
    for i, v in enumerate(videos, (page - 1) * 10 + 1):
        title = v.get("title", "（无标题）")
        author = v.get("author", "未知作者")
        typename = v.get("typename", "")
        pubdate = v.get("pubdate", "-")
        play = v.get("play", "-")
        like = v.get("like", "-")
        favorites = v.get("favorites", "-")
        video_review = v.get("video_review", "-")
        duration = v.get("duration", "-")
        bvid = v.get("bvid", "")
        url = v.get("url") or f"https://www.bilibili.com/video/{bvid}"
        desc = v.get("description", "")
        tag = v.get("tag", "")
        meta = f"   👤 UP：{author}"
        if typename:
            meta += f"  |  🗂 分区：{typename}"
        meta += f"  |  📅 {pubdate}  |  ⏱ {duration}"
        extra = ""
        if desc:
            extra += f"   📝 简介：{desc}\n"
        if tag:
            extra += f"   🏷 标签：{tag}\n"
        lines.append(
            f"{i}. {title}\n"
            f"{meta}\n"
            f"   ▶ 播放 {play}  |  👍 点赞 {like}  |  ⭐ 收藏 {favorites}  |  💬 弹幕 {video_review}\n"
            f"   🔗 URL：{url}\n"
            + extra
        )
    return "\n".join(lines)


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
    lines.append("  注: 下载时若目标画质不支持，自动降级到更低的可用画质")
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
                          part=None, all_parts=False, concurrency=1):
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
            det = detect_parts(bvid_or_url)
            if det.get("success") and det.get("is_multi"):
                pc = det.get("part_count", 1)
                p = [x for x in p if 1 <= x <= pc] or None
        try:
            res = download_video(bvid_or_url, download_dir, quality,
                                  part=p, all_parts=all_parts)
        except Exception as e:
            res = {"success": False, "error": str(e)}
        result = {"input": bvid_or_url, "success": bool(res.get("success")), "result": res}
        # 多P 静默截断提示：未要求 all_parts 且下载成功时，探测是否其实为多P
        if not all_parts and res.get("success"):
            det = detect_parts(bvid_or_url)
            if det.get("success") and det.get("is_multi") and det.get("part_count", 1) > 1:
                result["note"] = (
                    f"[注] 该视频含 {det['part_count']} 个分P，"
                    f"仅下载第1P；传 all_parts=true 可下载全部"
                )
        return result
    return _run_batch(items, work, concurrency)


def _batch_transcribe_files(paths, hot_words=None, concurrency=1):
    """对文件列表批量转写，返回 (results, ok, total)。

    先验证讯飞凭证；若缺失则所有项标记为失败。
    results 每项: transcribe_file 返回的 dict，或 {"success":False, "error":..., "video_name":...}
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
            return transcribe_file(path, hot_words=hot_words)
        except Exception as e:
            return {"success": False, "error": str(e), "video_name": os.path.basename(path)}

    # 按文件名保持输入顺序（同名文件排到末尾，保持原行为）
    path_map = {p: i for i, p in enumerate(paths)}
    sort_key = lambda r: path_map.get(os.path.basename(r.get("input", r.get("video_name", ""))), 9999)
    return _run_batch(paths, work, concurrency, sort_key=sort_key)


if __name__ == "__main__":
    main()
