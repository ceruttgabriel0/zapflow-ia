from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/whatsapp_ia"

    # AI Provider
    AI_PROVIDER: str = "openrouter"
    AI_PROVIDER_FALLBACK: str = "openai"
    OPENAI_API_KEY: str = ""
    OPENROUTER_API_KEY: str = ""

    # Redis
    REDIS_URL: str = "redis://redis:6379/0"

    # WAHA
    WAHA_API_URL: str = "http://localhost:3000"
    WAHA_API_KEY: str = ""

    # Google Calendar (Service Account)
    GOOGLE_SERVICE_ACCOUNT_FILE: str = "app/service_account.json"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
