from .config import Settings

class Staging(Settings):
    mock_preset_repo: bool = False
    mock_model_repo: bool = False

    # POSTGRES_PORT: int
    # POSTGRES_PASSWORD: str
    # POSTGRES_USER: str
    # POSTGRES_DB: str
    # POSTGRES_HOST: str
    DATABASE_URL: str

    class Config:
        env_prefix = ""
