import asyncio
import io
import struct
import wave
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.datastructures import UploadFile as StarletteUploadFile

from errors import APIError
from routers import audio
from utils import concatenate_wav_streams, split_text_for_tts


def make_wav(rate=16000, channels=1, width=2, frames=8):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(b"\x01" * channels * width * frames)
    return output.getvalue()


@pytest.mark.parametrize("text", [
    "汉" * 401,
    "An English sentence. " * 40,
    "第一句。\n第二句！\r\n" * 40,
    "x" * 149 + "." + "y" * 151,
    "  " + "😀" * 301 + "\n  ",
    "\n" * 301,
    "",
])
def test_tts_split_preserves_text_and_enforces_limit(text):
    chunks = split_text_for_tts(text)
    assert "".join(chunks) == text
    assert all(0 < len(chunk) <= 150 for chunk in chunks)


def test_tts_split_prefers_english_sentence_boundary():
    assert split_text_for_tts("First. " + "x" * 151)[0] == "First."


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_tts_split_rejects_invalid_limit(limit):
    with pytest.raises(ValueError):
        split_text_for_tts("text", limit)


def test_wav_join_preserves_all_frames_and_validates_single_part():
    first = make_wav(frames=7)
    second = make_wav(frames=11)
    assert concatenate_wav_streams([first]) == first
    with wave.open(io.BytesIO(concatenate_wav_streams([first, second])), "rb") as wav:
        assert wav.getnframes() == 18
        assert wav.getframerate() == 16000
        assert wav.readframes(18) == b"\x01" * 36


@pytest.mark.parametrize("second", [
    make_wav(rate=8000), make_wav(channels=2), make_wav(width=1),
])
def test_wav_join_rejects_mismatched_pcm_formats(second):
    with pytest.raises(APIError):
        concatenate_wav_streams([make_wav(), second])


def invalid_wavs():
    valid = make_wav()
    bad_encoding = bytearray(valid)
    struct.pack_into("<H", bad_encoding, 20, 3)
    bad_alignment = bytearray(valid)
    struct.pack_into("<H", bad_alignment, 32, 4)
    partial_frame = bytearray(valid[:-1])
    struct.pack_into("<I", partial_frame, 4, len(partial_frame) - 8)
    struct.pack_into("<I", partial_frame, 40, len(partial_frame) - 44)
    return [b"not audio", valid[:-2], bytes(bad_encoding), bytes(bad_alignment),
            bytes(partial_frame), make_wav(frames=0)]


@pytest.mark.parametrize("bad", invalid_wavs())
def test_wav_join_never_returns_invalid_or_partial_success(bad):
    for parts in ([bad], [make_wav(), bad], [bad, make_wav()]):
        with pytest.raises(APIError):
            concatenate_wav_streams(parts)


def test_wav_join_rejects_no_audio():
    with pytest.raises(APIError):
        concatenate_wav_streams([])


class AudioService:
    def __init__(self):
        self.cfg = SimpleNamespace(max_tts_chars=20000, max_audio_upload_bytes=25 * 1024 * 1024)
        self.calls = []
        self.error = None

    async def synthesize_speech(self, text, current_token=None, voice_type="mandarin"):
        self.calls.append(("tts", text, current_token, voice_type))
        if self.error:
            raise self.error
        return make_wav()

    async def transcribe_audio(self, content, filename, content_type, current_token=None):
        self.calls.append(("asr", content, filename, content_type, current_token))
        if self.error:
            raise self.error
        return "测试转写"


@pytest.fixture
def endpoint(monkeypatch):
    service = AudioService()
    app = FastAPI()
    app.state.service = service
    app.include_router(audio.router)
    monkeypatch.setattr(audio, "get_upstream_token", lambda request, service: None)

    def post(path, **kwargs):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.post(path, **kwargs)
        return asyncio.run(run())

    return service, post


@pytest.mark.parametrize("payload,param", [
    ([], "body"), (None, "body"), ({"input": 123}, "input"),
    ({"input": None}, "input"), ({"input": " "}, "input"),
    ({"input": "hi", "voice": None}, "voice"),
    ({"input": "hi", "voice": "unknown"}, "voice"),
    ({"input": "hi", "model": "whisper-1"}, "model"),
    ({"input": "hi", "response_format": "mp3"}, "response_format"),
    ({"input": "hi", "response_format": "pcm"}, "response_format"),
    ({"input": "hi", "speed": 2}, "speed"),
    ({"input": "hi", "speed": True}, "speed"),
    ({"input": "hi", "instructions": "whisper"}, "instructions"),
])
def test_tts_invalid_parameters_are_400_without_upstream(endpoint, payload, param):
    service, post = endpoint
    response = post("/v1/audio/speech", json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["param"] == param
    assert not service.calls


def test_tts_limit_uses_service_configuration(endpoint):
    service, post = endpoint
    service.cfg.max_tts_chars = 3
    assert post("/v1/audio/speech", json={"input": "four"}).status_code == 400
    assert not service.calls


@pytest.mark.parametrize("voice,expected", [(None, "mandarin"), ("shanghainese", "shanghainese"), ("Mandarin", "mandarin"), ("alloy", "mandarin")])
def test_tts_retains_wav_defaults_and_explicit_aliases(endpoint, voice, expected):
    service, post = endpoint
    payload = {"input": "测试", "model": "tts-1-hd"}
    if voice is not None:
        payload["voice"] = voice
    response = post("/v1/audio/speech", json=payload)
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content == make_wav()
    assert service.calls == [("tts", "测试", None, expected)]


@pytest.mark.parametrize("path,kwargs", [
    ("/v1/audio/speech", {"json": {"input": "hello"}}),
    ("/v1/audio/transcriptions", {"files": {"file": ("audio.wav", make_wav(), "audio/wav")}}),
])
def test_audio_preserves_structured_upstream_errors(endpoint, path, kwargs):
    service, post = endpoint
    service.error = APIError("Quota exhausted.", status_code=429, headers={"Retry-After": "30"})
    response = post(path, **kwargs)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"
    assert response.json()["error"]["message"] == "Quota exhausted."


@pytest.mark.parametrize("path,kwargs", [
    ("/v1/audio/speech", {"json": {"input": "hello"}}),
    ("/v1/audio/transcriptions", {"files": {"file": ("audio.wav", make_wav(), "audio/wav")}}),
])
def test_audio_unexpected_errors_do_not_expose_credentials(endpoint, caplog, path, kwargs):
    service, post = endpoint
    service.error = RuntimeError("private-token-must-not-leak")
    response = post(path, **kwargs)
    assert response.status_code == 502
    assert "private-token-must-not-leak" not in response.text
    assert "private-token-must-not-leak" not in caplog.text


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_asr_supports_json_and_text(endpoint, fmt):
    service, post = endpoint
    response = post("/v1/audio/transcriptions", files={"file": ("audio.wav", make_wav(), "audio/wav")},
                    data={"model": "whisper-1", "response_format": fmt})
    assert response.status_code == 200
    assert (response.text if fmt == "text" else response.json()["text"]) == "测试转写"
    assert service.calls[0][-1] is None


@pytest.mark.parametrize("fields", [
    {"model": "tts-1"}, {"response_format": "srt"}, {"language": "en"},
])
def test_asr_rejects_unsupported_parameters(endpoint, fields):
    service, post = endpoint
    response = post("/v1/audio/transcriptions", files={"file": ("audio.wav", make_wav(), "audio/wav")}, data=fields)
    assert response.status_code == 400
    assert not service.calls


@pytest.mark.parametrize("name,content,mime", [
    ("audio.wav", b"", "audio/wav"),
    ("notes.txt", b"hello", "text/plain"),
    ("audio.wav", make_wav(), "text/plain"),
])
def test_asr_rejects_empty_or_unsupported_file_and_closes_it(endpoint, monkeypatch, name, content, mime):
    service, post = endpoint
    closed = []
    original_close = StarletteUploadFile.close

    async def close(file):
        await original_close(file)
        closed.append(file.file.closed)

    monkeypatch.setattr(StarletteUploadFile, "close", close)
    response = post("/v1/audio/transcriptions", files={"file": (name, content, mime)})
    assert response.status_code == 400
    assert not service.calls
    assert closed and all(closed)


def test_asr_upload_limit_reads_only_limit_plus_one_and_closes(endpoint, monkeypatch):
    service, post = endpoint
    service.cfg.max_audio_upload_bytes = 100000
    reads, closed = [], []
    original_read, original_close = StarletteUploadFile.read, StarletteUploadFile.close

    async def read(file, size=-1):
        result = await original_read(file, size)
        reads.append((size, len(result)))
        return result

    async def close(file):
        await original_close(file)
        closed.append(file.file.closed)

    monkeypatch.setattr(StarletteUploadFile, "read", read)
    monkeypatch.setattr(StarletteUploadFile, "close", close)
    response = post("/v1/audio/transcriptions", files={"file": ("audio.wav", b"x" * 200000, "audio/wav")})
    assert response.status_code == 413
    assert sum(size for _, size in reads) == 100001
    assert all(0 < requested <= 65536 for requested, _ in reads)
    assert not service.calls
    assert closed and all(closed)


def test_asr_exact_upload_limit_is_allowed(endpoint):
    service, post = endpoint
    content = make_wav()
    service.cfg.max_audio_upload_bytes = len(content)
    response = post("/v1/audio/transcriptions", files={"file": ("audio.wav", content, "application/octet-stream")})
    assert response.status_code == 200
    assert service.calls[0][1] == content
