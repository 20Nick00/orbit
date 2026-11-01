"""Public listener: only signed Plaid webhooks, never the owner interface."""

from fastapi import FastAPI

from app.routers.plaid_webhook import router

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(router)
