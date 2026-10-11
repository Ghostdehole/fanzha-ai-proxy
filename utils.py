import io
import re
import struct
import wave
from typing import List, Tuple

from errors import APIError


def split_text_for_tts(text: str, max_chars: int = 150) -> List[str]:
    """Prefer sentence boundaries, with a hard length limit and no lost text."""
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundaries = list(re.finditer(r"[。！？；!?;.\r\n]", text[start:end]))
            if boundaries:
                end = start + boundaries[-1].end()
        chunks.append(text[start:end])
        start = end
    return chunks


def _read_pcm_wav(data: bytes, part_number: int) -> Tuple[tuple, bytes]:
    """Validate RIFF lengths as well as the PCM header before reading frames."""
    message = f"TTS audio part {part_number} is invalid or incomplete PCM WAV."
    try:
        if not isinstance(data, bytes) or len(data) < 12:
            raise ValueError("Missing WAV header")
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise ValueError("Expected RIFF/WAVE")
        if struct.unpack_from("<I", data, 4)[0] + 8 != len(data):
            raise ValueError("Inconsistent RIFF length")

        offset = 12
        fmt = None
        audio_size = None
        while offset < len(data):
            if offset + 8 > len(data):
                raise ValueError("Truncated chunk header")
            chunk_id = data[offset:offset + 4]
            chunk_size = struct.unpack_from("<I", data, offset + 4)[0]
            begin = offset + 8
            end = begin + chunk_size
            if end > len(data):
                raise ValueError("Truncated chunk")
            if chunk_id == b"fmt ":
                if fmt is not None or chunk_size < 16:
                    raise ValueError("Invalid format chunk")
                fmt = struct.unpack_from("<HHIIHH", data, begin)
            elif chunk_id == b"data":
                if audio_size is not None or fmt is None:
                    raise ValueError("Invalid data chunk")
                audio_size = chunk_size
            # Some PCM writers omit a final odd-byte pad. No sample is lost.
            offset = end + (chunk_size % 2 if end < len(data) else 0)

        if offset != len(data) or fmt is None or not audio_size:
            raise ValueError("Missing complete audio data")
        encoding, channels, rate, byte_rate, block_align, bits = fmt
        if encoding != 1 or channels < 1 or rate < 1 or bits not in (8, 16, 24, 32):
            raise ValueError("Unsupported PCM format")
        if block_align != channels * (bits // 8) or byte_rate != rate * block_align:
            raise ValueError("Inconsistent PCM format")
        if audio_size % block_align:
            raise ValueError("Partial PCM frame")
        with wave.open(io.BytesIO(data), "rb") as wav:
            params = (wav.getnchannels(), wav.getsampwidth(), wav.getframerate())
            frames = wav.readframes(wav.getnframes())
        if len(frames) != audio_size:
            raise ValueError("Incomplete PCM frames")
        return params, frames
    except (ValueError, EOFError, struct.error, wave.Error) as exc:
        raise APIError(message, status_code=502) from exc


def concatenate_wav_streams(wav_bytes_list: List[bytes]) -> bytes:
    """Join complete PCM WAV parts; reject any invalid or incompatible part."""
    if not wav_bytes_list:
        raise APIError("TTS synthesis returned empty audio.", status_code=502)

    decoded = [_read_pcm_wav(part, i) for i, part in enumerate(wav_bytes_list, 1)]
    params = decoded[0][0]
    if any(part_params != params for part_params, _ in decoded[1:]):
        raise APIError("TTS audio parts have incompatible PCM formats.", status_code=502)
    if len(wav_bytes_list) == 1:
        return wav_bytes_list[0]

    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(params[0])
        wav.setsampwidth(params[1])
        wav.setframerate(params[2])
        for _, frames in decoded:
            wav.writeframes(frames)
    return output.getvalue()
