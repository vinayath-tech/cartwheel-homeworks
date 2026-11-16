"""Tests for the search_arguments_misexpress_request rules.

Each test builds a small search call by hand and checks one behaviour.
Many of the examples are copied from real traces, named in the comments.

Run them:
    uv run pytest tests/test_search_rules.py -v
"""

import pytest

from analysis.code_evals.catalog import Catalog, normalise
from analysis.code_evals.search_evaluator import SearchArgumentsEvaluator
from analysis.code_evals.search_rules import SearchRules, numbers_user_wrote
from analysis.code_evals.trace_parts import SearchCall, TraceParts, get_search_calls, get_user_text
from analysis.code_evals.validate import disagreements, rows_with_search, score


@pytest.fixture
def rules(world):
    """The rules, using the temporary test database from conftest.py."""
    return SearchRules(Catalog(world["db"]))


# ----- normalise -----


def test_normalise_makes_two_spellings_equal():
    assert normalise("home_and_kitchen") == "home kitchen"
    assert normalise("Home/Kitchen") == "home kitchen"

# ----- Catalog -----

def test_is_category_plural_ending_with_ies(world):
    catalog = Catalog(world["db"])
    assert catalog.is_category("groceries") is True


# ----- Rule A: store_name_in_query -----


def test_store_name_in_query_fires_when_store_is_empty(rules):
    # support-0114 turn 0
    call = SearchCall(query="Atlas Stationery", store=None, max_price_usd=60)
    assert rules.store_name_in_query(call) is not None


def test_store_name_in_query_fires_when_title_words_come_first(rules):
    # support-0196 turn 0
    call = SearchCall(query="rustic pitcher Blue Heron Ceramics", store=None, max_price_usd=None)
    assert rules.store_name_in_query(call) is not None


def test_store_name_in_query_passes_when_store_is_set(rules):
    # support-0198 turn 0
    call = SearchCall(query="Rustic Pitcher", store="Blue Heron Ceramics", max_price_usd=None)
    assert rules.store_name_in_query(call) is None


def test_store_name_in_query_passes_for_ordinary_title_words(rules):
    # support-0250 turn 0
    call = SearchCall(query="blue storage basket", store=None, max_price_usd=None)
    assert rules.store_name_in_query(call) is None


# ----- Rule B: store_id_as_name -----


def test_store_id_as_name_fires_for_a_number_in_text(rules):
    # support-0202 turn 0
    call = SearchCall(query="Blue Heron Ceramics", store="1", max_price_usd=None)
    assert rules.store_id_as_name(call) is not None


def test_store_id_as_name_fires_for_a_real_number(rules):
    call = SearchCall(query="vase", store=1, max_price_usd=None)
    assert rules.store_id_as_name(call) is not None


def test_store_id_as_name_passes_for_a_store_name(rules):
    call = SearchCall(query="Rustic Pitcher", store="Blue Heron Ceramics", max_price_usd=None)
    assert rules.store_id_as_name(call) is None


def test_store_id_as_name_passes_when_store_is_empty(rules):
    call = SearchCall(query="vase", store=None, max_price_usd=None)
    assert rules.store_id_as_name(call) is None


# ----- Rule C: category_as_query -----


def test_category_as_query_fires_when_store_is_empty(rules):
    # support-0127 turn 0
    call = SearchCall(query="grocery", store=None, max_price_usd=40)
    assert rules.category_as_query(call) is not None

def test_category_as_query_fires_for_a_plural_ending_in_ies(rules):
    call = SearchCall(query="groceries", store=None, max_price_usd=None)
    assert rules.category_as_query(call) is not None

def test_category_as_query_handles_a_different_spelling(rules):
    # support-0115 turn 0: the catalog spells it home_and_kitchen
    call = SearchCall(query="home kitchen", store=None, max_price_usd=25)
    assert rules.category_as_query(call) is not None


def test_category_as_query_passes_when_store_is_set(rules):
    # support-0120 turn 0: reviewer decision 1
    call = SearchCall(query="books", store="Northwind Books", max_price_usd=20)
    assert rules.category_as_query(call) is None


def test_category_as_query_passes_for_title_words(rules):
    call = SearchCall(query="blue storage basket", store=None, max_price_usd=None)
    assert rules.category_as_query(call) is None


def test_category_as_query_does_not_catch_topic_words(rules):
    # support-0116 turn 0: the known gap in README.md
    call = SearchCall(query="gardening", store=None, max_price_usd=25)
    assert rules.category_as_query(call) is None


# ----- Rule D: unstated_price_cap -----


def test_unstated_price_cap_fires_for_a_cap_from_a_vague_word(rules):
    # support-0113 turn 0: $25 invented from "super affordable"
    call = SearchCall(query="electronics", store=None, max_price_usd=25)
    assert rules.unstated_price_cap(call, "keep it super affordable") is not None


def test_unstated_price_cap_passes_when_the_user_wrote_the_number(rules):
    # support-0114 turn 0
    call = SearchCall(query="Atlas Stationery", store=None, max_price_usd=60)
    assert rules.unstated_price_cap(call, "Show me something under $60.") is None


def test_unstated_price_cap_passes_when_there_is_no_cap(rules):
    call = SearchCall(query="vase", store=None, max_price_usd=None)
    assert rules.unstated_price_cap(call, "show me a vase") is None


def test_unstated_price_cap_is_not_excused_by_a_different_number(rules):
    # The reason for reviewer decision 4: any digit used to be enough.
    call = SearchCall(query="vase", store=None, max_price_usd=25)
    assert rules.unstated_price_cap(call, "a vase like the one in order 3124") is not None


def test_unstated_price_cap_does_not_understand_numbers_in_words(rules):
    # Known limit: "twenty" is not read as 20, so this is a false alarm.
    call = SearchCall(query="vase", store=None, max_price_usd=20)
    assert rules.unstated_price_cap(call, "a vase under twenty dollars") is not None


def test_numbers_user_wrote_reads_prices():
    assert numbers_user_wrote("under $20.50 or 1,000 dollars") == [20.5, 1000.0]
    assert numbers_user_wrote("no numbers here") == []


# ----- check_call: all four rules together -----


def test_check_call_reports_every_rule_that_fired(rules):
    # support-0113 turn 0
    call = SearchCall(query="electronics", store=None, max_price_usd=25)
    problems = rules.check_call(call, "keep it super affordable")
    assert list(problems) == ["category_as_query", "unstated_price_cap"]


def test_check_call_returns_nothing_for_a_good_call(rules):
    # support-0120 turn 0
    call = SearchCall(query="books", store="Northwind Books", max_price_usd=20)
    assert rules.check_call(call, "browse from Northwind Books, budget max $20") == {}


# ----- Reading a trace -----


def make_session():
    """A small two-turn conversation written by hand."""
    first_turn = {
        "messages": [
            {"role": "user", "text": "my budget is $60"},
            {
                "role": "tool_call",
                "name": "search_products",
                "arguments": {"query": "vase", "max_price_usd": 60},
            },
            {"role": "tool_call", "name": "get_order", "arguments": {"order_id": 1}},
        ]
    }
    second_turn = {"messages": [{"role": "user", "text": "anything else?"}]}
    return {"turns": [first_turn, second_turn]}


def test_get_search_calls_ignores_other_tools():
    session = make_session()
    search_calls = get_search_calls(session["turns"][0])
    assert len(search_calls) == 1
    assert search_calls[0].query == "vase"


def test_get_search_calls_treats_a_missing_argument_as_none():
    session = make_session()
    search_calls = get_search_calls(session["turns"][0])
    assert search_calls[0].store is None


def test_get_user_text_includes_earlier_turns():
    # Reviewer decision 4: a budget stated earlier still counts.
    session = make_session()
    assert "$60" in get_user_text(session, 1)


def test_get_user_text_leaves_out_later_turns():
    session = make_session()
    assert "anything else" not in get_user_text(session, 0)


# ----- The evaluator: one verdict for one turn -----


@pytest.fixture
def evaluator(world):
    """The evaluator, using the temporary test database from conftest.py."""
    return SearchArgumentsEvaluator(Catalog(world["db"]))


def make_parts(user_text, search_calls):
    """A small turn written by hand."""
    return TraceParts("trace-1", "test-scenario", 0, user_text, search_calls)


def test_evaluator_passes_a_turn_with_no_search_call(evaluator):
    verdict = evaluator.evaluate(make_parts("where is my order?", []))
    assert verdict.result == "Pass"
    assert verdict.rules_fired == []


def test_evaluator_passes_a_turn_with_a_good_search_call(evaluator):
    # support-0120 turn 0
    call = SearchCall(query="books", store="Northwind Books", max_price_usd=20)
    verdict = evaluator.evaluate(make_parts("Northwind Books, budget max $20", [call]))
    assert verdict.result == "Pass"


def test_evaluator_fails_a_turn_with_a_bad_search_call(evaluator):
    # support-0114 turn 0
    call = SearchCall(query="Atlas Stationery", store=None, max_price_usd=60)
    verdict = evaluator.evaluate(make_parts("Atlas Stationery under $60", [call]))
    assert verdict.result == "Fail"
    assert verdict.rules_fired == ["store_name_in_query"]

def test_evaluator_fails_a_turn_with_groceries_category(evaluator):
    call = SearchCall(query="groceries", store=None, max_price_usd=None)
    verdict = evaluator.evaluate(make_parts("List orders with groceries category", [call]))
    assert verdict.result == "Fail"
    assert verdict.rules_fired == ["category_as_query"]

def test_evaluator_fails_when_only_one_of_two_calls_is_bad(evaluator):
    # support-0193 turn 0: the second call is fine, the first is not
    bad_call = SearchCall(query="Heavy-Duty Vase", store="1", max_price_usd=None)
    good_call = SearchCall(query="Heavy-Duty Vase", store=None, max_price_usd=None)
    verdict = evaluator.evaluate(make_parts("do you have a vase?", [bad_call, good_call]))
    assert verdict.result == "Fail"
    assert verdict.rules_fired == ["store_id_as_name"]


def test_evaluator_gives_one_reason_for_each_rule_that_fired(evaluator):
    # support-0113 turn 0
    call = SearchCall(query="electronics", store=None, max_price_usd=25)
    verdict = evaluator.evaluate(make_parts("keep it super affordable", [call]))
    assert len(verdict.reasons) == 2


def test_evaluator_keeps_the_identity_of_the_turn(evaluator):
    verdict = evaluator.evaluate(make_parts("hello", []))
    assert verdict.trace_id == "trace-1"
    assert verdict.scenario_id == "test-scenario"
    assert verdict.turn == 0


# ----- Validation: comparing verdicts with labels -----


def make_row(label, evaluator_result, search_calls=1):
    """One row of the comparison, with only the fields these tests need."""
    return {"label": label, "evaluator": evaluator_result, "search_calls": search_calls}


def test_score_counts_all_four_outcomes():
    rows = [
        make_row("Pass", "Pass"),
        make_row("Pass", "Pass"),
        make_row("Pass", "Fail"),
        make_row("Fail", "Fail"),
        make_row("Fail", "Pass"),
    ]
    result = score(rows)
    assert result["both_pass"] == 2
    assert result["false_alarm"] == 1
    assert result["both_fail"] == 1
    assert result["missed_fail"] == 1


def test_score_works_out_the_rates():
    rows = [
        make_row("Pass", "Pass"),
        make_row("Pass", "Fail"),
        make_row("Fail", "Fail"),
        make_row("Fail", "Fail"),
    ]
    result = score(rows)
    assert result["tpr"] == 0.5  # 1 of 2 human Pass turns
    assert result["tnr"] == 1.0  # 2 of 2 human Fail turns


def test_rows_with_search_drops_turns_with_no_search_call():
    rows = [make_row("Pass", "Pass", search_calls=0), make_row("Pass", "Pass", search_calls=2)]
    assert len(rows_with_search(rows)) == 1


def test_disagreements_keeps_only_differences():
    rows = [make_row("Pass", "Pass"), make_row("Fail", "Pass")]
    assert disagreements(rows) == [make_row("Fail", "Pass")]
