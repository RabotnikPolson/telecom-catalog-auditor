from functools import lru_cache
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    APP_NAME: str = "Telecom Catalog Auditor"
    APP_ENV: str = "development"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    DATABASE_PATH: str = "catalog_audit.db"

    SERPER_API_KEY: str | None = Field(default=None)
    GEMINI_API_KEY: str | None = Field(default=None)
    OPENROUTER_API_KEY: str | None = Field(default=None)
    OPENROUTER_MODEL: str = Field(default="google/gemini-2.5-flash")

    MYSQL_HOST: str | None = Field(default=None)
    MYSQL_PORT: int = Field(default=3306)
    MYSQL_USER: str | None = Field(default=None)
    MYSQL_PASSWORD: str | None = Field(default=None)
    MYSQL_DATABASE: str = Field(default="laravel")

    @property
    def db_file_path(self) -> Path:
        return Path(self.DATABASE_PATH)


@lru_cache()
def get_settings() -> Settings:
    return Settings()
