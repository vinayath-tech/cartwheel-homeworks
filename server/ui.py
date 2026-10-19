"""Optional local chat page for the HW2 endpoints. Not a course deliverable.

Adds a browser page on top of server.app without editing it. The page calls
the same POST /sessions and POST /sessions/{id}/messages routes, so every
message is traced exactly like a curl request.

Run it in place of server.app:
    uv run uvicorn server.ui:app --port 8010
then open http://localhost:8010/ui
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi.responses import HTMLResponse

from server.app import app

_PAGE = Path(__file__).with_name("static") / "chat.html"


@app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def chat_page() -> HTMLResponse:
    # no-store: browsers always fetch the current page instead of a saved copy.
    return HTMLResponse(
        _PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"}
    )


@lru_cache(maxsize=1)
def _langfuse_project_id() -> str | None:
    """Look up the Langfuse project once so the page can link to sessions."""
    host = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
    public = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not (host and public and secret):
        return None
    auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
    request = urllib.request.Request(
        f"{host}/api/public/projects", headers={"Authorization": f"Basic {auth}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            projects = json.load(response).get("data") or []
    except (OSError, ValueError):
        return None
    return projects[0]["id"] if projects else None


@app.get("/ui/config", include_in_schema=False)
def chat_config() -> dict[str, Any]:
    project_id = _langfuse_project_id()
    host = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
    return {"langfuse_project_url": f"{host}/project/{project_id}" if project_id else None}
