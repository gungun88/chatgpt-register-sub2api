"""Run the local web panel with `python -m chatgpt_register_sub2api.web`."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("CHATGPT_REGISTER_PANEL_HOST", "127.0.0.1")
    port = int(os.environ.get("CHATGPT_REGISTER_PANEL_PORT", "7860"))
    uvicorn.run(
        "chatgpt_register_sub2api.web.app:app",
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
