"""Запуск монитора: ``python -m loadmon`` → http://127.0.0.1:8770

Порт переопределяется LOADMON_PORT, интервал сэмплинга LOADMON_INTERVAL (сек).
"""

from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("LOADMON_PORT", "8770"))
    uvicorn.run("loadmon.app:create_app", factory=True, host="127.0.0.1", port=port)
