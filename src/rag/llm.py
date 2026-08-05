"""LLM 封装：DeepSeek（OpenAI 兼容）+ 系统提示词模板。"""
import os

from openai import OpenAI

from src.settings import load_settings

SYSTEM_PROMPT = """你是原神攻略助手。请严格依据【参考资料】回答用户问题。
规则：
1. 回答必须基于参考资料，包含具体数值/条件/步骤；资料不足时明确说"知识库中暂无该信息"，禁止编造。
2. 用引用标记 [1][2] 标注信息来源，回答末尾列出"来源"列表（作者/平台/链接）。
3. 如资料涉及版本信息，回答中标注适用版本。
4. 保持中文回答，结构清晰，简洁直接，不要长篇大论。
5. 拒绝回答以下内容并明确说明无法提供：外挂、私服、代充、账号交易、盗号、辱骂他人、色情、暴力、违法及政治敏感内容。正常游戏攻略问题（养成、解密、宝箱、成就等）不受影响。"""


class DeepSeekLLM:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or load_settings()["llm"]
        self.model = cfg["model"]
        self.temperature = cfg["temperature"]
        self.max_tokens = cfg["max_tokens"]
        self.reasoning_effort = cfg.get("reasoning_effort")
        api_key = os.environ.get(cfg["api_key_env"])
        if not api_key:
            raise ValueError(
                f"缺少环境变量 {cfg['api_key_env']}，请复制 .env.example 为 config/.env 并填入 Key"
            )
        self.client = OpenAI(api_key=api_key, base_url=cfg["base_url"])

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        kwargs = dict(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        # v4-flash 支持 reasoning_effort：low 显著加快响应，high 深度推理
        if self.reasoning_effort:
            kwargs["extra_body"] = {"reasoning_effort": self.reasoning_effort}
        resp = self.client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        content = (choice.message.content or "").strip()
        if not content:
            if choice.finish_reason == "length":
                raise ValueError("回答被长度截断（模型思考时间过长），已调大 max_tokens，请重试")
            raise ValueError("模型返回了空回答，请重试")
        return content

    def generate_stream(self, system_prompt: str, user_prompt: str):
        """流式版 generate：逐段 yield 最终回答内容（跳过模型的思考过程）。"""
        kwargs = dict(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            stream=True,
        )
        if self.reasoning_effort:
            kwargs["extra_body"] = {"reasoning_effort": self.reasoning_effort}
        parts: list[str] = []
        for chunk in self.client.chat.completions.create(**kwargs):
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None) or ""
            if piece:
                parts.append(piece)
                yield piece
        if not "".join(parts).strip():
            raise ValueError("模型返回了空回答，请重试")
