import ipaddress
import logging
import os
from dataclasses import dataclass
from typing import Optional, Tuple

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("fanzha-proxy")


def clean_token(token: Optional[str]) -> str:
    if not token:
        return ""
    value = str(token).strip()
    parts = value.split(None, 1)
    if parts and parts[0].lower() == "bearer":
        return parts[1].strip() if len(parts) == 2 else ""
    return value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"true", "1"}


def env_positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be a positive integer") from None
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host.strip("[]"))
        return address.is_loopback or bool(getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback)
    except ValueError:
        return False


@dataclass(frozen=True)
class AppConfig:
    base_url: str = os.getenv("FANZHA_BASE_URL", "https://xzfzznt.gaj.sh.gov.cn").rstrip("/")
    access_token: str = clean_token(os.getenv("FANZHA_ACCESS_TOKEN", ""))
    refresh_token: str = clean_token(os.getenv("FANZHA_REFRESH_TOKEN", ""))
    default_model: str = os.getenv("DEFAULT_MODEL", "fanzha-ai")
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = env_positive_int("PORT", 8088)
    trust_env_proxy: bool = env_bool("TRUST_ENV_PROXY")
    proxy_api_key: str = os.getenv("PROXY_API_KEY", "").strip()
    cors_origins: Tuple[str, ...] = tuple(x.strip().rstrip("/") for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip())
    token_state_file: str = os.getenv("TOKEN_STATE_FILE", "").strip()
    max_request_body_bytes: int = env_positive_int("MAX_REQUEST_BODY_BYTES", 32 * 1024 * 1024)
    max_audio_upload_bytes: int = env_positive_int("MAX_AUDIO_UPLOAD_BYTES", 25 * 1024 * 1024)
    max_upstream_response_bytes: int = env_positive_int("MAX_UPSTREAM_RESPONSE_BYTES", 8 * 1024 * 1024)
    max_chat_chars: int = env_positive_int("MAX_CHAT_CHARS", 128000)
    max_tts_chars: int = env_positive_int("MAX_TTS_CHARS", 20000)
    require_stream_done: bool = env_bool("UPSTREAM_REQUIRE_DONE")

    def validate_server(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("PORT must be between 1 and 65535")
        if not is_loopback(self.host) and not self.proxy_api_key:
            raise ValueError("Set PROXY_API_KEY before listening on a non-loopback HOST")
        if "*" in self.cors_origins:
            raise ValueError("CORS_ORIGINS requires explicit trusted origins, not '*'")


config = AppConfig()
