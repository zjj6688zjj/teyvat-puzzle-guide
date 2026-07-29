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

from video_fetching_and_audio_textualizing_common import (
    # 常量
    DEFAULT_VIDEOS_DIR, DEFAULT_AUDIO_DIR, DEFAULT_OUTPUT_DIR,

    # 搜索
    search_bilibili,
    format_search_results,

    # 下载
    download_video, format_download_text, detect_parts, get_part_list, check_quality_available,
    preflight_quality_ok, post_download_quality_check, detect_parts_cached,

    # 转写
    ensure_xfyun_config,
    resolve_audio_path,
    transcribe_file,

    # 辅助
    format_file_list, format_file_table, parse_selection, SUPPORTED_EXTS, AUDIO_EXTS,
    parse_hotwords_string,
    check_dependencies, format_deps_text,
    PROBE_HINT,
)
import sys
import os
import time
import re
import json

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ── 核心逻辑全部来自 video_fetching_and_audio_textualizing_common ──────────────────────────────────

PAGE_SIZE = 10

# 用户偏好配置文件路径（首次运行设置后持久化）
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# 用户偏好（运行时由 main 载入；含 auto_transcribe_after_download 与 hot_words）
user_prefs = {"auto_transcribe_after_download": False, "hot_words": []}

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
# 搜索
# ============================================================


def _do_search(keyword, page=1):
    """执行搜索并打印结果，返回完整 data（含 videos / page_count）"""
    data = search_bilibili(keyword, page, PAGE_SIZE)
    print(f"\n{format_search_results(data, show_details=False, page_size=PAGE_SIZE)}")
    return data

# ============================================================
# 画质交互
# ============================================================


def _fmt_quality(q):
    """将配置的画质值转成展示文案：best 展示为「可下载的最高画质」，None 展示为「每次询问」。"""
    if q is None:
        return "每次询问"
    if q == "best":
        return "可下载的最高画质"
    return q


def _fmt_quality_pref(q):
    """画质偏好展示：未设置->每次询问；best->每次都默认为可下载的最高画质；其他->每次都默认为{画质}。"""
    if q is None:
        return "每次询问"
    if q == "best":
        return "每次都默认为可下载的最高画质"
    return f"每次都默认为{q}"


def _fmt_bool_pref(v):
    """降级/自动转写偏好展示：未设置->每次询问；True->每次都默认为是；False->每次都默认为否。"""
    if v is None:
        return "每次询问"
    return "每次都默认为是" if v else "每次都默认为否"


def _print_prefs_block(header, q, fb, auto, hw):
    """统一打印配置状态块（初始化/查看/更新 三处共用）。"""
    print(header)
    print(f"  下载时的指定画质：{_fmt_quality_pref(q)}")
    print(f"  下载源不提供指定画质时是否接受降画质下载：{_fmt_bool_pref(fb)}")
    print(f"  下载后是否直接自动转写：{_fmt_bool_pref(auto)}")
    if hw:
        print(f"  转写时每次都自动代入的热词有{len(hw)}个，列表如下：")
        for i, w in enumerate(hw, 1):
            print(f"    {i}: {w}")
    elif hw == []:
        print("  转写时每次都自动代入空热词表")
    else:
        print("  转写时不自动代入热词，每次询问")


def _prompt_choice(prompt_text, options, mark_key, mark_label: "str | None" = "(当前)", indent=2, enter_hint=None, enter_verb="保持当前"):
    """通用数字编号分行菜单（配置模式与初始化流程统一复用）。
    options: [(key, label), ...]，如 [('1','降质下载'), ('2','拒不下载'), ('0','每次询问')]
    mark_key: 标注并作为「直接回车」默认返回的选项 key（配置模式=当前值，初始化=默认值）。
    mark_label: 选项标注文案；为 None 时不显示选项标注。
        配置模式用 '(当前)'，初始化传 None（文案里不出现「默认」二字）。
    indent: prompt 的基准缩进空格数（选项行自动 +2）；统一为 2。
    enter_verb: 回车提示里描述「采用推荐项」的动词；配置模式默认「保持当前」（确有已存值可保持），
        初始化传「采用作者推荐」（首次新建，不存在「当前」，改用作者推荐措辞）。
    返回用户选择的 key 字符串（直接回车返回 mark_key）。"""
    key, _ = _prompt_choice_ex(prompt_text, options, mark_key, mark_label, indent, enter_hint, enter_verb)
    return key


def _prompt_choice_ex(prompt_text, options, mark_key, mark_label: "str | None" = "(当前)", indent=2, enter_hint=None, enter_verb="保持当前"):
    """与 _prompt_choice 相同，但返回 (key, is_enter) 元组：
    is_enter=True 表示用户直接回车（保持当前）；is_enter=False 表示显式输入了某个 key。
    仅第 4/4 热词项需要据此区分「回车保持当前」与「显式选 1 重新设置热词」。
    enter_hint: 回车提示文案；为 None 时按 mark_key 对应的选项文案动态生成
    （如「（或直接回车以保持降质下载）」），不写死「(当前)」。
    enter_verb: 见 _prompt_choice 同名参数。"""
    if enter_hint is None:
        # mark_key 必为选项中真实存在的键（所有调用点均已对齐），故 cur_label 恒非空
        cur_label = next((label for key, label in options if key == mark_key), "")
        enter_hint = f"（或直接回车以{enter_verb}的「{cur_label}」）"
    pad = " " * indent
    pad_opt = " " * (indent + 2)
    print(f"{pad}{prompt_text}")
    for key, label in options:
        mark = f" {mark_label}" if (mark_label and key == mark_key) else ""
        print(f"{pad_opt}{key}) {label}{mark}")
    keys = "/".join(k for k, _ in options)
    while True:
        try:
            ans = input(f"{pad_opt}请输入选项{enter_hint}: ").strip().lower()
        except KeyboardInterrupt:
            print()
            raise QuitProgram("检测到用户输入了 Ctrl+C，系统自动退出。再见！")
        except EOFError:
            raise QuitProgram("检测到用户输入了 Ctrl+Z（EOF），系统自动退出。再见！")
        if ans == "":
            return (mark_key, True)
        if any(ans == k for k, _ in options):
            return (ans, False)
        print(f"{pad_opt}[!] 无效输入: '{ans}'，请输入 {keys}（{enter_hint.strip('（）')}）")


# ============================================================
# 下载
# ============================================================

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
            confirm = input("  确定下载以上分P吗？（1 确定，2 重选，0 取消；直接回车默认为 1）: ").strip()
        except KeyboardInterrupt:
            # Ctrl+C 按全局约定退出程序，不应与「0 取消」的 return None 撞成同一哨兵
            print()  # Ctrl+C 不回显、无回车换行，补一行使其与 Ctrl+Z（EOF）退出的空行一致
            raise QuitProgram("检测到用户输入了 Ctrl+C，系统自动退出。再见！")
        except EOFError:
            raise QuitProgram("检测到用户输入了 Ctrl+Z（EOF），系统自动退出。再见！")
        if confirm in ("", "1"):
            return chosen
        if confirm == "0":
            return None
        if confirm == "2":
            continue  # 回到输入分P序号，重新选择
        print(f"  [!] 无效输入: '{confirm}'，可选: 0/1/2，或直接回车默认 1")


def _prompt_parts(bvid, part_count):
    """多P视频下载方式选择，返回 (parts, all_parts)。
    parts: None=只下第1P / list[int]=下载指定分P；all_parts=True 时 parts 为 None。"""
    print(f"    该视频共 {part_count} 个分P，如何下载？")
    print(f"    1) 下载全部P（共 {part_count} 个文件）")
    print(f"    2) 下载部分P（后面会问具体下载哪些分P）")
    print(f"    3) 仅下载第1P")
    while True:
        try:
            sel = input("  请选择（1/2/3，默认为 3）: ").strip()
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
        print(f"    [!] 无效输入: '{sel}'，可选: 1/2/3，或直接回车默认 3")


def _print_download_summary(download_results):
    """下载结尾聚合：打印成功/失败/跳过计数（明细来自 download_results，后续可改为表格）。"""
    if not download_results:
        return
    n_total = len(download_results)
    n_ok = sum(1 for r in download_results if r["status"] == "success")
    n_fail = sum(1 for r in download_results if r["status"] == "failed")
    n_skip = sum(1 for r in download_results if r["status"] == "skipped")
    if n_ok == n_total:
        _success(f"本次下载全部完成：成功 {n_ok}/{n_total} 个")
    elif n_ok == 0:
        _error(f"本次下载全部失败：成功 0/{n_total} 个（失败 {n_fail} 个，跳过 {n_skip} 个）")
    else:
        _info(f"本次下载完成：成功 {n_ok}/{n_total} 个（失败 {n_fail} 个，跳过 {n_skip} 个）")


def _print_transcribe_summary(results):
    """转写结尾聚合：打印成功/失败计数（明细来自 results，后续可改为表格）。"""
    if not results:
        return
    n_total = len(results)
    n_ok = sum(1 for r in results if r.get("success"))
    n_fail = n_total - n_ok
    if n_ok == n_total:
        _success(f"本次转写全部完成：成功 {n_ok}/{n_total} 个")
    elif n_ok == 0:
        _error(f"本次转写全部失败：成功 0/{n_total} 个（失败 {n_fail} 个）")
    else:
        _info(f"本次转写完成：成功 {n_ok}/{n_total} 个（失败 {n_fail} 个）")


def _do_download_batch(videos, indices, page_start=1):
    """批量下载指定序号（indices 为页面内 1-based，page_start 用于显示全局序号）"""
    downloaded_video_paths = []
    print()
    print(f"即将下载以下 {len(indices)} 个视频:")
    for local_num in indices:
        idx = local_num - 1
        v = videos[idx]
        global_num = page_start + idx
        print(f"  [{global_num}] {v['title']}")
    print()
    # 解析本次下载生效的配置；未设置默认值的项在每次下载时交互询问
    quality = user_prefs.get("default_quality")
    allow_fb_cfg = user_prefs.get("allow_quality_fallback")
    auto_cfg = user_prefs.get("auto_transcribe_after_download")
    hw_cfg = user_prefs.get("hot_words")

    # 预判本次是否有任何项需要交互询问；有则在开头统一打印一次横幅
    interacted = (
        quality is None
        or (allow_fb_cfg is None and quality != "best")
        or auto_cfg is None
        or (auto_cfg is True and hw_cfg is None)
    )
    if interacted:
        print("[来自配置模式的每次询问]")

    # 1/4 指定画质
    if quality is None:
        quality_ans = _prompt_choice(
            "[1/4] 本次下载时的指定画质：",
            [("1", "可下载的最高画质"), ("2", "1080p"), ("3", "720p"), ("4", "480p"), ("5", "360p")],
            mark_key="1", mark_label=None, indent=2, enter_verb="选择默认",
        )
        quality = {"1": "best", "2": "1080p", "3": "720p", "4": "480p", "5": "360p"}[quality_ans]
    else:
        # 配置中已指定画质，直接采用，跳过本次询问
        print(f"  （已采用配置中的设置：指定画质 = {_fmt_quality(quality)}，跳过 [1/4]「指定画质」询问）")
        interacted = True

    # 2/4 目标画质不可用时是否接受降级下载
    if allow_fb_cfg is None:
        if quality == "best":
            # best 即「可下载的最高画质」，不存在「指定画质源端不提供」的情况，无需询问是否降级
            allow_fb = False
            print("  （因 [1/4] 已选「可下载的最高画质」，不存在画质不达标需降级的情况；按「拒不下载」对待，已自动跳过 [2/4]「是否接受降画质」设置）")
            interacted = True
        else:
            fb_ans = _prompt_choice(
                "[2/4] 本次下载中下载源不提供指定画质时是否接受降画质下载：",
                [("1", "降质下载"), ("2", "拒不下载")],
                mark_key="1", mark_label=None, indent=2, enter_verb="选择默认",
            )
            allow_fb = (fb_ans == "1")
    else:
        # 配置中已设定降级策略，直接采用，跳过本次询问
        fb_label = "接受降画质下载" if allow_fb_cfg is True else "拒不下载"
        print(f"  （已采用配置中的设置：下载源不提供指定画质时 {fb_label}，跳过 [2/4]「是否接受降画质」询问）")
        allow_fb = allow_fb_cfg
        interacted = True

    # 3/4 是否默认在下载完成后直接自动转写
    if auto_cfg is None:
        auto_ans = _prompt_choice(
            "[3/4] 本次下载后是否直接自动转写：",
            [("1", "是"), ("2", "否")],
            mark_key="2", mark_label=None, indent=2, enter_verb="选择默认",
        )
        auto_transcribe = (auto_ans == "1")
    else:
        # 配置中已设定自动转写开关，直接采用，跳过本次询问
        auto_label = "下载后自动转写" if auto_cfg is True else "下载后手动转写（不自动转写）"
        print(f"  （已采用配置中的设置：{auto_label}，跳过 [3/4]「是否自动转写」询问）")
        auto_transcribe = auto_cfg
        interacted = True

    # 4/4 热词（供自动转写使用）
    if hw_cfg is None and auto_transcribe:
        hot_words = _input_hotwords(prompt="  [4/4] 本次转写时程序代入的热词")
    elif auto_transcribe:
        # 配置已保存热词（非空列表），本次自动转写将沿用，不再询问
        print(f"  （已采用配置中的设置：本次自动转写将代入已保存的 {len(hw_cfg)} 个热词，跳过 [4/4]「转写热词」输入）")
        hot_words = hw_cfg
        interacted = True
    else:
        # 本次不自动转写：无需代入热词，跳过询问
        print("  （因 [3/4] 已设为「不自动转写」，无需代入热词；本次不代入任何热词，已自动跳过 [4/4]「转写热词」输入）")
        hot_words = hw_cfg or []
        interacted = True

    download_results = []  # 本次批量下载各视频的结果汇总（供结尾聚合，后续可改为表格）
    for local_num in indices:
        idx = local_num - 1
        v = videos[idx]
        global_num = page_start + idx
        sep = "\n" if interacted else ""
        print(f"{sep}准备下载视频[{global_num}]...")
        print(f"- 视频标题: {v['title']}")
        q = quality
        print(f"- 观看网址: {v['url']}")
        print(f"- 目标画质: {_fmt_quality(q)}")

        if not v.get("bvid"):
            print(f"  [!] 下载 {v['title']} 失败: 缺少 BV 号，数据不完整")
            download_results.append({"title": v['title'], "status": "failed", "reason": "缺少 BV 号，数据不完整", "files": [], "audios": []})
            continue

        # 分P探测 + 交互选择
        parts, all_parts = None, False
        bvid = v["bvid"]

        # 下载前画质预检：若「不降级下载」且目标画质源端不可用，直接跳过（不浪费时间下载再删）
        ok, _reason = preflight_quality_ok(bvid, q, allow_fb)
        if not ok:
            print(f"  [跳过] 目标画质 {q} 源端不可用，已设置不降级下载，跳过该视频（未下载）")
            download_results.append({"title": v['title'], "status": "skipped", "reason": f"目标画质 {q} 源端不可用（不降级下载）", "files": [], "audios": []})
            continue
        # avail 为 None 时无法判定，继续走下载流程（其后仍会按降级逻辑兜底）

        det = detect_parts_cached(bvid)
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
            # 需转成 QuitProgram 优雅退出，避免抛 traceback（与 _transcribe_one 中音频提取、上传/转写阶段的 Ctrl+C 处理同源）
            print()
            raise QuitProgram("检测到用户输入了 Ctrl+C，系统自动退出。再见！")
        print()
        if result.get("success"):
            result, _deleted, _note = post_download_quality_check(result, allow_fb)
            qw = result.get("quality_warning")
            if _deleted:
                print(f"视频[{global_num}] 目标画质 {quality} 不可用，已设置不降级下载，放弃该视频")
                print(f"  [!] {qw}（文件已删除）")
                download_results.append({"title": v['title'], "status": "failed", "reason": f"目标画质 {quality} 不可用，已放弃并删除文件", "files": [], "audios": []})
                continue
            files = result.get("files") or ([result.get("file")] if result.get("file") else [])
            audios = result.get("audio_files") or ([result.get("audio_file")] if result.get("audio_file") else [])
            downloaded_video_paths.extend(files)
            print(f"视频[{global_num}]下载完成！（共 {len(files)} 个分P文件）")
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
            # 记录本视频下载结果（成功）：供结尾聚合，后续可改为表格
            download_results.append({
                "title": v['title'], "status": "success",
                "reason": qw or "", "files": files, "audios": audios,
            })
        else:
            text = format_download_text(result)
            print(text)
            print(f"  [!] 下载 {v['title']} 失败")
            download_results.append({
                "title": v['title'], "status": "failed",
                "reason": result.get("error") or "下载失败（原因见上方输出）",
                "files": [], "audios": [],
            })
            continue

    # —— 下载结尾聚合：成功 / 失败 / 跳过 计数（明细已存 download_results，后续可改为表格）——
    print()  # 与最后一个视频的下载提示之间空一行，不要紧连
    _print_download_summary(download_results)

    # 若用户在首次设置中开启了「下载后自动转写」，直接对本次下载的视频跑转写（热词取配置）
    if auto_transcribe and downloaded_video_paths:
        _auto_transcribe_files(downloaded_video_paths, hot_words)

    print()

# ============================================================
# 文件列表表格
# ============================================================


# ============================================================
# 热词输入（CLI 交互）
# ============================================================


def _print_hotword_report(valid, invalid, indent="    ", persistent=True):
    """把合规热词与不合规热词各列一张编号表（仅 CLI 输出一次）。

    persistent=True  （初始化 / 配置模式）：热词将写入 config.json，措辞用「已保存 / 未保存」。
    persistent=False （下载后自动转写 / 手动转写）：热词仅本次使用、不写盘，
                      措辞改为「一次性热词 / 未使用」，并提示进入配置模式可复用。"""
    if valid:
        if persistent:
            print(f"{indent}以下 {len(valid)} 个合规热词已保存：")
        else:
            print(f"{indent}本次转写将使用以下一次性热词（若需多次复用热词，请进入配置模式自行配置）：")
        for i, w in enumerate(valid, 1):
            print(f"{indent}{i}: {w}")
    if invalid:
        verb = "未保存" if persistent else "未使用"
        print(f"{indent}以下 {len(invalid)} 个热词因字数不符（需2~16，每个汉字/常用字符按 1 个字符计）而{verb}：")
        for i, w in enumerate(invalid, 1):
            print(f"{indent}{i}: {w}")


# 热词输入的统一规则提示：覆盖三方面——①长度(单个热词 2~16 字符)
# ②数量(最多 200 个) ③划分(多个热词仅以空格分隔，逗号/分号/顿号等标点视为词的一部分、不作为分隔符)
HOTWORD_RULES = "（热词要求：单个热词限 2~16 个字符，按 UTF-16 代码单元计数，普通汉字/字母每个算 1；单次转写最多上传 200 个热词，多个热词之间仅可用空格分隔，逗号/分号/顿号等标点视为词的一部分而不作为分隔符）"


def _input_hotwords(prompt=None, persistent=False):
    """让用户输入热词（仅保留 2-16 字符、最多 200 个的词）。

    prompt: 自定义输入提示（默认保留原提示）；统一规则提示 HOTWORD_RULES 会自动附加在提示末尾。
    persistent: True=热词将写入 config.json（初始化 / 配置模式）；
                False=仅本次转写使用、不写盘（下载后自动转写 / 手动转写场景）。"""
    if prompt is None:
        prompt = "请输入热词" + HOTWORD_RULES + "："
    else:
        # 调用方传入的 prompt 不含规则说明，这里统一附加，保证任意入口都有提示
        prompt = prompt + HOTWORD_RULES + "："
    try:
        raw = input(prompt).strip()
    except KeyboardInterrupt:
        # Ctrl+C 不应被当成「留空跳过热词」(return []) 而继续转写，按全局约定退出程序
        print()  # Ctrl+C 不回显、无回车换行，补一行使其与 Ctrl+Z（EOF）退出的空行一致
        raise QuitProgram("检测到用户输入了 Ctrl+C，系统自动退出。再见！")
    except EOFError:
        raise QuitProgram("检测到用户输入了 Ctrl+Z（EOF），系统自动退出。再见！")
    if not raw:
        return []
    valid, invalid = parse_hotwords_string(raw)
    # 合规热词按 persistent 决定「已保存 / 一次性使用」措辞；不合规热词同样按此分支告知
    _print_hotword_report(valid, invalid, indent="    ", persistent=persistent)
    return valid

# ============================================================
# 讯飞凭证检查（CLI UI 版）
# ============================================================


def _check_xfyun_config():
    """检查讯飞凭证是否就绪（CLI 交互提示版）"""
    is_ready, err_msg = ensure_xfyun_config()
    if not is_ready:
        print("\n[WARN] 讯飞凭证未配置，需要先按照 README.md 配置讯飞凭证后才能正常转写")
        return False
    return True


# ============================================================
# 用户偏好（首次运行设置 + 持久化）
# ============================================================


def _load_prefs():
    """从统一的 config.json 读取 CLI 偏好；文件不存在或偏好字段缺失返回 None。"""
    if not os.path.exists(CONFIG_PATH):
        return None
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    # 四个偏好字段都必须存在于 config 中，才视为已初始化；否则走首次询问
    #（即便文件里只有讯飞凭证）。
    if not all(k in cfg for k in ("default_quality", "allow_quality_fallback",
                                  "auto_transcribe_after_download", "hot_words")):
        return None
    return {
        "default_quality": cfg.get("default_quality"),
        "allow_quality_fallback": cfg.get("allow_quality_fallback"),
        "auto_transcribe_after_download": cfg.get("auto_transcribe_after_download"),
        "hot_words": cfg.get("hot_words"),
    }


def _save_prefs(prefs):
    """把 CLI 偏好写回统一的 config.json，保留已有讯飞凭证等其它字段。"""
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError):
            cfg = {}
    # 重新构造，保证写入顺序：先保留其它已有字段（如讯飞凭证），
    # 再按 default_quality -> allow_quality_fallback -> auto_transcribe_after_download
    # -> hot_words 的顺序写入这四个偏好字段
    other = {k: v for k, v in cfg.items()
             if k not in ("default_quality", "allow_quality_fallback",
                          "auto_transcribe_after_download", "hot_words")}
    new_cfg = dict(other)
    new_cfg["default_quality"] = prefs["default_quality"]
    new_cfg["allow_quality_fallback"] = prefs["allow_quality_fallback"]
    new_cfg["auto_transcribe_after_download"] = prefs["auto_transcribe_after_download"]
    new_cfg["hot_words"] = prefs["hot_words"]
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(new_cfg, f, ensure_ascii=False, indent=2)


def _ensure_user_prefs():
    """首次运行时询问并保存用户偏好；之后直接从配置文件读取。

    返回 (prefs, is_first_run)：
      - prefs：当前生效的偏好 dict（含四个偏好字段）
      - is_first_run：True 表示本次为首次初始化（已自行打印「运行时配置已初始化如下」），
                       False 表示偏好来自已保存的配置文件（由调用方决定是否展示 [当前的运行时配置]）
    """
    prefs = _load_prefs()
    if prefs is not None:
        # 偏好字段已由 _load_prefs 保证齐全，直接返回
        return prefs, False

    print("[对运行时配置进行手动初始化]")

    # 1/4 默认下载画质（直接回车默认=作者推荐的可下载最高画质）
    default_quality = _prompt_choice(
        "[1/4] 请确认下载时的指定画质：",
        [("1", "可下载的最高画质"), ("2", "1080p"), ("3", "720p"), ("4", "480p"), ("5", "360p"), ("0", "每次询问")],
        mark_key="1", mark_label=None, indent=2, enter_verb="采用作者推荐",
    )
    default_quality = {"1": "best", "2": "1080p", "3": "720p", "4": "480p", "5": "360p", "0": None}[default_quality]

    # 2/4 目标画质不可用时是否接受降级下载（直接回车默认 接受降级）
    # 若 1/4 已选「可下载的最高画质」(best)，则不存在降级情形，跳过本项询问并明确告知
    if default_quality == "best":
        allow_fb = False
        print("  （因 [1/4] 已选「可下载的最高画质」，不存在画质不达标需降级的情况，已自动跳过 [2/4]「是否接受降画质」设置）")
    else:
        fb_ans = _prompt_choice(
            "[2/4] 请确认下载源不提供指定画质时是否接受降画质下载：",
            [("1", "降质下载"), ("2", "拒不下载"), ("0", "每次询问")],
            mark_key="1", mark_label=None, indent=2, enter_verb="采用作者推荐",
        )
        allow_fb = {"1": True, "2": False, "0": None}[fb_ans]

    # 3/4 是否默认在下载完成后直接自动转写（直接回车默认 关闭）
    auto_ans = _prompt_choice(
        "[3/4] 请确认下载后是否直接自动转写：",
        [("1", "下载后自动转写"), ("2", "下载后手动转写"), ("0", "每次询问")],
        mark_key="2", mark_label=None, indent=2, enter_verb="采用作者推荐",
    )
    auto = {"1": True, "2": False, "0": None}[auto_ans]

    # 4/4 热词（供自动转写使用，存盘后不再交互询问；与配置模式 [4/4] 文案统一选项措辞，
    # 首次初始化不存在「当前」值，故 mark_label 用 None（不显示 (当前)），回车采用作者推荐）
    hw_ans = _prompt_choice(
        "[4/4] 请确认转写的时候，无论是否为自动转写，程序自动代入的热词：",
        [("1", "设热词，程序后面会问具体热词表"), ("2", "不设热词，程序直接代入空热词表"), ("0", "每次询问")],
        mark_key="2", mark_label=None, indent=2, enter_verb="采用作者推荐",
    )
    if hw_ans == "1":
        hot_words = _input_hotwords(prompt="    请输入热词", persistent=True)
    elif hw_ans == "2":
        hot_words = []
    else:  # "0" -> 每次转写时都重新询问热词
        hot_words = None

    prefs = {
        "default_quality": default_quality,
        "allow_quality_fallback": allow_fb,
        "auto_transcribe_after_download": auto,
        "hot_words": hot_words,
    }
    _save_prefs(prefs)
    _print_prefs_block(f"\n运行时配置已初始化如下：", default_quality, allow_fb, auto, hot_words)
    return prefs, True


def _print_prefs_status():
    """在前端展示当前用户偏好状态（自动转写开关 + 热词 + 画质），便于随时确认设置。"""
    auto = user_prefs.get("auto_transcribe_after_download")
    hw = user_prefs.get("hot_words")
    q = user_prefs.get("default_quality")
    fb = user_prefs.get("allow_quality_fallback")
    _print_prefs_block("[当前的运行时配置]", q, fb, auto, hw)


def _configure_download_prefs():
    """下载模式输入 c(onfig) 时，修改运行时配置（默认画质 + 降级开关 + 自动转写开关 + 热词）并即时存盘。"""
    global user_prefs
    print()
    print("[配置模式]")

    # 1/4 默认画质（直接回车保持当前值；输入 0 清除为「每次询问」；可下载的最高画质=最高可用画质）
    cur_q = user_prefs.get("default_quality")
    q_key = {"best": "1", "1080p": "2", "720p": "3", "480p": "4", "360p": "5"}.get(cur_q) or "0"
    new_q = _prompt_choice(
        "[1/4] 下载时的指定画质：",
        [("1", "可下载的最高画质"), ("2", "1080p"), ("3", "720p"), ("4", "480p"), ("5", "360p"), ("0", "每次询问")],
        mark_key=q_key, mark_label="(当前)", indent=2,
    )
    new_q = {"1": "best", "2": "1080p", "3": "720p", "4": "480p", "5": "360p", "0": None}[new_q]

    # 2/4 目标画质不可用时是否接受降级下载（直接回车保持当前值；输入 0 清除为「每次询问」）
    # 若 [1/4] 已选「可下载的最高画质」(best)，则不存在降级情形，跳过本项询问并明确告知
    if new_q == "best":
        new_fb = False
        print("  （因 [1/4] 已选「可下载的最高画质」，不存在画质不达标需降级的情况，已自动跳过 [2/4]「是否接受降画质」设置）")
    else:
        cur_fb = user_prefs.get("allow_quality_fallback")
        fb_cur = "1" if cur_fb is True else ("2" if cur_fb is False else "0")
        fb_ans = _prompt_choice(
            "[2/4] 下载源不提供指定画质时是否接受降画质下载：",
            [("1", "降质下载"), ("2", "拒不下载"), ("0", "每次询问")],
            mark_key=fb_cur, indent=2,
        )
        new_fb = {"1": True, "2": False, "0": None}[fb_ans]

    # 3/4 自动转写开关（直接回车保持当前值；输入 0 清除为「每次询问」）
    cur_auto = user_prefs.get("auto_transcribe_after_download")
    auto_cur = "1" if cur_auto is True else ("2" if cur_auto is False else "0")
    auto_ans = _prompt_choice(
        "[3/4] 下载后是否直接自动转写：",
        [("1", "是"), ("2", "否"), ("0", "每次询问")],
        mark_key=auto_cur, indent=2,
    )
    new_auto = {"1": True, "2": False, "0": None}[auto_ans]

    # 4/4 热词（直接回车=保持当前对应项；1=设热词并追问具体表；2=不设空表；0=每次询问）
    # 用 _prompt_choice_ex 拿到 is_enter：当前已是 1) 设热词时，
    # 回车 -> 原样保留现有热词表（不追问）；显式输入 1 -> 进入追问以修改热词。
    cur_hw = user_prefs.get("hot_words")
    hw_cur = "0" if cur_hw is None else ("2" if cur_hw == [] else "1")
    # 回车提示随当前项变化：当前是 1/2 时保持当前热词表，当前是 0(每次询问) 时保持「每次询问」
    hw_enter_hint = "（或直接回车以保持当前热词表）" if hw_cur in ("1", "2") else "（或直接回车以保持当前的「每次询问」）"
    hw_ans, hw_enter = _prompt_choice_ex(
        "[4/4] 转写的时候，无论是否为自动转写，程序自动代入的热词：",
        [("1", "设热词，程序后面会问具体热词表"), ("2", "不设热词，程序直接代入空热词表"), ("0", "每次询问")],
        mark_key=hw_cur, indent=2, enter_hint=hw_enter_hint,
    )
    if hw_ans == "1" and not hw_enter:
        # 复用统一热词输入函数：内置「长度 2~16、最多 200 个、仅空格分隔」规则提示与解析校验，
        # 与初始化 / 下载后自动转写 / 手动转写路径保持一致
        new_hw = _input_hotwords(prompt="    请输入热词", persistent=True)
    elif hw_ans == "1":
        # 当前已是 1) 设热词且用户直接回车保持当前：原样保留现有热词表，不再追问
        new_hw = cur_hw
    elif hw_ans == "2":
        new_hw = []
    else:  # "0" 每次询问
        new_hw = None

    # 写回并即时更新全局偏好
    prefs = {
        "default_quality": new_q,
        "allow_quality_fallback": new_fb,
        "auto_transcribe_after_download": new_auto,
        "hot_words": new_hw,
    }
    _save_prefs(prefs)
    user_prefs = prefs
    _print_prefs_block(f"\n运行时配置已更新如下：", new_q, new_fb, new_auto, new_hw)


# ============================================================
# 进度回调
# ============================================================


class ProgressTracker:
    def __init__(self, name: str):
        self.name = name
        self.start = time.time()
        self.last_dot = 0
        self._uploaded = False   # 是否已切到转写阶段（用于保留"上传中..."行）

    def __call__(self, order_id: str, status: str, elapsed: int):
        if status == "uploading":
            # 上传阶段只打印一次"上传中..."，换行保留，后续不被覆盖
            print(f"  上传中...", flush=True)
        elif status == "polling":
            dot = elapsed // 5
            if not self._uploaded or dot > self.last_dot:
                bar = _progress_bar(elapsed / 300)
                eta = _format_elapsed(elapsed)
                if not self._uploaded:
                    # 上传刚完成：换行保留"上传中..."，在下面另起一行开始转写进度（不带 \r）
                    self._uploaded = True
                    prefix = "  "
                else:
                    # 转写进度更新：用 \r 回到本行行首原地刷新进度条
                    prefix = "\r  "
                self.last_dot = dot
                print(f"{prefix}上传成功，转写中 {bar}  已用 {eta}...", end="", flush=True)

# ============================================================
# 转写流程（CLI 交互版）
# ============================================================


def _print_transcribe_help() -> None:
    """打印转写模式操作说明。"""
    print("[转写模式]")
    print("  可用操作指令:")
    ops = [
        ("1", "转写单个文件（第 1 个）"),
        ("2-4", "转写连续区间（第 2~4 个）"),
        ("2,5", "转写多个离散序号（空格 / 逗号分隔皆可）"),
        ("1,3-5,7", "混合转写（第 1、3、4、5、7 个）"),
        ("a(ll)", "转写全部文件"),
        ("s(earch)", "进入搜索模式"),
        ("c(onfig)", "进入配置模式"),
        ("q(uit)", "退出整个程序"),
    ]
    for _cmd, _desc in ops:
        print(f"    {_cmd:<20} -> {_desc}")
    print()


def _do_transcribe():
    """转写 videos/ 中已下载的视频（CLI 交互版）。

    返回 "search" 表示用户在转写模式中选择 s(earch) 进入搜索模式；
    其余情况（配置缺失 / 无视频 / 正常结束）返回 None。
    """
    if not _check_xfyun_config():
        print()
        return

    print()  # 进入转写模式时与上一行输入之间留一个空行，保持与搜索模式一致

    video_folder = DEFAULT_VIDEOS_DIR
    _info("支持的音频后缀: " + "/".join(sorted(AUDIO_EXTS)))
    _info("支持的视频后缀: " + "/".join(sorted(SUPPORTED_EXTS - AUDIO_EXTS)))
    _info(f"正在读取 videos/ 中的音视频文件信息（不读取子目录），{PROBE_HINT}")
    files = format_file_list(video_folder, probe=True)

    if not files:
        _warn("videos/ 中没有音视频文件，请先下载")
        return

    print(format_file_table(files, folder=video_folder, probe=True))
    print()
    # 转写模式主循环：留在转写模式继续接收操作指令（不回主菜单）。
    _print_prefs_status()
    print()
    _print_transcribe_help()

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
            if sel.lower() in ("c", "config"):
                _configure_download_prefs()
                print()
                _print_transcribe_help()
                continue

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
            break  # 列表末尾不额外空行；下方 print() 恰好提供 1 行空行

        # 热词：沿用配置模式的「每次询问 / 设热词 / 不设」三态，而非永远询问
        # 与配置模式 hw_cur 的取值逻辑保持一致：
        #   hot_words 为 None -> 「每次询问」(0)，仍需弹框询问
        #   hot_words 为 []    -> 「不设」(2)，静默用空表
        #   hot_words 非空列表 -> 「设热词」(1)，静默沿用已保存热词表
        hw_cfg = user_prefs.get("hot_words")
        if hw_cfg is None:
            print()
            print("[来自配置模式的每次询问]")
            hot_words = _input_hotwords(prompt="  本次转写时程序代入的热词")  # 「每次询问」：弹框让用户输入
        elif hw_cfg:
            print(f"\n  （沿用配置中已保存的 {len(hw_cfg)} 个热词：{hw_cfg}）")
            hot_words = hw_cfg                     # 「设热词」：静默沿用，不再询问
        else:
            print("\n  （沿用配置：本次不代入热词）")
            hot_words = []                         # 「不设」：静默用空表，不再询问

        # 执行前再次校验凭证：避免选完文件、输完热词后才发现凭证失效，白跑一遍
        if not _check_xfyun_config():
            print()
            return

        # 执行转写（逐个文件调用 _transcribe_one，与下载后自动转写共用同一逻辑）
        print("\n开始转写音视频...")
        results = []
        audio_dir = DEFAULT_AUDIO_DIR
        for i, f in enumerate(selected, 1):
            results.append(_transcribe_one(f["path"], hot_words, i, len(selected), audio_dir))

        # 转写完成：提示保存位置，随后外层 while 循环继续，重新显示操作指令（不回主菜单）
        print()  # 与上面每个文件的结果块之间留一个空行
        _print_transcribe_summary(results)
        print()  # 空一行，再回到"请输入操作指令>"


def _transcribe_one(video_path, hot_words, index, total, audio_dir):
    """转写单个文件，打印进度与结果；返回结果 dict（含 success / name / error）。

    供手动转写（_do_transcribe）与下载后自动转写（_auto_transcribe_files）共用。
    """
    name = os.path.basename(video_path)
    print(f"\n{'=' * 60}")
    print(f"  [{index}/{total}] 处理: {name}")
    print("=" * 60)

    # 音频提取
    try:
        audio_path, src = resolve_audio_path(video_path, audio_dir)
    except KeyboardInterrupt:
        # 音频提取（ffmpeg）过程中按 Ctrl+C，转成 QuitProgram 优雅退出，避免抛 traceback
        print()
        raise QuitProgram("检测到用户输入了 Ctrl+C，系统自动退出。再见！")
    except Exception as e:
        _error(f"音频提取失败: {e}")
        return {"name": name, "success": False, "error": str(e)}

    if src == "original":
        _info(f"音频文件，直接使用: {video_path}")
    elif src == "cached":
        _success(f"已有音频（下载时提取）: {audio_path}")
    else:
        size_mb = os.path.getsize(audio_path) / 1024**2
        _success(f"音频提取完成: {audio_path} ({size_mb:.1f} MB)")

    # 上传 + 转写
    tracker = ProgressTracker(name)
    try:
        hw_desc = f"热词表非空，值为{hot_words}" if hot_words else "热词表为空表"
        _info(f"上传并转写（{hw_desc}）...")
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

        return {
            "name": name,
            "success": True,
            "duration": res["duration"],
            "chars": char_count,
            "elapsed": res["elapsed"],
        }

    except KeyboardInterrupt:
        # 上传/转写过程中按 Ctrl+C：KeyboardInterrupt 不是 Exception 子类，
        # 不会被下面的 except Exception 捕获，需转成 QuitProgram 优雅退出
        print()
        raise QuitProgram("检测到用户输入了 Ctrl+C，系统自动退出。再见！")
    except TimeoutError as e:
        print()
        _error(f"转写超时: {e}")
        return {"name": name, "success": False, "error": str(e)}
    except Exception as e:
        print()
        _error(f"转写失败: {e}")
        return {"name": name, "success": False, "error": str(e)}


def _auto_transcribe_files(paths, hot_words):
    """下载完成后自动转写（不交互选文件、不询问热词）；热词来自用户配置。

    凭证缺失时跳过并提示，不抛错。结果保存到 DEFAULT_OUTPUT_DIR。
    """
    if not _check_xfyun_config():
        return
    if not paths:
        return
    print()
    _info(f"自动转写本次下载的 {len(paths)} 个视频...")
    audio_dir = DEFAULT_AUDIO_DIR
    results = []
    for i, p in enumerate(paths, 1):
        results.append(_transcribe_one(p, hot_words, i, len(paths), audio_dir))
    print()  # 与最后一个文件的转写提示之间空一行，不要紧连
    _print_transcribe_summary(results)


# ============================================================
# 搜索 + 下载模式
# ============================================================


def _print_download_help(page_start: int, count: int, show_prefs: bool = True) -> None:
    """打印下载模式操作说明；示例序号按当前页实际全局范围自适应。

    show_prefs=True（默认）时先打印 [当前的运行时配置]；配置完成后返回时不重复打印。
    """
    end = page_start + max(count - 1, 0)

    def rel(k: int) -> int:
        return min(page_start + k - 1, end)

    one = rel(1)
    three = rel(3)
    four = rel(4)
    five = rel(5)
    seven = rel(7)
    if show_prefs:
        print()
        _print_prefs_status()
    print()
    print("[下载模式]")
    print("  可用操作指令:")
    ops = [
        (str(one), f"下载单个视频及其音频（第 {one} 个）"),
        (f"{rel(2)}-{rel(4)}", f"下载连续区间（第 {rel(2)}~{rel(4)} 个）"),
        (f"{one} {three} {rel(6)} {rel(4)}", "下载多个离散序号（空格 / 逗号分隔皆可）"),
        (f"{one},{three}-{five},{seven}", f"混合下载（第 {one}、{three}、{four}、{five}、{seven} 个）"),
        ("a(ll)", "下载本页全部视频"),
        ("n(ext)", "下一页搜索结果"),
        ("p(revious)", "上一页搜索结果"),
        ("t(ranscribe)", "进入转写模式"),
        ("c(onfig)", "进入配置模式"),
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

            # 修改下载配置（c -> config）
            if cmd.lower() in ("c", "config"):
                _configure_download_prefs()
                _print_download_help(page_start, len(last_videos), show_prefs=False)
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


def _print_main_menu() -> None:
    """打印主模式菜单（进入程序时、以及从配置模式返回后重印，与各子模式行为一致）。"""
    print("[可进入的模式]")
    print("  s(earch)     -> 搜索模式")
    print("  t(ranscribe) -> 转写模式")
    print("  c(onfig)     -> 配置模式")
    print("  q(uit)       -> 退出整个程序")
    print()


def main():
    # 第一步：检查环境依赖
    print()
    deps_data = check_dependencies()
    print(format_deps_text(deps_data))

    # 缺少必要依赖（yt-dlp + ffmpeg/ffprobe）时，禁止进入程序
    if not deps_data.get("all_ready"):
        sys.exit(1)

    print()

    try:
        # 首次运行设置（或读取已保存偏好）：询问是否默认下载后自动转写及热词
        global user_prefs
        user_prefs, is_first_run = _ensure_user_prefs()
        if not is_first_run:
            # 偏好来自已保存的配置文件时，进入程序后展示一次 [当前的运行时配置]；
            # 首次初始化已由 _ensure_user_prefs 打印「配置初始化完成，如下：」，不再重复输出。
            _print_prefs_status()
        print()

        # 进入程序后先打印一次模式菜单；之后从配置模式返回时也重印，保持与各子模式一致
        _print_main_menu()
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
            elif choice in ("c", "config"):
                _configure_download_prefs()
                print()
                _print_main_menu()
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
