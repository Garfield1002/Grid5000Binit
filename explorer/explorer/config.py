import os
from dataclasses import dataclass


@dataclass
class Config:
    dsn: str
    listen: str = "0.0.0.0:8080"
    # The explorer's own schema, and the one holding the corpus and the g5k_* tables.
    schema: str = "explorer"
    data_schema: str = "public"
    run_job: bool = True
    # Result watermark: ids folded per chunk, and the share of time spent reading (the job sleeps
    # the rest, so a backfill leaves the disk to the controller).
    chunk: int = 20000
    duty: float = 0.25
    tc_chunk: int = 200
    idle_s: float = 30.0
    job_timeout_s: float = 120.0
    page_timeout_s: float = 5.0
    check_every_s: float = 3600.0
    check_max_tcs: int = 200

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ.get
        return cls(
            dsn=env("EXPLORER_DSN", "postgresql://explorer:explorer@localhost:5432/x86db"),
            listen=env("LISTEN", "0.0.0.0:8080"),
            schema=env("EXPLORER_SCHEMA", "explorer"),
            run_job=env("EXPLORER_JOB", "1") != "0",
            chunk=int(env("EXPLORER_CHUNK", "20000")),
            duty=float(env("EXPLORER_DUTY", "0.25")),
            idle_s=float(env("EXPLORER_IDLE_S", "30")),
            job_timeout_s=float(env("EXPLORER_JOB_TIMEOUT_S", "120")),
            page_timeout_s=float(env("EXPLORER_PAGE_TIMEOUT_S", "5")),
            check_every_s=float(env("EXPLORER_CHECK_EVERY_S", "3600")),
            check_max_tcs=int(env("EXPLORER_CHECK_MAX_TCS", "200")),
        )

    def conn_kwargs(self, timeout_s: float) -> dict:
        """Unqualified names resolve to the explorer's schema first, then to the data."""
        return {"options": f"-csearch_path={self.schema},{self.data_schema} "
                           f"-cstatement_timeout={int(timeout_s * 1000)}"}
