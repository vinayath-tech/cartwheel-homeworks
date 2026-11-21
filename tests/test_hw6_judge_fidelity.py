"""HW6: the Harbor judge must see the same input and parser as in HW5.

These checks run offline. They do not call a model.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from analysis.helpers.normalization import normalize_trace
from harbor_adapter.export import export_tasks
from replay.rollout import judge_trace_text

REPO_ROOT = Path(__file__).resolve().parents[1]
HW5_INPUTS = REPO_ROOT / "analysis" / "state" / "hw5_trace_inputs.json"


def hw5_record_to_transcript(record: dict) -> dict:
    """Rebuild a runtime transcript (the shape run_case returns) from one
    saved HW5 judge input. Each user message starts a new turn."""
    turns = []
    for message in record["trace"]:
        role = message["role"]
        if role == "user":
            turns.append({"user": message["text"], "messages": []})
            continue
        if role == "tool_call":
            # HW5 folded the tool name into the arguments; unfold it again.
            runtime_message = {
                "role": "tool_call",
                "name": message["name"],
                "arguments": message["arguments"]["arguments"],
            }
        elif role == "tool_result":
            runtime_message = {
                "role": "tool_result",
                "name": message["name"],
                "content": message["content"]["result"],
            }
        else:
            runtime_message = {"role": "assistant", "text": message["text"]}
        turns[-1]["messages"].append(runtime_message)
    return {"turns": turns}


def load_hw5_records() -> list[dict]:
    return json.loads(HW5_INPUTS.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "record", load_hw5_records(), ids=lambda record: record["trace_id"][:12]
)
def test_harbor_judge_text_matches_the_hw5_judge_input(record: dict) -> None:
    hw5_text = normalize_trace(record)["text"]
    harbor_text = judge_trace_text(hw5_record_to_transcript(record))
    assert harbor_text == hw5_text


def _generated_decode(tmp_path: Path) -> object:
    """Export one task with the frozen judge and load its _decode function."""
    cases_path = tmp_path / "cases.jsonl"
    case = {
        "id": "e-fidelity",
        "mode": "policy_answer_missing_specifics",
        "input": {"role": "shopper", "user_id": 1, "message": "hello"},
        "initial_state": {"world": "reseed", "fixture": None},
        "expected": {
            "assertions": ["The reply states the deciding value."],
            "judges": {"policy_answer_missing_specifics": "pass"},
        },
    }
    cases_path.write_text(json.dumps(case) + "\n")
    output = tmp_path / "tasks"
    export_tasks(cases_path, output, baseline=True)
    script = (
        output / "e-fidelity" / "tests" / "judge_policy_answer_missing_specifics.py"
    ).read_text()
    decode_source = re.search(r"def _decode\(.*?\n\n\n", script, re.S).group(0)
    namespace: dict = {}
    exec(decode_source, namespace)
    return namespace["_decode"]


def test_generated_judge_uses_the_frozen_prompt_and_model(tmp_path: Path) -> None:
    frozen = json.loads(
        (
            REPO_ROOT
            / "analysis"
            / "state"
            / "judges"
            / "policy_answer_missing_specifics-v0.json"
        ).read_text(encoding="utf-8")
    )
    _generated_decode(tmp_path)
    script = (
        tmp_path
        / "tasks"
        / "e-fidelity"
        / "tests"
        / "judge_policy_answer_missing_specifics.py"
    ).read_text()
    assert f"PROMPT = {frozen['prompt_text']!r}" in script
    assert "MODEL = 'gpt-4o-mini'" in script


def test_generated_judge_parser_matches_hw5(tmp_path: Path) -> None:
    decode = _generated_decode(tmp_path)
    assert decode({"critique": "states 7 days", "result": "Pass"}) == "pass"
    assert decode({"critique": "no window given", "result": "Fail"}) == "fail"
    # HW5 rejected anything else; it must not silently become a failed run.
    for bad in ["pass", "PASS", "Pass.", "Passed", "", None, "Unclear"]:
        with pytest.raises(ValueError):
            decode({"critique": "text", "result": bad})
    with pytest.raises(ValueError):
        decode({"critique": "", "result": "Pass"})
