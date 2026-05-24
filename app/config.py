"""Application configuration loaded from environment variables."""
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str
    alembic_database_url: str

    # Auth
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 10080  # 7 days
    password_reset_expire_minutes: int = 30

    # Storage
    receipt_storage_path: str = "./uploads/receipts"
    asset_storage_path: str = "./uploads/assets"
    max_photo_width: int = 800
    photo_quality: int = 85

    # Email — Resend (invite emails)
    resend_api_key: str
    email_from: str
    email_from_name: str = "Household Expenses"
    frontend_url: str = "http://localhost:3000"

    # Email — Gmail SMTP (password reset emails)
    gmail_sender: str
    gmail_app_password: str
    reset_token_expiry_hours: int = 1

    # Email — Brevo HTTP API (password reset emails, replaces Gmail SMTP)
    brevo_api_key: str
    brevo_sender_email: str
    brevo_sender_name: str = "HomieGhor"

    # App
    app_env: Literal["development", "production"] = "development"
    app_timezone: str = "Asia/Dhaka"
    cors_origins: str = "http://localhost:3000"

    # Currency
    currency_code: str = "BDT"
    currency_symbol: str = "৳"

    # Cloudinary — all default to "" so absence triggers local-filesystem fallback
    cloudinary_cloud_name: str = ""
    cloudinary_api_key: str = ""
    cloudinary_api_secret: str = ""
    cloudinary_upload_folder: str = "household-app"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
