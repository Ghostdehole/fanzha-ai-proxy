from fastapi import APIRouter, Request, Response, UploadFile, File, Form, status
from fastapi.responses import JSONResponse
from config import logger
from client import AntiFraudClient
from adapter import OpenAIAdapter

router = APIRouter(tags=["Audio"])


@router.post("/v1/audio/transcriptions")
async def transcribe_audio(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form("whisper-1")
):
    """OpenAI 标准 ASR 语音识别转文本"""
    service: AntiFraudClient = request.app.state.service
    token = OpenAIAdapter.get_auth_token(request, service.access_token)

    if not token:
        return OpenAIAdapter.error_response("Missing access token.", status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        file_content = await file.read()
        transcribed_text = await service.transcribe_audio(
            file_content, file.filename, file.content_type or "audio/mpeg", token
        )
        return JSONResponse(content={"text": transcribed_text})
    except Exception as e:
        logger.error(f"ASR 转写异常: {e}")
        return OpenAIAdapter.error_response(f"ASR error: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.post("/v1/audio/speech")
async def text_to_speech(request: Request):
    """OpenAI 标准 TTS 语音合成，支持普通话、方言及英文"""
    service: AntiFraudClient = request.app.state.service
    token = OpenAIAdapter.get_auth_token(request, service.access_token)

    if not token:
        return OpenAIAdapter.error_response("Missing access token.", status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        payload = await request.json()
    except Exception:
        return OpenAIAdapter.error_response("Invalid JSON payload.", status_code=status.HTTP_400_BAD_REQUEST)

    input_text = str(payload.get("input", "")).strip()
    if not input_text:
        return OpenAIAdapter.error_response("Missing 'input' field.", status_code=status.HTTP_400_BAD_REQUEST)

    # 涵盖官方支持的音色及方言
    voice = payload.get("voice", "mandarin").lower()
    valid_voices = {"mandarin", "shanghainese", "dongbei", "guangxi", "chongqing", "english"}
    if voice not in valid_voices:
        voice = "mandarin"

    try:
        audio_bytes = await service.synthesize_speech(input_text, token, voice)
        if not audio_bytes:
            return OpenAIAdapter.error_response("TTS synthesis returned empty audio.", status_code=status.HTTP_502_BAD_GATEWAY)
        return Response(content=audio_bytes, media_type="audio/wav")
    except Exception as e:
        logger.error(f"TTS 合成异常: {e}")
        return OpenAIAdapter.error_response(f"TTS error: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)