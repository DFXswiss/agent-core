"""Run the hub."""

from __future__ import annotations

import uvicorn

from .app import create_app
from .config import Config


def main() -> None:
    cfg = Config.from_env()
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
