"""
Monitoring and metrics service
"""
import time
import asyncio
from typing import Dict, Any, List
from datetime import datetime, timedelta
from collections import defaultdict, deque
from app.settings.logging_config import get_logger

logger = get_logger(__name__)

class MetricsCollector:
    """Collect and store application metrics"""
    
    def __init__(self):
        self.metrics = defaultdict(list)
        self.counters = defaultdict(int)
        self.gauges = defaultdict(float)
        self.histograms = defaultdict(deque)
        self._cleanup_task = None
        self._start_cleanup_task()
    
    def _start_cleanup_task(self):
        """Start background task to clean old metrics"""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_old_metrics())
    
    async def _cleanup_old_metrics(self):
        """Remove metrics older than 1 hour"""
        while True:
            try:
                cutoff_time = time.time() - 3600  # 1 hour ago
                
                for metric_name, values in self.histograms.items():
                    while values and values[0]['timestamp'] < cutoff_time:
                        values.popleft()
                
                await asyncio.sleep(300)  # Clean every 5 minutes
                
            except Exception as e:
                logger.error(f"Error in metrics cleanup: {e}")
                await asyncio.sleep(60)
    
    def increment_counter(self, name: str, value: int = 1, tags: Dict[str, str] = None):
        """Increment a counter metric"""
        key = f"{name}:{':'.join(f'{k}={v}' for k, v in (tags or {}).items())}"
        self.counters[key] += value
    
    def set_gauge(self, name: str, value: float, tags: Dict[str, str] = None):
        """Set a gauge metric"""
        key = f"{name}:{':'.join(f'{k}={v}' for k, v in (tags or {}).items())}"
        self.gauges[key] = value
    
    def record_histogram(self, name: str, value: float, tags: Dict[str, str] = None):
        """Record a histogram value"""
        key = f"{name}:{':'.join(f'{k}={v}' for k, v in (tags or {}).items())}"
        self.histograms[key].append({
            'value': value,
            'timestamp': time.time()
        })
        
        # Keep only last 1000 values per histogram
        if len(self.histograms[key]) > 1000:
            self.histograms[key].popleft()
    
    def get_metrics_summary(self) -> Dict[str, Any]:
        """Get summary of all metrics"""
        summary = {
            'counters': dict(self.counters),
            'gauges': dict(self.gauges),
            'histograms': {}
        }
        
        # Calculate histogram statistics
        for name, values in self.histograms.items():
            if values:
                vals = [v['value'] for v in values]
                vals.sort()
                count = len(vals)
                
                summary['histograms'][name] = {
                    'count': count,
                    'min': min(vals),
                    'max': max(vals),
                    'mean': sum(vals) / count,
                    'p50': vals[int(count * 0.5)] if count > 0 else 0,
                    'p95': vals[int(count * 0.95)] if count > 0 else 0,
                    'p99': vals[int(count * 0.99)] if count > 0 else 0,
                }
        
        return summary

# Global metrics collector
metrics = MetricsCollector()

class MonitoringService:
    """Service for application monitoring"""
    
    @staticmethod
    def record_request_duration(duration: float, method: str, path: str, status_code: int):
        """Record HTTP request duration"""
        metrics.record_histogram(
            'http_request_duration_seconds',
            duration,
            {
                'method': method,
                'path': path,
                'status': str(status_code)
            }
        )
    
    @staticmethod
    def increment_request_count(method: str, path: str, status_code: int):
        """Increment HTTP request count"""
        metrics.increment_counter(
            'http_requests_total',
            1,
            {
                'method': method,
                'path': path,
                'status': str(status_code)
            }
        )
    
    @staticmethod
    def record_database_query_duration(duration: float, query_type: str):
        """Record database query duration"""
        metrics.record_histogram(
            'database_query_duration_seconds',
            duration,
            {'type': query_type}
        )
    
    @staticmethod
    def increment_database_query_count(query_type: str, success: bool):
        """Increment database query count"""
        metrics.increment_counter(
            'database_queries_total',
            1,
            {
                'type': query_type,
                'success': str(success).lower()
            }
        )
    
    @staticmethod
    def record_cache_operation(operation: str, hit: bool, duration: float):
        """Record cache operation metrics"""
        metrics.increment_counter(
            'cache_operations_total',
            1,
            {
                'operation': operation,
                'result': 'hit' if hit else 'miss'
            }
        )
        
        metrics.record_histogram(
            'cache_operation_duration_seconds',
            duration,
            {'operation': operation}
        )
    
    @staticmethod
    def record_completion_created(model: str, status: str):
        """Record completion creation metrics"""
        metrics.increment_counter(
            'completions_created_total',
            1,
            {
                'model': model,
                'status': status
            }
        )
    
    @staticmethod
    def record_websocket_connection(action: str):
        """Record WebSocket connection metrics"""
        metrics.increment_counter(
            'websocket_connections_total',
            1,
            {'action': action}
        )
    
    @staticmethod
    def set_active_users(count: int):
        """Set active users gauge"""
        metrics.set_gauge('active_users', count)
    
    @staticmethod
    def set_active_websockets(count: int):
        """Set active WebSocket connections gauge"""
        metrics.set_gauge('active_websockets', count)
    
    @staticmethod
    def get_all_metrics() -> Dict[str, Any]:
        """Get all collected metrics"""
        return metrics.get_metrics_summary()
    
    @staticmethod
    async def get_system_metrics() -> Dict[str, Any]:
        """Get system-level metrics"""
        system_metrics = {
            'timestamp': datetime.utcnow().isoformat(),
            'uptime_seconds': time.time() - getattr(MonitoringService, '_start_time', time.time())
        }
        
        try:
            import psutil
            
            # CPU metrics
            system_metrics['cpu_percent'] = psutil.cpu_percent(interval=1)
            system_metrics['cpu_count'] = psutil.cpu_count()
            
            # Memory metrics
            memory = psutil.virtual_memory()
            system_metrics['memory'] = {
                'total_bytes': memory.total,
                'available_bytes': memory.available,
                'used_bytes': memory.used,
                'percent': memory.percent
            }
            
            # Disk metrics
            disk = psutil.disk_usage('/')
            system_metrics['disk'] = {
                'total_bytes': disk.total,
                'used_bytes': disk.used,
                'free_bytes': disk.free,
                'percent': (disk.used / disk.total) * 100
            }
            
        except ImportError:
            system_metrics['error'] = 'psutil not available'
        except Exception as e:
            system_metrics['error'] = str(e)
        
        return system_metrics

# Set start time for uptime calculation
MonitoringService._start_time = time.time()