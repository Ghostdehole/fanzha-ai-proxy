import io
import re
import wave
from typing import List
from config import logger


def split_text_for_tts(text: str, max_chars: int = 150) -> List[str]:
    """将长文本按标点断句，适配上游 TTS 长度限制。"""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    parts = re.split(r"(\n+|[。！？；!?;])", text)
    chunks = []
    current = ""
    for part in parts:
        if not part:
            continue
        if len(current) + len(part) <= max_chars:
            current += part
        else:
            if current.strip():
                chunks.append(current.strip())
            current = part
    if current.strip():
        chunks.append(current.strip())
    return chunks if chunks else [text[:max_chars]]


def concatenate_wav_streams(wav_bytes_list: List[bytes]) -> bytes:
    """将多个字节流格式的 PCM WAV 音频缝合成单个可播放的 WAV 文件。"""
    if not wav_bytes_list:
        return b""
    if len(wav_bytes_list) == 1:
        return wav_bytes_list[0]

    out_buffer = io.BytesIO()
    try:
        with wave.open(io.BytesIO(wav_bytes_list[0]), "rb") as first_wav:
            params = first_wav.getparams()
            with wave.open(out_buffer, "wb") as out_wav:
                out_wav.setparams(params)
                out_wav.writeframes(first_wav.readframes(first_wav.getnframes()))
                for extra_bytes in wav_bytes_list[1:]:
                    try:
                        with wave.open(io.BytesIO(extra_bytes), "rb") as next_wav:
                            out_wav.writeframes(next_wav.readframes(next_wav.getnframes()))
                    except Exception as e:
                        logger.warning(f"音频切片缝合跳过异常分片: {e}")
    except Exception as e:
        logger.error(f"WAV 缝合处理失败: {e}")
        return wav_bytes_list[0]

    return out_buffer.getvalue()