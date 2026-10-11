from pathlib import PurePosixPath

from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

from auth import get_upstream_token
from config import logger
from errors import APIError

router = APIRouter(tags=["Audio"])

_VOICES = {"mandarin", "shanghainese", "dongbei", "guangxi", "chongqing", "english"}
# Compatibility aliases only: these do not recreate OpenAI's individual voices.
_VOICE_ALIASES = {name: "mandarin" for name in ("alloy", "echo", "fable", "onyx", "nova", "shimmer")}
_AUDIO_EXTENSIONS = {".flac", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".ogg", ".wav", ".webm"}
_AUDIO_MIME_TYPES = {
    "audio/flac", "audio/x-flac", "audio/mpeg", "audio/mp3", "audio/mp4", "audio/m4a",
    "audio/x-m4a", "audio/ogg", "application/ogg", "audio/wav", "audio/x-wav",
    "audio/wave", "audio/vnd.wave", "audio/webm", "video/mp4", "video/mpeg", "video/webm",
}


def _invalid(message: str, param: str, status_code: int = 400) -> APIError:
    return APIError(message, status_code=status_code, err_type="invalid_request_error", param=param)


def _string_choice(value, allowed, param: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _invalid(f"Unsupported '{param}'. Supported values: {', '.join(sorted(allowed))}.", param)
    return value


@router.post("/v1/audio/transcriptions")
async def transcribe_audio(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form("whisper-1"),
    response_format: str = Form("json"),
):
    """Basic ASR: whisper-1 alias with JSON or plain text output."""
    service = request.app.state.service
    try:
        token = get_upstream_token(request, service)
        _string_choice(model, {"whisper-1"}, "model")
        _string_choice(response_format, {"json", "text"}, "response_format")
        form = await request.form()
        unsupported = set(form.keys()) - {"file", "model", "response_format"}
        if unsupported:
            name = sorted(unsupported)[0]
            raise _invalid(f"ASR parameter '{name}' is not supported.", name)
        if any(len(form.getlist(name)) > 1 for name in form.keys()):
            raise _invalid("Duplicate multipart fields are not supported.", "file")

        filename = PurePosixPath((file.filename or "").replace("\\", "/")).name
        extension = PurePosixPath(filename).suffix.lower()
        content_type = (file.content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
        if extension and extension not in _AUDIO_EXTENSIONS:
            raise _invalid("Unsupported audio file extension.", "file")
        if content_type not in _AUDIO_MIME_TYPES:
            if content_type != "application/octet-stream" or extension not in _AUDIO_EXTENSIONS:
                raise _invalid("Unsupported audio file type.", "file")

        limit = service.cfg.max_audio_upload_bytes
        content = bytearray()
        while True:
            chunk = await file.read(min(65536, limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > limit:
                raise _invalid(f"Audio file exceeds the {limit}-byte upload limit.", "file", 413)
        if not content:
            raise _invalid("Audio file must not be empty.", "file")

        text = await service.transcribe_audio(bytes(content), filename or "audio", content_type, token)
        if response_format == "text":
            return PlainTextResponse(content=text)
        return JSONResponse(content={"text": text})
    except APIError as exc:
        return exc.response()
    except Exception as exc:
        logger.error("ASR transcription failed (%s)", type(exc).__name__)
        return APIError("Audio transcription failed.", status_code=502).response()
    finally:
        await file.close()


@router.post("/v1/audio/speech")
async def text_to_speech(request: Request):
    """Basic TTS: WAV, speed 1, and explicit dialect/voice aliases only."""
    service = request.app.state.service
    try:
        token = get_upstream_token(request, service)
        try:
            payload = await request.json()
        except Exception as exc:
            raise _invalid("Invalid JSON payload.", "body") from exc
        if not isinstance(payload, dict):
            raise _invalid("JSON payload must be an object.", "body")
        unsupported = set(payload) - {"input", "voice", "model", "speed", "response_format"}
        if unsupported:
            name = sorted(unsupported)[0]
            raise _invalid(f"TTS parameter '{name}' is not supported.", name)

        input_text = payload.get("input")
        if not isinstance(input_text, str) or not input_text.strip():
            raise _invalid("'input' must be a nonempty string.", "input")
        if len(input_text) > service.cfg.max_tts_chars:
            raise _invalid(f"'input' exceeds the {service.cfg.max_tts_chars}-character limit.", "input")
        _string_choice(payload.get("model", "tts-1"), {"tts-1", "tts-1-hd"}, "model")
        _string_choice(payload.get("response_format", "wav"), {"wav"}, "response_format")
        speed = payload.get("speed", 1)
        if type(speed) not in (int, float) or speed != 1:
            raise _invalid("Only speed=1 is supported.", "speed")
        voice = payload.get("voice", "mandarin")
        if isinstance(voice, str):
            voice = voice.lower()
        voice = _string_choice(voice, _VOICES | set(_VOICE_ALIASES), "voice")
        voice = _VOICE_ALIASES.get(voice, voice)

        audio_bytes = await service.synthesize_speech(input_text.strip(), token, voice)
        if not audio_bytes:
            raise APIError("TTS synthesis returned empty audio.", status_code=502)
        return Response(content=audio_bytes, media_type="audio/wav")
    except APIError as exc:
        return exc.response()
    except Exception as exc:
        logger.error("TTS synthesis failed (%s)", type(exc).__name__)
        return APIError("Audio synthesis failed.", status_code=502).response()
