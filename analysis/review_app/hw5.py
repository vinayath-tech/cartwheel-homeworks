"""Homework 5 labels for one mode, kept apart from the Homework 4 labels.

HW5 uses 1 = Pass and 0 = Fail; HW4 used 1 = failure present. The HW4 files
in state/labels/ are never written here. HW5 labels go to
state/hw5_labels/<mode>.jsonl, the file the Cartwheel helpers read once it
exists (analysis/helpers/tools.py: _load_labels).

export: carry the HW4 labels over (flipped), except the traces listed for
  re-judging under the HW5 definition. Those go to state/hw5_queue.json and get
  a label only when the reviewer decides. A trace that already has an HW5
  record is never touched, so the export can be rerun safely.

    uv run python -m analysis.review_app.hw5 export --rejudge <trace_id> ...
"""

from __future__ import annotations

import argparse
import datetime as _dt
from typing import Any

from analysis.helpers import _state

MODE = "policy_answer_missing_specifics"


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def labels_path():
    return _state.state_path("hw5_labels", f"{MODE}.jsonl")


def live() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in _state.read_jsonl(labels_path()):
        if not row.get("superseded_by"):
            out[row["trace_id"]] = row
    return out


def queue() -> list[dict[str, Any]]:
    return _state.read_json(_state.state_path("hw5_queue.json"), [])


def dev_judgments() -> dict[str, Any]:
    """Verdicts and critiques of the latest judge version, on the dev split only.

    Test predictions are never returned: the reviewer must not see them before
    the judge is frozen.
    """
    history = _state.read_json(_state.state_path("judges", f"_history_{MODE}.json"), {"versions": []})
    if not history["versions"]:
        return {}
    judge_id = history["versions"][-1]["judge_id"]
    judge = _state.read_json(_state.state_path("judges", f"{judge_id}.json"))
    dev = set(_state.read_json(_state.state_path("splits.json"), {}).get(MODE, {}).get("dev", []))
    preds = judge.get("predictions", {}).get(judge["prompt_hash"], {})
    crits = judge.get("critiques", {}).get(judge["prompt_hash"], {})
    return {
        "judge_id": judge_id,
        "results": {t: {"pred": preds[t], "critique": crits.get(t, "")} for t in dev if t in preds},
    }


def save(trace_id: str, label: int, note: str, source: str = "human_hw5") -> dict[str, Any]:
    """Append an HW5 label (1 = Pass, 0 = Fail) and supersede the previous one."""
    if label not in (0, 1):
        raise ValueError("HW5 label must be 1 (Pass) or 0 (Fail)")
    if source == "human_hw5" and not note.strip():
        raise ValueError("write the reason for the HW5 label")
    path = labels_path()
    rows = _state.read_jsonl(path)
    record = {
        "trace_id": trace_id,
        "label": label,
        "source": source,
        "note": note,
        "ts": _utcnow(),
        "label_id": f"{trace_id}#hw5-{sum(1 for r in rows if r['trace_id'] == trace_id)}",
    }
    for row in rows:
        if row["trace_id"] == trace_id and not row.get("superseded_by"):
            row["superseded_by"] = record["label_id"]
    rows.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    _state.write_jsonl(path, rows)
    return record


def enqueue(trace_ids: list[str], reason: str, origin: str = "rejudge") -> int:
    """Add traces to the HW5 queue. A trace already queued is reopened: its
    existing HW5 label stays live until the reviewer decides again.

    ``origin`` records how a trace was found (rejudge, enrich, random). The
    page does not show it, so a candidate's origin does not suggest a label.
    """
    from analysis.review_app.server import live_labels

    hw4 = live_labels()[MODE]
    q = {e["trace_id"]: e for e in queue()}
    for t in trace_ids:
        entry = q.setdefault(t, {
            "trace_id": t,
            "hw4_label": ("Fail" if hw4[t]["label"] == 1 else "Pass") if t in hw4 else "not labelled",
            "origin": origin,
            "added_at": _utcnow(),
        })
        entry["reason"] = reason
        if t in live() and live()[t]["source"] == "human_hw5":
            entry["reopened_at"] = _utcnow()
    _state.write_json(_state.state_path("hw5_queue.json"), list(q.values()))
    return len(trace_ids)


def export(rejudge: list[str]) -> dict[str, int]:
    from analysis.review_app.server import live_labels

    hw4 = live_labels()[MODE]
    have = live()
    missing = [t for t in rejudge if t not in hw4]
    if missing:
        raise SystemExit(f"not labelled in HW4: {missing}")
    q = {e["trace_id"]: e for e in queue()}
    for t in rejudge:
        q.setdefault(t, {
            "trace_id": t,
            "hw4_label": "Fail" if hw4[t]["label"] == 1 else "Pass",
            "reason": "re-judge under the HW5 definition",
            "added_at": _utcnow(),
        })
    _state.write_json(_state.state_path("hw5_queue.json"), list(q.values()))

    path = labels_path()
    rows = _state.read_jsonl(path)
    carried = 0
    for t, rec in hw4.items():
        if t in have or t in q:
            continue
        rows.append({
            "trace_id": t,
            "label": 1 - int(rec["label"]),
            "source": f"hw4_{rec.get('source', 'human')}",
            "note": f"carried from HW4 label {rec['label_id']} ({'Fail' if rec['label'] == 1 else 'Pass'})",
            "ts": _utcnow(),
            "label_id": f"{t}#hw5-0",
            "hw4_label_id": rec["label_id"],
        })
        carried += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    _state.write_jsonl(path, rows)
    return {"carried": carried, "queued": len(q), "already_present": len(have)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("--rejudge", nargs="*", default=[])
    args = parser.parse_args()
    if args.cmd == "export":
        print(export(args.rejudge))


if __name__ == "__main__":
    main()
