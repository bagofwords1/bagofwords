import os
import yaml
from pydantic_settings import BaseSettings
from dotenv import load_dotenv
from fastapi_mail import FastMail, ConnectionConfig
from .app_config import AppConfig

class Settings(BaseSettings):
    PROJECT_NAME: str = "MetricChat"
    PROJECT_VERSION: str = open("../VERSION").read().strip() if os.path.exists("../VERSION") else "0.0.321"
    API_PREFIX: str = "/api"
    DEBUG: bool = True
    TESTING: bool = False
    TEST_DATABASE_URL: str = "sqlite:///db/test_{}.db".format(os.getpid())
    ENVIRONMENT: str = os.environ.get("ENVIRONMENT", "development")
    app_config: AppConfig | None = None
    email_client: FastMail | None = None

    @property
    def version(self) -> str:
        return self.PROJECT_VERSION

    @classmethod
    def load(cls):
        # Load YAML configuration
        environment = os.environ.get("ENVIRONMENT", "development")
        print("Loading settings for environment:", environment)

        # Load environment variables first
        if environment == "development":
            
            dotenv_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                ".env"
            )
            print(f"Loading .env from: {dotenv_path}")
            load_dotenv(dotenv_path)

        # Load and validate config using Pydantic
        yaml_path = os.environ.get('MC_CONFIG_PATH') or os.environ.get('BOW_CONFIG_PATH')
        if not yaml_path:
            yaml_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
                "configs/metricchat.dev.yaml" if environment == "development" else "metricchat.yaml"
            )
        
        print(f"Loading config from: {yaml_path}")
        
        # Process environment variables in the YAML before validation
        def resolve_env_vars(config):
            if isinstance(config, dict):
                return {k: resolve_env_vars(v) for k, v in config.items()}
            elif isinstance(config, list):
                return [resolve_env_vars(i) for i in config]
            elif isinstance(config, str) and config.startswith("${") and config.endswith("}"):
                # Extract env var name from ${VAR_NAME}
                env_var_name = config[2:-1]
                env_value = os.environ.get(env_var_name)
                if env_value is not None:
                    return env_value
                # If env var is not set and this is encryption key, generate one
                if env_var_name in ("MC_ENCRYPTION_KEY", "BOW_ENCRYPTION_KEY"):
                    from .app_config import generate_fernet_key
                    new_key = generate_fernet_key()
                    os.environ[env_var_name] = new_key  # Save for future use
                    return new_key
                return None  # Env var not set — return None instead of raw placeholder
            return config

        with open(yaml_path, "r") as yaml_file:
            yaml_config = yaml.safe_load(yaml_file)
            # Resolve environment variables before validation
            yaml_config = resolve_env_vars(yaml_config)
            # Validate config using Pydantic model
            app_config = AppConfig(**yaml_config)

        # Create the environment-specific settings instance
        if environment == "development":
            from .development import Development
            settings = Development(app_config=app_config)
        elif environment == "staging":
            from .staging import Staging
            settings = Staging(app_config=app_config)
        elif environment == "production":
            from .production import Production
            settings = Production(app_config=app_config)
        else:
            raise ValueError(f"Unknown environment: {environment}")
            
        # Setup email client if SMTP settings exist
        if app_config.smtp_settings:
            email_config = ConnectionConfig(
                MAIL_USERNAME=app_config.smtp_settings.username,
                MAIL_PASSWORD=app_config.smtp_settings.password,
                MAIL_FROM_NAME=app_config.smtp_settings.from_name,
                MAIL_FROM=app_config.smtp_settings.from_email,
                MAIL_PORT=app_config.smtp_settings.port,
                MAIL_SERVER=app_config.smtp_settings.host,
                MAIL_STARTTLS=app_config.smtp_settings.use_tls,
                MAIL_SSL_TLS=app_config.smtp_settings.use_ssl,
                USE_CREDENTIALS=app_config.smtp_settings.use_credentials,
                VALIDATE_CERTS=app_config.smtp_settings.validate_certs,
                TEMPLATE_FOLDER=None
            )
            settings.email_client = FastMail(email_config)

        return settings
    

settings = Settings.load()