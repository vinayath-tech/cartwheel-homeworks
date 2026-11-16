"""HW6: tests for the searched_or_asked and order_looked_up checks.

They run offline on the seeded test world and do not call a model.
"""

from __future__ import annotations

from replay.rollout import apply_checks


def _transcript(reply: str, tool_calls: list[dict]) -> dict:
    turn = {"user": "hello", "reply": reply, "tool_calls": tool_calls}
    return {"turns": [turn], "final_reply": reply}


def _case(check: dict) -> dict:
    return {"expected": {"checks": [check]}}


def _call(name: str, args: dict, result: dict | None = None) -> dict:
    return {"name": name, "args": args, "result": result or {}}


# --- searched_or_asked -------------------------------------------------------

def test_searched_or_asked_passes_a_search(world) -> None:
    transcript = _transcript("Here are some ideas.", [_call("search_products", {"query": "trowel"})])
    assert apply_checks(_case({"check": "searched_or_asked"}), transcript, world["db"])["passed"]


def test_searched_or_asked_passes_a_question(world) -> None:
    transcript = _transcript("How much would you like to spend?", [])
    assert apply_checks(_case({"check": "searched_or_asked"}), transcript, world["db"])["passed"]


def test_searched_or_asked_fails_when_agent_does_neither(world) -> None:
    transcript = _transcript("I can't help with that.", [])
    assert not apply_checks(_case({"check": "searched_or_asked"}), transcript, world["db"])["passed"]


# --- order_looked_up ---------------------------------------------------------

CHECK = {"check": "order_looked_up", "order_id": 1695}


def test_order_id_argument_counts(world) -> None:
    transcript = _transcript("Not eligible.", [_call("check_return_eligibility", {"order_id": 1695})])
    assert apply_checks(_case(CHECK), transcript, world["db"])["passed"]


def test_order_id_sent_as_text_counts(world) -> None:
    transcript = _transcript("Not eligible.", [_call("get_order", {"order_id": "1695"})])
    assert apply_checks(_case(CHECK), transcript, world["db"])["passed"]


def test_find_order_result_counts(world) -> None:
    result = {"ok": True, "orders": [{"order_id": 1695}]}
    transcript = _transcript("Found it.", [_call("find_order", {"query": "walnut blocks"}, result)])
    assert apply_checks(_case(CHECK), transcript, world["db"])["passed"]


def test_list_my_orders_alone_does_not_count(world) -> None:
    result = {"ok": True, "orders": [{"order_id": 6446}, {"order_id": 1695}]}
    transcript = _transcript("Order 6446 is eligible.", [_call("list_my_orders", {}, result)])
    assert not apply_checks(_case(CHECK), transcript, world["db"])["passed"]


def test_a_different_order_does_not_count(world) -> None:
    transcript = _transcript("Order 6446 is eligible.", [_call("get_order", {"order_id": 6446})])
    assert not apply_checks(_case(CHECK), transcript, world["db"])["passed"]
