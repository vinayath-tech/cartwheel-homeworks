"""HW6: the search_arguments_follow_request check must agree with HW5.

The check in replay/rollout.py copies the HW5 code evaluator in
analysis/code_evals/. These tests run both copies on every HW4 turn and
fail if they ever disagree. They run offline and do not call a model.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from analysis.code_evals.catalog import Catalog
from analysis.code_evals.search_evaluator import SearchArgumentsEvaluator
from analysis.code_evals.trace_parts import load_all_trace_parts
from replay.rollout import _search_catalog, apply_checks, search_call_problems

REPO_ROOT = Path(__file__).resolve().parents[1]
HW4_DB = REPO_ROOT / "data" / "cartwheel.db"
HW4_TRACES = REPO_ROOT / "traces" / "support_traces.json"

needs_hw4_data = pytest.mark.skipif(
    not HW4_DB.exists() or not HW4_TRACES.exists(),
    reason="needs the HW4 database and trace export",
)


@needs_hw4_data
def test_both_copies_agree_on_every_hw4_turn() -> None:
    hw5_evaluator = SearchArgumentsEvaluator(Catalog(HW4_DB))
    connection = sqlite3.connect(HW4_DB)
    store_names, categories = _search_catalog(connection)
    connection.close()

    all_parts = load_all_trace_parts()
    turns_with_search = 0
    disagreements = []
    for parts in all_parts.values():
        hw5_verdict = hw5_evaluator.evaluate(parts)

        problems = []
        for call in parts.search_calls:
            args = {
                "query": call.query,
                "store": call.store,
                "max_price_usd": call.max_price_usd,
            }
            problems.extend(
                search_call_problems(args, parts.user_text, store_names, categories)
            )
        hw6_rules = [problem.split(":")[0] for problem in problems]

        if parts.search_calls:
            turns_with_search += 1
        if hw6_rules != hw5_verdict.rules_fired:
            disagreements.append(
                (parts.scenario_id, parts.turn, hw5_verdict.rules_fired, hw6_rules)
            )

    assert turns_with_search > 0, "expected HW4 turns with a search_products call"
    assert disagreements == []


def _transcript(turns: list[dict]) -> dict:
    return {"turns": turns, "final_reply": turns[-1]["reply"]}


def _search_turn(user: str, args: dict) -> dict:
    return {
        "user": user,
        "reply": "Here is what I found.",
        "tool_calls": [{"name": "search_products", "args": args, "result": {}}],
    }


def _case() -> dict:
    return {"expected": {"checks": [{"check": "search_arguments_follow_request"}]}}


@pytest.fixture
def seeded_db(world) -> Path:
    """The fresh seeded database from conftest.py, like a Harbor run."""
    return world["db"]


def _a_store_name(db: Path) -> str:
    connection = sqlite3.connect(db)
    name = connection.execute("SELECT name FROM stores ORDER BY id LIMIT 1").fetchone()[0]
    connection.close()
    return name


def test_check_passes_good_arguments(seeded_db: Path) -> None:
    store = _a_store_name(seeded_db)
    transcript = _transcript(
        [_search_turn(f"blue mug from {store} under $20", {"query": "blue mug", "store": store, "max_price_usd": 20})]
    )
    assert apply_checks(_case(), transcript, seeded_db)["passed"] is True


def test_check_fails_store_name_in_query(seeded_db: Path) -> None:
    store = _a_store_name(seeded_db)
    transcript = _transcript(
        [_search_turn(f"anything from {store}?", {"query": f"{store} mug", "store": None})]
    )
    outcome = apply_checks(_case(), transcript, seeded_db)
    assert outcome["passed"] is False
    assert "store_name_in_query" in outcome["failed"][0]


def test_check_fails_unstated_price_cap(seeded_db: Path) -> None:
    transcript = _transcript(
        [_search_turn("a cheap blue mug please", {"query": "blue mug", "max_price_usd": 15})]
    )
    outcome = apply_checks(_case(), transcript, seeded_db)
    assert outcome["passed"] is False
    assert "unstated_price_cap" in outcome["failed"][0]


def test_price_stated_in_an_earlier_turn_counts(seeded_db: Path) -> None:
    first = {"user": "my budget is $25", "reply": "Sure.", "tool_calls": []}
    second = _search_turn("show me blue mugs", {"query": "blue mug", "max_price_usd": 25})
    assert apply_checks(_case(), _transcript([first, second]), seeded_db)["passed"] is True


def test_run_without_a_search_passes_this_check(seeded_db: Path) -> None:
    transcript = _transcript([{"user": "hi", "reply": "Hello!", "tool_calls": []}])
    assert apply_checks(_case(), transcript, seeded_db)["passed"] is True
