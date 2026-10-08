import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import data
from .config import Config
from .data import Filter, Store
from .jobs import run_forever
from .schema import ensure

log = logging.getLogger("explorer")
PAGES = Path(__file__).with_name("pages")


def create_app(cfg: Config, store: Store | None = None) -> FastAPI:
    store = store or Store()
    holder: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        kw = cfg.conn_kwargs(cfg.page_timeout_s)
        kw["row_factory"] = dict_row
        pool = ConnectionPool(cfg.dsn, min_size=1, max_size=4, kwargs=kw, open=True)
        pool.wait()
        with pool.connection() as conn:
            ensure(conn, cfg.schema)
        holder["pool"] = pool
        stop = threading.Event()
        if cfg.run_job:
            threading.Thread(target=run_forever, args=(cfg, stop), name="explorer-job", daemon=True).start()
        log.info("explorer started on %s", cfg.listen)
        try:
            yield
        finally:
            stop.set()
            pool.close()

    app = FastAPI(title="x86db explorer", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
    app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")

    def view(build, *args, **kw):
        """What a page shows, as JSON: every view is one data function."""
        with holder["pool"].connection() as conn:
            d = build(store, conn, *args, **kw)
        if d is None:
            raise HTTPException(404, "not found")
        return JSONResponse(d)

    def page(name: str):
        """The page of a view: a static file whose script fetches the .json twin of its URL."""
        return FileResponse(PAGES / f"{name}.html", media_type="text/html")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/")
    def matrix_page():
        return page("matrix")

    @app.get("/matrix.json")
    def matrix_json(xc: Optional[str] = None, xk: Optional[str] = None, all: bool = False):
        return view(data.matrix, flt=Filter.parse(xc, xk), show_all=all)

    @app.get("/checks")
    def checks_page():
        return page("checks")

    @app.get("/checks.json")
    def checks_json():
        return view(data.checks)

    # The .json routes come first: "/i/{name}" would take "ADD.json" as a mnemonic.
    @app.get("/i/{name}.json")
    def instruction_json(name: str, xc: Optional[str] = None, xk: Optional[str] = None, all: bool = False):
        return view(data.instruction, name, flt=Filter.parse(xc, xk), show_all=all)

    @app.get("/i/{name}")
    def instruction_page(name: str):
        return page("instruction")

    @app.get("/tc/{tc}.json")
    def test_case_json(tc: int, xc: Optional[str] = None, xk: Optional[str] = None, after: int = -1):
        return view(data.test_case, tc, flt=Filter.parse(xc, xk), after=after)

    @app.get("/tc/{tc}")
    def test_case_page(tc: int):
        return page("test_case")

    @app.get("/tc/{tc}/{si}.json")
    def state_json(tc: int, si: int, all: bool = False):
        return view(data.state, tc, si, show_all=all)

    @app.get("/tc/{tc}/{si}")
    def state_page(tc: int, si: int):
        return page("state")

    return app
