"""全局配置加载：settings.yaml + config/.env"""
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"

# 加载密钥文件（不存在则忽略，靠环境变量兜底）
load_dotenv(CONFIG_DIR / ".env")


def load_settings() -> dict:
    """读取 config/settings.yaml，返回 dict。"""
    with open(CONFIG_DIR / "settings.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_path(rel: str) -> Path:
    """把 settings.yaml 里的相对路径解析为项目根下的绝对路径。"""
    return PROJECT_ROOT / rel
