"""Docker entry point: install privacy filtering after Uvicorn configures logging."""

import uvicorn

from app.logging import install_access_log_filter


def main():
    config = uvicorn.Config("app.main:app", host="0.0.0.0", port=8000)
    install_access_log_filter()
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
