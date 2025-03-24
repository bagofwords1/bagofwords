import os
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from app.settings.config import settings

# Create a custom JSON formatter for production
class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        
        # Add exception info if present
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        
        # Add extra fields from record
        for key, value in record.__dict__.items():
            if key not in ["args", "asctime", "created", "exc_info", "exc_text", 
                          "filename", "funcName", "id", "levelname", "levelno", 
                          "lineno", "module", "msecs", "message", "msg", 
                          "name", "pathname", "process", "processName", 
                          "relativeCreated", "stack_info", "thread", "threadName"]:
                log_record[key] = value
        
        return json.dumps(log_record)

def setup_logging():
    """Configure application-wide logging"""
    # Create logs directory if it doesn't exist
    os.makedirs("logs", exist_ok=True)
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO if settings.ENVIRONMENT == "production" else logging.DEBUG)
    
    # Clear any existing handlers to avoid duplication
    if root_logger.handlers:
        root_logger.handlers.clear()
    
    # Create formatters based on environment
    if settings.ENVIRONMENT == "production":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s'
        )
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # File handler with rotation
    file_handler = RotatingFileHandler(
        "logs/app.log",
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    # Set levels for third-party loggers to reduce noise
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)
    logging.getLogger("aiosqlite").setLevel(logging.INFO)
    
    # Create and configure app-specific loggers
    app_logger = logging.getLogger("app")
    request_logger = logging.getLogger("app.request")
    db_logger = logging.getLogger("app.db")
    auth_logger = logging.getLogger("app.auth")
    
    # Return the configured loggers
    return {
        "root": root_logger,
        "app": app_logger,
        "request": request_logger,
        "db": db_logger,
        "auth": auth_logger
    }

# Function to get a logger for a specific module
def get_logger(module_name=None):
    """
    Get a logger for a specific module.
    
    Args:
        module_name: The name of the module (usually __name__)
        
    Returns:
        A configured logger instance
    """
    if module_name:
        return logging.getLogger(module_name)
    return logging.getLogger("app")
