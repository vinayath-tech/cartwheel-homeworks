"""The four rules for search_arguments_misexpress_request.

Each rule looks at one search_products call.
If the call breaks the rule, the rule returns a sentence saying why.
If the call is fine, the rule returns None.

Try it:
    uv run python -m analysis.code_evals.search_rules
"""

import re

from analysis.code_evals.catalog import Catalog
from analysis.code_evals.trace_parts import find, load_all_trace_parts


def store_is_empty(call):
    """Return True when the agent did not fill in the store argument."""
    return call.store is None or call.store == ""


def numbers_user_wrote(user_text):
    """Return every number in the text: "$20 or 1,000" gives [20.0, 1000.0]."""
    text_without_commas = user_text.replace(",", "")

    # Find digits, optionally followed by a decimal part.
    number_strings = re.findall(r"\d+\.?\d*", text_without_commas)

    numbers = []
    for number_string in number_strings:
        numbers.append(float(number_string))
    return numbers


class SearchRules:
    """The four rules. Each rule is one method."""

    def __init__(self, catalog):
        self.catalog = catalog

    def store_name_in_query(self, call):
        """Rule A: the store name belongs in store, not in query."""
        if not store_is_empty(call):
            return None

        store_name = self.catalog.find_store_name_in(call.query or "")
        if store_name is None:
            return None

        return f"query '{call.query}' holds store name '{store_name}' and store is empty"

    def store_id_as_name(self, call):
        """Rule B: store must be a name such as 'Atlas Stationery', not a number."""
        if store_is_empty(call):
            return None

        if not str(call.store).isdigit():
            return None

        return f"store is the id '{call.store}', not a store name"

    def category_as_query(self, call):
        """Rule C: query must not be only a category when no store is set.

        A category word is allowed when the store is set (support-0120 t0).
        """
        if not store_is_empty(call):
            return None

        if not self.catalog.is_category(call.query or ""):
            return None

        return f"query '{call.query}' is a catalog category, not title words"

    def unstated_price_cap(self, call, user_text):
        """Rule D: the price cap must be a number the user wrote."""
        if call.max_price_usd is None:
            return None

        if float(call.max_price_usd) in numbers_user_wrote(user_text):
            return None

        return f"max_price_usd is {call.max_price_usd} but the user never wrote that number"

    def check_call(self, call, user_text):
        """Run all four rules on one call.

        Returns a dictionary of the rules that fired: {rule name: reason}.
        The dictionary is empty when the call is fine.
        """
        results = {
            "store_name_in_query": self.store_name_in_query(call),
            "store_id_as_name": self.store_id_as_name(call),
            "category_as_query": self.category_as_query(call),
            "unstated_price_cap": self.unstated_price_cap(call, user_text),
        }

        problems = {}
        for rule_name, reason in results.items():
            if reason is not None:
                problems[rule_name] = reason
        return problems


# This block runs only when you run this file directly.
if __name__ == "__main__":
    rules = SearchRules(Catalog())
    all_parts = load_all_trace_parts()

    examples = [
        ("support-0114", 0),
        ("support-0127", 0),
        ("support-0113", 0),
        ("support-0115", 0),
        ("support-0120", 0),
    ]

    for scenario_id, turn in examples:
        parts = find(all_parts, scenario_id, turn)
        for call in parts.search_calls:
            print(parts.scenario_id, "turn", parts.turn, "| query:", call.query, "| store:", call.store)
            print("  ", rules.check_call(call, parts.user_text))
