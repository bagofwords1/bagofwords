import os
import yaml
from pydantic_settings import BaseSettings
from dotenv import load_dotenv
from fastapi_mail import FastMail, ConnectionConfig
from .bow_config import BowConfig

class Settings(BaseSettings):
    PROJECT_NAME: str = "Bag of words"
    PROJECT_VERSION: str = open("../VERSION").read().strip()
    API_PREFIX: str = "/api"
    DEBUG: bool = True
    TESTING: bool = False
    TEST_DATABASE_URL: str = "sqlite:///db/test_{}.db".format(os.getpid())
    ENVIRONMENT: str = os.environ.get("ENVIRONMENT", "development")
    bow_config: BowConfig | None = None
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

        # Load and validate bow-config using Pydantic.
        # BOW_CONFIG_PATH overrides the default; restrict it to an absolute
        # path with a yaml extension so this env var can't be used to read
        # an arbitrary file off disk via path traversal.
        yaml_path = os.environ.get('BOW_CONFIG_PATH')
        if yaml_path:
            yaml_path = os.path.abspath(yaml_path)
            if not yaml_path.endswith((".yaml", ".yml")):
                raise RuntimeError(
                    f"BOW_CONFIG_PATH must point to a .yaml or .yml file: {yaml_path!r}"
                )
            if not os.path.isfile(yaml_path):
                raise RuntimeError(f"BOW_CONFIG_PATH does not exist: {yaml_path!r}")
        else:
            yaml_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
                "configs/bow-config.dev.yaml" if environment == "development" else "bow-config.yaml"
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
                if env_var_name == "BOW_ENCRYPTION_KEY":
                    from .bow_config import generate_fernet_key
                    new_key = generate_fernet_key()
                    os.environ["BOW_ENCRYPTION_KEY"] = new_key  # Save for future use
                    return new_key
                return None  # Env var not set — return None instead of raw placeholder
            return config

        # Inline path-traversal guard at the sink (Snyk python/PT). Reject
        # traversal sequences and ensure the resolved path exists.
        if ".." in yaml_path or not yaml_path.endswith((".yaml", ".yml")):
            raise RuntimeError(f"Config path is not a yaml file: {yaml_path!r}")
        yaml_path_safe = os.path.realpath(yaml_path)
        if not os.path.isfile(yaml_path_safe):
            raise RuntimeError(f"Config path is not a regular file: {yaml_path!r}")

        with open(yaml_path_safe, "r") as yaml_file:
            yaml_config = yaml.safe_load(yaml_file)
            # Resolve environment variables before validation
            yaml_config = resolve_env_vars(yaml_config)
            # Validate config using Pydantic model
            bow_config = BowConfig(**yaml_config)
            
        # Create the environment-specific settings instance
        if environment == "development":
            from .development import Development
            settings = Development(bow_config=bow_config)
        elif environment == "staging":
            from .staging import Staging
            settings = Staging(bow_config=bow_config)
        elif environment == "production":
            from .production import Production
            settings = Production(bow_config=bow_config)
        else:
            raise ValueError(f"Unknown environment: {environment}")
            
        # Setup email client.
        # - With use_credentials=True (default), both username and password must be
        #   present. In dev/test, BOW_SMTP_PASSWORD is often unset and resolves to
        #   None; in that case, leave email_client unset rather than failing to start.
        # - With use_credentials=False, the relay accepts mail without auth and
        #   username/password are not required.
        smtp = bow_config.smtp_settings
        if smtp and ((not smtp.use_credentials) or (smtp.username and smtp.password)):
            email_config = ConnectionConfig(
                MAIL_USERNAME=smtp.username or "",
                MAIL_PASSWORD=smtp.password or "",
                MAIL_FROM_NAME=smtp.from_name,
                MAIL_FROM=smtp.from_email,
                MAIL_PORT=smtp.port,
                MAIL_SERVER=smtp.host,
                MAIL_STARTTLS=smtp.use_tls,
                MAIL_SSL_TLS=smtp.use_ssl,
                USE_CREDENTIALS=smtp.use_credentials,
                VALIDATE_CERTS=smtp.validate_certs,
                TEMPLATE_FOLDER=None
            )
            settings.email_client = FastMail(email_config)

        return settings
    

settings = Settings.load()