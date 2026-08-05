"""Streamlit 对话界面：左侧知识库统计 + 主区聊天 + 来源引用 + 分阶段进度。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st

from src.rag.pipeline import RAGPipeline

st.set_page_config(page_title="原神AI攻略助手", page_icon="🧭")

_STAGE_LABEL = {
    "embed": "向量化问题…",
    "retrieve": "检索知识库…",
    "generate": "模型生成回答…",
}


@st.cache_resource(show_spinner=False)
def get_pipeline():
    """缓存管道：OpenAI 客户端与 FAISS 索引跨会话复用，
    避免每条消息都重建连接（冷启动 embed 一次 6 秒+）。"""
    return RAGPipeline()


try:
    pipeline = get_pipeline()
    kb_size = pipeline.store.size()
except Exception as e:
    st.error(f"初始化失败：{e}\n请检查 config/.env 中的密钥配置。")
    st.stop()

st.sidebar.title("🧭 原神AI攻略助手")
st.sidebar.markdown(f"**知识库条目：** {kb_size} 条")
st.sidebar.markdown("**模型：** deepseek-v4-flash")
st.sidebar.markdown("**Embedding：** qwen3.7-text-embedding")
st.sidebar.markdown("---")
st.sidebar.caption("仅支持攻略问答 · 来源均标注")

st.title("原神攻略问答")

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        for src in msg.get("sources", []):
            st.caption(f"📎 [{src['platform']}] {src['title']} — {src['source_url']}")

if prompt := st.chat_input("问点什么，比如：胡桃的圣遗物怎么搭配？"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        box = st.empty()
        with st.status("处理中…", expanded=True) as status:
            text = ""
            for kind, payload in pipeline.ask_stream(prompt):
                if kind == "stage":
                    status.update(label=_STAGE_LABEL.get(payload, payload))
                elif kind == "text":
                    text += payload
                    box.markdown(text + "▌")
                else:  # done
                    result = payload
            status.update(label="完成", state="complete")
        box.markdown(result["answer"])

        for src in result["sources"]:
            st.caption(
                f"📎 [{src['platform']}] {src['title']} — {src['source_url']}"
                + (f"（{src['game_version']}）" if src["game_version"] else "")
            )
        s = result.get("stages_ms", {})
        parts = [f"⏱️ 总耗时 {result['latency_ms']} ms"]
        if "retrieve" in s:
            parts.append(f"检索 {s['retrieve']} ms")
        if "generate" in s:
            parts.append(f"生成 {s['generate']} ms")
        parts.append(f"命中 {result['hit_count']} 条")
        st.caption(" · ".join(parts))

    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": result["answer"],
            "sources": result["sources"],
        }
    )
