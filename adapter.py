import time
import json
import hashlib
from typing import Any, Dict, List, Optional
from fastapi import Request
from fastapi.responses import JSONResponse
from config import clean_token


class OpenAIAdapter:
    """OpenAI 标准协议转换与适配器"""

    @staticmethod
    def get_auth_token(request: Request, default_token: str) -> str:
        """从请求头提取有效 Token，支持客户端传参覆盖默认 Token。"""
        auth_header = request.headers.get("Authorization", "")
        req_token = clean_token(auth_header)
        if req_token in ("sk-no-key-needed", "") or req_token.startswith("sk-"):
            return default_token
        return req_token

    @staticmethod
    def extract_prompt_and_files(messages: List[Dict[str, Any]]) -> tuple[str, List[Any]]:
        """从 messages 列表中提取最后提问文本及多模态图片。"""
        target_content = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                target_content = msg.get("content")
                break
        if target_content is None and messages:
            target_content = messages[-1].get("content")

        extracted_text_parts: List[str] = []
        extracted_files: List[Any] = []

        if isinstance(target_content, str):
            extracted_text_parts.append(target_content)
        elif isinstance(target_content, list):
            for item in target_content:
                if isinstance(item, str):
                    extracted_text_parts.append(item)
                elif isinstance(item, dict):
                    if item.get("type") == "text" or "text" in item:
                        extracted_text_parts.append(item.get("text", ""))
                    elif item.get("type") == "image_url":
                        img_info = item.get("image_url", {})
                        url_val = img_info.get("url") if isinstance(img_info, dict) else str(img_info)
                        if url_val:
                            extracted_files.append(url_val)

        return "\n".join(filter(None, extracted_text_parts)), extracted_files

    @staticmethod
    def compute_conversation_anchor(messages: List[Dict[str, Any]]) -> str:
        """根据对话首条消息生成哈希锚点，维持多轮会话的连续性。"""
        first_user_prompt = ""
        for msg in messages:
            if msg.get("role") == "user":
                text, _ = OpenAIAdapter.extract_prompt_and_files([msg])
                first_user_prompt = text
                break
        if not first_user_prompt and messages:
            text, _ = OpenAIAdapter.extract_prompt_and_files([messages[0]])
            first_user_prompt = text

        return hashlib.md5(first_user_prompt.encode("utf-8")).hexdigest()

    @staticmethod
    def parse_sse_line(raw_json: str) -> Optional[str]:
        """解析上游反诈 SSE 数据帧中的文本。"""
        try:
            event = json.loads(raw_json)
            if not isinstance(event, dict):
                return None
            data_node = event.get("data")
            if isinstance(data_node, dict):
                return data_node.get("answer") or data_node.get("content") or data_node.get("text")
            elif isinstance(data_node, str):
                return data_node
            return event.get("answer") or event.get("content") or event.get("text")
        except Exception:
            return None

    @staticmethod
    def format_chunk(chat_id: str, model: str, delta_dict: dict, finish_reason: Optional[str] = None) -> str:
        """组装 OpenAI 标准流式 Chunk。"""
        chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta_dict, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    @staticmethod
    def error_response(message: str, status_code: int = 400, err_type: str = "invalid_request_error"):
        """统一返回 OpenAI 格式错误响应。"""
        return JSONResponse(
            status_code=status_code,
            content={"error": {"message": message, "type": err_type, "param": None, "code": status_code}},
        )