from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables and optional .env."""

    app_environment: Literal["development", "test", "production"] = "development"

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_database: str = "medical_ai"
    mysql_user: str = "medical_ai"
    mysql_password: str = Field(default="", repr=False)

    query_default_limit: int = 100
    query_max_limit: int = 1000
    query_max_distinct_values: int = 200
    query_timeout_ms: int = 30000
    privacy_min_group_size: int = Field(default=5, ge=2, le=100)

    api_max_body_bytes: int = 2_000_000
    api_max_question_chars: int = 2_000

    auth_session_cookie_name: str = "medical_ai_session"
    auth_session_cookie_secure: bool = True
    auth_session_cookie_samesite: Literal["Lax", "Strict"] = "Lax"
    auth_session_idle_minutes: int = Field(default=30, ge=5, le=1440)
    auth_session_absolute_hours: int = Field(default=12, ge=1, le=168)
    auth_session_touch_minutes: int = Field(default=5, ge=1, le=60)
    auth_csrf_cookie_name: str = "medical_ai_csrf"
    auth_csrf_header_name: str = "X-CSRF-Token"
    auth_enforcement_enabled: bool = False

    rate_limit_enabled: bool = False
    redis_url: str = Field(default="", repr=False)
    rate_limit_key_secret: str = Field(default="", repr=False)
    login_rate_limit_ip_attempts: int = Field(default=20, ge=1, le=10_000)
    login_rate_limit_account_attempts: int = Field(default=10, ge=1, le=10_000)
    login_rate_limit_window_seconds: int = Field(default=300, ge=1, le=86_400)

    mcp_transport: Literal["stdio", "sse", "streamable-http"] = "stdio"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 3001
    mcp_allow_unscoped_tools: bool = True

    llm_base_url: str = ""
    llm_api_key: str = Field(default="", repr=False)
    llm_model: str = ""
    llm_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_retry_base_delay_seconds: float = Field(default=0.5, ge=0, le=10)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_production_security_posture(self) -> "Settings":
        """Prevent production startup with an anonymous or insecure browser path."""

        if self.app_environment == "production":
            if not self.auth_enforcement_enabled:
                raise ValueError("production requires AUTH_ENFORCEMENT_ENABLED=true")
            if not self.auth_session_cookie_secure:
                raise ValueError("production requires secure authentication cookies")
            if self.mcp_allow_unscoped_tools:
                raise ValueError("production forbids unscoped MCP tools")
            if not self.rate_limit_enabled:
                raise ValueError("production requires RATE_LIMIT_ENABLED=true")
        if self.rate_limit_enabled:
            if not self.redis_url.strip():
                raise ValueError("enabled rate limiting requires REDIS_URL")
            if not self.redis_url.startswith(("redis://", "rediss://", "unix://")):
                raise ValueError("REDIS_URL must use redis://, rediss://, or unix://")
            if len(self.rate_limit_key_secret.encode("utf-8")) < 32:
                raise ValueError("RATE_LIMIT_KEY_SECRET must contain at least 32 UTF-8 bytes")
        return self

    def mysql_url(self) -> URL:
        return URL.create(
            "mysql+pymysql",
            username=self.mysql_user,
            password=self.mysql_password,
            host=self.mysql_host,
            port=self.mysql_port,
            database=self.mysql_database,
            query={"charset": "utf8mb4"},
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
