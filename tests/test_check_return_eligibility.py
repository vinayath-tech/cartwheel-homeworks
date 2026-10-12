"""Tests for the additional HW1 tool, check_return_eligibility.

Offline: the `world` fixture seeds a fresh dev world (as of 2026-07-01).
"""

from __future__ import annotations

import pytest

from agent import db, tools
from agent.auth import AuthContext

SHOPPER_1 = AuthContext(user_id=1, role="shopper")
MERCHANT_STORE_2 = AuthContext(user_id=9002, role="merchant", store_id=2)
SUPPORT = AuthContext(user_id=9501, role="support")


def test_store_override_window_applies(world: dict) -> None:
    # Saltbox Pantry (7-day override): delivered 8 days ago is out, 4 days ago is in.
    late = tools.check_return_eligibility(SUPPORT, 7489)
    assert late["ok"] is True
    assert late["return_window_days"] == 7
    assert late["window_policy_id"] == "store-saltbox-pantry-policy"
    assert late["days_since_delivery"] == 8
    assert late["eligible"] is False

    recent = tools.check_return_eligibility(SUPPORT, 3604)
    assert recent["eligible"] is True
    assert recent["days_remaining"] == 3


def test_platform_window_and_refunded_order(world: dict) -> None:
    outside = tools.check_return_eligibility(SHOPPER_1, 3980)
    assert outside["window_policy_id"] == "cw-returns"
    assert outside["return_window_days"] == 30
    assert outside["eligible"] is False

    inside = tools.check_return_eligibility(SHOPPER_1, 4455)
    assert inside["eligible"] is True


def test_not_yet_delivered_says_window_starts_on_delivery(world: dict) -> None:
    with db.connection() as conn:
        row = conn.execute("SELECT id FROM orders WHERE status = 'shipped' AND store_id = 13").fetchone()
    result = tools.check_return_eligibility(SUPPORT, row["id"])
    assert result["eligible"] is False
    assert result["return_window_days"] == 7
    assert "starts on the delivery date" in result["reason"]


@pytest.mark.parametrize("order_id", [8001, 8002])
def test_bad_delivery_data_is_reported_not_guessed(world: dict, order_id: int) -> None:
    result = tools.check_return_eligibility(SUPPORT, order_id)
    assert result["eligible"] is False
    assert result["days_since_delivery"] is None
    assert "cannot be computed" in result["reason"]


def test_agrees_with_stamped_refund_flag(world: dict) -> None:
    # The tool must never contradict the flag that issue_refund trusts.
    with db.connection() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM orders ORDER BY id LIMIT 500")]
        flags = {i: db.get_order(conn, i).refund_eligible for i in ids}
    for order_id, flag in flags.items():
        assert tools.check_return_eligibility(SUPPORT, order_id)["eligible"] == flag, order_id


def test_scope_and_unknown_order(world: dict) -> None:
    assert tools.check_return_eligibility(MERCHANT_STORE_2, 4127)["error"] == "permission_denied"
    assert tools.check_return_eligibility(SHOPPER_1, 7489)["error"] == "permission_denied"
    assert tools.check_return_eligibility(SUPPORT, 999999)["error"] == "not_found"
