"""Part D follow-up searches, written as suggestions for the reviewer.

recheck-search-args (run R1): a mode found late (search_arguments_misexpress_request,
  from the AgentDebug comparison) may apply to traces reviewed before it existed.
  Every reviewed trace with a search_products call that is not already in the mode
  is suggested, with the user's words, the arguments and the result count. The card
  states evidence only; the reviewer decides.

close-negatives-assumed-target (run CN1): acts_on_assumed_target has no close
  negatives. A candidate looks like the mode but may pass: either the final reply asks
  the user for the order or a clarification, or the user gave no order id and
  list_my_orders returned several orders. These suggestions carry
  purpose='close_negative', so accepting one records a Pass, not a Fail.

    uv run python -m analysis.review_app.search_recheck recheck-search-args
    uv run python -m analysis.review_app.search_recheck close-negatives-assumed-target
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import sys
from typing import Any

from analysis.helpers import _state
from analysis.review_app.batches import _load, _text, _tool_results
from analysis.review_app.search_mode import clip_text
from analysis.review_app.server import _retrying


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _reviewed() -> list[str]:
    manifest = _state.read_json(_state.state_path("sample_manifest.json"))
    return list(dict.fromkeys(t for b in manifest["batches"] for t in b["trace_ids"]))


def _mode(name: str) -> dict[str, Any]:
    patterns = _state.read_json(_state.state_path("patterns.json"), {"modes": []})
    mode = next((m for m in patterns["modes"] if m["name"] == name), None)
    if mode is None:
        sys.exit(f"{name} is not in patterns.json")
    return mode


def _first_user(turn: dict[str, Any]) -> tuple[int, str] | None:
    for i, m in enumerate(turn["messages"]):
        if m.get("role") == "user" and m.get("text"):
            return i, re.split(r"(?<=[.!?])\s+", m["text"].strip())[0]
    return None


def _final(turn: dict[str, Any]) -> tuple[int, str] | None:
    finals = [i for i, m in enumerate(turn["messages"]) if m.get("role") == "assistant" and m.get("text")]
    return (finals[-1], turn["messages"][finals[-1]]["text"]) if finals else None


def recheck_search_args(by: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    name = "search_arguments_misexpress_request"
    mode = _mode(name)
    known = set(mode["positive_trace_ids"]) | set(mode["close_negative_trace_ids"])
    hits = []
    for tid in _reviewed():
        info = by[tid]
        turn = info["trace"]
        calls = [m for m in turn["messages"] if m.get("role") == "tool_call" and m.get("name") == "search_products"]
        if not calls or tid in known:
            continue
        counts = [c.get("count", len(c.get("products") or [])) for c in _tool_results(turn, "search_products")]
        args = "; ".join(", ".join(f"{k}={v!r}" for k, v in (c.get("arguments") or c.get("args") or {}).items() if k != "limit") for c in calls)
        anchor = _first_user(turn)
        hits.append({
            "trace_id": tid, "info": info, "anchor": anchor,
            "rationale": f"Recheck: search_products was called with {args}; results returned: "
                         f"{', '.join(map(str, counts)) or 'none (tool error)'}. The user said: "
                         f"\"{clip_text(_text(turn, 'user'), 160)}\"",
            "evidence": {"search_args": [c.get("arguments") or c.get("args") for c in calls], "result_counts": counts},
        })
    return {"mode": name, "purpose": "positive", "population": "reviewed traces (Batches 1-3)",
            "signal": "trace calls search_products and is not yet a positive or close negative of the mode",
            "known_excluded": len(known)}, hits


ASK = re.compile(r"\bwhich\b|do you mean|did you mean|are you referring|could you clarify|can you clarify|"
                 r"specify|more details about|order (?:number|id)", re.I)
INTENT = re.compile(r"refund|return|money back|broke|damaged|where is|hasn'?t arrived|cancel", re.I)


def close_negatives_assumed_target(by: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    name = "acts_on_assumed_target"
    mode = _mode(name)
    known = set(mode["positive_trace_ids"]) | set(mode["close_negative_trace_ids"])
    hits = []
    for tid, info in by.items():
        if tid in known:
            continue
        turn, final = info["trace"], _final(info["trace"])
        if final is None:
            continue
        idx, text = final
        asks = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip().endswith("?") and ASK.search(s)]
        history = info["session"]["turns"][: info["turn"] + 1]
        user_all = " ".join(_text(t, "user") for t in history)
        lists = [c for c in _tool_results(turn, "list_my_orders") if (c.get("count") or len(c.get("orders") or [])) > 1]
        no_id_many = (not re.search(r"\b\d{3,4}\b", user_all) and INTENT.search(_text(turn, "user")) and lists)
        if not asks and not no_id_many:
            continue
        returned = {str(o["order_id"]) for c in lists for o in c.get("orders") or []}
        named = sorted(set(re.findall(r"\b\d{3,4}\b", text)) & returned)
        if asks:
            what, quote = "the final reply asks the user to identify the order or clarify", asks[0].strip()
        else:
            what = (f"the user gave no order id; list_my_orders returned {max(c.get('count', 0) for c in lists)} orders; "
                    f"the reply names {len(named)} of them")
            quote = re.split(r"(?<=[.!?])\s+", text.strip())[0]
        hits.append({
            "trace_id": tid, "info": info, "anchor": (idx, quote),
            "rationale": f"Close-negative candidate: {what}. The user said: \"{clip_text(_text(turn, 'user'), 140)}\"",
            "evidence": {"asks": bool(asks), "no_id_many_orders": bool(no_id_many), "order_ids_in_reply": named,
                         "reviewed": tid in set(_reviewed())},
        })
    return {"mode": name, "purpose": "close_negative", "population": "all final-run traces",
            "signal": "final reply asks which order / for an order id or clarification, OR the user gave no order "
                      "id with a refund/return/delivery request and list_my_orders returned more than one order",
            "known_excluded": len(known)}, hits


SEARCHES = {"recheck-search-args": ("R1", recheck_search_args),
            "close-negatives-assumed-target": ("CN1", close_negatives_assumed_target)}


def run(which: str) -> dict[str, Any]:
    run_id, fn = SEARCHES[which]
    _, by = _load()
    meta, hits = fn(by)
    sugg_path = _state.state_path("suggestions.json")
    suggestions = _state.read_json(sugg_path, [])
    if any(s.get("search_run") == run_id for s in suggestions):
        sys.exit(f"run {run_id} already exists; use a new run id")
    new = []
    for h in hits:
        s, idx_quote = h["info"]["session"], h["anchor"] or (None, None)
        new.append({
            "id": f"pd-{run_id}-{h['trace_id'][:12]}",
            "trace_id": h["trace_id"], "session_id": s["session_id"], "scenario_id": s["scenario_id"],
            "mode": meta["mode"], "purpose": meta["purpose"],
            "quote": idx_quote[1], "idx": idx_quote[0],
            "rationale": h["rationale"], "evidence": h["evidence"],
            "source": f"search_recheck.py {which}", "search_run": run_id,
            "status": "pending", "created_at": _utcnow(),
        })
    _retrying(_state.write_json, sugg_path, suggestions + new)
    manifest_path = _state.state_path("sample_manifest.json")
    manifest = _state.read_json(manifest_path)
    record = {"run": run_id, **meta, "command": f"uv run python -m analysis.review_app.search_recheck {which}",
              "run_at": _utcnow(), "suggested": len(new), "trace_ids": [n["trace_id"] for n in new]}
    manifest.setdefault("part_d_searches", []).append(record)
    _retrying(_state.write_json, manifest_path, manifest)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("search", choices=sorted(SEARCHES))
    record = run(parser.parse_args().search)
    print(f"{record['mode']} run {record['run']} ({record['purpose']}): {record['suggested']} suggestions")


if __name__ == "__main__":
    main()
