"""
Health check endpoints for monitoring
"""
import asyncio
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db
from app.services.cache_service import CacheService
from app.settings.config import settings
from app.settings.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["health"])

@router.get("/health")
async def health_check():
    """Basic health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": settings.PROJECT_VERSION,
        "environment": settings.ENVIRONMENT
    }

@router.get("/health/detailed")
async def detailed_health_check(db: AsyncSession = Depends(get_async_db)):
    """Detailed health check including database and cache"""
    health_status = {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": settings.PROJECT_VERSION,
        "environment": settings.ENVIRONMENT,
        "checks": {}
    }
    
    # Database check
    try:
        start_time = datetime.utcnow()
        await db.execute(text("SELECT 1"))
        db_response_time = (datetime.utcnow() - start_time).total_seconds()
        
        health_status["checks"]["database"] = {
            "status": "healthy",
            "response_time_seconds": db_response_time
        }
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        health_status["checks"]["database"] = {
            "status": "unhealthy",
            "error": str(e)
        }
        health_status["status"] = "unhealthy"
    
    # Cache check
    try:
        start_time = datetime.utcnow()
        test_key = "health_check_test"
        test_value = "test_value"
        
        await CacheService.cache.set(test_key, test_value, 10)
        retrieved_value = await CacheService.cache.get(test_key)
        await CacheService.cache.delete(test_key)
        
        cache_response_time = (datetime.utcnow() - start_time).total_seconds()
        
        if retrieved_value == test_value:
            health_status["checks"]["cache"] = {
                "status": "healthy",
                "response_time_seconds": cache_response_time,
                "stats": await CacheService.get_cache_stats()
            }
        else:
            health_status["checks"]["cache"] = {
                "status": "unhealthy",
                "error": "Cache value mismatch"
            }
            health_status["status"] = "unhealthy"
            
    except Exception as e:
        logger.error(f"Cache health check failed: {e}")
        health_status["checks"]["cache"] = {
            "status": "unhealthy",
            "error": str(e)
        }
        health_status["status"] = "unhealthy"
    
    # Memory check (basic)
    try:
        import psutil
        memory = psutil.virtual_memory()
        health_status["checks"]["memory"] = {
            "status": "healthy" if memory.percent < 90 else "warning",
            "usage_percent": memory.percent,
            "available_gb": round(memory.available / (1024**3), 2)
        }
        
        if memory.percent > 95:
            health_status["status"] = "unhealthy"
            
    except ImportError:
        health_status["checks"]["memory"] = {
            "status": "unknown",
            "error": "psutil not available"
        }
    except Exception as e:
        health_status["checks"]["memory"] = {
            "status": "error",
            "error": str(e)
        }
    
    # Return appropriate status code
    if health_status["status"] == "unhealthy":
        raise HTTPException(status_code=503, detail=health_status)
    
    return health_status

@router.get("/health/ready")
async def readiness_check(db: AsyncSession = Depends(get_async_db)):
    """Readiness check for Kubernetes/container orchestration"""
    try:
        # Check if we can connect to database
        await db.execute(text("SELECT 1"))
        
        # Check if critical services are available
        # Add more checks as needed
        
        return {
            "status": "ready",
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Readiness check failed: {e}")
        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat()
            }
        )

@router.get("/health/live")
async def liveness_check():
    """Liveness check for Kubernetes/container orchestration"""
    return {
        "status": "alive",
        "timestamp": datetime.utcnow().isoformat()
    }

@router.get("/metrics")
async def get_metrics():
    """Get application metrics for monitoring"""
    from app.services.monitoring_service import MonitoringService
    
    return {
        "application_metrics": MonitoringService.get_all_metrics(),
        "system_metrics": await MonitoringService.get_system_metrics()
    }