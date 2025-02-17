from .config import Settings

class Production(Settings):
    mock_preset_repo: bool = False
    mock_model_repo: bool = False

    # POSTGRES_PORT: int
    # POSTGRES_PASSWORD: str
    # POSTGRES_USER: str
    # POSTGRES_DB: str
    # POSTGRES_HOST: str
    SENTRY_DSN: str = "https://14a9bcccd561da545af332cc7884ef5f@o4508181564620800.ingest.us.sentry.io/4508181564751872"

    class Config:
        env_prefix = ""
