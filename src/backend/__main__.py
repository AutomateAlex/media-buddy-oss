"""Entry point for the bundled backend executable.

Invoked as ``media_buddy_backend.exe`` after PyInstaller bundles this
module. Runs uvicorn with our FastAPI app on 127.0.0.1:8000.

In dev we run uvicorn directly via the command line; this entry only
fires inside the packaged build.
"""
from __future__ import annotations

import logging
import os
import sys


def main() -> None:
    # Default to INFO logging so the Electron parent can surface useful
    # diagnostics. The user can override with MEDIA_BUDDY_LOG_LEVEL.
    log_level = os.environ.get("MEDIA_BUDDY_LOG_LEVEL", "info").lower()
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # PyInstaller-bundled apps need uvicorn imported normally — no reload,
    # single worker (BackgroundWorker thread inside provides concurrency).
    from backend.lib.diagnostics import configure_file_logging
    configure_file_logging()

    import uvicorn
    from backend.main import app

    host = os.environ.get("MEDIA_BUDDY_HOST", "127.0.0.1")
    port = int(os.environ.get("MEDIA_BUDDY_PORT", "8000"))

    uvicorn.run(app, host=host, port=port, log_level=log_level, reload=False)


if __name__ == "__main__":
    sys.exit(main() or 0)
