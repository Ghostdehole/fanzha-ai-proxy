"""Safe, consistent OpenAI-shaped errors shared by routes and upstream adapters."""
from typing import Dict, Optional

from fastapi.responses import JSONResponse


class APIError(Exception):
    def __init__(self, message: str, status_code: int = 502, err_type: str = "upstream_error", param: Optional[str] = None, headers: Optional[Dict[str, str]] = None):
        super().__init__(message)
        # Invalid JSON strings may contain unpaired UTF-16 surrogates. Error
        # responses must remain encodable even when they name such a field.
        self.message = message.encode("utf-8", errors="replace").decode("utf-8")
        self.status_code = status_code
        self.err_type = err_type
        self.param = param.encode("utf-8", errors="replace").decode("utf-8") if param is not None else None
        self.headers = headers or {}

    def as_dict(self) -> dict:
        return {"error": {"message": self.message, "type": self.err_type, "param": self.param, "code": self.status_code}}

    def response(self) -> JSONResponse:
        return JSONResponse(self.as_dict(), status_code=self.status_code, headers={"Cache-Control": "no-store", **self.headers})
