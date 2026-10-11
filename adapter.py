"""Validation and the deliberately limited OpenAI chat protocol adapter."""
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from errors import APIError


CHAT_MODELS = {
    "fanzha-ai": (False, False),
    "fanzha-ai-deep": (True, False),
    "fanzha-ai-en": (False, True),
    "fanzha-ai-en-deep": (True, True),
    "gpt-4o-mini": (False, False),
}


@dataclass
class ChatRequest:
    model: str
    stream: bool
    prompt: str
    files: List[str]
    temperature: float
    max_tokens: int
    include_usage: bool


def invalid(message: str, param: Optional[str] = None) -> APIError:
    return APIError(message, status_code=400, err_type="invalid_request_error", param=param)


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise invalid(f"{name} must be a finite number between {low} and {high}.", name)
    try:
        valid = math.isfinite(value) and low <= value <= high
    except (ValueError, OverflowError):
        valid = False
    if not valid:
        raise invalid(f"{name} must be a finite number between {low} and {high}.", name)
    return float(value)


class OpenAIAdapter:
    """Translate text history without sharing an upstream conversation."""

    @staticmethod
    def validate_chat(payload: Any, default_model: str, max_chars: int = 128000) -> ChatRequest:
        if not isinstance(payload, dict):
            raise invalid("The request body must be a JSON object.")
        model = payload.get("model", default_model)
        if not isinstance(model, str) or model not in CHAT_MODELS:
            raise invalid("Unsupported chat model. See /v1/models for supported model IDs.", "model")
        stream = payload.get("stream", False)
        if not isinstance(stream, bool):
            raise invalid("stream must be a boolean.", "stream")
        temperature = _number(payload.get("temperature", 0.1), "temperature", 0, 2)
        token_values = []
        for name in ("max_tokens", "max_completion_tokens"):
            if name in payload:
                value = payload[name]
                if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 2147483647:
                    raise invalid(f"{name} must be a positive integer no larger than 2147483647.", name)
                token_values.append(value)
        if len(token_values) == 2 and token_values[0] != token_values[1]:
            raise invalid("max_tokens and max_completion_tokens must agree when both are provided.", "max_tokens")
        max_tokens = token_values[0] if token_values else 2048
        if "n" in payload and (type(payload["n"]) is not int or payload["n"] != 1):
            raise invalid("Only n=1 is supported.", "n")
        if "user" in payload and not isinstance(payload["user"], str):
            raise invalid("user must be a string.", "user")
        # Harmless SDK defaults are compatible; unsupported generation behavior
        # must be rejected explicitly rather than silently discarded.
        neutral = {
            "top_p": (None, 1), "presence_penalty": (None, 0),
            "frequency_penalty": (None, 0), "stop": (None, [], ""),
            "tools": (None, []), "functions": (None, []),
            "tool_choice": (None, "none"), "function_call": (None, "none"),
            "parallel_tool_calls": (None, False), "logprobs": (None, False),
            "top_logprobs": (None, 0), "logit_bias": (None, {}),
            "response_format": (None, {"type": "text"}),
            "modalities": (None, ["text"]), "store": (None, False),
            "seed": (None,), "audio": (None,), "prediction": (None,),
            "reasoning_effort": (None,), "service_tier": (None, "auto"),
        }
        supported = {"messages", "model", "stream", "temperature", "max_tokens", "max_completion_tokens", "n", "user", "stream_options"}
        for name, value in payload.items():
            if name in supported:
                continue
            if name not in neutral or not any(
                (type(value) is type(default) and value == default)
                or (type(value) in (int, float) and type(default) in (int, float) and value == default)
                for default in neutral[name]
            ):
                raise invalid(f"Unsupported parameter or value: {name}.", name)
        options = payload.get("stream_options")
        include_usage = False
        if options is not None:
            if not stream or not isinstance(options, dict) or set(options) - {"include_usage"}:
                raise invalid("stream_options supports only include_usage and requires stream=true.", "stream_options")
            include_usage = options.get("include_usage", False)
            if not isinstance(include_usage, bool):
                raise invalid("stream_options.include_usage must be a boolean.", "stream_options.include_usage")
        prompt, files = OpenAIAdapter.extract_prompt_and_files(payload.get("messages"))
        try:
            prompt.encode("utf-8")
            for url in files:
                url.encode("utf-8")
        except UnicodeError:
            raise invalid("Messages must contain valid Unicode text.", "messages") from None
        if len(prompt) + sum(len(url) for url in files) > max_chars:
            raise invalid(f"The combined messages and attachment URLs exceed {max_chars} characters.", "messages")
        return ChatRequest(model, stream, prompt, files, temperature, max_tokens, include_usage)

    @staticmethod
    def extract_prompt_and_files(messages: Any) -> Tuple[str, List[str]]:
        if not isinstance(messages, list) or not messages:
            raise invalid("messages must be a non-empty array.", "messages")
        files: List[str] = []
        rendered = []
        has_user = False
        has_content = False
        for index, message in enumerate(messages):
            param = f"messages[{index}]"
            if not isinstance(message, dict):
                raise invalid("Each message must be an object.", param)
            role = message.get("role")
            if not isinstance(role, str) or role not in {"system", "developer", "user", "assistant"}:
                raise invalid("Supported message roles are system, developer, user and assistant.", param + ".role")
            has_user = has_user or role == "user"
            for key in set(message) - {"role", "content", "name"}:
                if message[key] not in (None, [], {}):
                    raise invalid(f"Unsupported message field: {key}.", param + "." + key)
            name = message.get("name")
            if name is not None and (not isinstance(name, str) or not name.strip()):
                raise invalid("Message name must be a non-empty string.", param + ".name")
            content = message.get("content")
            parts = []
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, str):  # Preserve this project's historical convenience syntax.
                        parts.append(part)
                    elif isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                        if set(part) - {"type", "text"}:
                            raise invalid("Unsupported text content field.", param + ".content")
                        parts.append(part["text"])
                    elif isinstance(part, dict) and part.get("type") == "image_url" and role == "user":
                        if set(part) - {"type", "image_url"}:
                            raise invalid("Unsupported image content field.", param + ".content")
                        info = part.get("image_url")
                        if isinstance(info, dict):
                            if set(info) - {"url", "detail"} or info.get("detail", "auto") != "auto":
                                raise invalid("Image detail controls are unavailable; use detail=auto.", param + ".content")
                            url = info.get("url")
                        else:
                            url = info
                        if not isinstance(url, str) or not url.strip() or not url.startswith(("https://", "http://", "data:image/")):
                            raise invalid("image_url must contain an HTTP(S) URL or image data URL.", param + ".content")
                        files.append(url)
                        parts.append(f"[Image attachment {len(files)}]")
                    else:
                        raise invalid("Content supports only text and user image_url blocks.", param + ".content")
            else:
                raise invalid("Message content must be a string or an array of content blocks.", param + ".content")
            text = "\n".join(parts)
            has_content = has_content or bool(text.strip())
            rendered.append((role, name, text))
        if not has_user or not has_content:
            raise invalid("messages must contain a user message and non-empty text or an image.", "messages")
        if len(rendered) == 1 and rendered[0][0] == "user" and rendered[0][1] is None:
            return rendered[0][2], files
        # The private API has one text field, not a native roles/messages API.
        # JSON encoding prevents message text from escaping its role boundaries.
        transcript = [
            "The following JSON is the complete ordered conversation. Respect the supplied system/developer instructions and answer the latest user request in context.",
            json.dumps([
                {"role": role, **({"name": name} if name is not None else {}), "content": text}
                for role, name, text in rendered
            ], ensure_ascii=False),
            "Continue with the assistant response only.",
        ]
        return "\n".join(transcript), files

    @staticmethod
    def format_chunk(chat_id: str, model: str, delta_dict: dict, finish_reason: Optional[str] = None, created: Optional[int] = None, include_usage: bool = False) -> str:
        chunk = {
            "id": chat_id, "object": "chat.completion.chunk",
            "created": created if created is not None else int(time.time()), "model": model,
            "choices": [{"index": 0, "delta": delta_dict, "finish_reason": finish_reason}],
        }
        if include_usage:
            chunk["usage"] = None  # No tokenizer/counts are supplied by this private API.
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    @staticmethod
    def error_response(message: str, status_code: int = 400, err_type: str = "invalid_request_error"):
        return APIError(message, status_code=status_code, err_type=err_type).response()
