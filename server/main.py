"""Guarded ASGI entry point for the supported ``main:app`` runtime surface."""
from __future__ import annotations

from lifecycle.bootstrap import MinimalStatusApplication, evaluate_startup


STARTUP_CONTEXT = evaluate_startup(install=True)

if STARTUP_CONTEXT.imports_application:
    from application import *  # noqa: F401,F403 - preserve the historical main module surface
    from application import app
else:
    app = MinimalStatusApplication(STARTUP_CONTEXT)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
