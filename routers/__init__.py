from routers.models import router as models_router
from routers.chat import router as chat_router
from routers.audio import router as audio_router
from routers.dashboard import router as dashboard_router

__all__ = ["models_router", "chat_router", "audio_router", "dashboard_router"]