"""Part E: propose a Pass/Fail for every reviewed trace x final mode.

A proposal is not a label. Each one carries the evidence it came from, and
the reviewer confirms proposals (in bulk per mode, after spot checks) before
anything is written to labels/ or Langfuse. Sources, in priority order:

  existing_label   a live label already in labels/<mode>.jsonl (kept as is)
  positive         the trace is a confirmed positive of the mode        -> Fail
  close_negative   the trace is a confirmed close negative of the mode  -> Pass
  note_names_mode  an active open code on the trace names the mode      -> Fail
  not_applicable   the mode's precondition is structurally absent       -> Pass
  no_failure       the reviewer marked the trace 'No failure observed'  -> Pass
  human            none of the above; the reviewer judges it

not_applicable rules (only where the definition cannot be met without it):
  search_arguments_misexpress_request  no search_products call in the turn
  order_list_misreported               no order-lookup result so far in the conversation
  tool_failure_recovery_mishandled     no tool exception so far in the conversation
  invalid_record_presented_as_valid    no data-quality product/order id in any tool result so far
  unrequested_eligibility_verdict      no assistant text in the turn mentions eligibility, refunds or returns

    uv run python -m analysis.review_app.prefill            # dry run: counts only
    uv run python -m analysis.review_app.prefill --write    # write state/label_proposals.json
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from typing import Any

from analysis.helpers import _state
from analysis.review_app.batches import DQ_ORDERS, DQ_PRODUCTS, _load, _tool_results
from analysis.review_app.search_mode import ORDER_TOOLS, _result_product_ids
from analysis.review_app.server import EXCEPTION_PREFIX, _labels_path, _retrying

VERDICT_WORDS = re.compile(r"eligib|refund|return", re.I)


def _calls(turn: dict[str, Any], name: str) -> bool:
    return any(m.get("role") == "tool_call" and m.get("name") == name for m in turn["messages"])


def _exception(turns: list[dict[str, Any]]) -> bool:
    return any(m.get("role") == "tool_result" and EXCEPTION_PREFIX in str(m.get("content"))
               for t in turns for m in t["messages"])


def _order_ids(turns: list[dict[str, Any]]) -> set[int]:
    ids = set()
    for t in turns:
        for c in _tool_results(t):
            for rec in [c.get("order") or {}, *(c.get("orders") or [])]:
                if isinstance(rec, dict) and isinstance(rec.get("order_id"), int):
                    ids.add(rec["order_id"])
    return ids


def not_applicable(mode: str, turn: dict[str, Any], history: list[dict[str, Any]]) -> str | None:
    """The reason the mode cannot apply to this turn, or None if it might."""
    if mode == "search_arguments_misexpress_request" and not _calls(turn, "search_products"):
        return "no search_products call in this turn"
    if mode == "order_list_misreported" and not any(m.get("role") == "tool_result" and m.get("name") in ORDER_TOOLS
                                                    for t in history for m in t["messages"]):
        return "no order-lookup result (list_my_orders, get_order, find_order) so far in the conversation"
    if mode == "tool_failure_recovery_mishandled" and not _exception(history):
        return "no tool exception so far in the conversation"
    if mode == "invalid_record_presented_as_valid" and not (
            _result_product_ids(history) & DQ_PRODUCTS or _order_ids(history) & DQ_ORDERS):
        return "no data-quality product or order id in any tool result so far"
    if mode == "unrequested_eligibility_verdict" and not any(
            VERDICT_WORDS.search(m.get("text") or "") for m in turn["messages"] if m.get("role") == "assistant"):
        return "no assistant text in this turn mentions eligibility, refunds or returns"
    return None


def build() -> list[dict[str, Any]]:
    _, by = _load()
    manifest = _state.read_json(_state.state_path("sample_manifest.json"))
    review = list(dict.fromkeys(manifest["pre_review"]["trace_ids"] + [t for b in manifest["batches"] for t in b["trace_ids"]]))
    modes = _state.read_json(_state.state_path("patterns.json"))["modes"]
    anns = [a for a in _state.read_json(_state.state_path("annotations.json"))["annotations"]
            if a.get("status", "active") == "active"]
    no_fail = {a["trace_id"] for a in anns if a["kind"] == "no_failure"}
    live = {}
    for m in modes:
        for r in _state.read_jsonl(_labels_path(m["name"])):
            if not r.get("superseded_by") and not r.get("withdrawn"):
                live[(r["trace_id"], m["name"])] = r

    out = []
    for tid in review:
        info = by[tid]
        history = info["session"]["turns"][: info["turn"] + 1]
        notes = [a["note"] for a in anns if a["trace_id"] == tid and a["kind"] == "open_code"]
        for m in modes:
            name = m["name"]
            key = (tid, name)
            if key in live:
                p = {"label": live[key]["label"], "source": "existing_label", "why": live[key].get("note") or ""}
            elif tid in m["positive_trace_ids"]:
                p = {"label": 1, "source": "positive", "why": "confirmed positive in patterns.json"}
            elif tid in m["close_negative_trace_ids"]:
                p = {"label": 0, "source": "close_negative", "why": "confirmed close negative in patterns.json"}
            elif any(name in n for n in notes):
                p = {"label": 1, "source": "note_names_mode", "why": next(n for n in notes if name in n)}
            elif (why := not_applicable(name, info["trace"], history)):
                p = {"label": 0, "source": "not_applicable", "why": why}
            elif tid in no_fail:
                p = {"label": 0, "source": "no_failure", "why": "reviewer marked 'No failure observed'"}
            else:
                p = {"label": None, "source": "human", "why": ""}
            # A Fail that the structural rule says is impossible means the rule (or the label) is wrong.
            if p["label"] == 1 and (why := not_applicable(name, info["trace"], history)):
                p["conflict"] = f"Fail, but {why}"
            out.append({"trace_id": tid, "scenario_id": info["session"]["scenario_id"], "turn": info["turn"] + 1,
                        "mode": name, **p})
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true", help="write state/label_proposals.json")
    args = parser.parse_args()
    rows = build()
    traces = {r["trace_id"] for r in rows}
    print(f"{len(traces)} traces x {len(rows) // max(len(traces), 1)} modes = {len(rows)} pairs")
    print("by source:", dict(Counter(r["source"] for r in rows)))
    print(f"{'mode':<38}{'Fail':>5}{'Pass':>5}{'human':>6}   sources")
    for mode in dict.fromkeys(r["mode"] for r in rows):
        rs = [r for r in rows if r["mode"] == mode]
        c = Counter(r["label"] for r in rs)
        print(f"{mode:<38}{c[1]:>5}{c[0]:>5}{c[None]:>6}   {dict(Counter(r['source'] for r in rs))}")
    human_traces = Counter(r["trace_id"] for r in rows if r["source"] == "human")
    print(f"traces needing a human judgment: {len(human_traces)} (pairs: {sum(human_traces.values())})")
    for r in rows:
        if r.get("conflict"):
            print(f"  CONFLICT {r['scenario_id']} t{r['turn']} {r['mode']}: {r['conflict']} ({r['source']})")
    if args.write:
        _retrying(_state.write_json, _state.state_path("label_proposals.json"), rows)
        print("wrote state/label_proposals.json")


if __name__ == "__main__":
    main()
