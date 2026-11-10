"""Compare the search evaluator with the human labels and save a report.

This is a development result, not a held-out test: the rules were shaped
using these same labels.

Run it:
    uv run python -m analysis.code_evals.validate
"""

import json
from pathlib import Path

from analysis.code_evals.catalog import Catalog
from analysis.code_evals.search_evaluator import SearchArgumentsEvaluator
from analysis.code_evals.trace_parts import load_all_trace_parts
from analysis.helpers.tools import _wilson_interval
from analysis.review_app.server import live_labels

MODE = "search_arguments_misexpress_request"
REPORT_PATH = Path(__file__).resolve().parents[1] / "report" / f"code-eval-{MODE}.json"


def load_labels():
    """Return {trace_id: "Pass" or "Fail"}. In this label file, 1 means Fail."""
    labels = {}
    for trace_id, record in live_labels()[MODE].items():
        if record["label"] == 1:
            labels[trace_id] = "Fail"
        else:
            labels[trace_id] = "Pass"
    return labels


def compare_with_labels(evaluator, all_parts, labels):
    """Return one row for each labelled turn: the label beside the verdict."""
    rows = []
    for trace_id, label in labels.items():
        parts = all_parts[trace_id]
        verdict = evaluator.evaluate(parts)
        row = {
            "trace_id": trace_id,
            "scenario_id": parts.scenario_id,
            "turn": parts.turn,
            "label": label,
            "evaluator": verdict.result,
            "search_calls": len(parts.search_calls),
            "rules_fired": verdict.rules_fired,
            "reasons": verdict.reasons,
        }
        rows.append(row)
    return rows


def score(rows):
    """Count agreements and work out the rates.

    As in HW5, Pass is the positive class:
    TPR = how many human Pass turns the evaluator also passed.
    TNR = how many human Fail turns the evaluator also failed.
    """
    both_pass = 0
    both_fail = 0
    false_alarm = 0  # human Pass, evaluator Fail
    missed_fail = 0  # human Fail, evaluator Pass

    for row in rows:
        if row["label"] == "Pass" and row["evaluator"] == "Pass":
            both_pass += 1
        elif row["label"] == "Fail" and row["evaluator"] == "Fail":
            both_fail += 1
        elif row["label"] == "Pass" and row["evaluator"] == "Fail":
            false_alarm += 1
        else:
            missed_fail += 1

    human_pass = both_pass + false_alarm
    human_fail = both_fail + missed_fail

    return {
        "turns": len(rows),
        "human_pass": human_pass,
        "human_fail": human_fail,
        "both_pass": both_pass,
        "both_fail": both_fail,
        "false_alarm": false_alarm,
        "missed_fail": missed_fail,
        "tpr": both_pass / human_pass if human_pass > 0 else None,
        "tpr_interval": _wilson_interval(both_pass, human_pass),
        "tnr": both_fail / human_fail if human_fail > 0 else None,
        "tnr_interval": _wilson_interval(both_fail, human_fail),
    }


def rows_with_search(rows):
    """Keep only turns with a search call. The other turns are automatic Passes."""
    kept = []
    for row in rows:
        if row["search_calls"] > 0:
            kept.append(row)
    return kept


def disagreements(rows):
    """Keep only turns where the evaluator and the human differ."""
    kept = []
    for row in rows:
        if row["label"] != row["evaluator"]:
            kept.append(row)
    return kept


def count_rules(rows):
    """Count how many labelled turns each rule fired on, and how many were real Fails."""
    counts = {}
    for row in rows:
        # set() so a rule that fires on two calls counts once for the turn.
        for rule_name in set(row["rules_fired"]):
            if rule_name not in counts:
                counts[rule_name] = {"turns": 0, "human_fail": 0}
            counts[rule_name]["turns"] += 1
            if row["label"] == "Fail":
                counts[rule_name]["human_fail"] += 1
    return counts


def unlabelled_search_turns(all_parts, labels):
    """Return turns with a search call but no label: possible fresh test data."""
    names = []
    for trace_id, parts in all_parts.items():
        if trace_id not in labels and len(parts.search_calls) > 0:
            names.append(f"{parts.scenario_id} t{parts.turn}")
    return sorted(names)


def print_score(title, result):
    print(title)
    print("  Turns:", result["turns"], "| human Pass:", result["human_pass"], "| human Fail:", result["human_fail"])
    print("  Both Pass:", result["both_pass"], "| Both Fail:", result["both_fail"])
    print("  False alarms:", result["false_alarm"], "| Missed Fails:", result["missed_fail"])
    print("  TPR (Pass agreement):", round(result["tpr"], 3), result["tpr_interval"])
    print("  TNR (Fail agreement):", round(result["tnr"], 3), result["tnr_interval"])
    print()


# This block runs only when you run this file directly.
if __name__ == "__main__":
    evaluator = SearchArgumentsEvaluator(Catalog())
    all_parts = load_all_trace_parts()
    labels = load_labels()

    rows = compare_with_labels(evaluator, all_parts, labels)
    searched_rows = rows_with_search(rows)
    all_score = score(rows)
    searched_score = score(searched_rows)
    different = disagreements(rows)
    rule_counts = count_rules(rows)
    unlabelled = unlabelled_search_turns(all_parts, labels)

    print_score("All labelled turns", all_score)
    print_score("Only labelled turns with a search call", searched_score)

    print("Each rule on labelled turns:")
    for rule_name, count in rule_counts.items():
        print(" ", rule_name, "| fired on", count["turns"], "| human Fail on", count["human_fail"])
    print()

    print("Disagreements:", len(different))
    for row in different:
        print(" ", row["scenario_id"], "turn", row["turn"], "| human:", row["label"], "| evaluator:", row["evaluator"])
        for reason in row["reasons"]:
            print("     ", reason)
    print()

    print("Turns with a search call and no label:", len(unlabelled), unlabelled)

    report = {
        "mode": MODE,
        "kind": "development result: the rules were shaped using these labels",
        "model_calls": 0,
        "all_labelled_turns": all_score,
        "labelled_turns_with_search": searched_score,
        "rules": rule_counts,
        "disagreements": different,
        "unlabelled_search_turns": unlabelled,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("\nSaved", REPORT_PATH)
