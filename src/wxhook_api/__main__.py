from __future__ import annotations

import uvicorn

from wxhook_api.core.config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "wxhook_api.main:app",
        host=settings.host,
        port=settings.port,
        workers=1,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
