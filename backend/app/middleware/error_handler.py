"""
Error handling middleware for better error management and logging
"""
import traceback
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from app.settings.logging_config import get_logger
from app.settings.config import settings

logger = get_logger(__name__)

class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    """Middleware to handle and log errors consistently"""
    
    async def dispatch(self, request: Request, call_next):
        try:
            response = await call_next(request)
            return response
        except HTTPException as e:
            # Log HTTP exceptions for monitoring
            logger.warning(
                f"HTTP Exception: {e.status_code} - {e.detail}",
                extra={
                    "status_code": e.status_code,
                    "detail": e.detail,
                    "path": request.url.path,
                    "method": request.method,
                    "client_ip": request.client.host if request.client else None,
                    "user_agent": request.headers.get("user-agent"),
                }
            )
            raise e
        except Exception as e:
            # Log unexpected errors
            error_id = f"error_{hash(str(e) + str(request.url.path))}"
            
            logger.error(
                f"Unhandled exception [{error_id}]: {str(e)}",
                extra={
                    "error_id": error_id,
                    "error_type": type(e).__name__,
                    "path": request.url.path,
                    "method": request.method,
                    "client_ip": request.client.host if request.client else None,
                    "user_agent": request.headers.get("user-agent"),
                    "traceback": traceback.format_exc() if settings.DEBUG else None,
                }
            )
            
            # Return appropriate error response
            if settings.DEBUG:
                return JSONResponse(
                    status_code=500,
                    content={
                        "error": "Internal Server Error",
                        "detail": str(e),
                        "error_id": error_id,
                        "traceback": traceback.format_exc()
                    }
                )
            else:
                return JSONResponse(
                    status_code=500,
                    content={
                        "error": "Internal Server Error",
                        "detail": "An unexpected error occurred. Please try again later.",
                        "error_id": error_id
                    }
                )

def add_error_handler(app):
    """Add error handling middleware to the app"""
    app.add_middleware(ErrorHandlerMiddleware)