"""The code evaluator for search_arguments_misexpress_request.

It gives one turn of a conversation one verdict: Pass or Fail.

Try it:
    uv run python -m analysis.code_evals.search_evaluator
"""

from analysis.code_evals.catalog import Catalog
from analysis.code_evals.search_rules import SearchRules
from analysis.code_evals.trace_parts import find, load_all_trace_parts


class Verdict:
    """The evaluator's answer for one turn."""

    def __init__(self, trace_id, scenario_id, turn, result, rules_fired, reasons):
        self.trace_id = trace_id
        self.scenario_id = scenario_id
        self.turn = turn
        self.result = result            # "Pass" or "Fail"
        self.rules_fired = rules_fired  # for example ["category_as_query"]
        self.reasons = reasons          # one sentence for each rule that fired


class SearchArgumentsEvaluator:
    """Runs the rules on every search call in a turn."""

    def __init__(self, catalog):
        self.rules = SearchRules(catalog)

    def evaluate(self, parts):
        """Return a Verdict for one turn.

        One broken rule on one search call is enough for Fail.
        A turn with no search call has nothing to break, so it is Pass.
        """
        rules_fired = []
        reasons = []

        for call in parts.search_calls:
            problems = self.rules.check_call(call, parts.user_text)
            for rule_name, reason in problems.items():
                rules_fired.append(rule_name)
                reasons.append(reason)

        if len(rules_fired) > 0:
            result = "Fail"
        else:
            result = "Pass"

        return Verdict(
            trace_id=parts.trace_id,
            scenario_id=parts.scenario_id,
            turn=parts.turn,
            result=result,
            rules_fired=rules_fired,
            reasons=reasons,
        )


def print_verdict(verdict):
    print(verdict.scenario_id, "turn", verdict.turn, "->", verdict.result)
    for reason in verdict.reasons:
        print("   ", reason)


# This block runs only when you run this file directly.
if __name__ == "__main__":
    evaluator = SearchArgumentsEvaluator(Catalog())
    all_parts = load_all_trace_parts()

    examples = [
        ("support-0114", 0),  # store name in query
        ("support-0113", 0),  # two rules fire
        ("support-0120", 0),  # search with good arguments
        ("support-0114", 1),  # no search call
        ("support-0193", 0),  # two search calls, one bad
    ]
    for scenario_id, turn in examples:
        print_verdict(evaluator.evaluate(find(all_parts, scenario_id, turn)))

    pass_count = 0
    fail_count = 0
    for parts in all_parts.values():
        verdict = evaluator.evaluate(parts)
        if verdict.result == "Pass":
            pass_count += 1
        else:
            fail_count += 1

    print()
    print("All turns:", len(all_parts), "| Pass:", pass_count, "| Fail:", fail_count)
