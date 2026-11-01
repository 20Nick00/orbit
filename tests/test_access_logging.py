import logging
from pathlib import Path

from app.logging import StripQueryString, install_access_log_filter
from app.main import create_app


def test_access_filter_strips_all_query_parameters(caplog, monkeypatch):
    logger = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "propagate", True)
    install_access_log_filter()
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    logger.info(
        '%s - "%s %s HTTP/%s" %d',
        "local",
        "GET",
        "/settings/plaid/callback?state=sentinel-state&token=sentinel-token",
        "1.1",
        303,
    )
    assert "/settings/plaid/callback HTTP/1.1" in caplog.text
    assert "sentinel" not in caplog.text and "state=" not in caplog.text


def test_filter_idempotent_main_app_and_docker_startup():
    create_app()
    create_app()
    assert (
        sum(isinstance(f, StripQueryString) for f in logging.getLogger("uvicorn.access").filters)
        == 1
    )
    assert "python -m app.serve" in Path("Dockerfile").read_text(encoding="utf-8")


def test_docker_entrypoint_filters_after_uvicorn_config(monkeypatch):
    from app import serve

    calls = []
    monkeypatch.setattr(serve.uvicorn, "Config", lambda *a, **kw: calls.append("config"))
    monkeypatch.setattr(serve, "install_access_log_filter", lambda: calls.append("filter"))

    class Server:
        def __init__(self, config):
            pass

        def run(self):
            calls.append("run")

    monkeypatch.setattr(serve.uvicorn, "Server", Server)
    serve.main()
    assert calls == ["config", "filter", "run"]


def test_access_filter_hides_employer_rule_names():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("local", "GET", "/paycheck/rules/acme-corp?x=1", "1.1", 200),
        None,
    )
    assert StripQueryString().filter(record)
    assert record.args[2] == "/paycheck/rules/[employer]"
