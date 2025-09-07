"""
Performance monitoring middleware
"""
import time
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from app.settings.logging_config import get_logger

logger = get_logger(__name__)

class PerformanceMonitorMiddleware(BaseHTTPMiddleware):
    """Middleware to monitor request performance"""
    
    def __init__(self, app, slow_request_threshold: float = 1.0):
        super().__init__(app)
        self.slow_request_threshold = slow_request_threshold
    
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        
        response = await call_next(request)
        
        process_time = time.time() - start_time
        
        # Add performance header
        response.headers["X-Process-Time"] = str(process_time)
        
        # Record metrics
        from app.services.monitoring_service import MonitoringService
        MonitoringService.record_request_duration(
            process_time, 
            request.method, 
            request.url.path, 
            response.status_code
        )
        MonitoringService.increment_request_count(
            request.method, 
            request.url.path, 
            response.status_code
        )
        
        # Log slow requests
        if process_time > self.slow_request_threshold:
            logger.warning(
                f"Slow request detected: {request.method} {request.url.path}",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "process_time": process_time,
                    "status_code": response.status_code,
                    "client_ip": request.client.host if request.client else None,
                }
            )
        
        # Log all requests in debug mode
        logger.debug(
            f"{request.method} {request.url.path} - {response.status_code} - {process_time:.3f}s",
            extra={
                "method": request.method,
                "path": request.url.path,
                "process_time": process_time,
                "status_code": response.status_code,
            }
        )
        
        return response

def add_performance_monitor(app, slow_request_threshold: float = 1.0):
    """Add performance monitoring middleware to the app"""
    app.add_middleware(PerformanceMonitorMiddleware, slow_request_threshold=slow_request_threshold)