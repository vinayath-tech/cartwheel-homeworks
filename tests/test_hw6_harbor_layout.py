"""HW6: the summary must read the job layout that Harbor 0.23 writes.

Harbor 0.23 does not list trials inside the job's result.json. It writes one
result.json per trial, each in its own subfolder. These tests build that
layout by hand. They run offline and do not call a model.
"""

from __future__ import annotations

import json
from pathlib import Path

from harbor_adapter.summary import load_trial_results, summarize_job


def _write_trial(job: Path, folder: str, case_id: str, reward: float, started_at: str) -> None:
    trial_dir = job / folder
    trial_dir.mkdir()
    trial = {
        "task_name": f"cartwheel/evals__{case_id}",
        "trial_name": folder,
        "started_at": started_at,
        "verifier_result": {"rewards": {"reward": reward}},
        "exception_info": None,
    }
    (trial_dir / "result.json").write_text(json.dumps(trial))


def _make_job(tmp_path: Path) -> Path:
    job = tmp_path / "job"
    job.mkdir()
    # The job file has stats only, like Harbor 0.23.
    (job / "result.json").write_text(json.dumps({"n_total_trials": 5, "stats": {}}))
    rewards = [1, 0, 1, 1, 0]
    for attempt, reward in enumerate(rewards):
        # Folder names are random in Harbor, so do not rely on their order.
        folder = f"e-401__{'zyxwv'[attempt]}"
        _write_trial(job, folder, "e-401", reward, f"2026-07-01T00:00:0{attempt}Z")
    return job


def test_trials_are_read_from_subfolders_in_start_order(tmp_path: Path) -> None:
    job = _make_job(tmp_path)
    trials = load_trial_results(job)
    names = [trial["trial_name"] for trial in trials]
    assert names == ["e-401__z", "e-401__y", "e-401__x", "e-401__w", "e-401__v"]


def test_baseline_summary_works_with_subfolder_layout(tmp_path: Path) -> None:
    job = _make_job(tmp_path)
    cases_path = tmp_path / "cases.jsonl"
    case = {
        "id": "e-401",
        "mode": "response_quality",
        "input": {"role": "shopper", "user_id": 1, "message": "hello"},
        "initial_state": {"world": "reseed", "fixture": None},
        "expected": {"checks": [{"check": "reply_asks_question"}]},
    }
    cases_path.write_text(json.dumps(case) + "\n")

    markdown, passed = summarize_job(
        job, cases_path=cases_path, expected_attempts=5, classify=True
    )

    assert passed is True
    assert "| `e-401` | 3 | 5 |" in markdown
    assert "`baseline_pass_rate: 0.6`" in markdown
