"""Part D: retrieve candidate instances of one confirmed mode as suggestions.

The search is a retrieval signal, not a label. Every hit goes to
``state/suggestions.json`` as a pending suggestion that the reviewer accepts
or rejects in the review interface. The run, its signal and its counts are
recorded under ``part_d_searches`` in ``state/sample_manifest.json``.

Mode: order_list_misreported (the denial half; it was item_in_results_reported_missing
until the axial merge, and runs v1, v2, B and C were made under that name). A trace
is a candidate when

  A. the user, in this turn or an earlier one, names a product by its title,
  B. a tool result in the conversation so far holds an order or product with
     one of that title's product ids, and
  C. the final reply of the turn denies finding it.

Tier 1 hits meet A, B and C. Tier 2 hits meet A and C but not B: the reply
denies the item and it is really absent from the results, which is the
mode's boundary, so they are expected to be rejected (or become close
negatives). Traces whose open codes already define the mode are counted as a
recall check instead of being suggested again.

    uv run python -m analysis.review_app.search_mode --run v1
    uv run python -m analysis.review_app.search_mode --run v2 --match partial
    uv run python -m analysis.review_app.search_mode --run B --match any
    uv run python -m analysis.review_app.search_mode --run C --match partial --scope orders
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import sqlite3
import sys
from contextlib import closing
from typing import Any

from analysis.helpers import _state
from analysis.review_app.batches import _load, _text, _tool_results
from analysis.review_app.server import REPO, _retrying

MODE = "order_list_misreported"
DENIAL = re.compile(
    r"(?:don'?t|do not|didn'?t|did not|couldn'?t|could not|can'?t|cannot|unable to|wasn'?t able to)\s+"
    r"(?:see|find|locate|spot)|not (?:among|in|part of) (?:them|your|the)|wasn'?t (?:among|in)|"
    r"no (?:record|order|purchase)s? (?:of|for)|a different item",
    re.I,
)


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


GENERIC = {"set", "kit", "pack", "trio", "duo", "bundle", "classic", "compact", "modern", "deluxe", "travel"}


def _titles(match: str) -> dict[str, set[int]]:
    """Map the phrases a user may type to product ids.

    full: the whole title. partial: also the title's last two words and its
    last word (e.g. 'dry bag', 'granola'), skipping short or generic words.
    Partial phrases are shared by many products, so they retrieve more.
    """
    uri = f"file:{REPO / 'data' / 'cartwheel.db'}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        rows = conn.execute("SELECT id, title FROM products WHERE length(title) >= 6").fetchall()
    out: dict[str, set[int]] = {}
    for pid, title in rows:
        words = title.lower().split()
        keys = {" ".join(words)}
        if match == "partial":
            if len(words) >= 3:
                keys.add(" ".join(words[-2:]))
            if len(words[-1]) >= 5 and words[-1] not in GENERIC:
                keys.add(words[-1])
        for key in keys:
            out.setdefault(key, set()).add(pid)
    return out


ORDER_TOOLS = ("list_my_orders", "get_order", "find_order")


def _result_product_ids(turns: list[dict[str, Any]], tools: tuple[str, ...] | None = None) -> set[int]:
    """Product ids in tool results; with tools, only results of those tools."""
    ids: set[int] = set()
    for turn in turns:
        results = _tool_results(turn) if tools is None else (c for t in tools for c in _tool_results(turn, t))
        for c in results:
            for rec in [c.get("order") or {}, *(c.get("orders") or []), *(c.get("products") or [])]:
                if isinstance(rec, dict) and isinstance(rec.get("product_id"), int):
                    ids.add(rec["product_id"])
    return ids


def _denial(turn: dict[str, Any]) -> tuple[int, str] | None:
    """The final reply's index in the turn and the sentence that denies the item."""
    finals = [i for i, m in enumerate(turn["messages"]) if m.get("role") == "assistant" and m.get("text")]
    if not finals:
        return None
    i = finals[-1]
    for sentence in re.split(r"(?<=[.!?])\s+", turn["messages"][i]["text"]):
        if DENIAL.search(sentence):
            return i, sentence.strip()
    return None


def clip_text(text: str, n: int = 120) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def search(run: str, match: str, scope: str = "any") -> dict[str, Any]:
    """scope 'orders' applies the revised definition: only order-lookup results
    count, a named product is required, and only tier 1 matches are suggested.
    It also re-checks every earlier suggestion, so the run shows whether the
    revision separates the accepted traces from the rejected ones."""
    sessions, _ = _load()
    titles = _titles(match)
    patterns = _state.read_json(_state.state_path("patterns.json"), {"modes": []})
    mode = next((m for m in patterns["modes"] if m["name"] == MODE), None)
    if mode is None:
        sys.exit(f"{MODE} is not in patterns.json")
    known = set(mode["positive_trace_ids"])

    sugg_path = _state.state_path("suggestions.json")
    suggestions = _state.read_json(sugg_path, [])
    if any(s.get("search_run") == run and s.get("mode") == MODE for s in suggestions):
        sys.exit(f"run {run} already exists for {MODE}; use a new --run name")
    already = {s["trace_id"] for s in suggestions if s.get("mode") == MODE}
    rejected = {s["trace_id"] for s in suggestions if s.get("mode") == MODE and s.get("status") == "rejected"}
    order_scope = scope == "orders"

    hits, recall, rematched_rejected = [], [], []
    for s in sessions:
        for n, turn in enumerate(s["turns"]):
            history = s["turns"][: n + 1]
            user = " ".join(_text(t, "user") for t in history).lower()
            named = {t: ids for t, ids in titles.items() if re.search(rf"\b{re.escape(t)}\b", user)}
            if not named and match != "any":
                continue
            denial = _denial(turn)
            if denial is None:
                continue
            in_results = _result_product_ids(history, ORDER_TOOLS if order_scope else None)
            found = {t: sorted(ids & in_results) for t, ids in named.items() if ids & in_results}
            tid = turn["trace_id"]
            if order_scope and not found:
                continue
            if tid in known:
                recall.append(tid)
                continue
            if tid in rejected:
                rematched_rejected.append(tid)
            if tid in already:
                continue
            idx, quote = denial
            tier = 1 if found else 2
            if order_scope:
                # Evidence only: the card must not tell the reviewer what to decide.
                where = "; ".join(f"'{t}' = product {', '.join(map(str, ids))}" for t, ids in found.items())
                rationale = (f"Search {run} match: the user named a product ({where}); an order-lookup result in the "
                             f"conversation holds it; the final reply says: \"{clip_text(quote)}\"")
            elif tier == 1:
                why = "; ".join(f"'{t}' is product {', '.join(map(str, ids))}, present in a tool result" for t, ids in found.items())
                rationale = f"Tier 1: the user named the item, it is in the tool results ({why}), and the reply denies it."
            elif named:
                rationale = (f"Tier 2 (boundary probe): the user named {', '.join(repr(t) for t in named)} and the reply "
                             f"denies it, but no tool result holds that product. Expected to fall outside the mode.")
            else:
                tools = sorted({m.get("name") for m in turn["messages"] if m.get("role") == "tool_call"}) or ["no tool"]
                rationale = (f"Tier 2 (boundary probe): the reply denies finding something after {', '.join(tools)}, "
                             f"but the user named no product title. Expected to fall outside the mode.")
            hits.append({
                "id": f"pd-{run}-{tid[:12]}",
                "trace_id": tid,
                "session_id": s["session_id"],
                "scenario_id": s["scenario_id"],
                "mode": MODE,
                "quote": quote,
                "idx": idx,
                "rationale": rationale,
                "evidence": {"tier": tier, "named_titles": sorted(named), "matched_product_ids": found},
                "source": f"search_mode.py {run}",
                "search_run": run,
                "status": "pending",
                "created_at": _utcnow(),
            })

    # The review server may be reading these files; retry past a Windows sharing violation.
    _retrying(_state.write_json, sugg_path, suggestions + hits)
    manifest_path = _state.state_path("sample_manifest.json")
    manifest = _state.read_json(manifest_path)
    record = {
        "run": run,
        "mode": MODE,
        "definition_at_run": mode["definition"],
        "signal": (f"user names a product ({match} title match); an order-lookup result ({', '.join(ORDER_TOOLS)}) "
                   f"holds that product id; the final reply denies finding it" if order_scope else
                   f"user names a product ({match} title match); a tool result holds that product id (tier 1) or "
                   f"not (tier 2); the final reply denies finding it"),
        "title_match": match,
        "scope": scope,
        "command": f"uv run python -m analysis.review_app.search_mode --run {run} --match {match}"
                   + (f" --scope {scope}" if order_scope else ""),
        "run_at": _utcnow(),
        "population": "all final-run traces",
        "known_positives": len(known),
        "known_positives_retrieved": len(recall),
        "previously_rejected_rematched": len(rematched_rejected),
        "suggested": len(hits),
        "tier_counts": {t: sum(h["evidence"]["tier"] == t for h in hits) for t in (1, 2)},
        "trace_ids": [h["trace_id"] for h in hits],
    }
    manifest.setdefault("part_d_searches", []).append(record)
    _retrying(_state.write_json, manifest_path, manifest)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True, help="run name, e.g. v1; a revised definition gets a new run")
    parser.add_argument("--match", choices=["full", "partial", "any"], default="full",
                        help="how product names are matched; any = every denial reply, named product or not")
    parser.add_argument("--scope", choices=["any", "orders"], default="any",
                        help="orders = the revised definition: only order-lookup tool results count")
    args = parser.parse_args()
    record = search(args.run, args.match, args.scope)
    print(f"{record['mode']} run {record['run']}: {record['suggested']} suggestions {record['tier_counts']}; "
          f"recall {record['known_positives_retrieved']}/{record['known_positives']} known positives; "
          f"previously rejected re-matched: {record['previously_rejected_rematched']}")


if __name__ == "__main__":
    main()
