# B站视频下载 + 语音转写流水线

用于搜索下载 Bilibili 视频，自动转写其中音频，并将转写文字结果保存在本地。

提供了 CLI 和 MCP 两个版本，后者可集成到 OpenClaw、Claude、CodeBuddy、Trae 等 AI 助手中。

本仓库的名字 `teyvat-puzzle-guide` 是本项目的最终目标，当前已有文件实现的功能只是项目的一小部分。

---

## 项目架构

本项目暂仅由三个 Python 文件组成核心部分，职责分明、无循环依赖：

```
┌─────────────────────────────────────────────────────────────┐
│  video_fetching_and_audio_textualizing_common.py           │
│  ★ 所有核心业务逻辑（无 CLI 交互、无 MCP 协议）              │
│  ★ B站 WBI 签名 / curl 请求 / 双搜索接口                    │
│  ★ yt-dlp 下载 / 画质降级 / 分P探测 / 音频提取              │
│  ★ 讯飞 ASR 凭证 / 上传 / 轮询 / 三格式存储                 │
│  ★ 公共工具：依赖检查、文件扫描、热词校验、路径工具           │
└────────────────────────┬───────────────────────────────────┘
                         │ import（纯函数调用，无状态共享）
          ┌──────────────┴──────────────────┐
          ▼                                  ▼
┌─────────────────────┐           ┌────────────────────────────┐
│  video_fetching_    │           │  video_fetching_           │
│  and_audio_         │           │  and_audio_                │
│  textualizing_      │           │  textualizing_             │
│  cli.py             │           │  mcp.py                    │
│                     │           │                            │
│  ★ 交互式菜单       │           │  ★ JSON-RPC 2.0 over stdio│
│  ★ 分页浏览/选择    │           │  ★ 8 个 MCP 工具           │
│  ★ 流水线入口       │           │  ★ MCP 协议编解码          │
│  ★ 结果格式化打印   │           │                            │
└─────────────────────┘           └────────────────────────────┘
```

## 功能特性

- **视频搜索** — 搜索 Bilibili 视频并展示详细信息（播放量、点赞、收藏等）
- **智能下载** — 支持 1080p/720p/480p/360p 多种画质，如果视频源不支持下载用户指定的画质，则自动一阶阶降级至可下载画质，下载视频到 `videos/` 后自动提取音频到 `videos/audios/`
- **分P（多P）视频下载** — 自动探测分P数量，支持「全部分P / 指定某P / 仅第1P」下载；多P 逐个下载，每P下完立即提取音频（不等全部下完）；探测失败或非交互环境自动降级，不阻塞下载
- **语音转写** — 基于讯飞 ASR API，支持热词优化，输出纯文本 + SRT 字幕 + 分段JSON，保存到 `videos/transcripts/`
- **批量处理** — 支持批量下载、批量转写，工具自动识别单条/批量输入，支持区间选择
- **CLI 交互** — 提供命令行工具，支持分页浏览、选择下载
- **MCP 协议** — 一站式 MCP Server，8 个工具，覆盖搜索→下载→转写全流程

---

## 文件结构

```
teyvat-puzzle-guide/
├── README.md                                          # 本文档
├── xfyun_config.json                                 # 讯飞 API 凭证（app_id / secret_key / api_key）
├── video_fetching_and_audio_textualizing_common.py    # 公共核心逻辑（被 CLI 和 MCP 共同引用）
├── video_fetching_and_audio_textualizing_cli.py       # 下载+转写 CLI（一站式流水线，交互式菜单）
├── video_fetching_and_audio_textualizing_mcp.py       # MCP Server（8个工具，JSON-RPC 2.0 over stdio）
├── .bilibili_cookies.txt                             # 本仓库不包含，在 curl 首次请求自动生成，保存 buvid3 等 Cookie
└── videos/                                           # 本仓库不包含，在程序首次启动时自动生成
    ├── [视频标题]-BV号.mp4                             # 下载的视频文件
    ├── audios/                                          # 下载后自动提取音频的目录
    │   └── [视频标题]-BV号.m4a                         # 提取的音频文件
    └── transcripts/                                    # 转写结果输出目录
        ├── [视频标题]-BV号_text.txt                    # 纯文本转写结果
        ├── [视频标题]-BV号.srt                         # SRT 字幕文件（带时间戳）
        └── [视频标题]-BV号_segments.json                # 分段 JSON（含时间戳、逐句详情）
```

---

### 公共 API 参考（common.py 公开函数）

下表列出 `common.py` 中所有对外公开函数（不含 `_` 前缀内部函数），可供 CLI/MCP 直接调用：

| 函数签名 | 返回值 | 说明 |
|----------|--------|------|
| `search_bilibili(keyword, page=1, page_size=10)` | `dict` | 搜索 B站视频，返回视频列表含元数据 |
| `download_video(bvid_or_url, download_dir, quality, part, all_parts, echo)` | `dict` | 下载视频（含分P控制），下载完成后自动提取音频 |
| `format_download_text(data)` | `str` | 格式化下载结果为可读文本 |
| `detect_parts(bvid_or_url)` | `dict` | 探测视频分P数量（`is_multi`/`part_count`） |
| `get_part_list(bvid_or_url)` | `dict` | 获取视频全部分P元数据（含各P标题） |
| `check_dependencies()` | `dict` | 检查 yt-dlp / ffmpeg / ffprobe / 讯飞凭证 |
| `format_deps_text(data)` | `str` | 格式化依赖检查结果 |
| `transcribe_file(video_path, hot_words, ...)` | `dict` | 单文件转写（自动识别 .mp4/.mp3/.m4a/.wav/.flac） |
| `parse_hotwords_string(hot_raw)` | `(list[str], list[str])` | 热词字符串（逗号/分号/顿号/空格分隔）→ 返回 `(valid, invalid)` 两个列表；valid 为长度 2-16 的合法热词（最多 200 个），invalid 为被过滤的非法词 |
| `format_file_list(folder, probe=False)` | `list[dict]` | 扫描目录，返回文件列表（大小/修改时间/类型/格式）；`probe=True` 时额外用 ffprobe 探测每个文件的时长与画质（视频高度） |
| `parse_selection(sel, max_idx, page_start=1)` | `list[int]` | 解析选择字符串（`1-3,5,7-10` → `[1,2,3,5,7,8,9,10]`），`page_start` 支持搜索翻页后把全局序号换算回页内序号 |
| `ensure_xfyun_config()` | `(bool, str)` | 加载并验证 `xfyun_config.json` 凭证；返回 `(is_ready, error_message)`，未配置时 `is_ready=False` 并带说明 |
| `resolve_audio_path(video_path, audio_dir)` | `tuple[str, str]` | 根据视频路径推断对应音频文件路径；返回 `(音频路径, 来源)`，来源为 `original`/`cached`/`extracted` 之一 |

> 注：上表仅列出非 `_` 前缀的顶层公开**函数**。模块另导出两个公开**类**（亦为对外 API，被上述函数内部调用），也在下面列出。

### 公共类（common.py 公开类）

| 类 | 关键成员 | 说明 |
|----|----------|------|
| `XFYunConfig` | `.load(config_path=None)`、`.is_ready`（property）、`.APP_ID` / `.SECRET_KEY` / `.API_KEY` / `.UPLOAD_URL` / `.RESULT_URL` | 讯飞凭证单例管理：优先读环境变量 `XF_APP_ID` / `XF_SECRET_KEY`，缺失时回退读 `xfyun_config.json`；`is_ready` 为 True 表示 APP_ID 与 SECRET_KEY 均已非空 |
| `XFYunASR` | `.transcribe(file_path, hot_words, pd, ...)`、`.upload_request(...)`、`.query_request(...)`、静态 `extract_text/extract_srt/extract_segments(...)` | 讯飞录音文件转写客户端；构造时需凭证就绪，否则抛 `ValueError`。`transcribe` 走「上传 → 轮询」流程，返回含 `plain_text`/`srt`/`segments` 的结果 dict |

---

### 可用 MCP 工具

> **CLI vs MCP**：CLI 是人机交互界面（菜单驱动，结果直接展示），搜索、列表查看、依赖检查等操作在 CLI 侧通过交互流程完成，不需要暴露为独立工具。MCP 模式下 AI 代理需要"主动发现"系统能力和"按需调用"特定功能，因此将这些操作封装为独立工具。

| 工具名 | 功能 |
|--------|------|
| **通用 / 系统检查** || 
| `check_deps` | 检查依赖（yt-dlp / ffmpeg / ffprobe / 讯飞凭证）——横跨搜索下载与语音转写两类，是总体环境检查，而非某一步骤的专属工具 |
| **搜索与下载** ||
| `search_video` | 搜索 Bilibili 视频，返回标题/UP主/分区/日期/时长/播放/点赞/收藏/弹幕/链接，支持分页（`page`/`page_size`） |
| `download_video` | 下载视频（自动识别单条/批量：传字符串单条下载，传数组批量并发下载。支持画质选择与自动降级；支持分P：传 `part` 指定某P 或 `all_parts=true` 下载全部分P，每P下完立即提取音频到 videos/audios/） |
| `get_video_info` | 获取单个视频详细信息（可用画质高度、大小、标题、时长等），下载前查清该视频支持的画质用 |
| `get_part_list` | 获取视频分P列表（各分P序号与标题），下载多P视频前调用，用于决定下哪些分P |
| `get_quality_summary` | 批量查询多个视频画质支持情况（每个画质支持哪些序号），下载前批量预查用 |
| **语音转写** ||
| `list_videos` | 列出 videos/ 中音视频文件（文件名/类型/大小/时长/画质/修改时间），默认用 ffprobe 探测时长与画质（`probe` 可关），用于决定转写哪些文件 |
| `transcribe` | 转写为文字 + SRT（自动识别单条/批量：传字符串单文件，传数组批量，传文件夹路径自动扫描，支持 `indices` 区间选择，支持 `concurrency` 并发控制）。保存到 videos/transcripts/ |

---

## 技术栈

| 技术 | 用途 |
|------|------|
| **Python 3.8+** | 运行环境 |
| **hashlib / hmac / base64** | 讯飞 ASR 签名 + B站 WBI 签名（MD5） |
| **subprocess（调用）/ shutil（找路径）** | subprocess 调 curl.exe/ffprobe/ffmpeg/yt-dlp；shutil.which 仅定位命令；讯飞 ASR 不经 subprocess |
| **curl.exe（Windows Schannel）** | B站**部分** Web API：搜索(search/type、all/v2)、nav、wbi/view（取分P标题），带 buvid cookie（WBI 签名仅 search/type 与 wbi/view）。因本机 Python OpenSSL 与 B站 CDN TLS 握手超时，改用 curl.exe（Windows Schannel）发起 |
| **yt-dlp** | 视频下载；分P数量探测、画质/格式详情 |
| **ffmpeg / ffprobe** | 音频提取、画质/时长探测 |
| **讯飞录音文件转写 API v2** | 语音转文字，HTTP 走 urllib（不经 curl） |
| **urllib（request / parse）** | 讯飞上传/轮询请求 + WBI 签名参数编码 + 关键词转义 |
| **concurrent.futures.ThreadPoolExecutor** | 批量下载/转写的并发执行 |
| **MCP (Model Context Protocol)** | JSON-RPC 2.0 over stdio（手写，无第三方 mcp 库） |
| **Bilibili Web API** | 搜索/分P标题由 curl.exe 发起；分P数量探测与画质详情由 yt-dlp 完成 |

---

## 依赖安装

### 1. Python 环境

可用以下终端命令确认是否安装 Python 3.8 或更高版本：

```powershell
python --version
```

可用的 Python 环境是这个项目的使用前提，请确保已安装 Python 3.8 或更高版本。

### 2. 安装 FFmpeg

1. 访问 [FFmpeg Builds](https://www.gyan.dev/ffmpeg/builds/)，在页面中找到 **`ffmpeg-release-full.7z`** 并点击下载
2. 将下载的 `ffmpeg-release-full.7z` 解压到本地某个目录，例如 `D:\ffmpeg`，解压后会得到一个类似 `D:\ffmpeg\ffmpeg-8.1.2-full_build` 的文件夹（版本号可能不同）
3. 配置环境变量：
   - 访问 Windows 设置 → 系统 → 关于 → 高级系统设置 → 环境变量
   - 在「系统变量」中找到 `Path`，点击编辑 → 新建
   - 填入 FFmpeg 解压后**里面 `bin` 文件夹**的完整路径，例如：`D:\ffmpeg\ffmpeg-8.1.2-full_build\bin`
   - 点击确定保存

### 3. 安装 yt-dlp

1. 打开 [yt-dlp Releases](https://github.com/yt-dlp/yt-dlp/releases)，在最新 Release 的 Assets 中找到 **`yt-dlp.exe`** 并点击下载
2. 将 `yt-dlp.exe` 移动到上面提到的 **`bin` 文件夹**下，这样 yt-dlp 与 FFmpeg 便共用同一个 PATH 路径，无需额外配置

### 4. 验证安装

重新打开一个终端（新的窗口才能加载更新后的环境变量），可通过输入以下命令自行确认是否安装成功：

```powershell
ffmpeg -version
ffprobe -version
yt-dlp --version
```

### 5. 配置讯飞 API 凭证

访问 [讯飞开放平台](https://www.xfyun.cn/)，获取 APPID、APISecret、APIKey 后，编辑 `xfyun_config.json`：

```json
{
  "app_id": "你的appid",
  "secret_key": "你的secret_key",
  "api_key": "你的api_key"
}
```

---

## 识别语言限定

**当前默认仅支持中英文混合识别。**

语音转写基于讯飞录音文件转写 API，`language=cn` 配合默认的自动中英文模式（`languageType=1`），可以同时识别普通话和英文混说内容。

**当前代码层面的限制：** `video_fetching_and_audio_textualizing_common.py` 的 `upload_request` 中，`language` 参数直接写死为 `"cn"`，未作为可配置项暴露。其他语种（en / ja / ko / ru 等，详见[讯飞 API 文档](https://www.xfyun.cn/doc/asr/ifasr_new/API.html)）需在移植时改为可选参数。本项目代码层面不做修改。

---

## 许可证

本项目仅供学习交流使用，请遵守 Bilibili 用户协议和讯飞 API 使用规范。

---

## 代码仓库

本项目同步托管于以下三个平台，欢迎 Star / Fork / Issue：

| 平台 | 地址 |
|------|------|
| GitHub | https://github.com/zjj6688zjj/teyvat-puzzle-guide |
| Gitee | https://gitee.com/zjj6688zjj/teyvat-puzzle-guide |
| GitCode | https://gitcode.com/2401_83013032/teyvat-puzzle-guide |

---

## 致谢

**技术支持：**
- [Python](https://www.python.org/) — 适合人生苦短的我
- [ffmpeg](https://ffmpeg.org/) / [ffprobe](https://ffmpeg.org/) — 音视频处理引擎（格式转换、音频提取、画质检测）
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — 视频下载工具
- [讯飞开放平台](https://www.xfyun.cn/) — 语音转写服务
- [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) — 模型上下文协议规范

**内容平台与创作者：**
- [Bilibili](https://www.bilibili.com/) — 你所热爱的，就是你的生活
- [米哈游 miHoYo](https://www.mihoyo.com/) / [原神·国服](https://ys.mihoyo.com/) — 优秀的开放世界二游，值得去探索，不对，应该是去启动
- 所有贡献优质内容的 B站 UP 主，这个项目因你们而在内容上可能存在

**开源社区：**
- [GitHub](https://github.com/) / [Gitee](https://gitee.com/) / [GitCode](https://gitcode.com/) — 代码托管与协作平台
- 所有为本项目所依赖的开源库贡献过代码的开发者们，这个项目因你们而在技术上可能存在
- 所有使用本 CLI 和 MCP 工具集的用户们，这个项目因你们的反馈（虽然目前还没有）而持续进步
