import logging
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Thread, get_ident

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.logging import install_access_log_filter
from app.routers import (
    accounts,
    add,
    almanac,
    dashboard,
    health,
    paycheck,
    property,
    pwa,
    settings,
    trends,
)
from app.security import CSRFMiddleware
from core.config import get_settings
from core.snapshots import MixedCurrencyError
from jobs.scheduler import build_scheduler
from paystubs.service import start_background


def _run_webhook(server):
    # Uvicorn otherwise prints bind errors and tracebacks before raising SystemExit.
    # Suppress only this listener's Uvicorn logs, leaving the main server unaffected.
    listener_ident = get_ident()
    uvicorn_logger = logging.getLogger("uvicorn.error")

    def other_threads(record):
        return record.thread != listener_ident

    uvicorn_logger.addFilter(other_threads)
    try:
        server.run()
    except (Exception, SystemExit) as exc:
        logging.getLogger(__name__).warning("Webhook startup: %s", type(exc).__name__)
    finally:
        uvicorn_logger.removeFilter(other_threads)


@asynccontextmanager
async def lifespan(app):
    handle = None
    scheduler = None
    webhook_server = None
    webhook_thread = None
    try:
        handle = start_background()
    except Exception as exc:
        logging.getLogger(__name__).warning("Paystub startup: %s", type(exc).__name__)
    try:
        config = get_settings()
        if config.webhook_port is not None:
            from app.webhook_app import app as webhook_app

            webhook_server = uvicorn.Server(
                uvicorn.Config(
                    webhook_app,
                    host=config.webhook_bind,
                    port=config.webhook_port,
                    log_config=None,
                    access_log=False,
                )
            )
            thread = Thread(target=_run_webhook, args=(webhook_server,), daemon=True)
            thread.start()
            webhook_thread = thread
    except Exception as exc:
        logging.getLogger(__name__).warning("Webhook startup: %s", type(exc).__name__)
    try:
        if get_settings().scheduler_enabled:
            scheduler = build_scheduler(get_settings())
            scheduler.start()
        yield
    finally:
        if webhook_thread is not None:
            webhook_server.should_exit = True
            webhook_thread.join()
        if scheduler is not None:
            scheduler.shutdown(wait=True)
        if handle is not None:
            handle.stop()


def create_app() -> FastAPI:
    install_access_log_filter()
    app = FastAPI(title="Orbit", lifespan=lifespan)
    app.add_middleware(CSRFMiddleware)
    config = get_settings()
    allowed_hosts = [
        host.strip().lower() for host in config.allowed_hosts.split(",") if host.strip()
    ]
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=allowed_hosts,
    )
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).resolve().parent / "static"),
        name="static",
    )
    app.include_router(health.router)
    app.include_router(pwa.router)
    app.include_router(dashboard.router)
    app.include_router(accounts.router)
    app.include_router(add.router)
    app.include_router(paycheck.router)
    app.include_router(settings.router)
    app.include_router(property.router)
    app.include_router(trends.router)
    app.include_router(almanac.router)

    @app.exception_handler(MixedCurrencyError)
    async def mixed_currency(request, exc):
        return HTMLResponse(
            "Cannot combine accounts in different currencies. "
            "Use accounts in one currency to view these totals.",
            status_code=400,
        )

    @app.get("/", include_in_schema=False)
    def index():
        return RedirectResponse(url="/dashboard")

    return app


app = create_app()
