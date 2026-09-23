from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_user: str
    postgres_password: str
    postgres_db: str
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_stream: str = "transactions"
    redis_group: str = "processors"
    redis_stream_maxlen: int = 100_000
    redis_socket_timeout: float = 2.0
    redis_connect_timeout: float = 2.0

    pg_pool_min: int = 1
    pg_pool_max: int = 10
    pg_command_timeout: float = 5.0
    health_probe_timeout: float = 2.0

    crash_after_commit: bool = False
    worker_block_ms: int = 5000
    worker_batch: int = 16
    worker_min_idle_ms: int = 30000

    @property
    def _pg_credentials(self) -> str:
        return f"{quote_plus(self.postgres_user)}:{quote_plus(self.postgres_password)}"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self._pg_credentials}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def alembic_database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self._pg_credentials}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"


settings = Settings()