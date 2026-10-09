from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    session_ttl_hours: int = 8
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000

    model_config = SettingsConfigDict(env_prefix="WORKHUB_", env_file=".env", extra="ignore")


settings = Settings()
