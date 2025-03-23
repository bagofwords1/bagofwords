from .config import Settings

class Development(Settings):
    DEBUG: bool = True
    SENTRY_DSN: str = ""
