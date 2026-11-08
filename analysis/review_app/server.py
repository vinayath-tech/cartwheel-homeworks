"""Session-grouped review server for Homework 4.

Adapted from ``analysis/server.py``: a standard-library HTTP server over plain
files in ``analysis/state/``. It changes three things the reference does not
handle for Cartwheel traces:

  - Cartwheel writes one Langfuse trace per user turn, so traces are grouped
    by ``cartwheel.session_id`` and served as whole conversations. Notes and
    labels still attach to the individual trace, because Langfuse scores do.
  - Suggestions are never deleted. A decision records ``accepted`` or
    ``rejected`` with a reason, so the rejected suggestions stay inspectable.
  - Binary labels have their own endpoint. Each label is appended to
    ``state/labels/<mode>.jsonl`` (superseding the previous line for that
    trace) and written to Langfuse as a score with a deterministic id, so a
    relabel updates the score instead of adding a second one.

The facts endpoint reads the seeded SQLite world read-only so the reviewer can
check a reply against ground truth (an order's product title, a shopper's
total order count). It reflects the current seed, not the database at run time.

API:

    GET  /                          the review app
    GET  /api/sessions              all sessions (conversation, turns, flags)
    GET  /api/facts?session=<id>    ground truth for entities in one session
    GET  /api/manifest              review batches (state/sample_manifest.json)
    POST /api/manifest              replace the manifest
    GET  /api/annotations           open codes and "no failure observed" notes
    POST /api/annotations           replace the annotation list
    GET  /api/patterns              the taxonomy
    POST /api/patterns              replace the taxonomy
    GET  /api/suggestions           all suggestions, pending and decided
    POST /api/suggestions           replace the suggestion list (agent pushes)
    POST /api/suggestions/decide    {id, decision: accepted|rejected, reason}
    GET  /api/labels                live label per (mode, trace)
    POST /api/labels                {trace_id, mode, label: 0|1, note}
    POST /api/labels/sync           retry Langfuse writes that failed
    GET  /api/hw5                   HW5 definition, re-judge queue, live HW5 labels
    POST /api/hw5/label             {trace_id, label: 1 Pass | 0 Fail, note}; local file only

Run it from the repository root:

    uv run --env-file .env python -m analysis.review_app.server
    uv run python -m analysis.review_app.server --source traces/support_traces.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from analysis.helpers import _state
from analysis.helpers.normalization import normalize_trace

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
STATIC = HERE / "static"
DEFAULT_EXPORT = REPO / "traces" / "support_traces.json"

# Whole-document state files. GET reads the file, POST overwrites it.
DOC_FILES: dict[str, tuple[str, Any]] = {
    "/api/manifest": ("sample_manifest.json", {"batches": []}),
    "/api/annotations": ("annotations.json", {"annotations": []}),
    "/api/patterns": ("patterns.json", {"modes": []}),
    "/api/suggestions": ("suggestions.json", []),
}
# Read-only: written by prefill.py, confirmed through /api/proposals/confirm.
READ_ONLY_FILES: dict[str, tuple[str, Any]] = {
    "/api/proposals": ("label_proposals.json", []),
}

# The agent framework reports a tool that raised as this plain string instead
# of a structured {"ok": false} result, e.g. "... Error: database is locked".
EXCEPTION_PREFIX = "An error occurred while running the tool"

# One lock serializes every state write, so two quick clicks in the browser
# never interleave a label append with a suggestion decision. Reads of the
# document files take it too: on Windows a file open for reading cannot be
# replaced, so a poll that overlaps a save made the save fail.
WRITE_LOCK = threading.Lock()


def _retrying(write: Any, *args: Any) -> None:
    """Run a state write, retrying briefly while another process (an editor,
    a file indexer) holds the file open."""
    for attempt in range(8):
        try:
            write(*args)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.05 * (attempt + 1))

SESSIONS: list[dict[str, Any]] = []
TRACE_INDEX: dict[str, dict[str, Any]] = {}
SOURCE_LABEL = ""


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# trace loading and session grouping
# ---------------------------------------------------------------------------


def _load_raw_export(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["traces"] if isinstance(data, dict) else data


def _tool_flags(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return the harness exceptions and structured tool errors in one trace."""
    flags: list[dict[str, str]] = []
    for message in messages:
        if message.get("role") != "tool_result":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.startswith(EXCEPTION_PREFIX):
            flags.append(
                {
                    "kind": "harness_exception",
                    "tool": str(message.get("name")),
                    "detail": content.split("Error:", 1)[-1].strip(),
                }
            )
        elif isinstance(content, dict) and content.get("ok") is False:
            flags.append(
                {
                    "kind": "tool_error",
                    "tool": str(message.get("name")),
                    "detail": str(content.get("error")),
                }
            )
    return flags


def _turn_record(trace: dict[str, Any]) -> dict[str, Any]:
    return {
        "trace_id": trace["trace_id"],
        "timestamp": trace.get("timestamp"),
        "messages": trace["trace"],
        "flags": _tool_flags(trace["trace"]),
        "models": trace.get("models", []),
        "permalink": trace.get("permalink"),
    }


def build_sessions(traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group normalized per-turn traces into chronological conversations."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trace in traces:
        key = trace["meta"].get("session_id") or f"trace:{trace['trace_id']}"
        grouped[key].append(trace)

    sessions = []
    for session_id, group in grouped.items():
        group.sort(key=lambda t: t.get("timestamp") or "")
        first = group[0]
        turns = [_turn_record(t) for t in group]
        sessions.append(
            {
                "session_id": session_id,
                "scenario_id": first["meta"].get("scenario_id"),
                "role": first["meta"].get("role"),
                "user_id": first["metadata"].get("cartwheel.user_id"),
                "store_id": first["meta"].get("store"),
                "prompt_version": first["meta"].get("prompt_version"),
                "turns": turns,
                "harness_affected": any(
                    f["kind"] == "harness_exception" for t in turns for f in t["flags"]
                ),
            }
        )
    sessions.sort(key=lambda s: (s["scenario_id"] or "", s["session_id"]))
    return sessions


def _fetch_langfuse_traces() -> tuple[list[dict[str, Any]], str]:
    """Pull the Module 1 scenario traces from Langfuse.

    The trace list endpoint times out on this project even for small pages,
    while single-trace reads succeed. So try a short list first, and on
    failure take the trace identifiers from the Module 1 export and read each
    complete record from Langfuse, in parallel.
    """
    from langfuse import Langfuse

    from analysis.helpers import langfuse_io

    if not langfuse_io.is_configured():
        raise RuntimeError("Langfuse is not configured (run with --env-file .env)")
    client = langfuse_io._client()
    ids: list[str] = []
    label = "langfuse"
    try:
        lister = Langfuse(timeout=20)
        page = 1
        while True:
            batch = lister.api.trace.list(page=page, limit=50, fields="core").data or []
            ids.extend(t.id for t in batch)
            if len(batch) < 50:
                break
            page += 1
    except Exception as exc:
        print(f"trace list failed ({type(exc).__name__}); using export trace ids", flush=True)
        ids = [t["id"] for t in _load_raw_export(DEFAULT_EXPORT)]
        label = "langfuse (trace ids from the export; the list endpoint timed out)"
    print(f"fetching {len(ids)} full records from Langfuse ...", flush=True)

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:
        fulls = list(pool.map(client.api.trace.get, ids))
    projects = client.api.projects.get().data
    traces = []
    for full in fulls:
        trace = normalize_trace(full)
        if not trace["meta"].get("scenario_id"):
            continue
        if projects:
            trace["permalink"] = f"/project/{projects[0].id}/traces/{trace['trace_id']}"
        traces.append(trace)
    return traces, label


def _load_export(source: str) -> tuple[list[dict[str, Any]], str]:
    path = Path(source)
    if not path.is_absolute():
        path = REPO / path
    traces = []
    for raw in _load_raw_export(path):
        trace = normalize_trace(raw)
        if raw.get("htmlPath") and not trace.get("permalink"):
            trace["permalink"] = raw["htmlPath"]
        traces.append(trace)
    return traces, f"export: {path.relative_to(REPO)}"


def load_sessions(source: str) -> tuple[list[dict[str, Any]], str]:
    """Load traces from Langfuse (``langfuse``) or an export file.

    Langfuse is the canonical store. When it cannot be reached, fall back to
    the Module 1 export and put the reason in the source label, which the
    interface displays, so the fallback is recorded rather than silent.
    """
    if source != "langfuse":
        traces, label = _load_export(source)
        return build_sessions(traces), label
    try:
        print("loading traces from Langfuse ...", flush=True)
        traces, label = _fetch_langfuse_traces()
        # The project also holds the Homework 3 pilot run. The review
        # population is the final run, whose trace ids the export lists.
        final_ids = {t["id"] for t in _load_raw_export(DEFAULT_EXPORT)}
        dropped = sum(t["trace_id"] not in final_ids for t in traces)
        traces = [t for t in traces if t["trace_id"] in final_ids]
        label = f"{label}, final run only ({dropped} pilot or other traces excluded)"
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}".strip()
        print(f"Langfuse unavailable ({reason}); falling back to the export", flush=True)
        traces, label = _load_export(str(DEFAULT_EXPORT))
        label = f"{label} (Langfuse unavailable: {reason[:120]})"
    return build_sessions(traces), label


# ---------------------------------------------------------------------------
# ground-truth facts from the seeded database
# ---------------------------------------------------------------------------

ORDER_PATTERNS = [
    re.compile(r'"order_id":\s*(\d+)'),
    re.compile(r"order\s*#?\s*(\d{3,6})", re.IGNORECASE),
    re.compile(r"#(\d{3,6})"),
]
PRODUCT_PATTERN = re.compile(r'"product_id":\s*(\d+)')
STORE_PATTERN = re.compile(r'"store_id":\s*(\d+)')


def _session_blob(session: dict[str, Any]) -> str:
    return json.dumps([t["messages"] for t in session["turns"]], ensure_ascii=False)


def _db() -> sqlite3.Connection:
    from agent.config import db_path

    conn = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def session_facts(session: dict[str, Any]) -> dict[str, Any]:
    """Look up every order, product, store, and the caller in one session."""
    blob = _session_blob(session)
    order_ids = sorted({int(m) for p in ORDER_PATTERNS for m in p.findall(blob)})
    product_ids = {int(m) for m in PRODUCT_PATTERN.findall(blob)}
    store_ids = {int(m) for m in STORE_PATTERN.findall(blob)}

    facts: dict[str, Any] = {"caller": None, "orders": [], "products": [], "stores": []}
    with closing(_db()) as conn:
        user_id = session.get("user_id")
        if user_id and str(user_id).isdigit():
            row = conn.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
            if row:
                caller = dict(row)
                caller["order_count"] = conn.execute(
                    "SELECT count(*) FROM orders WHERE user_id = ?", (row["id"],)
                ).fetchone()[0]
                if row["store_id"]:
                    caller["store_order_count"] = conn.execute(
                        "SELECT count(*) FROM orders WHERE store_id = ?", (row["store_id"],)
                    ).fetchone()[0]
                    store_ids.add(row["store_id"])
                facts["caller"] = caller

        for order_id in order_ids:
            row = conn.execute(
                "SELECT o.*, p.title AS product_title, s.name AS store_name "
                "FROM orders o JOIN products p ON p.id = o.product_id "
                "JOIN stores s ON s.id = o.store_id WHERE o.id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                facts["orders"].append({"id": order_id, "missing": True})
                continue
            order = dict(row)
            order["total_usd"] = order.pop("total_cents") / 100
            order["refunds"] = [
                {**dict(r), "amount_usd": r["amount_cents"] / 100}
                for r in conn.execute("SELECT * FROM refunds WHERE order_id = ?", (order_id,))
            ]
            facts["orders"].append(order)
            product_ids.add(row["product_id"])
            store_ids.add(row["store_id"])

        for product_id in sorted(product_ids):
            row = conn.execute(
                "SELECT p.id, p.title, p.category, p.price_cents, s.name AS store_name "
                "FROM products p JOIN stores s ON s.id = p.store_id WHERE p.id = ?",
                (product_id,),
            ).fetchone()
            if row:
                product = dict(row)
                product["price_usd"] = product.pop("price_cents") / 100
                facts["products"].append(product)

        for store_id in sorted(store_ids):
            row = conn.execute("SELECT * FROM stores WHERE id = ?", (store_id,)).fetchone()
            if row:
                facts["stores"].append(dict(row))

        case_rows = conn.execute("SELECT * FROM data_quality_cases").fetchall()
        referenced = {("order", o) for o in order_ids} | {("product", p) for p in product_ids}
        facts["data_quality_cases"] = [
            dict(r) for r in case_rows if (r["entity_type"], r["entity_id"]) in referenced
        ]
    facts["note"] = "Current seeded database, read-only. It matches the run only if the seed is unchanged."
    return facts


# ---------------------------------------------------------------------------
# labels and Langfuse scores
# ---------------------------------------------------------------------------


def _labels_path(mode: str) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", mode):
        raise ValueError(f"mode names must be snake_case: {mode!r}")
    return _state.state_path("labels", f"{mode}.jsonl")


def _score_id(trace_id: str, mode: str) -> str:
    """Deterministic Langfuse score id, so a relabel updates the same score."""
    return hashlib.sha256(f"hw4:{trace_id}:{mode}".encode()).hexdigest()[:32]


def _push_score(record: dict[str, Any], mode: str) -> None:
    from analysis.helpers import langfuse_io

    if not langfuse_io.is_configured():
        raise RuntimeError("Langfuse is not configured")
    from langfuse.api import CreateScoreRequest

    # The direct API call raises on failure. client.create_score batches the
    # write and its flush only logs errors, so a failed write looked synced.
    request = CreateScoreRequest(
        id=_score_id(record["trace_id"], mode),
        trace_id=record["trace_id"],
        name=mode,
        value=int(record["label"]),
        data_type="NUMERIC",
        comment=record.get("note") or None,
        metadata={"source": record["source"], "label_id": record["label_id"], "convention": "1 = failure present"},
    )
    api = langfuse_io._client().api
    for attempt in range(3):
        try:
            api.score.create(request=request, request_options={"timeout_in_seconds": 30})
            return
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def _delete_score(trace_id: str, mode: str) -> None:
    from analysis.helpers import langfuse_io

    if not langfuse_io.is_configured():
        raise RuntimeError("Langfuse is not configured")
    langfuse_io._client().api.score.delete(_score_id(trace_id, mode))


def _known_modes() -> list[str]:
    patterns = _state.read_json(_state.state_path("patterns.json"), {"modes": []})
    modes = patterns.get("modes", patterns) if isinstance(patterns, dict) else []
    if isinstance(modes, dict):
        return list(modes)
    return [m["name"] for m in modes if isinstance(m, dict) and m.get("name")]


def live_labels() -> dict[str, dict[str, dict[str, Any]]]:
    """Return {mode: {trace_id: live record}} for every mode in the taxonomy."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for mode in _known_modes():
        live: dict[str, dict[str, Any]] = {}
        for row in _state.read_jsonl(_labels_path(mode)):
            if not row.get("superseded_by") and not row.get("withdrawn"):
                live[row["trace_id"]] = row
        out[mode] = live
    return out


def withdraw_label(trace_id: str, mode: str, note: str) -> dict[str, Any]:
    """Take back a label without replacing it with a judgment.

    Used when an accepted suggestion is reopened or rejected: the reviewer has
    withdrawn the Fail, which is not the same as deciding Pass. The local file
    keeps the history (append-only); the Langfuse score is deleted.
    """
    path = _labels_path(mode)
    with WRITE_LOCK:
        rows = _state.read_jsonl(path)
        live = [r for r in rows if r["trace_id"] == trace_id and not r.get("superseded_by")]
        if not live or live[-1].get("withdrawn"):
            return {}
        record = {
            "trace_id": trace_id,
            "label": None,
            "withdrawn": True,
            "source": "withdrawn",
            "note": note,
            "ts": _utcnow(),
            "label_id": f"{trace_id}#{sum(1 for r in rows if r['trace_id'] == trace_id)}",
            "langfuse_synced": False,
        }
        for row in live:
            row["superseded_by"] = record["label_id"]
        try:
            _delete_score(trace_id, mode)
            record["langfuse_synced"] = True
        except Exception as exc:
            record["langfuse_error"] = str(exc)
        rows.append(record)
        _retrying(_state.write_jsonl, path, rows)
    return record


def save_label(trace_id: str, mode: str, label: int, note: str, source: str = "human") -> dict[str, Any]:
    """Append a label, supersede the previous one, then write the score."""
    if label not in (0, 1):
        raise ValueError("label must be 0 (failure absent) or 1 (failure present)")
    if trace_id not in TRACE_INDEX:
        raise ValueError(f"unknown trace id {trace_id}")
    path = _labels_path(mode)
    with WRITE_LOCK:
        rows = _state.read_jsonl(path)
        count = sum(1 for r in rows if r["trace_id"] == trace_id)
        record = {
            "trace_id": trace_id,
            "label": label,
            "source": source,
            "note": note,
            "ts": _utcnow(),
            "label_id": f"{trace_id}#{count}",
            "langfuse_synced": False,
        }
        for row in rows:
            if row["trace_id"] == trace_id and not row.get("superseded_by"):
                row["superseded_by"] = record["label_id"]
        try:
            _push_score(record, mode)
            record["langfuse_synced"] = True
        except Exception as exc:  # network path; the local copy is kept for retry
            record["langfuse_error"] = str(exc)
        rows.append(record)
        _retrying(_state.write_jsonl, path, rows)
    return record


def sync_unsynced() -> dict[str, int]:
    written, failed = 0, 0
    with WRITE_LOCK:
        for mode in _known_modes():
            path = _labels_path(mode)
            rows = _state.read_jsonl(path)
            changed = False
            for row in rows:
                if row.get("superseded_by") or row.get("langfuse_synced"):
                    continue
                try:
                    if row.get("withdrawn"):
                        _delete_score(row["trace_id"], mode)
                    else:
                        _push_score(row, mode)
                    row["langfuse_synced"] = True
                    row.pop("langfuse_error", None)
                    written += 1
                except Exception as exc:
                    row["langfuse_error"] = str(exc)
                    failed += 1
                changed = True
            if changed:
                _retrying(_state.write_jsonl, path, rows)
    return {"written": written, "failed": failed}


# ---------------------------------------------------------------------------
# suggestion decisions
# ---------------------------------------------------------------------------


def confirm_proposals(mode: str, trace_ids: list[str] | None = None) -> dict[str, int]:
    """Turn the reviewer-confirmed proposals for one mode into labels.

    Only pairs with a proposed value and no live label are written, so a
    label the reviewer set by hand is never overwritten. trace_ids limits the
    confirmation to those traces. Each label keeps its proposal source.
    """
    proposals = _state.read_json(_state.state_path("label_proposals.json"), [])
    live = live_labels().get(mode, {})
    wanted = set(trace_ids) if trace_ids else None
    written = failed = skipped = 0
    for p in proposals:
        if p["mode"] != mode or p["label"] is None or p["source"] == "existing_label":
            continue
        if (wanted is not None and p["trace_id"] not in wanted) or p["trace_id"] in live:
            skipped += 1
            continue
        record = save_label(p["trace_id"], mode, int(p["label"]), f"confirmed proposal ({p['source']}): {p['why']}",
                            source=f"proposal_{p['source']}")
        written += 1
        failed += 0 if record.get("langfuse_synced") else 1
    return {"written": written, "langfuse_failed": failed, "skipped": skipped}


def decide_suggestion(suggestion_id: str, decision: str, reason: str) -> dict[str, Any]:
    """Record, change or reopen a decision on a suggestion.

    accepted: adds (or restores) the annotation and a Fail label. A suggestion
      with purpose 'close_negative' instead gets a Pass label and no
      annotation: accepting it says the trace resembles the mode but passes.
    rejected: needs a reason naming the boundary that excludes the trace.
    pending: reopens it. Leaving 'accepted' retracts the annotation and
    withdraws the label and its Langfuse score. Every earlier decision is kept
    in decision_history.
    """
    if decision not in ("accepted", "rejected", "pending"):
        raise ValueError("decision must be accepted, rejected or pending")
    if decision == "rejected" and not reason.strip():
        raise ValueError("a rejection needs a reason (the boundary that excludes it)")
    path = _state.state_path("suggestions.json")
    ann_path = _state.state_path("annotations.json")
    with WRITE_LOCK:
        suggestions = _state.read_json(path, [])
        match = next((s for s in suggestions if s.get("id") == suggestion_id), None)
        if match is None:
            raise ValueError(f"unknown suggestion {suggestion_id}")
        previous = match.get("status") or "pending"
        if previous != "pending" or match.get("decision_reason"):
            match.setdefault("decision_history", []).append(
                {"status": previous, "reason": match.get("decision_reason"), "decided_at": match.get("decided_at")})
        match.update({"status": decision, "decision_reason": reason if decision != "pending" else None,
                      "decided_at": _utcnow() if decision != "pending" else None})
        _retrying(_state.write_json, path, suggestions)

        close_negative = match.get("purpose") == "close_negative"
        doc = _state.read_json(ann_path, {"annotations": []})
        ann = next((a for a in doc.get("annotations", []) if a.get("id") == f"a-{suggestion_id}"), None)
        if close_negative:
            pass
        elif decision == "accepted":
            note = reason or match.get("rationale") or f"[{match.get('mode')}]"
            if ann is None:
                doc.setdefault("annotations", []).append({
                    "id": f"a-{suggestion_id}",
                    "trace_id": match["trace_id"],
                    "session_id": match.get("session_id"),
                    "quote": match.get("quote"),
                    "idx": match.get("idx"),
                    "note": note,
                    "kind": "open_code",
                    "source": "accepted_suggestion",
                    "suggestion_id": suggestion_id,
                    "mode": match.get("mode"),
                    "status": "active",
                    "ts": _utcnow(),
                    "revisions": [],
                })
            else:
                ann["revisions"] = [*(ann.get("revisions") or []), {"note": ann["note"], "ts": ann["ts"], "status": ann.get("status")}]
                ann.update(note=note, status="active", ts=_utcnow())
            _retrying(_state.write_json, ann_path, doc)
        elif ann is not None and ann.get("status") != "retracted":
            ann["revisions"] = [*(ann.get("revisions") or []), {"note": ann["note"], "ts": ann["ts"], "status": ann.get("status")}]
            ann.update(status="retracted", ts=_utcnow())
            _retrying(_state.write_json, ann_path, doc)

    if match.get("mode"):
        if decision == "accepted":
            save_label(match["trace_id"], match["mode"], 0 if close_negative else 1, reason, source="accepted_suggestion")
        elif previous == "accepted":
            withdraw_label(match["trace_id"], match["mode"], f"suggestion {suggestion_id} changed to {decision}: {reason}")
    return match


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        return

    def _json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return None

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        if path in ("/", "/index.html"):
            body = (STATIC / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/sessions":
            self._json(
                {
                    "source": SOURCE_LABEL,
                    "langfuse_host": os.environ.get("LANGFUSE_HOST", "").rstrip("/"),
                    "sessions": SESSIONS,
                }
            )
            return
        if path == "/api/facts":
            session_id = parse_qs(url.query).get("session", [""])[0]
            session = next((s for s in SESSIONS if s["session_id"] == session_id), None)
            if session is None:
                self._json({"error": "unknown session"}, 404)
                return
            try:
                self._json(session_facts(session))
            except Exception as exc:
                self._json({"error": f"facts lookup failed: {exc}"}, 500)
            return
        if path == "/api/labels":
            with WRITE_LOCK:
                labels = live_labels()
            self._json(labels)
            return
        if path == "/api/hw5":
            from analysis.review_app import hw5

            with WRITE_LOCK:
                doc = {
                    "mode": hw5.MODE,
                    "definition": _state.read_json(_state.state_path("hw5_definition.json"), {}),
                    "queue": hw5.queue(),
                    "labels": hw5.live(),
                    "dev_judge": hw5.dev_judgments(),
                }
            self._json(doc)
            return
        if path in DOC_FILES or path in READ_ONLY_FILES:
            name, default = DOC_FILES.get(path) or READ_ONLY_FILES[path]
            with WRITE_LOCK:
                doc = _state.read_json(_state.state_path(name), default)
            self._json(doc)
            return
        self._json({"error": f"unknown path {path}"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        data = self._body()
        if data is None and path != "/api/labels/sync":
            self._json({"error": "expected a JSON body"}, 400)
            return
        try:
            if path == "/api/labels":
                record = save_label(
                    str(data["trace_id"]), str(data["mode"]), int(data["label"]), str(data.get("note") or "")
                )
                status = 200 if record["langfuse_synced"] else 207
                self._json(record, status)
                return
            if path == "/api/labels/sync":
                self._json(sync_unsynced())
                return
            if path == "/api/suggestions/decide":
                self._json(decide_suggestion(str(data["id"]), str(data["decision"]), str(data.get("reason") or "")))
                return
            if path == "/api/hw5/label":
                from analysis.review_app import hw5

                if str(data["trace_id"]) not in TRACE_INDEX:
                    raise ValueError(f"unknown trace id {data['trace_id']}")
                with WRITE_LOCK:
                    self._json(hw5.save(str(data["trace_id"]), int(data["label"]), str(data.get("note") or "")))
                return
            if path == "/api/proposals/confirm":
                self._json(confirm_proposals(str(data["mode"]), data.get("trace_ids")))
                return
            if path in DOC_FILES:
                name, _ = DOC_FILES[path]
                with WRITE_LOCK:
                    _retrying(_state.write_json, _state.state_path(name), data)
                self._json({"ok": True})
                return
        except (KeyError, ValueError) as exc:
            self._json({"error": str(exc)}, 400)
            return
        except OSError as exc:  # the browser shows this as a toast instead of silently losing the save
            self._json({"error": f"save failed: {exc}"}, 500)
            return
        self._json({"error": f"cannot POST to {path}"}, 404)


def main() -> None:
    global SESSIONS, TRACE_INDEX, SOURCE_LABEL
    parser = argparse.ArgumentParser(description="Homework 4 review interface")
    parser.add_argument("--source", default="langfuse", help="'langfuse' or a trace export path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8030)
    args = parser.parse_args()

    SESSIONS, SOURCE_LABEL = load_sessions(args.source)
    TRACE_INDEX = {t["trace_id"]: s for s in SESSIONS for t in s["turns"]}
    affected = sum(s["harness_affected"] for s in SESSIONS)
    print(
        f"{len(SESSIONS)} sessions, {len(TRACE_INDEX)} traces from {SOURCE_LABEL}; "
        f"{affected} sessions contain a harness exception"
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"review interface on http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
