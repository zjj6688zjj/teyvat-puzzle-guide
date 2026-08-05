# 原神AI攻略助手（MVP）

一个本地运行的原神攻略 AI 问答系统：语义检索本地攻略知识库 + DeepSeek 生成带来源引用的回答。

## 技术栈

- LLM：DeepSeek `deepseek-v4-flash`（OpenAI 兼容，可在 `config/settings.yaml` 切换模型）
- Embedding：阿里百炼 `qwen3.7-text-embedding`（1024 维）
- 向量检索：FAISS（本地文件，后续可替换为阿里云 pgvector/Milvus）
- 界面：Streamlit；服务：FastAPI

## 目录结构

```
原神AI攻略助手_MVP/
├── src/                    ← 全部核心代码
│   ├── api/                ← FastAPI 服务
│   ├── app/                ← Streamlit 聊天界面
│   ├── ingest/             ← 加载 / 清洗 / 分段 / 向量化
│   └── rag/                ← 检索 + 生成问答管道
├── scripts/                ← 4 个脚本
├── tests/                  ← 单元测试
├── config/
│   └── settings.yaml       ← 模型与路径配置（config\.env 含密钥，不入库）
├── data/
│   ├── raw/                ← 攻略数据目录（仅保留空目录，攻略文件不入库）
│   │   ├── text_guides/            ← 文本攻略 .md/.txt 丢这里
│   │   └── video_transcripts/      ← 视频转写（可选）
│   └── eval/               ← 评测题集
├── requirements.txt        ← 依赖清单
├── .env.example            ← 密钥模板（复制成 config\.env 填 Key）
├── .gitignore              ← 忽略规则：密钥 / 数据 / 缓存不入库
├── README.md               ← 开发说明
└── 用户使用手册.md          ← 用户手册
```

## 快速开始

> ⚠️ **环境准备**：装好 Python 3.10+ 后，记得把它的 `Scripts` 目录（一般在 `C:\Users\你的用户名\AppData\Local\Programs\Python\Python310\Scripts`）加进系统环境变量 PATH，否则 `pip`、`streamlit` 等命令会报"不是内部或外部命令"。

```powershell
# 1. 安装依赖
cd 原神AI攻略助手_MVP
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. 配置密钥（只改这一个文件）
copy .env.example config\.env
# 编辑 config\.env，填入 DEEPSEEK_API_KEY 与 DASH_SCOPE_API_KEY

# 3. 验证密钥连通性
python scripts/check_env.py

# 4. 准备攻略数据并构建知识库（交付包不含数据，这一步必须做）
#    方式A（推荐）：把攻略 .md/.txt 放进 data\raw\text_guides\
#    方式B（米游社爬虫数据）：把 output 文件夹放到本项目文件夹的上一级
python scripts/build_kb.py

# 5. 启动对话界面
streamlit run src/app/chat.py
# 或启动 API 服务
uvicorn src.api.main:app --reload
```

> ⚠️ 交付包**不包含任何攻略数据**。首次启动界面显示"知识库还是空的"属正常现象，请先完成第 4 步建库。

## 数据放哪

| 来源 | 目录 | 说明 |
|---|---|---|
| 文本攻略（**推荐**） | `data/raw/text_guides/` | 目录已建好，直接丢 `.md`/`.txt`，文件头可用 `<!-- source_url: ... -->` 注释标注来源链接 |
| 米游社爬虫产物 | 项目文件夹**上一级**的 `output/` | 结构 `output/{post_id}/text/article.json`；目录名可在 `settings.yaml` 的 `raw_output` 修改 |
| 视频转写（兼容） | `data/raw/video_transcripts/` | teyvat 产出的 `_text.txt` / `.srt` |

`build_kb.py` 为**增量追加**：已入库内容按哈希去重，新加攻略后重跑即可扩充，不会覆盖。

## 评测

- 评测题集：`data/eval/questions.jsonl`（每行一条，`expect` 字段标注预期行为：answer/out_of_kb/refuse/other）
- 运行：`python scripts/eval.py`

## 密钥与模型配置

- 密钥：`config\.env`（模板见 `.env.example`，**切勿外传**）
- 模型名与参数：`config/settings.yaml`（模型、温度、max_tokens、reasoning_effort、召回条数、分段大小等）

## 鸣谢

- 攻略数据来自米游社「原神」社区解密区创作者的内容整理
- LLM：DeepSeek（`deepseek-v4-flash`）
- Embedding：阿里云百炼（`qwen3.7-text-embedding`）
- 向量检索：FAISS
- 界面与接口：Streamlit、FastAPI
