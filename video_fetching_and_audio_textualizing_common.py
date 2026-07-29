# -*- coding: utf-8 -*-
"""
B站视频 + 语音转写 流水线 公共模块
===================================
被 CLI 和 MCP Server 共同引用，包含所有跨脚本复用的核心逻辑：

  搜索: B站 WBI 签名 / curl 请求 / 双搜索接口 / 结果格式化
  下载: yt-dlp 下载 / 画质降级 / 分P探测 / 音频自动提取
  转写: 讯飞 ASR 凭证 / 上传 / 轮询 / txt+srt+json 三格式存储
  辅助: 依赖检查 / 文件扫描 / 热词校验 / 路径工具

本模块为 CLI 与 MCP 共用的核心逻辑集合，不含命令行参数解析与 MCP 协议收发。
进度/结果类输出统一写到 sys.stderr（如分P下载结果打印 _print_part_result），不占用 sys.stdout；
MCP 另在 tools/call 派发处用 redirect_stdout 将业务逻辑对 stdout 的写入临时重定向到黑洞，
作为兜底防御，确保 JSON-RPC 通道不被任何意外输出污染。
"""

import sys
import json
import re
import os
import time
import hashlib
import hmac
import base64
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import unicodedata

# ============================================================
# 路径常量（基于本脚本所在目录）
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_VIDEOS_DIR = os.path.join(SCRIPT_DIR, "videos")
DEFAULT_AUDIO_DIR = os.path.join(DEFAULT_VIDEOS_DIR, "audios")
DEFAULT_OUTPUT_DIR = os.path.join(DEFAULT_VIDEOS_DIR, "transcripts")
os.makedirs(DEFAULT_VIDEOS_DIR, exist_ok=True)
os.makedirs(DEFAULT_AUDIO_DIR, exist_ok=True)
os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)

# ============================================================
# 画质降级常量
# ============================================================
QUALITY_VALID = ("best", "1080p", "720p", "480p", "360p")

QUALITY_HEIGHT = {"1080p": 1080, "720p": 720, "480p": 480, "360p": 360}

VIDEO_EXTS = ('.mp4', '.mkv', '.webm', '.flv', '.mov', '.m4v')
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg"}
SUPPORTED_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".m4v",
                  ".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg"}

# 讯飞 ASR 固定语言
ASR_LANGUAGE = "cn"

# B站 WBI 签名 mixinKey 混淆表（当前版本）
WBI_MIXIN_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 52, 44, 34,
]

# ============================================================
# 通用工具函数
# ============================================================


def _disp_width(s) -> int:
    """按终端显示宽度计算字符串宽度（东亚全角/宽字符按 2 计）。"""
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
    out, w = "", 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > max_width - 2:
            break
        out += ch
        w += cw
    return out + ".."


def _format_bytes(size):
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.2f} GB"
    elif size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.2f} MB"
    elif size >= 1024:
        return f"{size / 1024:.2f} KB"
    return f"{size} B"


def _format_number(num):
    if isinstance(num, int):
        if num >= 100000000:
            return f"{num / 100000000:.1f}亿"
        elif num >= 10000:
            return f"{num / 10000:.1f}万"
        elif num == 0:
            return "0"
        return str(num)
    return str(num) if num else "0"


def _format_timestamp(ts):
    if isinstance(ts, int) and ts > 0:
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except Exception:
            return str(ts)
    return "-"


def _clean_html(text):
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    return text.strip()


def _get_video_height(filepath):
    """用 ffprobe 获取视频文件的实际高度，失败返回 None"""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=height", "-of", "csv=p=0", filepath]
    try:
        proc = _run_capture(cmd)
        if proc.returncode == 0 and proc.stdout.strip().isdigit():
            return int(proc.stdout.strip())
    except Exception:
        pass
    return None


def _which(cmd):
    """查找命令的完整路径"""
    try:
        import shutil
        return shutil.which(cmd) or "未查"
    except Exception:
        return "未查"


def _run_capture(cmd, timeout=30):
    """统一 subprocess 捕获式调用：utf-8 + replace 容错。
    超时/命令未找到等异常原样抛出，由调用方兜底。"""
    return subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout
    )


def _stream_output(proc, echo=False):
    """流式读取 subprocess 输出（优先 UTF-8，回退到 GBK/系统编码）。
    当 echo=True 时，同时将进度行输出到 stderr（仅打印 yt-dlp 进度行，跳过纯空行）。"""
    output_lines = []
    encodings = ["utf-8", "gbk", "gb2312", "cp936"]
    try:
        while True:
            chunk = proc.stdout.readline()
            if not chunk:
                break
            decoded = False
            for enc in encodings:
                try:
                    line = chunk.decode(enc, errors="strict").rstrip()
                    decoded = True
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            if not decoded:
                line = chunk.decode("utf-8", errors="replace").rstrip()
            output_lines.append(line)
            if echo and line.strip():
                try:
                    sys.stderr.buffer.write((line + "\n").encode("utf-8"))
                    sys.stderr.buffer.flush()
                except Exception:
                    pass
    except Exception:
        pass
    return "\n".join(output_lines)


def _get_ytdlp_cmd():
    """获取 yt-dlp 调用方式：优先 yt-dlp，回退 python -m yt_dlp"""
    try:
        import shutil
        if shutil.which("yt-dlp"):
            return ["yt-dlp"]
    except Exception:
        pass
    return [sys.executable, "-m", "yt_dlp"]

# ============================================================
# 讯飞 ASR 凭证配置
# ============================================================


class XFYunConfig:
    """凭证管理，从环境变量或 config.json 读取"""
    instance = None

    def __new__(cls):
        if cls.instance is None:
            cls.instance = super().__new__(cls)
            cls.instance.APP_ID = ""
            cls.instance.SECRET_KEY = ""
        return cls.instance

    def load(self, config_path: str = None):
        self.APP_ID = os.getenv("XF_APP_ID", "").strip()
        self.SECRET_KEY = os.getenv("XF_SECRET_KEY", "").strip()

        if not self.APP_ID or not self.SECRET_KEY:
            if config_path is None:
                config_path = os.path.join(SCRIPT_DIR, "config.json")
            if os.path.exists(config_path):
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    self.APP_ID = cfg.get("app_id", self.APP_ID)
                    self.SECRET_KEY = cfg.get("secret_key", self.SECRET_KEY)
                except Exception:
                    pass

        self.UPLOAD_URL = "https://raasr.xfyun.cn/v2/api/upload"
        self.RESULT_URL = "https://raasr.xfyun.cn/v2/api/getResult"

    @property
    def is_ready(self) -> bool:
        return bool(self.APP_ID and self.SECRET_KEY)


def ensure_xfyun_config():
    """统一讯飞凭证检查，CLI/MCP 共用。
    返回 (is_ready: bool, error_message: str)。"""
    cfg = XFYunConfig()
    cfg.load()
    if not cfg.is_ready:
        return False, (
            "讯飞凭证未配置，请设置环境变量 XF_APP_ID / XF_SECRET_KEY "
            "或创建 config.json"
        )
    return True, ""

# ============================================================
# 讯飞 ASR 签名生成
# ============================================================


def _build_signa(app_id: str, secret_key: str) -> tuple:
    """生成讯飞 API 签名: Base64(HmacSHA1(MD5(appid+ts), secret_key))"""
    ts = str(int(time.time()))
    base_string = f"{app_id}{ts}"
    md5_hex = hashlib.md5(base_string.encode("utf-8")).hexdigest()
    sign_bytes = hmac.new(
        secret_key.encode("utf-8"),
        md5_hex.encode("utf-8"),
        hashlib.sha1
    ).digest()
    return base64.b64encode(sign_bytes).decode("utf-8"), ts

# ============================================================
# 音频处理（ffprobe 时长 / ffmpeg 提取）
# ============================================================


def _get_audio_duration(file_path: str) -> int:
    """用 ffprobe 获取音频/视频时长（秒），失败则按文件大小估算"""
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of",
            "default=noprint_wrappers=1:nokey=1", file_path
        ]
        result = _run_capture(cmd)
        if result.returncode == 0 and result.stdout.strip():
            return int(float(result.stdout.strip()))
    except Exception:
        pass
    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    return max(10, int(size_mb * 64))


def _extract_audio(video_path: str, output_path: str = None) -> str:
    """用 ffmpeg 提取视频音频为 m4a（优先流拷贝，失败重编码）"""
    if output_path is None:
        stem = os.path.splitext(os.path.basename(video_path))[0]
        os.makedirs(DEFAULT_AUDIO_DIR, exist_ok=True)
        output_path = os.path.join(DEFAULT_AUDIO_DIR, f"{stem}.m4a")

    if os.path.exists(output_path):
        return output_path

    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-c:a", "copy",
        "-f", "ipod", output_path
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=600
        )
        if proc.returncode != 0:
            cmd_reencode = [
                "ffmpeg", "-y", "-i", video_path,
                "-vn", "-c:a", "aac", "-b:a", "128k",
                "-f", "ipod", output_path
            ]
            proc = subprocess.run(
                cmd_reencode, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=600
            )
        if proc.returncode == 0 and os.path.exists(output_path):
            return output_path
        raise Exception(proc.stderr[-300:])
    except Exception as e:
        raise RuntimeError(f"音频提取失败: {e}")


def resolve_audio_path(video_path: str, audio_dir: str = None):
    """给定视频/音频文件路径，返回 (音频路径, 来源)。

    来源为以下之一:
      'original'  —— 文件本身就是音频，直接返回原路径；
      'cached'    —— 复用下载阶段已提取到 <audio_dir>/<stem>.m4a 的音频，避免重复 ffmpeg 提取；
      'extracted' —— 本次用 ffmpeg 新提取。
    """
    ext = os.path.splitext(video_path)[1].lower()
    if ext in AUDIO_EXTS:
        return video_path, "original"

    if audio_dir is None:
        audio_dir = DEFAULT_AUDIO_DIR
    stem = os.path.splitext(os.path.basename(video_path))[0]
    pre_audio = os.path.join(audio_dir, f"{stem}.m4a")
    if os.path.exists(pre_audio):
        return pre_audio, "cached"
    return _extract_audio(video_path), "extracted"

# ============================================================
# 讯飞 ASR 转写客户端
# ============================================================


def _iter_lattice(order_result_json):
    """解析讯飞 orderResult，返回 [(bg_ms, ed_ms, [词...]), ...]。
    顶层 JSON 解析失败返回 None；单条 lattice 解析失败则跳过。"""
    try:
        data = json.loads(order_result_json)
    except Exception:
        return None
    out = []
    for item in data.get("lattice", []):
        try:
            best = json.loads(item["json_1best"])
            st = best.get("st", {})
            bg = int(st.get("bg", 0))
            ed = int(st.get("ed", 0))
            words = []
            for r in st.get("rt", []):
                for w in r.get("ws", []):
                    for c in w.get("cw", []):
                        words.append(c.get("w", ""))
            out.append((bg, ed, words))
        except Exception:
            continue
    return out


class XFYunASR:
    """讯飞录音文件转写客户端"""

    def __init__(self, app_id: str = None, secret_key: str = None):
        cfg = XFYunConfig()
        cfg.load()
        self.app_id = app_id or cfg.APP_ID
        self.secret_key = secret_key or cfg.SECRET_KEY
        if not self.app_id or not self.secret_key:
            raise ValueError(
                "缺少讯飞凭证！请设置环境变量 XF_APP_ID / XF_SECRET_KEY "
                "或创建 config.json"
            )

    def sign(self) -> tuple:
        return _build_signa(self.app_id, self.secret_key)

    def upload_request(self, file_path, hot_words=None, pd=None):
        signa, ts = self.sign()
        file_size = os.path.getsize(file_path)
        duration = _get_audio_duration(file_path)
        file_name = os.path.basename(file_path)

        params = {
            "appId": self.app_id,
            "ts": ts,
            "signa": signa,
            "fileName": file_name,
            "fileSize": str(file_size),
            "duration": str(duration * 1000),
            "language": ASR_LANGUAGE,
        }
        if hot_words:
            valid = [w for w in hot_words if 2 <= len(w) <= 16]
            if len(valid) > 200:
                valid = valid[:200]
            params["hotWord"] = "|".join(valid)
        if pd:
            params["pd"] = pd

        query = urllib.parse.urlencode(params)
        with open(file_path, "rb") as f:
            audio_bytes = f.read()

        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(audio_bytes)),
        }
        req = urllib.request.Request(
            XFYunConfig().UPLOAD_URL + "?" + query,
            data=audio_bytes, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise RuntimeError(f"上传请求失败: {e}")

        if result.get("code") != "000000":
            raise RuntimeError(
                f"上传失败 [{result.get('code')}]: {result.get('descInfo')}"
            )
        return {
            "order_id": result["content"]["orderId"],
            "duration": duration,
        }

    def query_request(self, order_id, result_type="transfer"):
        signa, ts = self.sign()
        params = {
            "appId": self.app_id,
            "ts": ts,
            "signa": signa,
            "orderId": order_id,
            "resultType": result_type,
        }
        query = urllib.parse.urlencode(params)
        req = urllib.request.Request(
            XFYunConfig().RESULT_URL + "?" + query, method="GET"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise RuntimeError(f"查询请求失败: {e}")

    def transcribe(
        self, file_path, hot_words=None,
        pd="game", poll_interval=5, max_wait=1800, progress_callback=None
    ):
        if progress_callback:
            progress_callback("", "uploading", 0)
        upload_result = self.upload_request(
            file_path, hot_words=hot_words, pd=pd
        )
        order_id = upload_result["order_id"]
        duration = upload_result["duration"]

        start_time = time.time()
        while True:
            elapsed = int(time.time() - start_time)
            if elapsed > max_wait:
                raise TimeoutError(
                    f"转写超时（>{max_wait}秒），视频可能过长或服务繁忙"
                )
            if progress_callback:
                progress_callback(order_id, "polling", elapsed)

            resp = self.query_request(order_id)
            code = resp.get("code", "")
            if code != "000000":
                raise RuntimeError(f"查询失败 [{code}]: {resp.get('descInfo')}")

            info = resp["content"]["orderInfo"]
            status = info["status"]

            if status == 4:
                raw_result = resp["content"].get("orderResult", "")
                return {
                    "success": True,
                    "order_id": order_id,
                    "plain_text": self.extract_text(raw_result),
                    "srt": self.extract_srt(raw_result),
                    "segments": self.extract_segments(raw_result),
                    "duration": info.get("realDuration", duration * 1000) // 1000,
                    "raw": resp,
                }
            elif status == -1:
                fail_type = info.get("failType", -1)
                raise RuntimeError(f"转写失败，failType={fail_type}")

            time.sleep(poll_interval)

    @staticmethod
    def extract_text(order_result_json):
        parsed = _iter_lattice(order_result_json)
        if parsed is None:
            return order_result_json
        return "".join("".join(words) for _, _, words in parsed)

    @staticmethod
    def extract_srt(order_result_json):
        parsed = _iter_lattice(order_result_json)
        if parsed is None:
            return ""

        def fmt(ms):
            h = ms // 3600000
            m = (ms % 3600000) // 60000
            s = (ms % 60000) // 1000
            ms_ = ms % 1000
            return f"{h:02d}:{m:02d}:{s:02d},{ms_:03d}"

        subs, idx = [], 1
        for bg, ed, words in parsed:
            text = "".join(words).strip()
            if not text:
                continue
            subs.append(f"{idx}\n{fmt(bg)} --> {fmt(ed)}\n{text}\n")
            idx += 1
        return "\n".join(subs)

    @staticmethod
    def extract_segments(order_result_json):
        parsed = _iter_lattice(order_result_json)
        if parsed is None:
            return []
        segments = []
        for bg, ed, words in parsed:
            text = "".join(words).strip()
            if text:
                segments.append({"text": text, "start_ms": bg, "end_ms": ed})
        return segments

# ============================================================
# 热词校验（统一工具）
# ============================================================


def validate_hotwords(raw_words: list):
    """校验热词列表：每个词 2-16 字符，最多 200 个。
    返回 (valid, invalid) 两个列表。"""
    if not raw_words:
        return [], []
    valid = [w for w in raw_words if 2 <= len(w) <= 16]
    invalid = [w for w in raw_words if not (2 <= len(w) <= 16)]
    if len(valid) > 200:
        valid = valid[:200]
    return valid, invalid


def parse_hotwords_string(hot_raw: str):
    """统一热词解析：仅以空白（空格、制表符等）分隔 → 清洗 → 校验。
    逗号/分号（中英文均可）、顿号等标点一律视为热词的一部分，不作为分隔符。
    长度限 2~16 字符、最多 200 个（由 validate_hotwords 保证）。
    返回 (valid, invalid) 两个列表。
    CLI 和 MCP 共用此函数，避免各自实现分隔、校验逻辑。"""
    if not hot_raw or not hot_raw.strip():
        return [], []
    all_words = [w.strip() for w in hot_raw.split() if w.strip()]
    return validate_hotwords(all_words)

# ============================================================
# 文件扫描工具
# ============================================================


def _format_clock(seconds) -> str:
    """把秒数格式化为 时:分:秒 / 分:秒（如 1:02:03、12:34）。无效返回 '-'。"""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "-"
    if seconds < 0:
        return "-"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _probe_media(file_path: str, want_height: bool = True):
    """一次 ffprobe 同时获取时长(秒, int|None) 与视频高度(int|None)。

    want_height=False 时（纯音频）跳过高度提取。ffprobe 失败均返回 None。
    """
    duration, height = None, None
    cmd = ["ffprobe", "-v", "error",
           "-show_entries", "format=duration:stream=height,codec_type",
           "-of", "json", file_path]
    try:
        proc = _run_capture(cmd)
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            d = data.get("format", {}).get("duration")
            if d:
                try:
                    duration = int(float(d))
                except (TypeError, ValueError):
                    duration = None
            if want_height:
                for s in data.get("streams", []):
                    if s.get("codec_type") == "video" and s.get("height"):
                        height = int(s["height"])
                        break
    except Exception:
        pass
    return duration, height


# ffprobe 探测提示：必须在探测进行「前」给出，避免探测完成后才提示“稍慢”（逻辑倒置）
PROBE_HINT = "含 ffprobe 探测时长/画质，文件较多时可能稍慢，请稍候…"


def format_file_list(folder: str, probe: bool = False) -> list:
    """扫描文件夹，返回音视频文件列表（按修改时间倒序）。

    probe=True 时额外用 ffprobe 探测每个文件的时长与视频画质（较慢），
    为每项补充 duration / duration_str / height / quality_str 字段；
    无论是否 probe，均包含 ext / is_audio / type_str（如 '.mp4 视频'）。
    """
    if not os.path.exists(folder):
        return []
    exts = SUPPORTED_EXTS
    files = []
    for f in os.listdir(folder):
        fp = os.path.join(folder, f)
        if os.path.isfile(fp) and os.path.splitext(f)[1].lower() in exts:
            size = os.path.getsize(fp)
            mtime = os.path.getmtime(fp)
            ext = os.path.splitext(f)[1].lower()
            is_audio = ext in AUDIO_EXTS
            item = {
                "name": f,
                "path": fp,
                "size": size,
                "size_str": _format_bytes(size),
                "mtime": mtime,
                "mtime_str": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
                "ext": ext,
                "is_audio": is_audio,
                "type_str": f"{ext} 音频" if is_audio else f"{ext} 视频",
            }
            if probe:
                duration, height = _probe_media(fp, want_height=not is_audio)
                item["duration"] = duration
                item["duration_str"] = _format_clock(duration) if duration else "-"
                item["height"] = height
                if is_audio:
                    item["quality_str"] = "-"
                elif height:
                    item["quality_str"] = f"{height}p"
                else:
                    item["quality_str"] = "未知"
            files.append(item)
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return files


def is_transcribed(name, output_dir=DEFAULT_OUTPUT_DIR) -> bool:
    """判断某视频是否已转写：要求纯文本 / 字幕 / 分段 JSON 三份产物全部存在。

    参数 name 可为文件名或完整路径，内部取 basename 并去掉扩展名得到 stem。
    产物文件名沿用 _save_transcript：<stem>_text.txt / <stem>.srt / <stem>_segments.json。
    """
    stem = os.path.splitext(os.path.basename(name))[0]
    return (
        os.path.exists(os.path.join(output_dir, f"{stem}_text.txt"))
        and os.path.exists(os.path.join(output_dir, f"{stem}.srt"))
        and os.path.exists(os.path.join(output_dir, f"{stem}_segments.json"))
    )


def parse_selection(sel: str, max_idx: int, page_start: int = 1) -> list:
    """解析用户选择，返回页面内 1-based 序号列表（已扣除 page_start 偏移）。

    支持格式（数字之间可用空格或逗号分隔，二者可混用）:
      'all' / 'a'        -> 该页全部
      '1'                -> 单个
      '1 3 6 4'          -> 多个离散（空格分隔）
      '1,3,5'            -> 多个离散（逗号分隔）
      '1-5'              -> 连续区间（支持倒序自动纠正）
      '1,3-5,7'          -> 混合
    越界 / 重复项会自动过滤，非法输入被忽略。

    page_start: 该页全局起始序号（搜索翻页后可能不为 1），用于把全局序号
                换算回页内序号；转写模式固定为 1。
    """
    sel = (sel or "").strip().lower()
    if sel in ("all", "a"):
        return list(range(1, max_idx + 1))

    page_end = page_start + max_idx - 1
    raw = []
    for m in re.finditer(r'(\d+)\s*-\s*(\d+)|(\d+)', sel):
        if m.group(1) is not None:
            start, end = int(m.group(1)), int(m.group(2))
            if start > end:
                start, end = end, start
            raw.extend(range(start, end + 1))
        else:
            raw.append(int(m.group(3)))

    seen = set()
    result = []
    for n in raw:
        if page_start <= n <= page_end and n not in seen:
            seen.add(n)
            result.append(n - page_start + 1)
    return result

# ============================================================
# 转写结果保存
# ============================================================


def _save_transcript(video_name: str, result: dict, output_dir: str) -> dict:
    """保存转写结果（纯文本 + SRT + 分段JSON），返回各文件路径"""
    os.makedirs(output_dir, exist_ok=True)
    stem = re.sub(r'[\\/:*?"<>|]', '_', os.path.splitext(video_name)[0])
    saved = {}

    # 纯文本
    txt_path = os.path.join(output_dir, f"{stem}_text.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"# {video_name}\n")
        f.write(f"# 转写时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# 时长: {result.get('duration', '?')}秒\n")
        f.write(f"# 语速: ~{len(result.get('plain_text', '')) / max(result.get('duration', 1), 1):.0f}字/秒\n")
        f.write("\n" + "=" * 40 + "\n\n")
        f.write(result.get("plain_text", ""))
    saved["text_path"] = txt_path

    # SRT
    srt_path = os.path.join(output_dir, f"{stem}.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(result.get("srt", ""))
    saved["srt_path"] = srt_path

    # JSON 分段
    json_path = os.path.join(output_dir, f"{stem}_segments.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "video_name": video_name,
            "transcribe_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration": result.get("duration"),
            "segments": result.get("segments", []),
            "plain_text": result.get("plain_text", ""),
        }, f, ensure_ascii=False, indent=2)
    saved["segments_path"] = json_path

    return saved

# ============================================================
# B站 视频搜索
# ============================================================
# 注意：因本机 Python OpenSSL 与 B站 CDN 的 TLS 握手超时，
# 搜索/分P 等 B站 Web API 请求改用 curl.exe（Windows Schannel）通过 subprocess 执行；
# 视频下载与画质探测仍由 yt-dlp 完成（不走 curl）。


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# curl cookie jar 路径（持久化，跨调用复用 cookie）
CURL_COOKIE_JAR = os.path.join(SCRIPT_DIR, ".bilibili_cookies.txt")


def _curl_get(url, headers=None, timeout=15, include_cookies=True):
    """使用 curl.exe 发起 GET 请求，返回 (响应文本, exit_code)。
    自动带上 cookie jar 实现跨请求登录态保持。"""
    cmd = ["curl.exe", "-s", "--connect-timeout", "5", "--max-time", str(timeout)]
    if include_cookies:
        cmd += ["--cookie", CURL_COOKIE_JAR, "--cookie-jar", CURL_COOKIE_JAR]
    if headers:
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        out = proc.stdout.decode("utf-8", errors="replace")
        return out, proc.returncode
    except subprocess.TimeoutExpired:
        return "", -1
    except FileNotFoundError:
        return "", -2
    except Exception as e:
        return str(e), -3


def _curl_get_json(url, headers=None, timeout=15, include_cookies=True):
    """使用 curl.exe 获取并解析 JSON 响应。"""
    text, rc = _curl_get(url, headers, timeout, include_cookies)
    if rc == -2:
        return None, "curl.exe 未找到，请确认 curl 已安装"
    if rc != 0 or not text.strip():
        return None, f"curl 请求失败 (rc={rc}): {text[-200:]}"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as e:
        return None, f"JSON 解析失败: {e}"


# WBI state: mixin key 缓存
wbi_state = {"mixin": None, "ts": 0}


def _get_wbi():
    """获取 WBI mixin key；失败返回 None。"""
    st = wbi_state
    if st["mixin"] and (time.time() - st["ts"] < 3600):
        return st["mixin"]
    try:
        # 1) 访问主页获取 cookie（buvid3 等反爬必需）
        _curl_get("https://www.bilibili.com",
                  headers={"User-Agent": UA, "Referer": "https://www.bilibili.com"},
                  timeout=10, include_cookies=True)
        # 2) nav 接口拿 WBI 密钥
        nav_headers = {
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
        }
        data, err = _curl_get_json("https://api.bilibili.com/x/web-interface/nav",
                                   headers=nav_headers, timeout=10, include_cookies=True)
        if err or data is None:
            return None
        wbi = data.get("data", {}).get("wbi_img", {})
        if not wbi:
            return None
        img_key = wbi["img_url"].rsplit("/", 1)[-1].split(".")[0]
        sub_key = wbi["sub_url"].rsplit("/", 1)[-1].split(".")[0]
        mixin = "".join((img_key + sub_key)[t] for t in WBI_MIXIN_ENC_TAB)[:32]
    except Exception:
        return None
    st.update(mixin=mixin, ts=time.time())
    return mixin


def _wbi_sign_params(params: dict, mixin: str) -> dict:
    """通用 WBI 签名：给定业务参数，附加 wts 并计算 w_rid。返回可拼 query 的 dict。"""
    p = dict(params)
    p["wts"] = int(time.time())
    p["w_rid"] = hashlib.md5(
        ("&".join(f"{k}={p[k]}" for k in sorted(p)) + mixin).encode()
    ).hexdigest()
    return p


def _wbi_sign(keyword, page, mixin):
    """生成 search/type 所需的 WBI 签名参数。"""
    return _wbi_sign_params(
        {"search_type": "video", "keyword": keyword, "page": page}, mixin
    )


def _build_videos(video_results, page_size):
    """把 B站 视频条目列表统一成内部 video dict（两种搜索接口共用）。"""
    videos = []
    for item in video_results[:page_size]:
        bvid = item.get("bvid", "")
        # 跳过无 BV 号的数据异常条目（如直播）
        if not bvid:
            continue
        duration_str = item.get("duration", "")
        url_v = f"https://www.bilibili.com/video/{bvid}"
        videos.append({
            "bvid": bvid,
            "aid": item.get("aid", 0),
            "title": _clean_html(item.get("title", "")),
            "author": item.get("author", ""),
            "typename": item.get("typename", ""),
            "pubdate": _format_timestamp(item.get("pubdate", 0)),
            "duration": duration_str,
            "play": _format_number(item.get("play", 0)),
            "favorites": _format_number(item.get("favorites", 0)),
            "like": _format_number(item.get("like", 0)),
            "video_review": _format_number(item.get("video_review", 0)),
            "description": _clean_html(item.get("description", ""))[:120],
            "url": url_v,
            "pic": item.get("pic", ""),
            "tag": item.get("tag", ""),
        })
    return videos


def _finalize(total_raw, videos, page, page_size):
    """统一校正 total / page_count（两种搜索接口共用）。
    all/v2 的 numResults 跨类型且常封顶(1000)，search/type 为真实视频数。
    首页返回视频数 < 每页上限时收口为 1 页，避免凭空空页。"""
    if videos and len(videos) < page_size and page == 1:
        total = len(videos)
        page_count = 1
    else:
        total = total_raw
        page_count = max(1, (total + page_size - 1) // page_size)
    return total, page_count


def _search_type(keyword, page=1, page_size=10):
    """search/type?search_type=video（WBI 签名 + buvid cookie）。返回统一格式 dict。"""
    mixin = _get_wbi()
    if not mixin:
        return {"success": False, "error": "WBI 初始化失败（无法获取签名/ cookie）"}
    params = _wbi_sign(keyword, page, mixin)
    query = urllib.parse.urlencode(params)
    url = f"https://api.bilibili.com/x/web-interface/search/type?{query}"
    headers = {
        "User-Agent": UA,
        "Referer": "https://search.bilibili.com",
        "Accept": "application/json, text/plain, */*",
    }
    data, err = _curl_get_json(url, headers=headers, timeout=15, include_cookies=True)
    if err:
        return {"success": False, "error": f"搜索失败: {err}"}
    if data.get("code") != 0:
        return {"success": False, "error": f"B站 API 错误: {data.get('message', 'unknown')}"}

    dd = data.get("data", {})
    video_results = dd.get("result") or []
    raw_total = dd.get("numResults", 0)
    videos = _build_videos(video_results, page_size)
    total, page_count = _finalize(raw_total, videos, page, page_size)
    return {
        "success": True,
        "keyword": keyword,
        "page": page,
        "total": total,
        "page_count": page_count,
        "videos": videos,
    }


def _search_all_v2(keyword, page=1, page_size=10):
    """回退方案：all/v2 综合搜索。numResults 跨类型且常封顶(1000)，仅作兜底。"""
    encoded_keyword = urllib.parse.quote(keyword)
    url = (
        f"https://api.bilibili.com/x/web-interface/search/all/v2"
        f"?keyword={encoded_keyword}&page={page}"
    )
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com",
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    data, err = _curl_get_json(url, headers=headers, timeout=15, include_cookies=True)
    if err:
        return {"success": False, "error": f"搜索失败: {err}"}
    if data.get("code") != 0:
        return {"success": False, "error": f"B站 API 错误: {data.get('message', 'unknown')}"}

    dd = data.get("data", {})
    video_results = []
    for r in dd.get("result", []):
        if r.get("result_type") == "video":
            video_results = r.get("data", [])
            break

    raw_total = dd.get("numResults", 0)
    videos = _build_videos(video_results, page_size)
    total, page_count = _finalize(raw_total, videos, page, page_size)
    return {
        "success": True,
        "keyword": keyword,
        "page": page,
        "total": total,
        "page_count": page_count,
        "videos": videos,
    }


def search_bilibili(keyword, page=1, page_size=10):
    """搜索 B站 视频：优先 search/type（准确），失败回退 all/v2。"""
    data = _search_type(keyword, page, page_size)
    if data.get("success"):
        return data
    return _search_all_v2(keyword, page, page_size)


def format_search_results(data: dict, show_details: bool = False, page_size: int = 10) -> str:
    """将搜索结果统一格式化为可读字符串（CLI / MCP 共用，避免双份排版逻辑）。

    show_details=True（MCP）时额外输出每条视频的「简介 / 标签」两行，便于 agent
    获取语义线索；False（CLI）时仅展示核心字段，保持终端紧凑。
    其余样式（半角冒号、单空格字段分隔、页眉文案、分隔线）已全部统一，不再区分。

    失败 / 无结果 / 正常 三种情形均在此一处处理，CLI 与 MCP 共用同一套输出。
    """
    if not data.get("success"):
        return f"搜索失败: {data.get('error', '未知错误')}"

    keyword = data.get("keyword", "")
    page = data.get("page", 1)
    total = data.get("total", 0)
    page_count = data.get("page_count", 1)
    videos = data.get("videos", [])

    header = f"关键词: {keyword} | 第 {page}/{page_count} 页 | 共 {total} 个视频"
    sep = "─" * 60
    if not videos:
        return f"{header}\n{sep}\n未找到相关视频"

    # 按本页最大序号对齐：最大数字 ] 后留 1 空格，其余补到同一列，保证整齐
    max_i = (page - 1) * page_size + len(videos)
    align = len(f"[{max_i}]") + 1

    lines = [header, sep]
    for i, v in enumerate(videos, (page - 1) * page_size + 1):
        title = v.get("title", "（无标题）")
        author = v.get("author", "-")
        typename = v.get("typename", "")
        pubdate = v.get("pubdate", "-")
        duration = v.get("duration", "-")
        play = v.get("play", "-")
        like = v.get("like", "-")
        favorites = v.get("favorites", "-")
        video_review = v.get("video_review", "-")
        bvid = v.get("bvid", "")
        url = v.get("url") or f"https://www.bilibili.com/video/{bvid}"
        indent = " " * align
        pad = " " * (align - len(f"[{i}]"))
        meta = f"{indent}UP: {author}"
        if typename:
            meta += f" | 分区: {typename}"
        meta += f" | 日期: {pubdate} | 时长: {duration}"
        block = (
            f"[{i}]{pad}{title}\n"
            f"{meta}\n"
            f"{indent}播放 {play} | 点赞 {like} | 收藏 {favorites} | 弹幕 {video_review}\n"
            f"{indent}URL: {url}\n"
        )
        if show_details:
            desc = v.get("description", "")
            tag = v.get("tag", "")
            if desc:
                block += f"{indent}简介: {desc}\n"
            if tag:
                block += f"{indent}标签: {tag}\n"
        lines.append(block.rstrip("\n"))
    return "\n".join(lines)


def format_file_table(files: list, folder: str = None, indices=None, probe: bool = False) -> str:
    """把 format_file_list 返回的文件列表渲染为对齐表格（CLI / MCP 共用，避免双份拼表逻辑）。

    files: format_file_list 的返回（含 name/type_str/size_str/duration_str/quality_str/mtime_str/is_audio）。
    folder / probe: 仅用于表外摘要与脚注，不影响表格本身。
    indices: 需高亮的序号集合（如 CLI 选择预览），命中行追加 ' <--'。
    渲染样式（固定列宽 + '|' 分隔 + '='/'-' 边框）已统一，CLI 与 MCP 输出逐字相同。
    """
    n_video = sum(1 for f in files if not f.get("is_audio"))
    n_audio = len(files) - n_video

    cols = [
        ("序号", 8, "center"),
        ("文件名", 40, "left"),
        ("类型", 11, "left"),
        ("大小", 11, "right"),
        ("时长", 9, "right"),
        ("画质", 7, "center"),
        ("是否已转写", 10, "center"),
        ("修改日期", 16, "left"),
    ]
    header = " | ".join(_pad(name, w, "center") for name, w, _a in cols)
    line_w = _disp_width(header)
    lines = ["=" * line_w, header, "-" * line_w]
    for i, f in enumerate(files, 1):
        mark = " <--" if indices and i in indices else ""
        transcribed = "是" if is_transcribed(f["name"]) else "否"
        row = [
            _pad(f"[{i}]", cols[0][1], cols[0][2]),
            _pad(_truncate_disp(f["name"], cols[1][1]), cols[1][1], cols[1][2]),
            _pad(f.get("type_str", "-"), cols[2][1], cols[2][2]),
            _pad(f["size_str"], cols[3][1], cols[3][2]),
            _pad(f.get("duration_str", "-"), cols[4][1], cols[4][2]),
            _pad(f.get("quality_str", "-"), cols[5][1], cols[5][2]),
            _pad(transcribed, cols[6][1], cols[6][2]),
            _pad(f.get("mtime_str", ""), cols[7][1], cols[7][2]),
        ]
        lines.append(" | ".join(row) + mark)
    lines.append("=" * line_w)

    summary = f"共 {len(files)} 个文件（{n_video} 视频 / {n_audio} 音频"
    if folder:
        summary += f"，文件夹: {folder}"
    summary += "）"
    note = "（仅扫描根目录，不读取子目录"
    if probe:
        note += "；时长/画质经 ffprobe 探测"
    note += "；“已转写” 依据转写产物目录是否存在同名文件判定；列表已按修改日期降序排列）"
    return f"{summary}:\n" + "\n".join(lines) + "\n" + note


# ============================================================
# yt-dlp 视频下载
# ============================================================


def _extract_bvid(url: str) -> str:
    m = re.search(r'/(BV\w+)', url)
    return m.group(1) if m else None


def _parse_bvid_or_url(bvid_or_url: str):
    """把 'BV...' 或 B站链接归一化为 (bvid, base_url)；无效返回 (None, None)。"""
    if not bvid_or_url:
        return None, None
    if bvid_or_url.startswith("BV"):
        bvid = bvid_or_url
        base = f"https://www.bilibili.com/video/{bvid}"
    elif "bilibili.com" in bvid_or_url:
        base = bvid_or_url
        bvid = _extract_bvid(base)
    else:
        return None, None
    if not bvid:
        return None, None
    return bvid, base


def _find_downloaded_files(out_dir: str, bvid: str):
    """从下载目录中根据 BV 号找到所有匹配的视频文件（按文件名排序）。"""
    if not os.path.isdir(out_dir):
        return []
    return sorted(
        os.path.join(out_dir, f)
        for f in os.listdir(out_dir)
        if bvid in f and f.endswith(VIDEO_EXTS)
    )


def _find_downloaded_file(out_dir: str, bvid: str) -> str:
    """从下载目录中根据 BV 号找到实际下载的文件（取第一个匹配）"""
    files = _find_downloaded_files(out_dir, bvid)
    return files[0] if files else None


def _find_part_downloaded_file(out_dir: str, bvid: str, part_no: int) -> str:
    """多P 视频下载某个指定分P 后，精确找到该分P 的文件。

    B站 yt-dlp 在分P 文件名里会把 id 写成 '<bvid>_p<part_no>'，其后紧跟扩展名点号，
    因此用 '<bvid>_p<part_no>.' 做唯一匹配，可正确区分各分P；
    即使该文件此前已存在（重下时覆盖），按名匹配依然正确，不会误命中字母序首个（p01）。"""
    if not os.path.isdir(out_dir):
        return None
    token = f"{bvid}_p{int(part_no)}."
    for f in sorted(os.listdir(out_dir)):
        if f.endswith(VIDEO_EXTS) and token in f:
            return os.path.join(out_dir, f)
    # 回退：仅含 bvid 的文件（兼容非 B站 / 单P 等）
    return _find_downloaded_file(out_dir, bvid)


def _find_new_downloaded_file(out_dir: str, bvid: str, before: set) -> str:
    """下载完成后，从 out_dir 中找出本次「新生成」的、匹配该 BV 号的视频文件。

    多P 视频的所有分P 文件名里都含同一个 BV 号，旧逻辑 _find_downloaded_file
    直接取 sorted 后的首个匹配，导致每个分P 都错误地命中 p01（字母序最小），
    于是视频/音频路径全部显示成第 1P。

    这里在下载前先快照目录，下载后只在新出现的文件里挑选，从而精确拿到
    本次真正生成的那个分P 文件。若没找到新文件（如覆盖式重下），则回退旧逻辑。"""
    if not os.path.isdir(out_dir):
        return _find_downloaded_file(out_dir, bvid)
    candidates = []
    for f in os.listdir(out_dir):
        if f in before:
            continue
        if bvid in f and f.endswith(VIDEO_EXTS):
            candidates.append(f)
    if not candidates:
        return _find_downloaded_file(out_dir, bvid)
    candidates.sort(
        key=lambda f: os.path.getmtime(os.path.join(out_dir, f)),
        reverse=True,
    )
    return os.path.join(out_dir, candidates[0])


def detect_parts(bvid_or_url):
    """探测视频是否为分P（多P）。返回 {success, is_multi, part_count, bvid, title}。
    探测失败时不阻塞下载，按单P处理（is_multi=False）。
    先用 --flat-playlist 快速探测；若无效则回退到 --playlist-end 2 精确探测。"""
    bvid, url = _parse_bvid_or_url(bvid_or_url)
    if not bvid:
        return {"success": False, "error": "无效的视频标识"}

    ytdlp = _get_ytdlp_cmd()

    def parse_part_count(info):
        pc = info.get("playlist_count")
        ents = info.get("entries") or []
        return pc if (isinstance(pc, int) and pc > 0) else len(ents)

    # 方案1：快速探测（--flat-playlist，适合大多数情况）
    for cmd in [
        [*ytdlp, "--flat-playlist", "--dump-single-json", "--no-warnings", url],
        # 方案2：回退精确探测（去掉 --flat-playlist，限取前2条避免过慢）
        [*ytdlp, "--playlist-end", "2", "--dump-single-json", "--no-warnings", url],
    ]:
        try:
            proc = _run_capture(cmd, timeout=120)
            if proc.returncode != 0:
                continue
            info = json.loads(proc.stdout.strip())
            part_count = parse_part_count(info)
            if part_count > 1:
                return {
                    "success": True,
                    "is_multi": True,
                    "part_count": part_count,
                    "bvid": bvid,
                    "title": info.get("title", ""),
                }
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
            continue

    # 探测失败或确认为单P，按单P处理
    return {"success": True, "is_multi": False, "part_count": 1,
            "bvid": bvid, "title": ""}


def _clean_part_title(raw, base="", index=None):
    """清洗 yt-dlp/B站 分P标题里的冗余前缀，只保留分P真实小标题。

    - base: 视频整体标题，用于剥离「大标题 + 小标题」拼接
    - index: 分P序号，用于精确剥离 "p01 1 标题" / "p01 标题" / "1 标题" 等序号标记
    """
    t = (raw or "").strip()
    if base and t.startswith(base):
        t = t[len(base):].strip()
    # 形如 "p01 1 标题" / "P1 1 标题" 的分P序号前缀
    t = re.sub(r"^p\d+\s+\d+\s+", "", t, flags=re.IGNORECASE).strip()
    # 形如 "p01 标题" / "P1 标题" 的分P序号前缀（单数字，无第二序号）
    t = re.sub(r"^p\d+\s+", "", t, flags=re.IGNORECASE).strip()
    # 仅当分P标题以自身序号开头时剥离 "1 标题" 这类冗余序号
    # （要求序号后紧跟空白，故 "2.0 ..." 等不会被误删）
    if index is not None:
        t = re.sub(rf"^{re.escape(str(index))}\s+", "", t).strip()
    return t or raw


def _get_bili_view(bvid):
    """通过 B站 view 接口（x/web-interface/wbi/view）一次性拿到视频总标题与各分P标题。
    优点：单请求、秒回，分P数量再多也极快（不像 yt-dlp 完整提取需逐P抓元数据）。
    返回 (title, [part_title, ...])；失败返回 (None, None)。"""
    mixin = _get_wbi()
    if not mixin:
        return None, None
    params = _wbi_sign_params({"bvid": bvid}, mixin)
    query = urllib.parse.urlencode(params)
    url = f"https://api.bilibili.com/x/web-interface/wbi/view?{query}"
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
    }
    data, err = _curl_get_json(url, headers=headers, timeout=15, include_cookies=True)
    if err or data is None or data.get("code") != 0:
        return None, None
    d = data.get("data") or {}
    title = (d.get("title") or "").strip()
    pages = d.get("pages") or []
    parts = [(p.get("part") or "").strip() for p in pages]
    return title, parts


def get_part_list(bvid_or_url):
    """获取视频各分P的序号与标题，供下载前展示。
    返回 {success, bvid, parts:[{index, title}], part_count}。
    仅通过 B站 view 接口（x/web-interface/wbi/view）获取：单请求秒回、任意分P数都拿真实标题，且不含总标题前缀。
    """
    bvid, _url = _parse_bvid_or_url(bvid_or_url)
    if not bvid:
        return {"success": False, "error": "无效的视频标识"}

    api_title, api_parts = _get_bili_view(bvid)
    if not api_parts:
        return {"success": False, "error": "无法获取分P列表（B站接口请求失败）"}

    base = (api_title or "").strip()
    parts = [{"index": i, "title": _clean_part_title(t, base, i)}
             for i, t in enumerate(api_parts, start=1)]
    if not parts:
        return {"success": False, "error": "无法获取分P列表"}
    return {"success": True, "bvid": bvid, "parts": parts, "part_count": len(parts)}


def _check_quality(video_file, quality):
    """检查下载视频的实际画质，返回 (actual_quality, quality_warning).

    当 video_file 为 None 或无法读取时，返回请求画质而无警告。"""
    if not video_file or not os.path.exists(video_file):
        return quality, None
    actual_height = _get_video_height(video_file)
    if actual_height is None:
        return quality, "无法验证实际画质（ffprobe 失败），以实际文件为准"
    if actual_height <= 0:
        return quality, None
    actual_quality = f"{actual_height}p"
    quality_warning = None
    if quality != "best":
        requested_height = QUALITY_HEIGHT.get(quality, 0)
        if actual_height < requested_height:
            quality_warning = (
                f"目标画质 {quality}，实际视频源支持的最高画质仅 "
                f"{actual_height}p，已自动降级"
            )
    return actual_quality, quality_warning


def max_available_height(bvid_or_url):
    """下载前探测视频源能提供的最高视频分辨率高度（像素）。

    通过 yt-dlp 仅拉取元数据（-J，不下载）获取所有格式的 height 并取最大值。
    网络失败 / 解析失败 / 无视频格式时返回 None。"""
    bvid, base = _parse_bvid_or_url(bvid_or_url)
    if not bvid:
        return None
    ytdlp = _get_ytdlp_cmd()
    cmd = [*ytdlp, "-J", "--no-playlist", "--no-warnings", base]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except Exception:
        return None
    heights = [f.get("height") for f in data.get("formats", []) if f.get("height")]
    return max(heights) if heights else None


def check_quality_available(bvid_or_url, quality):
    """下载前判断目标画质是否可取（供「不降级下载」模式预跳过用）。

    返回:
      True  -> 目标画质可满足（quality=best 或 源最高高度 >= 请求高度）
      False -> 源最高高度 < 请求高度，目标画质确实不可用
      None  -> 无法判定（探测失败），交由下载流程自行处理
    """
    if quality == "best" or quality not in QUALITY_HEIGHT:
        return True
    req_h = QUALITY_HEIGHT[quality]
    max_h = max_available_height(bvid_or_url)
    if max_h is None:
        return None
    return max_h >= req_h


def preflight_quality_ok(item, quality, allow_fb):
    """下载前画质预检（不降级下载模式），返回 (ok, reason)。

    - ok=True   允许继续下载；
    - ok=False  目标画质源端不可用且不允许降级，应直接跳过（不下载），reason 为说明文本。
    quality='best' 或 允许降级时恒为 True；预检探测失败时返回 True，交由下载后兜底处理。
    """
    if quality == "best" or allow_fb:
        return True, None
    try:
        avail = check_quality_available(item, quality)
    except Exception:
        return True, None
    if avail is False:
        return False, f"目标画质 {quality} 源端不可用，已设置不降级下载，跳过该视频（未下载）"
    return True, None


def post_download_quality_check(result, allow_fb):
    """下载后画质降级兜底（不降级下载模式）。

    若实际画质低于目标且不允许降级，删除已下载的视频/音频文件并标记放弃。
    返回 (result, deleted, note)：deleted=True 时 result 已被改写（success=False, status=abandoned_quality_fallback）。
    """
    qw = result.get("quality_warning")
    if not qw or allow_fb:
        return result, False, None
    files = result.get("files") or ([result.get("file")] if result.get("file") else [])
    audios = result.get("audio_files") or ([result.get("audio_file")] if result.get("audio_file") else [])
    for f in files:
        if f and os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass
    for a in audios:
        if a and os.path.exists(a):
            try:
                os.remove(a)
            except OSError:
                pass
    new_result = dict(result)
    new_result["success"] = False
    new_result["status"] = "abandoned_quality_fallback"
    new_result["reason"] = "目标画质不可用，已设置不降级下载，放弃并删除已下载文件"
    note = f"[!] {qw}（文件已删除，已放弃该视频）"
    return new_result, True, note


# 分P探测缓存：同一个 bvid/url 在一次进程生命周期内只探测一次
_parts_cache = {}


def detect_parts_cached(bvid_or_url):
    """带缓存的分P探测，避免批量下载时对同一视频重复请求网络。"""
    key = str(bvid_or_url)
    if key in _parts_cache:
        return _parts_cache[key]
    try:
        _parts_cache[key] = detect_parts(bvid_or_url)
    except Exception:
        _parts_cache[key] = {"success": False, "error": "detect_parts 异常"}
    return _parts_cache[key]


def _print_part_result(part_no, vf, af, q, qw):
    """多分P下载时，单个分P完成后打印其结果（实际画质/本地视频/本地音频）。

    输出定向到 sys.stderr，避免污染 sys.stdout；在 MCP（stdio）场景下
    stdout 是 JSON-RPC 通道，任何 stdout 文本都会破坏协议，故进度/日志类
    输出统一走 stderr（与 echo 进度输出一致）。"""
    if not vf:
        print(f"  [!] 分P[{part_no}] 下载失败（未生成视频文件）", file=sys.stderr)
        return
    print(f"  分P[{part_no}] 下载完成！", file=sys.stderr)
    print(f"  - 实际画质: {q}" + (f"  [!] {qw}" if qw else ""), file=sys.stderr)
    print(f"  - 本地视频: {vf}", file=sys.stderr)
    print(f"  - 本地音频: {af or '提取失败（视频已保存）'}", file=sys.stderr)


def _download_one(url, out_dir, bvid, quality, template_suffix=None, echo=False, part_no=None):
    """下载单个视频 URL，立即提取音频。
    part_no: 指定分P 序号（1-based）时，用于精确匹配该分P 生成的文件；
             为 None 时退化为「新生成文件」检测。
    返回 (video_file_path, audio_file_path, output_text, success)."""
    if template_suffix is None:
        template_suffix = "%(title)s-%(id)s.%(ext)s"
    output_template = os.path.join(out_dir, template_suffix)
    ytdlp = _get_ytdlp_cmd()
    cmd = [
        *ytdlp, "-o", output_template, "--no-mtime", "--embed-metadata",
        "--retries", "3", "--fragment-retries", "3", "--no-playlist",
    ]
    if quality in QUALITY_HEIGHT:
        h = QUALITY_HEIGHT[quality]
        cmd += ["-f", f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"]
    else:
        cmd += ["-f", "bv*+ba/best"]
    cmd += ["--merge-output-format", "mp4"]
    cmd.append(url)

    proc = None
    try:
        # 下载前快照目录，用于精确识别本次真正生成的文件
        # （多P 视频各分P 文件名共享同一 BV 号，旧逻辑总会命中字母序首个 p01）
        _before = set(os.listdir(out_dir)) if os.path.isdir(out_dir) else set()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.PIPE, bufsize=0)
        output_lines = _stream_output(proc, echo=echo)
        try:
            proc.wait(timeout=3600)
        except subprocess.TimeoutExpired:
            proc.kill()
            return None, None, output_lines, False
        if proc.returncode != 0:
            error_msg = output_lines[-1000:] if len(output_lines) > 1000 else output_lines
            return None, None, error_msg, False
        # 多P：按分P 唯一标识（'<bvid>_p<part_no>.'）精确匹配；
        # 否则按本次新生成的文件检测，最后回退到含 bvid 的首个匹配。
        if part_no is not None:
            vf = _find_part_downloaded_file(out_dir, bvid, part_no)
            if not vf or not os.path.exists(vf):
                vf = _find_new_downloaded_file(out_dir, bvid, _before)
        else:
            vf = _find_new_downloaded_file(out_dir, bvid, _before)
        if not vf or not os.path.exists(vf):
            return None, None, output_lines, False
        try:
            af = _extract_audio(vf)
        except Exception:
            af = None
        return vf, af, output_lines, True
    except KeyboardInterrupt:
        # 用户按 Ctrl+C 中止：可能发生在 proc.wait(timeout=3600)（yt-dlp 下载）阶段，
        # 也可能发生在其后 _extract_audio(vf)（ffmpeg 提取音频）阶段——两者都在此 try 内。
        # KeyboardInterrupt 是 BaseException 子类，不会被下方 except Exception 捕获，
        # 这里先杀掉可能残留的 yt-dlp 子进程，再原样抛出，
        # 由上层调用方处理（CLI 侧会捕获并转成 QuitProgram 优雅退出）。
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        raise
    except FileNotFoundError:
        return None, None, "", False
    except Exception as e:
        return None, None, str(e), False


def download_video(bvid_or_url, download_dir=None, quality="best", part=None, all_parts=False, echo=False):
    """使用 yt-dlp 下载 B站视频，自动提取音频。
    part: 指定分P序号（1-based）；all_parts: 下载全部分P（逐个下载，每P下完立即提取音频）。默认只下第1P。
    echo: 为 True 时实时输出 yt-dlp 下载进度到 stderr。"""
    bvid, base = _parse_bvid_or_url(bvid_or_url)
    if not bvid:
        return {"success": False, "error": "无效的视频标识，请提供 BV 号或 B站视频链接"}

    out_dir = download_dir or DEFAULT_VIDEOS_DIR
    os.makedirs(out_dir, exist_ok=True)

    # === all_parts：逐个下载每个分P，每P下完立即提取音频 ===
    if all_parts and not (part and isinstance(part, int) and part > 0):
        det = detect_parts(bvid_or_url)
        part_count = det.get("part_count", 1) if det.get("success") else 1
        if not det.get("is_multi") or part_count <= 1:
            # 探测失败或实为单P，回退到单P下载
            all_parts = False
        else:
            vfiles = []
            audio_files = []
            output_texts = []
            _cached_q = None
            _cached_qw = None
            for p in range(1, part_count + 1):
                part_url = f"{base}?p={p}"
                vf, af, out_text, ok = _download_one(part_url, out_dir, bvid, quality, echo=echo, part_no=p)
                vfiles.append(vf)
                audio_files.append(af)
                output_texts.append(out_text)
                if vf and _cached_q is None:
                    _cached_q, _cached_qw = _check_quality(vf, quality)
                _print_part_result(p, vf, af, _cached_q, _cached_qw)

            # 画质检查
            first_vf = next((vf for vf in vfiles if vf), None)
            actual_quality, quality_warning = _check_quality(first_vf, quality)

            ok_vfiles = [vf for vf in vfiles if vf]
            ok_audio = [a for a in audio_files if a]
            combined_output = "\n".join(output_texts)
            message = (
                "\u2713 下载完成（共 " + str(len(ok_vfiles)) + " 个分P）！\n"
                f"- 实际画质: {actual_quality}"
                + (f"  [!] {quality_warning}" if quality_warning else "")
                + f"\n- 视频文件: {len(ok_vfiles)} 个"
                + f"\n- 音频文件: {len(ok_audio)} 个"
            )
            return {
                "success": True,
                "url": base,
                "download_dir": out_dir,
                "file": ok_vfiles[0] if ok_vfiles else None,
                "audio_file": next((a for a in audio_files if a), None),
                "files": ok_vfiles,
                "audio_files": ok_audio,
                "part_count": len(ok_vfiles),
                "quality_requested": quality,
                "quality_actual": actual_quality,
                "quality_warning": quality_warning,
                "output_snippet": combined_output[-500:] if len(combined_output) > 500 else combined_output,
                "message": message,
            }

    # === 指定多个分P（part 为 list[int]）===
    if isinstance(part, list) and part:
        vfiles = []
        audio_files = []
        output_texts = []
        _cached_q = None
        _cached_qw = None
        for p in part:
            part_url = f"{base}?p={p}"
            vf, af, out_text, ok = _download_one(part_url, out_dir, bvid, quality, echo=echo, part_no=p)
            vfiles.append(vf)
            audio_files.append(af)
            output_texts.append(out_text)
            if vf and _cached_q is None:
                _cached_q, _cached_qw = _check_quality(vf, quality)
            _print_part_result(p, vf, af, _cached_q, _cached_qw)

        # 画质检查（用第一个成功视频）
        first_vf = next((vf for vf in vfiles if vf), None)
        actual_quality, quality_warning = _check_quality(first_vf, quality)

        ok_vfiles = [vf for vf in vfiles if vf]
        ok_audio = [a for a in audio_files if a]
        combined_output = "\n".join(output_texts)
        message = (
            "\u2713 下载完成（共 " + str(len(ok_vfiles)) + " 个分P）！\n"
            f"- 实际画质: {actual_quality}"
            + (f"  [!] {quality_warning}" if quality_warning else "")
            + f"\n- 视频文件: {len(ok_vfiles)} 个"
            + f"\n- 音频文件: {len(ok_audio)} 个"
        )
        return {
            "success": True,
            "url": base,
            "download_dir": out_dir,
            "file": ok_vfiles[0] if ok_vfiles else None,
            "audio_file": next((a for a in audio_files if a), None),
            "files": ok_vfiles,
            "audio_files": ok_audio,
            "part_count": len(ok_vfiles),
            "quality_requested": quality,
            "quality_actual": actual_quality,
            "quality_warning": quality_warning,
            "output_snippet": combined_output[-500:] if len(combined_output) > 500 else combined_output,
            "message": message,
        }

    # === 单P / 指定某P ===
    if part and isinstance(part, int) and part > 0:
        url = f"{base}?p={part}"
        single_part_no = part
    else:
        url = base
        single_part_no = None

    vf, af, out_text, ok = _download_one(url, out_dir, bvid, quality, echo=echo, part_no=single_part_no)

    if not ok:
        # 构造有意义的错误提示
        if "HTTP Error 403" in out_text or "403" in out_text:
            error_hint = "B站拒绝访问(403)，可能需要登录 Cookie。"
        elif "HTTP Error 404" in out_text or "404" in out_text:
            error_hint = "视频不存在或已下架。"
        elif "wbi" in out_text.lower():
            error_hint = "B站 wbi 签名校验失败，yt-dlp 版本可能过旧。"
        elif "yt-dlp 未找到" in out_text:
            error_hint = "yt-dlp 未找到。请确认已安装: pip install yt-dlp"
        elif not out_text.strip():
            error_hint = "yt-dlp 未找到。请确认已安装: pip install yt-dlp"
        else:
            error_hint = out_text[-300:]
        failed_file = _find_downloaded_file(out_dir, bvid)
        return {
            "success": False,
            "error": error_hint,
            "url": url,
            "file": failed_file,
            "output_snippet": out_text[-500:],
        }

    # 单P 成功：画质检查
    actual_quality, quality_warning = _check_quality(vf, quality)

    return {
        "success": True,
        "url": url,
        "download_dir": out_dir,
        "file": vf,
        "audio_file": af,
        "quality_requested": quality,
        "quality_actual": actual_quality,
        "quality_warning": quality_warning,
        "output_snippet": out_text[-500:] if len(out_text) > 500 else out_text,
        "message": (
            "\u2713 下载完成！\n"
            f"- 实际画质: {actual_quality}"
            + (f"  [!] {quality_warning}" if quality_warning else "")
            + f"\n- 本地视频: {vf}"
            + f"\n- 本地音频: {af or '提取失败（视频已保存）'}"
        ),
    }


def format_download_text(data):
    if data.get("success"):
        return f"[OK] {data.get('message', '下载成功')}"
    return f"[X] 下载失败: {data.get('error', '')}"

# ============================================================
# 视频信息 / 画质查询
# ============================================================

# ============================================================
# 依赖检查
# ============================================================


def check_dependencies():
    """检查 yt-dlp 和 ffmpeg/ffprobe 是否可用"""
    results = {}
    ytdlp_cmd = _get_ytdlp_cmd()

    # yt-dlp
    try:
        proc = _run_capture([*ytdlp_cmd, "--version"], timeout=10)
        if proc.returncode == 0:
            results["yt-dlp"] = {"available": True, "version": proc.stdout.strip(), "path": _which("yt-dlp")}
        else:
            results["yt-dlp"] = {"available": False, "error": proc.stderr.strip()}
    except FileNotFoundError:
        results["yt-dlp"] = {"available": False, "error": "yt-dlp 未安装。安装方法: pip install yt-dlp"}
    except Exception as e:
        results["yt-dlp"] = {"available": False, "error": str(e)}

    # ffmpeg
    try:
        proc = _run_capture(["ffmpeg", "-version"], timeout=10)
        if proc.returncode == 0:
            version_line = proc.stdout.split("\n")[0] if proc.stdout else ""
            results["ffmpeg"] = {"available": True, "version": version_line.strip(), "path": _which("ffmpeg")}
        else:
            results["ffmpeg"] = {"available": False, "error": proc.stderr.strip()}
    except FileNotFoundError:
        results["ffmpeg"] = {"available": False, "error": "ffmpeg 未安装。安装方法: winget install ffmpeg"}
    except Exception as e:
        results["ffmpeg"] = {"available": False, "error": str(e)}

    # ffprobe
    try:
        proc = _run_capture(["ffprobe", "-version"], timeout=10)
        if proc.returncode == 0:
            version_line = proc.stdout.split("\n")[0] if proc.stdout else ""
            results["ffprobe"] = {"available": True, "version": version_line.strip(), "path": _which("ffprobe")}
        else:
            results["ffprobe"] = {"available": False, "error": proc.stderr.strip()}
    except FileNotFoundError:
        results["ffprobe"] = {"available": False, "error": "ffprobe 未安装。安装方法: winget install ffmpeg"}
    except Exception as e:
        results["ffprobe"] = {"available": False, "error": str(e)}

    # 讯飞凭证
    cfg = XFYunConfig()
    cfg.load()
    if cfg.is_ready:
        results["xfyun-asr"] = {"available": True, "note": f"app_id: {cfg.APP_ID[:4]}****"}
    else:
        results["xfyun-asr"] = {
            "available": False,
            "error": "未配置，请设置环境变量 XF_APP_ID / XF_SECRET_KEY 或创建 config.json",
        }

    all_ok = (
        results.get("yt-dlp", {}).get("available", False)
        and results.get("ffmpeg", {}).get("available", False)
        and results.get("ffprobe", {}).get("available", False)
    )
    return {"success": True, "all_ready": all_ok, "dependencies": results}


def format_deps_text(data):
    deps = data.get("dependencies", {})

    def _render_group(title, names):
        out = [f"  {title}:"]
        max_name_len = max((len(n) for n in names), default=0)
        for name in names:
            info = deps.get(name)
            if info is None:
                continue
            pad = " " * (max_name_len - len(name))
            if info.get("available"):
                out.append(f"    [OK] {name}{pad}")
            else:
                out.append(f"    [X]  {name}{pad}")
        return out

    lines = ["[依赖清单]"]
    lines += _render_group("必要依赖", ("yt-dlp", "ffmpeg", "ffprobe"))
    lines += _render_group("非必要依赖", ("xfyun-asr",))

    lines.append("")
    lines.append("[依赖检查结果]")
    if not data.get("all_ready"):
        lines.append("  缺少必要依赖，请先按 README.md 安装所有必要依赖并配置环境变量后再运行")
    elif not deps.get("xfyun-asr", {}).get("available", False):
        lines.append("  讯飞凭证未配置，转写功能暂不可用，仅搜索/下载可用")
    else:
        lines.append("  所有必要依赖及讯飞凭证均已就绪，可完整使用所有功能")
    return "\n".join(lines)

# ============================================================
# 统一转写流程（供 CLI 和 MCP 复用）
# ============================================================


def transcribe_file(video_path: str,
                    hot_words: list = None,
                    audio_dir: str = None,
                    output_dir: str = None,
                    progress_callback=None) -> dict:
    """统一的单文件转写流程，返回标准化结果 dict。

    返回: {
        "success": bool,
        "video_name": str,
        "saved": dict (text_path/srt_path/segments_path),
        "result": dict (转写原始结果),
        "duration": int, "chars": int, "elapsed": int,
        "error": str (仅失败时),
    }
    """
    audio_dir = audio_dir or DEFAULT_AUDIO_DIR
    output_dir = output_dir or DEFAULT_OUTPUT_DIR
    start_t = time.time()

    # 1. 音频准备
    audio_path, src = resolve_audio_path(video_path, audio_dir)

    # 2. 转写
    client = XFYunASR()
    result = client.transcribe(
        audio_path,
        hot_words=hot_words,
        pd="game",
        poll_interval=5,
        progress_callback=progress_callback,
    )

    # 3. 保存
    video_name = os.path.basename(video_path)
    saved = _save_transcript(video_name, result, output_dir)

    elapsed = int(time.time() - start_t)
    char_count = len(result.get("plain_text", ""))

    return {
        "success": True,
        "video_name": video_name,
        "saved": saved,
        "result": result,
        "duration": result.get("duration", 0),
        "chars": char_count,
        "elapsed": elapsed,
        "audio_src": src,
        "audio_path": audio_path,
    }

# ============================================================
# 批量下载（统一入口，CLI / MCP 共用）
# ============================================================


def _run_batch(items, work, concurrency=1, sort_key=None):
    """通用批量并发执行。work(item)->结果 dict(需含 'success')；返回 (results, ok, total)。
    results 默认按输入顺序排序（sort_key 可覆盖）。"""
    items = [x for x in items if x]
    if not items:
        return [], 0, 0
    conc = max(1, min(int(concurrency), len(items)))
    results = []
    with ThreadPoolExecutor(max_workers=conc) as ex:
        for f in as_completed(ex.submit(work, it) for it in items):
            results.append(f.result())
    if sort_key is None:
        def sort_key(r):
            return items.index(r.get("input"))
    results.sort(key=sort_key)
    ok = sum(1 for r in results if r.get("success"))
    return results, ok, len(results)
