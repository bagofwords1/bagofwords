from .config import Settings

class Development(Settings):
    DEBUG: bool = True
    TESTING: bool = False
    SENTRY_DSN: str = ""
