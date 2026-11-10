"""Pull out of a trace the two things the search rules look at.

1. The search_products calls the agent made in the judged turn.
2. Everything the user has written in the conversation so far.

Try it:
    uv run python -m analysis.code_evals.trace_parts
"""

from analysis.review_app.batches import _load


class SearchCall:
    """One search_products call and the arguments the agent chose."""

    def __init__(self, query, store, max_price_usd):
        self.query = query                  # keywords matched against title and description
        self.store = store                  # store name filter, or None
        self.max_price_usd = max_price_usd  # price cap, or None


class TraceParts:
    """The parts of one judged turn that the rules need."""

    def __init__(self, trace_id, scenario_id, turn, user_text, search_calls):
        self.trace_id = trace_id
        self.scenario_id = scenario_id    # for example "support-0114"
        self.turn = turn                  # 0 for the first turn
        self.user_text = user_text        # all user messages so far
        self.search_calls = search_calls  # list of SearchCall, may be empty


def get_search_calls(turn):
    """Return the search_products calls made in this one turn."""
    search_calls = []

    for message in turn["messages"]:
        if message.get("role") == "tool_call" and message.get("name") == "search_products":
            arguments = message.get("arguments") or {}

            # .get gives None when the agent left an argument out.
            call = SearchCall(
                query=arguments.get("query"),
                store=arguments.get("store"),
                max_price_usd=arguments.get("max_price_usd"),
            )
            search_calls.append(call)

    return search_calls


def get_user_text(session, turn_index):
    """Return what the user wrote in this turn and every earlier turn.

    A user can state a budget once and rely on it later. Later turns are left
    out because the agent could not have known them yet.
    """
    user_messages = []

    for turn in session["turns"][: turn_index + 1]:
        for message in turn["messages"]:
            if message.get("role") == "user":
                user_messages.append(message.get("text") or "")

    return "\n".join(user_messages)


def read_trace_parts(session, turn_index):
    """Make a TraceParts for one turn of a conversation."""
    turn = session["turns"][turn_index]

    return TraceParts(
        trace_id=turn["trace_id"],
        scenario_id=session["scenario_id"],
        turn=turn_index,
        user_text=get_user_text(session, turn_index),
        search_calls=get_search_calls(turn),
    )


def load_all_trace_parts():
    """Return TraceParts for every turn in the export, keyed by trace id."""
    # _load is the review app's loader. It reads traces/support_traces.json.
    sessions, entries = _load()

    all_parts = {}
    for trace_id, entry in entries.items():
        all_parts[trace_id] = read_trace_parts(entry["session"], entry["turn"])

    return all_parts


def find(all_parts, scenario_id, turn):
    """Find one turn by its readable name, for example ("support-0114", 0)."""
    for parts in all_parts.values():
        if parts.scenario_id == scenario_id and parts.turn == turn:
            return parts

    raise KeyError(f"no trace for {scenario_id} turn {turn}")


# This block runs only when you run this file directly.
if __name__ == "__main__":
    all_parts = load_all_trace_parts()
    print(len(all_parts), "turns loaded\n")

    parts = find(all_parts, "support-0114", 1)
    print(parts.scenario_id, "turn", parts.turn)
    print("User wrote:", parts.user_text)
    print("Search calls in this turn:", len(parts.search_calls))
    print()

    parts = find(all_parts, "support-0115", 0)
    print(parts.scenario_id, "turn", parts.turn)
    print("User wrote:", parts.user_text)
    for call in parts.search_calls:
        print("Search:", call.query, "| store:", call.store, "| max price:", call.max_price_usd)
