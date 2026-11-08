"""Homework 5: judge inputs, label split, and judge runs for one mode.

Part B
    uv run python -m analysis.run_judges prepare   # write state/hw5_trace_inputs.json
    uv run python -m analysis.run_judges split     # write state/splits.json (run once)

Part C (paid: gpt-4o-mini through DocETL; the key comes from .env)
    uv run --env-file .env python -m analysis.run_judges dev --prompt analysis/prompts/<mode>-v0.txt

Part D (freezes the judge once, then paid)
    uv run --env-file .env python -m analysis.run_judges test --judge <mode>-v0

Every prompt version reads the same saved inputs:

    export CARTWHEEL_JUDGE_TRACE_SOURCE="$PWD/analysis/state/hw5_trace_inputs.json"

Labels come from state/hw5_labels/<mode>.jsonl (1 = Pass, 0 = Fail).
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from analysis.helpers import _state, freeze_judge, judge_alignment, register_judge, run_judge, split_labels
from analysis.helpers.normalization import normalize_trace
from analysis.helpers.tools import _prompt_hash
from analysis.review_app import hw5
from analysis.review_app.batches import _load

MODE = hw5.MODE
INPUTS = _state.state_path("hw5_trace_inputs.json")

# Reviewer decisions for Part B (2026-11-03).
EXCLUDED_SCENARIOS = {
    "support-0180": "turns still carry HW4 labels that predate the blocked-refund rule; not re-judged",
    "support-0190": "close variant of support-0188 (same damaged order 8003, same refund refusal); 0188 kept",
}
# Pass conversations are eligible only when they involve a policy question, so
# Pass agreement measures telling good policy answers from bad ones rather
# than recognising turns with no policy question at all.
POLICY_TOOLS = {"check_return_eligibility", "get_policy", "search_help_center", "issue_refund"}
POLICY_WORDS = re.compile(
    r"return|refund|money back|policy|window|dispute|escalat|how long|cutoff|deadline", re.I
)
# Only these message fields reach the judge. Langfuse metadata, scenario ids,
# labels and review notes never do.
MESSAGE_FIELDS = {"role", "text", "name", "arguments", "content"}


def _policy_relevant(turn: dict[str, Any]) -> bool:
    tools = {m.get("name") for m in turn["messages"] if m.get("role") == "tool_call"}
    asked = " ".join(m.get("text") or "" for m in turn["messages"] if m.get("role") == "user")
    return bool(tools & POLICY_TOOLS) or bool(POLICY_WORDS.search(asked))


def _judge_message(message: dict[str, Any]) -> dict[str, Any]:
    """Keep role and text or tool data.

    The helper flattens a tool_call to its arguments and a tool_result to its
    content, dropping the tool name. The name is folded into that data so the
    judge can tell a get_order result from a get_policy result.
    """
    m = {k: v for k, v in message.items() if k in MESSAGE_FIELDS}
    if m["role"] == "tool_call":
        m["arguments"] = {"tool": m.get("name"), "arguments": m.get("arguments")}
    elif m["role"] == "tool_result":
        m["content"] = {"tool": m.get("name"), "result": m.get("content")}
    return m


def _record(session: dict[str, Any], turn_index: int) -> dict[str, Any]:
    """Earlier turns plus the judged turn; the judged reply is the last message."""
    messages = [msg for t in session["turns"][: turn_index + 1] for msg in t["messages"]]
    trace = [_judge_message(m) for m in messages if m.get("role") in {"user", "assistant", "tool_call", "tool_result"}]
    if not trace or trace[-1]["role"] != "assistant":
        raise ValueError(f"{session['turns'][turn_index]['trace_id']}: judged turn has no final reply")
    return {"trace_id": session["turns"][turn_index]["trace_id"], "trace": trace}


def select_records() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One record per eligible labelled conversation, plus a selection report."""
    _, by = _load()
    labels = hw5.live()
    convs: dict[str, list[str]] = defaultdict(list)
    for tid in labels:
        convs[by[tid]["session"]["session_id"]].append(tid)

    records, report = [], defaultdict(int)
    for sid, tids in convs.items():
        session = by[tids[0]]["session"]
        if session["scenario_id"] in EXCLUDED_SCENARIOS:
            report["excluded_by_decision"] += 1
            continue
        order = sorted(tids, key=lambda t: by[t]["turn"])
        fails = [t for t in order if labels[t]["label"] == 0]
        if fails:
            chosen = fails[0]
        else:
            relevant = [t for t in order if _policy_relevant(by[t]["trace"])]
            if not relevant:
                report["pass_without_policy_question"] += 1
                continue
            chosen = relevant[0]
        records.append(_record(session, by[chosen]["turn"]))
        report["fail" if fails else "pass"] += 1
    records.sort(key=lambda r: r["trace_id"])
    return records, dict(report)


def check_inputs(records: list[dict[str, Any]]) -> None:
    """Fail loudly on leakage or a record the judge cannot read."""
    labels = hw5.live()
    ids = [r["trace_id"] for r in records]
    assert len(ids) == len(set(ids)), "duplicate trace ids"
    for r in records:
        assert set(r) == {"trace_id", "trace"}, f"{r['trace_id']}: extra fields {set(r) - {'trace_id', 'trace'}}"
        assert r["trace_id"] in labels, f"{r['trace_id']}: no HW5 label"
        for m in r["trace"]:
            assert set(m) <= MESSAGE_FIELDS, f"{r['trace_id']}: message fields {set(m) - MESSAGE_FIELDS}"
        blob = json.dumps(r["trace"])
        assert "support-0" not in blob, f"{r['trace_id']}: scenario id in the input"
        assert labels[r["trace_id"]]["note"][:60] not in blob, f"{r['trace_id']}: review note in the input"
        if not normalize_trace(r)["text"].strip():
            raise ValueError(f"{r['trace_id']}: renders to empty judge text")


def prepare_inputs() -> list[dict[str, Any]]:
    if INPUTS.exists():
        raise SystemExit(f"{INPUTS} exists. Keep the export unchanged after the first prompt run; delete it only before then.")
    records, report = select_records()
    check_inputs(records)
    _state.write_json(INPUTS, records)
    chars = sum(len(normalize_trace(r)["text"]) for r in records)
    print(f"wrote {len(records)} records to {INPUTS}")
    print(f"selection: {report}")
    print(f"judge text: {chars} characters in total, about {chars // 4} tokens, {chars // 4 // len(records)} per record")
    return records


def split_data(mode: str = MODE) -> dict[str, list[str]]:
    splits_file = _state.read_json(_state.state_path("splits.json"), default={})
    if mode in splits_file:
        raise SystemExit(f"splits.json already holds {mode}; the split is made once and kept unchanged.")
    records = json.loads(INPUTS.read_text(encoding="utf-8"))
    splits = split_labels(
        mode,
        fractions=(0.20, 0.40, 0.40),
        seed=7,
        min_per_class=10,
        eligible_trace_ids=[record["trace_id"] for record in records],
    )
    labels = hw5.live()
    for name in ("train", "dev", "test"):
        ids = splits[name]
        n_pass = sum(labels[t]["label"] == 1 for t in ids)
        print(f"{name:5s}: {len(ids):3d} records  Pass {n_pass:3d}  Fail {len(ids) - n_pass:3d}")
    return splits


JUDGE_MODEL = "gpt-4o-mini"
REPORT = _state.state_path().parent / "report"


def _use_saved_inputs() -> None:
    # Every run reads the frozen export, even when Langfuse is configured.
    os.environ["CARTWHEEL_JUDGE_TRACE_SOURCE"] = str(INPUTS)


def _registered(mode: str, prompt_text: str) -> str | None:
    """The judge id already registered for this exact prompt, if any, so a
    rerun resumes the cached predictions instead of adding a version."""
    history = _state.read_json(_state.state_path("judges", f"_history_{mode}.json"), {"versions": []})
    want = _prompt_hash(prompt_text, JUDGE_MODEL)
    return next((v["judge_id"] for v in history["versions"] if v["prompt_hash"] == want), None)


def run_development(mode: str, prompt_path: str) -> dict[str, Any]:
    _use_saved_inputs()
    prompt_text = Path(prompt_path).read_text(encoding="utf-8")
    judge_id = _registered(mode, prompt_text)
    if judge_id is None:
        judge_id = register_judge(mode=mode, prompt_text=prompt_text, judge_model=JUDGE_MODEL)["judge_id"]
        print(f"registered {judge_id} from {prompt_path}")
    else:
        print(f"resuming {judge_id} (prompt already registered)")
    run_judge(judge_id, split="dev", batch_size=10)
    development = judge_alignment(judge_id, split="dev")
    development.update({"prompt_path": prompt_path, "model": JUDGE_MODEL, "created_at": hw5._utcnow()})
    _state.write_json(REPORT / f"dev-{judge_id}.json", development)
    d = development
    print(f"{judge_id} dev: TPR {d['tpr']:.2f} {d['tpr_interval']}  TNR {d['tnr']:.2f} {d['tnr_interval']}")
    print(f"  TP {d['tp']}  FN {d['fn']}  TN {d['tn']}  FP {d['fp']}  n {d['n']}  disagreements {len(d['disagreements'])}")
    return development


def run_test(judge_id: str) -> dict[str, Any]:
    """Freeze the chosen judge once, score the held-out test split, save the metrics.

    Rerunning after an interruption resumes: a frozen judge is not frozen
    again, and cached test predictions are not paid for twice.
    """
    _use_saved_inputs()
    judge = _state.read_json(_state.state_path("judges", f"{judge_id}.json"))
    if judge.get("status") != "frozen":
        freeze_judge(judge_id)
        print(f"froze {judge_id}")
    else:
        print(f"{judge_id} already frozen at {judge['frozen_at']}; resuming")
    run_judge(judge_id, split="test", batch_size=10)
    test = judge_alignment(judge_id, split="test")
    test.update({
        "n_pass": test["tp"] + test["fn"],
        "n_fail": test["tn"] + test["fp"],
        "model": JUDGE_MODEL,
        "created_at": hw5._utcnow(),
    })
    _state.write_json(REPORT / f"test-{judge_id}.json", test)
    t = test
    print(f"{judge_id} test: TPR {t['tpr']:.2f} {t['tpr_interval']}  TNR {t['tnr']:.2f} {t['tnr_interval']}")
    print(f"  TP {t['tp']}  FN {t['fn']}  TN {t['tn']}  FP {t['fp']}  Pass {t['n_pass']}  Fail {t['n_fail']}")
    return test


def main() -> None:
    parser = argparse.ArgumentParser(description="Homework 5 judge pipeline")
    parser.add_argument("step", choices=["prepare", "split", "dev", "test"])
    parser.add_argument("--prompt", help="prompt file for the dev step")
    parser.add_argument("--judge", help="judge id for the test step")
    args = parser.parse_args()
    if args.step == "prepare":
        prepare_inputs()
    elif args.step == "split":
        split_data()
    elif args.step == "dev":
        run_development(MODE, args.prompt)
    elif args.step == "test":
        run_test(args.judge)


if __name__ == "__main__":
    main()
