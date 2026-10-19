"""HW2 Part D: authentication tests for the session endpoints.

These run offline: no Langfuse, Docker, or model key. Both refusals
happen before the agent would run.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from server import app as server_app


def test_create_session_rejects_role_that_differs_from_database(world) -> None:
    # Setup: start with no sessions. In the practice world, user 1 is a shopper.
    server_app._SESSIONS.clear()

    # Action: user 1 claims to be support.
    with pytest.raises(HTTPException) as refused:
        server_app.create_session(server_app.SessionCreate(user_id=1, role="support"))

    # Check: refused as Forbidden, and no session was created.
    assert refused.value.status_code == 403
    assert server_app._SESSIONS == {}


def test_token_for_one_session_cannot_authorize_another(world) -> None:
    # Setup: two real sessions, A (shopper 1) and B (merchant 9002).
    server_app._SESSIONS.clear()
    a = server_app.create_session(server_app.SessionCreate(user_id=1, role="shopper"))
    b = server_app.create_session(server_app.SessionCreate(user_id=9002, role="merchant"))

    # Action: send a message to B's session, but show A's wristband.
    with pytest.raises(HTTPException) as refused:
        asyncio.run(server_app.post_message(
            b["session_id"],
            server_app.MessageIn(message="Show my recent orders."),
            authorization=f"Bearer {a['token']}",
        ))

    # Check: refused as Forbidden ("token is for another session").
    assert refused.value.status_code == 403
