"""
Input validation middleware
"""
import json
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from app.settings.logging_config import get_logger

logger = get_logger(__name__)

class InputValidationMiddleware(BaseHTTPMiddleware):
    """Middleware to validate and sanitize input data"""
    
    def __init__(self, app, max_request_size: int = 10 * 1024 * 1024):  # 10MB default
        super().__init__(app)
        self.max_request_size = max_request_size
    
    async def dispatch(self, request: Request, call_next):
        # Check request size
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > self.max_request_size:
            logger.warning(
                f"Request too large: {content_length} bytes from {request.client.host}",
                extra={
                    "content_length": content_length,
                    "client_ip": request.client.host if request.client else None,
                    "path": request.url.path,
                }
            )
            raise HTTPException(
                status_code=413,
                detail="Request entity too large"
            )
        
        # Validate JSON content for POST/PUT/PATCH requests
        if request.method in ["POST", "PUT", "PATCH"]:
            content_type = request.headers.get("content-type", "")
            if "application/json" in content_type:
                try:
                    # Read and validate JSON
                    body = await request.body()
                    if body:
                        json.loads(body.decode())
                        # Recreate request with validated body
                        request._body = body
                except json.JSONDecodeError as e:
                    logger.warning(
                        f"Invalid JSON in request from {request.client.host}: {str(e)}",
                        extra={
                            "client_ip": request.client.host if request.client else None,
                            "path": request.url.path,
                            "error": str(e),
                        }
                    )
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid JSON format"
                    )
                except UnicodeDecodeError:
                    logger.warning(
                        f"Invalid encoding in request from {request.client.host}",
                        extra={
                            "client_ip": request.client.host if request.client else None,
                            "path": request.url.path,
                        }
                    )
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid character encoding"
                    )
        
        return await call_next(request)

def add_validation_middleware(app, max_request_size: int = 10 * 1024 * 1024):
    """Add input validation middleware to the app"""
    app.add_middleware(InputValidationMiddleware, max_request_size=max_request_size)