"""
Security middleware for rate limiting and request validation
"""
import time
from collections import defaultdict, deque
from typing import Dict, Deque
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from app.settings.logging_config import get_logger

logger = get_logger(__name__)

class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory rate limiting middleware"""
    
    def __init__(self, app, requests_per_minute: int = 60, requests_per_hour: int = 1000):
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self.requests_per_hour = requests_per_hour
        self.minute_requests: Dict[str, Deque[float]] = defaultdict(deque)
        self.hour_requests: Dict[str, Deque[float]] = defaultdict(deque)
    
    def _get_client_ip(self, request: Request) -> str:
        """Get client IP address"""
        # Check for forwarded headers first (for reverse proxy setups)
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip
        
        return request.client.host if request.client else "unknown"
    
    def _cleanup_old_requests(self, request_times: Deque[float], window_seconds: int):
        """Remove requests older than the time window"""
        current_time = time.time()
        while request_times and current_time - request_times[0] > window_seconds:
            request_times.popleft()
    
    def _is_rate_limited(self, client_ip: str) -> bool:
        """Check if client is rate limited"""
        current_time = time.time()
        
        # Clean up old requests
        self._cleanup_old_requests(self.minute_requests[client_ip], 60)
        self._cleanup_old_requests(self.hour_requests[client_ip], 3600)
        
        # Check minute limit
        if len(self.minute_requests[client_ip]) >= self.requests_per_minute:
            return True
        
        # Check hour limit
        if len(self.hour_requests[client_ip]) >= self.requests_per_hour:
            return True
        
        # Add current request
        self.minute_requests[client_ip].append(current_time)
        self.hour_requests[client_ip].append(current_time)
        
        return False
    
    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for health checks and static files
        if request.url.path in ["/health", "/docs", "/redoc", "/openapi.json"]:
            return await call_next(request)
        
        client_ip = self._get_client_ip(request)
        
        if self._is_rate_limited(client_ip):
            logger.warning(
                f"Rate limit exceeded for IP: {client_ip}",
                extra={
                    "client_ip": client_ip,
                    "path": request.url.path,
                    "method": request.method,
                }
            )
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded. Please try again later.",
                headers={"Retry-After": "60"}
            )
        
        return await call_next(request)

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to responses"""
    
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        
        # Add security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        
        # Add CSP header for non-API routes
        if not request.url.path.startswith("/api/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https:; "
                "font-src 'self' data:; "
                "connect-src 'self' ws: wss:;"
            )
        
        return response

def add_security_middleware(app, enable_rate_limiting: bool = True):
    """Add security middleware to the app"""
    if enable_rate_limiting:
        app.add_middleware(RateLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)