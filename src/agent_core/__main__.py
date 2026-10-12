"""Run the hub."""

from __future__ import annotations

import uvicorn

from .app import create_app
from .config import Config
from .log_redact import log_config


def main() -> None:
    cfg = Config.from_env()
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info", log_config=log_config())


if __name__ == "__main__":
    main()
