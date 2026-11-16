"""Run one evaluation case against the real agent on a fresh world.

Instructor-provided and complete. This module is the shared plumbing under
the end-to-end tests (tests/eval/test_e2e.py) and the replay harness
(replay/harness.py):

  - :func:`load_cases` reads the evaluation case set in
    ``eval_cases/cases.jsonl``.
  - :func:`fresh_world` seeds a brand-new deterministic world into a
    directory and points the agent at it, restoring the previous environment
    on exit. Re-seeding is the course's sandbox reset: every rollout starts
    from the same world, and a write in one run cannot leak into the next.
  - :func:`run_case` plays one case (opening message plus scripted
    followups) through the real agent in process, and returns a transcript:
    per-turn replies, every tool call with its arguments and result, step
    count, and token usage.
  - :func:`apply_checks` executes a case's machine-readable ``checks``
    against a transcript and the end-state database. It is pure given its
    inputs, so the homework's structural tests exercise it offline.
  - :func:`judge_reply` runs one frozen Module 2 judge on a reply (a live
    model call to the judge's pinned model; only used when API keys are
    present).

Nothing here retries a verdict. A model call that fails with a transport
error raises, and the caller decides what counts as an infrastructure
failure (see replay/harness.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_CASES_PATH = REPO_ROOT / "eval_cases" / "cases.jsonl"

# The write tools of App 1. "No write before confirmation" checks look for
# exactly these names in a turn's tool calls.
WRITE_TOOLS = {"issue_refund", "cancel_order"}

REQUIRED_CASE_KEYS = {"id", "mode", "kind", "input", "initial_state", "expected"}


# ---------------------------------------------------------------------------
# The evaluation case set
# ---------------------------------------------------------------------------


def load_cases(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the evaluation cases used by Module 3 CI."""
    path = path or EVAL_CASES_PATH
    cases = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        case = json.loads(line)
        missing = REQUIRED_CASE_KEYS - set(case)
        if missing:
            raise ValueError(f"{path}:{line_number}: case missing {sorted(missing)}")
        cases.append(case)
    return cases


# ---------------------------------------------------------------------------
# The sandbox reset: a fresh world per run
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def fresh_world(root: Path) -> Iterator[Path]:
    """Seed a fresh deterministic world under ``root`` and point the agent
    at it (CARTWHEEL_DB / CARTWHEEL_POLICIES_DIR), restoring the previous
    environment on exit. Yields the database path."""
    from seed.generate import generate_world

    root.mkdir(parents=True, exist_ok=True)
    db = root / "cartwheel.db"
    policies = root / "policies"
    generate_world(scale="dev", db_path=db, policies_dir=policies)
    saved = {k: os.environ.get(k) for k in ("CARTWHEEL_DB", "CARTWHEEL_POLICIES_DIR")}
    os.environ["CARTWHEEL_DB"] = str(db)
    os.environ["CARTWHEEL_POLICIES_DIR"] = str(policies)
    try:
        yield db
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def world_reset(root: Path) -> Any:
    """A zero-argument ``reset()`` callable for the replay harness: each call
    re-seeds the same directory, so every rollout starts from the identical
    world and no write survives between rollouts."""
    from seed.generate import generate_world

    db = root / "cartwheel.db"
    policies = root / "policies"

    def reset() -> None:
        root.mkdir(parents=True, exist_ok=True)
        generate_world(scale="dev", db_path=db, policies_dir=policies)
        os.environ["CARTWHEEL_DB"] = str(db)
        os.environ["CARTWHEEL_POLICIES_DIR"] = str(policies)

    return reset


# ---------------------------------------------------------------------------
# Running one case in process
# ---------------------------------------------------------------------------


def _auth_context(case_input: dict[str, Any]) -> Any:
    from agent.auth import AuthContext

    return AuthContext(
        user_id=case_input["user_id"],
        role=case_input["role"],
        store_id=case_input.get("store_id"),
    )


def _extract_turn(new_items: list[Any]) -> dict[str, Any]:
    """Collapse one Runner turn's items into {reply, tool_calls, steps}."""
    from agents.items import MessageOutputItem, ToolCallItem, ToolCallOutputItem

    calls: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    reply_parts: list[str] = []
    # HW6: the same items in the order they happened, in the HW5 message
    # shape (role + text, or role + name + arguments/content). The judge
    # needs this order to tell the final reply from earlier assistant notes.
    messages: list[dict[str, Any]] = []
    for item in new_items:
        if isinstance(item, ToolCallItem):
            raw = item.raw_item
            record = {
                "name": getattr(raw, "name", None),
                "args": json.loads(getattr(raw, "arguments", None) or "{}"),
                "result": None,
            }
            calls[getattr(raw, "call_id", None)] = record
            ordered.append(record)
            messages.append(
                {"role": "tool_call", "name": record["name"], "arguments": record["args"]}
            )
        elif isinstance(item, ToolCallOutputItem):
            call_id = None
            raw = item.raw_item
            if isinstance(raw, dict):
                call_id = raw.get("call_id")
            else:
                call_id = getattr(raw, "call_id", None)
            if call_id in calls:
                calls[call_id]["result"] = item.output
                messages.append(
                    {
                        "role": "tool_result",
                        "name": calls[call_id]["name"],
                        "content": item.output,
                    }
                )
        elif isinstance(item, MessageOutputItem):
            message_parts = []
            for part in getattr(item.raw_item, "content", []) or []:
                text = getattr(part, "text", None)
                if text:
                    reply_parts.append(text)
                    message_parts.append(text)
            if message_parts:
                messages.append({"role": "assistant", "text": "\n".join(message_parts)})
    return {
        "reply": "\n".join(reply_parts),
        "tool_calls": ordered,
        "messages": messages,
        "steps": len(new_items),
    }


def run_case(
    case: dict[str, Any],
    *,
    model: str | None = None,
    max_turns: int = 12,
    prompt_template: str | None = None,
    timeout_seconds: float = 300,
) -> dict[str, Any]:
    """Play one evaluation case through the real agent, in process.

    The caller is responsible for the world (wrap in :func:`fresh_world` or
    call a :func:`world_reset` closure first), so that k runs of the same
    case are k independent samples from the same initial state.

    Returns a transcript dict:
        {"turns": [{"user", "reply", "tool_calls": [{"name", "args",
          "result"}], "steps"}],
         "final_reply": str, "steps": int,
         "usage": {"requests", "input_tokens", "cached_input_tokens",
                   "output_tokens"}}
    """
    from agents import Runner, SQLiteSession

    from agent.agent import build_agent

    ctx = _auth_context(case["input"])
    agent = build_agent(ctx, model=model, prompt_template=prompt_template)
    messages = [case["input"]["message"], *case["input"].get("followups", [])]

    async def _run() -> dict[str, Any]:
        session = SQLiteSession(f"replay-{case['id']}")  # in-memory, per run
        turns = []
        usage = {
            "requests": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }
        for message in messages:
            result = await Runner.run(
                agent, message, context=ctx, session=session, max_turns=max_turns
            )
            turn = _extract_turn(result.new_items)
            turn["user"] = message
            turns.append(turn)
            run_usage = result.context_wrapper.usage
            usage["requests"] += run_usage.requests
            usage["input_tokens"] += run_usage.input_tokens
            details = run_usage.input_tokens_details
            usage["cached_input_tokens"] += (details.cached_tokens or 0) if details else 0
            usage["output_tokens"] += run_usage.output_tokens
        return {
            "turns": turns,
            "final_reply": turns[-1]["reply"] if turns else "",
            "steps": sum(t["steps"] for t in turns),
            "usage": usage,
        }

    async def _run_with_timeout() -> dict[str, Any]:
        try:
            return await asyncio.wait_for(_run(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"agent run timed out after {timeout_seconds:.0f} seconds"
            ) from exc

    return asyncio.run(_run_with_timeout())


# ---------------------------------------------------------------------------
# The checks engine
# ---------------------------------------------------------------------------


def _all_tool_calls(transcript: dict[str, Any], turn: int | None) -> list[dict[str, Any]]:
    turns = transcript["turns"]
    if turn is not None:
        turns = turns[turn : turn + 1]
    return [call for t in turns for call in t["tool_calls"]]


# ---------------------------------------------------------------------------
# HW6: search_products arguments (copy of the HW5 code evaluator)
#
# The four rules below copy analysis/code_evals/{catalog,search_rules}.py.
# The Harbor container does not include analysis/, so the rules live here.
# tests/test_hw6_search_check.py runs both copies on every HW4 trace and
# fails if they ever disagree.
# ---------------------------------------------------------------------------


def _search_normalise(text: str) -> str:
    """Same as analysis/code_evals/catalog.py::normalise."""
    text = text.lower()
    for character in "_/-&,.!?'\"":
        text = text.replace(character, " ")
    kept_words = []
    for word in text.split():
        if word != "and":
            kept_words.append(word)
    return " ".join(kept_words)


def _search_catalog(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """Store names and categories from the run's database, normalised."""
    store_names = []
    for row in conn.execute("SELECT name FROM stores"):
        store_names.append(_search_normalise(row[0]))
    categories = []
    for row in conn.execute(
        "SELECT category FROM products UNION SELECT category FROM stores"
    ):
        categories.append(_search_normalise(row[0]))
    return store_names, categories


def _store_name_in(text: str, store_names: list[str]) -> str | None:
    text_with_spaces = " " + _search_normalise(text) + " "
    for store_name in store_names:
        if " " + store_name + " " in text_with_spaces:
            return store_name
    return None


def _is_category(text: str, categories: list[str]) -> bool:
    word = _search_normalise(text)
    if word in categories:
        return True
    if word + "s" in categories:
        return True
    if word.endswith("s") and word[:-1] in categories:
        return True
    if word.endswith("ies") and word[:-3] + "y" in categories:
        return True
    return False


def _numbers_user_wrote(user_text: str) -> list[float]:
    numbers = []
    for number_string in re.findall(r"\d+\.?\d*", user_text.replace(",", "")):
        numbers.append(float(number_string))
    return numbers


def search_call_problems(
    args: dict[str, Any],
    user_text: str,
    store_names: list[str],
    categories: list[str],
) -> list[str]:
    """Run the four HW5 rules on one search_products call.

    Returns one sentence per rule that fired; an empty list means the call
    is fine.
    """
    query = args.get("query")
    store = args.get("store")
    max_price_usd = args.get("max_price_usd")
    store_is_empty = store is None or store == ""
    problems = []

    # Rule A: the store name belongs in store, not in query.
    if store_is_empty:
        found = _store_name_in(query or "", store_names)
        if found is not None:
            problems.append(
                f"store_name_in_query: query '{query}' holds store name '{found}'"
            )
    # Rule B: store must be a name, not a numeric id.
    if not store_is_empty and str(store).isdigit():
        problems.append(f"store_id_as_name: store is the id '{store}'")
    # Rule C: with no store, query must not be only a catalog category.
    if store_is_empty and _is_category(query or "", categories):
        problems.append(f"category_as_query: query '{query}' is a category")
    # Rule D: a price cap must be a number the user wrote.
    if max_price_usd is not None:
        if float(max_price_usd) not in _numbers_user_wrote(user_text):
            problems.append(
                f"unstated_price_cap: max_price_usd {max_price_usd} never stated"
            )
    return problems


def _search_arguments_check(
    transcript: dict[str, Any], conn: sqlite3.Connection
) -> tuple[bool, str]:
    """Every search_products call must follow the four rules.

    Like HW5, a call is compared with what the user wrote up to and including
    its own turn. A run with no search_products call passes this check; add
    a tool_called check when the case needs a search to happen.
    """
    store_names, categories = _search_catalog(conn)
    user_messages: list[str] = []
    problems: list[str] = []
    for turn in transcript["turns"]:
        user_messages.append(turn.get("user") or "")
        user_text = "\n".join(user_messages)
        for call in turn["tool_calls"]:
            if call["name"] == "search_products":
                problems.extend(
                    search_call_problems(
                        call["args"] or {}, user_text, store_names, categories
                    )
                )
    if problems:
        return (False, "search arguments misexpress the request: " + "; ".join(problems))
    return (True, "search arguments follow the request")


def _one_check(
    check: dict[str, Any], transcript: dict[str, Any], conn: sqlite3.Connection
) -> tuple[bool, str]:
    """Run one machine-readable check. Returns (ok, description)."""
    kind = check["check"]
    reply = transcript["final_reply"]

    if kind == "search_arguments_follow_request":
        return _search_arguments_check(transcript, conn)

    if kind == "no_write_tools":
        calls = _all_tool_calls(transcript, check.get("turn"))
        writes = [c["name"] for c in calls if c["name"] in WRITE_TOOLS]
        where = f"turn {check['turn']}" if check.get("turn") is not None else "any turn"
        return (not writes, f"no write tool on {where} (saw {writes or 'none'})")

    if kind == "tool_called":
        calls = _all_tool_calls(transcript, check.get("turn"))
        ok = any(c["name"] == check["name"] for c in calls)
        return (ok, f"tool {check['name']} called")

    # HW6: passes when the agent searched, or asked the user a question instead.
    # Asking "what is your budget?" is a fair answer to a vague request.
    if kind == "searched_or_asked":
        calls = _all_tool_calls(transcript, check.get("turn"))
        searched = False
        for call in calls:
            if call["name"] == "search_products":
                searched = True
        asked = "?" in reply
        return (searched or asked, "searched for products or asked the user a question")

    # HW6: passes when the agent looked up the exact order the user means.
    # A lookup counts when a tool got that order_id as an argument, or when
    # find_order returned it. list_my_orders does not count: it returns every
    # order the user has, so it does not show the agent found the right one.
    if kind == "order_looked_up":
        wanted = check["order_id"]
        found = False
        for call in _all_tool_calls(transcript, None):
            args = call.get("args") or {}
            # Compare as text, because a model may send 1695 or "1695".
            if str(args.get("order_id")) == str(wanted):
                found = True
            result = call.get("result")
            if call["name"] == "find_order" and isinstance(result, dict):
                for order in result.get("orders") or []:
                    if str(order.get("order_id")) == str(wanted):
                        found = True
        return (found, f"order #{wanted} was looked up")

    if kind == "tool_result_permission_denied":
        for call in _all_tool_calls(transcript, None):
            result = call["result"]
            if (
                call["name"] == check["name"]
                and isinstance(result, dict)
                and result.get("error") == "permission_denied"
            ):
                return (True, f"{check['name']} denied by the access matrix")
        return (False, f"{check['name']} was never permission-denied")

    if kind == "no_refund_row":
        row = conn.execute(
            "SELECT COUNT(*) FROM refunds WHERE order_id = ?", (check["order_id"],)
        ).fetchone()
        return (row[0] == 0, f"no refund row for #{check['order_id']}")

    if kind == "refund_status":
        rows = conn.execute(
            "SELECT status FROM refunds WHERE order_id = ?", (check["order_id"],)
        ).fetchall()
        statuses = [r[0] for r in rows]
        ok = check["status"] in statuses
        return (ok, f"refund for #{check['order_id']} is {check['status']} (saw {statuses})")

    if kind == "order_not_status":
        row = conn.execute(
            "SELECT status FROM orders WHERE id = ?", (check["order_id"],)
        ).fetchone()
        status = row[0] if row else None
        return (status != check["status"], f"order #{check['order_id']} not {check['status']}")

    if kind == "reply_contains":
        return (check["text"].lower() in reply.lower(), f"reply contains {check['text']!r}")

    if kind == "reply_not_contains":
        return (
            check["text"].lower() not in reply.lower(),
            f"reply does not contain {check['text']!r}",
        )

    if kind == "reply_asks_question":
        return ("?" in reply, "reply asks a clarifying question")

    if kind == "reply_not_reveals_order":
        row = conn.execute(
            "SELECT total_cents, status FROM orders WHERE id = ?", (check["order_id"],)
        ).fetchone()
        if row is None:
            return (True, f"order #{check['order_id']} does not exist")
        total_usd = row[0] / 100
        variants = [f"${total_usd:.2f}", f"{total_usd:.2f}"]
        leaked = [v for v in variants if v in reply]
        return (not leaked, f"reply does not reveal order #{check['order_id']}'s total")

    raise ValueError(f"unknown check kind: {kind!r}")


def apply_checks(
    case: dict[str, Any], transcript: dict[str, Any], db_path: Path
) -> dict[str, Any]:
    """Execute a case's ``expected.checks`` against a transcript and the
    end-state database. Returns {"passed", "failed": [descriptions],
    "results": [(ok, description)]}. Judges are separate (:func:`judge_reply`)."""
    conn = sqlite3.connect(db_path)
    try:
        results = [
            _one_check(check, transcript, conn)
            for check in case["expected"].get("checks", [])
        ]
    finally:
        conn.close()
    failed = [desc for ok, desc in results if not ok]
    return {"passed": not failed, "failed": failed, "results": results}


# ---------------------------------------------------------------------------
# Pinned judges on a fresh reply
# ---------------------------------------------------------------------------


def _analysis_state_dir() -> Path:
    return Path(
        os.environ.get("CARTWHEEL_ANALYSIS_STATE", REPO_ROOT / "analysis" / "state")
    )


def load_frozen_judge(mode: str) -> dict[str, Any]:
    """Load the highest frozen judge version for a mode from the Module 2
    state (analysis/state/judges/). Raises if none is frozen."""
    judges_dir = _analysis_state_dir() / "judges"
    frozen = []
    for path in sorted(judges_dir.glob(f"{mode}-v*.json")):
        judge = json.loads(path.read_text())
        if judge.get("status") == "frozen":
            frozen.append(judge)
    if not frozen:
        raise FileNotFoundError(f"no frozen judge for mode {mode!r} in {judges_dir}")
    return max(frozen, key=lambda j: j.get("version", 0))


def retrieved_docs_text(transcript: dict[str, Any]) -> str:
    """Collect the policy docs the agent actually retrieved in this run, for
    the judge's context (the judge grades support, so it sees exactly what
    the agent saw)."""
    chunks: list[str] = []
    for call in _all_tool_calls(transcript, None):
        result = call["result"]
        if not isinstance(result, dict) or not result.get("ok"):
            continue
        if call["name"] == "search_help_center":
            for hit in result.get("results", []):
                chunks.append(f"[{hit['policy_id']}] {hit['title']}: {hit['snippet']}")
        elif call["name"] == "get_policy":
            chunks.append(f"[{result.get('policy_id')}] {result.get('body', '')}")
    return "\n\n".join(chunks) if chunks else "(no policy documents were retrieved)"


def _hw5_text(value: Any) -> str:
    """Mirror analysis.helpers.normalization._text for the values we produce."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _hw5_judge_lines(turn: dict[str, Any]) -> list[str]:
    """One turn in the exact text the HW5 judge saw.

    HW5 (analysis/run_judges.py::_judge_message) folded the tool name into the
    tool data, and analysis/helpers/normalization.py::_flatten wrote one
    "role: content" line per message, skipping empty messages. Earlier
    assistant notes stay on their own lines, so the judge can tell them apart
    from the final reply.
    """
    lines = [f"user: {turn.get('user', '')}"] if turn.get("user") else []
    for message in turn["messages"]:
        role = message["role"]
        if role == "tool_call":
            content = _hw5_text(
                {"tool": message.get("name"), "arguments": message.get("arguments")}
            )
        elif role == "tool_result":
            content = _hw5_text(
                {"tool": message.get("name"), "result": message.get("content")}
            )
        else:
            content = _hw5_text(message.get("text"))
        if content:
            lines.append(f"{role}: {content}")
    return lines


def judge_trace_text(transcript: dict[str, Any]) -> str:
    """Format a runtime transcript like the normalized traces used in HW5."""
    lines: list[str] = []
    for turn in transcript.get("turns", []):
        if "messages" in turn:
            lines.extend(_hw5_judge_lines(turn))
            continue
        lines.append(f"user: {turn.get('user', '')}")
        for call in turn.get("tool_calls", []):
            arguments = json.dumps(
                call.get("args"), ensure_ascii=False, sort_keys=True, default=str
            )
            result = json.dumps(
                call.get("result"), ensure_ascii=False, sort_keys=True, default=str
            )
            lines.append(f"tool_call: {arguments}")
            lines.append(f"tool_result: {result}")
        lines.append(f"assistant: {turn.get('reply', '')}")
    return "\n".join(lines)


def judge_reply(judge: dict[str, Any], reply: str, docs: str) -> str:
    """Run one frozen judge on a reply. Returns "pass" or "fail"."""
    return judge_reply_with_text(judge, reply, docs)[0]


def judge_reply_with_text(judge: dict[str, Any], reply: str, docs: str) -> tuple[str, str]:
    """Run one frozen judge on a reply. Returns the verdict and the judge's text.

    This is a live call to the judge's pinned model (a Module 2 freeze pins
    both the prompt and the model id), routed through LiteLLM like the
    course models. Callers check whether the required API key is present.
    """
    import litellm

    from agent.agent import LITELLM_COURSE_MODELS

    model = LITELLM_COURSE_MODELS.get(judge["model"], judge["model"])
    user_content = (
        f"Agent reply:\n{reply}\n\nPolicy documents retrieved in the trace:\n{docs}"
    )
    response = litellm.completion(
        model=model,
        messages=[
            {"role": "system", "content": judge["prompt_text"]},
            {"role": "user", "content": user_content},
        ],
        temperature=0,
        num_retries=3,
        timeout=120,
    )
    text = response.choices[0].message.content or ""
    match = re.search(r'"answer"\s*:\s*"(pass|fail)"', text)
    if match:
        return match.group(1), text
    lowered = text.lower()
    return ("fail" if "fail" in lowered else "pass"), text
