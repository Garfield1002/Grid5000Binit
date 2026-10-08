import os
from dataclasses import dataclass


@dataclass
class Config:
    dsn: str
    token: str
    listen: str = "0.0.0.0:8080"
    log_dir: str = "./logs"
    silent_after_s: float = 120.0
    # Extra libpq/psycopg connection kwargs (used by tests for search_path).
    conn_kwargs: dict | None = None
    # Objective table; empty = the packaged targets.csv.
    targets_file: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("CONTROLLER_TOKEN", "")
        if not token:
            raise SystemExit("CONTROLLER_TOKEN must be set")
        return cls(
            dsn=os.environ.get("X86DB_DSN", "postgresql://x86db:x86db@localhost:5432/x86db"),
            token=token,
            listen=os.environ.get("LISTEN", "0.0.0.0:8080"),
            log_dir=os.environ.get("LOG_DIR", "./logs"),
            silent_after_s=float(os.environ.get("SILENT_AFTER_S", "120")),
            targets_file=os.environ.get("TARGETS_FILE", ""),
        )
