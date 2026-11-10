"""Import the reviewer's Langfuse comments as open codes.

Part A asks for a first review in the standard Langfuse view, where notes are
written as comments. This copies every comment in the project into
``state/annotations.json`` so the notes enter the open-coding record instead
of staying behind in Langfuse.

A comment on an observation is attached to its parent trace. The same text on
a trace and on one of its observations is imported once. A comment starting
with "Error" becomes an open code; any other comment records a reviewed trace
with no failure observed and keeps the reviewer's text as the note. Existing
imports are skipped by comment id, so the command can be rerun.

    uv run --env-file .env python -m analysis.review_app.import_comments
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from langfuse import Langfuse

from analysis.helpers import _state
from analysis.review_app.server import DEFAULT_EXPORT, _load_raw_export


def _all_comments(client: Langfuse) -> list[Any]:
    out, page = [], 1
    while True:
        batch = client.api.comments.get(page=page, limit=100).data or []
        out.extend(batch)
        if len(batch) < 100:
            return out
        page += 1


def main() -> None:
    client = Langfuse(timeout=30)
    meta = {}
    for raw in _load_raw_export(DEFAULT_EXPORT):
        attrs = (raw.get("metadata") or {}).get("attributes") or {}
        meta[raw["id"]] = (attrs.get("cartwheel.session_id"), raw.get("cartwheel_scenario_id"))

    path = _state.state_path("annotations.json")
    doc = _state.read_json(path, {"annotations": []})
    anns = doc.setdefault("annotations", [])
    seen_ids = {a.get("langfuse_comment_id") for a in anns}
    seen_text = {(a["trace_id"], a["note"]) for a in anns}

    added = []
    for c in sorted(_all_comments(client), key=lambda c: str(c.created_at)):
        object_type = str(getattr(c.object_type, "value", c.object_type)).upper()
        if object_type == "TRACE":
            trace_id = c.object_id
        elif object_type == "OBSERVATION":
            trace_id = client.api.observations.get(c.object_id).trace_id
        else:
            continue
        note = c.content.strip()
        if c.id in seen_ids or (trace_id, note) in seen_text:
            continue
        session_id, scenario_id = meta.get(trace_id, (None, None))
        failure = note.lower().startswith("error")
        record = {
            "id": f"lf-{c.id}",
            "trace_id": trace_id,
            "session_id": session_id,
            "scenario_id": scenario_id,
            "quote": None,
            "idx": None,
            "note": note,
            "kind": "open_code" if failure else "no_failure",
            "source": "langfuse_comment",
            "langfuse_comment_id": c.id,
            "langfuse_object": f"{object_type.lower()}:{c.object_id}",
            "status": "active",
            "ts": c.created_at.isoformat() if isinstance(c.created_at, _dt.datetime) else str(c.created_at),
            "revisions": [],
        }
        anns.append(record)
        seen_ids.add(c.id)
        seen_text.add((trace_id, note))
        added.append(record)

    _state.write_json(path, doc)
    print(f"imported {len(added)} comments ({len(anns)} annotations in total)")
    for r in added:
        print(json.dumps({k: r[k] for k in ("scenario_id", "trace_id", "kind", "note")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
