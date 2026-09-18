import os
import logging
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 配置标准日志格式
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("fanzha-proxy")


def clean_token(token: Optional[str]) -> str:
    """清理 Token 字符串，去除空白符和 Bearer 前缀。"""
    if not token:
        return ""
    token = str(token).strip()
    return token[7:].strip() if token.startswith("Bearer ") else token


@dataclass(frozen=True)
class AppConfig:
    """全局配置容器"""
    base_url: str = os.getenv("FANZHA_BASE_URL", "https://xzfzznt.gaj.sh.gov.cn")
    access_token: str = clean_token(os.getenv("FANZHA_ACCESS_TOKEN", ""))
    refresh_token: str = clean_token(os.getenv("FANZHA_REFRESH_TOKEN", ""))
    default_model: str = os.getenv("DEFAULT_MODEL", "fanzha-ai")
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8088"))
    trust_env_proxy: bool = os.getenv("TRUST_ENV_PROXY", "false").lower() == "true"


config = AppConfig()