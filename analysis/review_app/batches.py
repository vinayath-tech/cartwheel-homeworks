"""Select review batches and record them in ``state/sample_manifest.json``.

Sampling unit. Batches are drawn over individual traces, so a uniform draw
gives every trace (not every conversation) the same chance. A drawn trace
brings the other turns of its conversation into the same batch, because the
first failure of a follow-up turn often sits in an earlier turn. Those turns
are recorded as ``session_sibling`` picks so the manifest shows which traces
were drawn and which came along.

No trace belongs to more than one batch, and conversations reviewed during
Part A in the standard Langfuse view are excluded from every batch.

    uv run python -m analysis.review_app.batches batch1 --seed 20261027
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import random
import re
import sys
from collections import Counter
from typing import Any

from analysis.helpers import _state
from analysis.helpers.selection import _FEATURES, _feature_vector, _kmeans, _standardize
from analysis.review_app.server import DEFAULT_EXPORT, REPO, build_sessions, _load_export

# Conversations the reviewer read in the standard Langfuse view (Part A).
PRE_REVIEW_SCENARIOS = [
    "support-0110", "support-0140", "support-0148", "support-0189", "support-0232", "support-0242",
    "support-0247", "support-0250",
]

HARNESS_POLICY = (
    "Option A: traces containing a harness exception (for example 'database is locked') stay "
    "in the sample and are flagged. The exception itself is not a failure mode; the agent's "
    "handling of it is reviewed."
)


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _load() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    traces, _ = _load_export(str(DEFAULT_EXPORT))
    sessions = build_sessions(traces)
    by_trace = {}
    for s in sessions:
        for i, t in enumerate(s["turns"]):
            by_trace[t["trace_id"]] = {"session": s, "turn": i, "trace": t}
    for trace in traces:
        by_trace[trace["trace_id"]]["features"] = trace["features"]
    return sessions, by_trace


def _manifest(sessions: list[dict[str, Any]], by_trace: dict[str, Any]) -> dict[str, Any]:
    path = _state.state_path("sample_manifest.json")
    manifest = _state.read_json(path, None) or {}
    if not manifest.get("batches"):
        pre = [s for s in sessions if s["scenario_id"] in PRE_REVIEW_SCENARIOS]
        manifest = {
            "trace_source": str(DEFAULT_EXPORT.relative_to(REPO)),
            "population": {"sessions": len(sessions), "traces": len(by_trace)},
            "sampling_unit": "trace; a drawn trace brings its conversation's other turns into the same batch",
            "harness_affected_policy": HARNESS_POLICY,
            "pre_review": {
                "description": "Part A review in the standard Langfuse view; not counted in any batch",
                "scenario_ids": PRE_REVIEW_SCENARIOS,
                "trace_ids": [t["trace_id"] for s in pre for t in s["turns"]],
            },
            "batches": [],
        }
    return manifest


def _excluded_sessions(manifest: dict[str, Any], by_trace: dict[str, Any]) -> set[str]:
    ids = set(manifest["pre_review"]["trace_ids"])
    for batch in manifest["batches"]:
        ids.update(batch["trace_ids"])
    anns = _state.read_json(_state.state_path("annotations.json"), {"annotations": []})
    ids.update(a["trace_id"] for a in anns.get("annotations", []) if a.get("trace_id") in by_trace)
    return {by_trace[t]["session"]["session_id"] for t in ids if t in by_trace}


def _pick(by_trace: dict[str, Any], trace_id: str, method: str, reason: str, **extra: Any) -> dict[str, Any]:
    hit = by_trace[trace_id]
    return {
        "trace_id": trace_id,
        "scenario_id": hit["session"]["scenario_id"],
        "role": hit["session"]["role"],
        "turn": hit["turn"] + 1,
        "method": method,
        "reason": reason,
        "harness_affected": any(f["kind"] == "harness_exception" for f in hit["trace"]["flags"]),
        **extra,
    }


def _with_siblings(picks: list[dict[str, Any]], by_trace: dict[str, Any]) -> list[dict[str, Any]]:
    out, seen = [], set()
    for p in picks:
        session = by_trace[p["trace_id"]]["session"]
        for t in session["turns"]:
            tid = t["trace_id"]
            if tid in seen:
                continue
            seen.add(tid)
            if tid == p["trace_id"]:
                out.append(p)
            else:
                out.append(_pick(by_trace, tid, "session_sibling", f"other turn of drawn trace {p['trace_id'][:8]}"))
    return out


def _tool_names(trace: dict[str, Any]) -> list[str]:
    return sorted({m["name"] for m in trace["messages"] if m.get("role") == "tool_call" and m.get("name")})


def uniform_picks(pool: list[str], k: int, rng: random.Random, by_trace: dict[str, Any]) -> list[dict[str, Any]]:
    """Draw ``k`` traces uniformly, at most one per conversation."""
    picks, used = [], set()
    for tid in rng.sample(pool, len(pool)):
        sid = by_trace[tid]["session"]["session_id"]
        if sid in used:
            continue
        used.add(sid)
        picks.append(_pick(by_trace, tid, "uniform", "uniform random draw over unreviewed traces"))
        if len(picks) == k:
            break
    return picks


def cluster_picks(pool: list[str], k: int, seed: int, by_trace: dict[str, Any]) -> list[dict[str, Any]]:
    """Cluster traces on structure and take the member nearest each centroid.

    Features: the shared numeric trace features, the turn position, and one
    indicator per tool called. They describe what the trace did, never
    whether it failed, so the draw cannot select on a predicted failure.
    """
    tools = sorted({n for tid in pool for n in _tool_names(by_trace[tid]["trace"])})
    rows = []
    for tid in pool:
        names = set(_tool_names(by_trace[tid]["trace"]))
        rows.append(
            _feature_vector({"features": by_trace[tid]["features"]})
            + [float(by_trace[tid]["turn"])]
            + [1.0 if n in names else 0.0 for n in tools]
        )
    vectors = _standardize(rows)
    assign = _kmeans(vectors, k=k, seed=seed)

    members: dict[int, list[int]] = {}
    for i, c in enumerate(assign):
        members.setdefault(c, []).append(i)
    picks, used = [], set()
    for c in sorted(members, key=lambda c: -len(members[c])):
        idxs = members[c]
        centroid = [sum(vectors[i][d] for i in idxs) / len(idxs) for d in range(len(vectors[0]))]
        ranked = sorted(idxs, key=lambda i: sum((a - b) ** 2 for a, b in zip(vectors[i], centroid)))
        for i in ranked:
            tid = pool[i]
            sid = by_trace[tid]["session"]["session_id"]
            if sid in used:
                continue
            used.add(sid)
            f = by_trace[tid]["features"]
            names = _tool_names(by_trace[tid]["trace"]) or ["no tools"]
            picks.append(
                _pick(
                    by_trace, tid, "cluster",
                    f"nearest to centroid of cluster {c} (n={len(idxs)}): turn {by_trace[tid]['turn'] + 1}, "
                    f"{int(f['tool_call_count'])} tool calls ({', '.join(names)})",
                    cluster=c, cluster_size=len(idxs),
                )
            )
            break
    return picks


def batch1(seed: int, k_uniform: int, k_cluster: int) -> dict[str, Any]:
    sessions, by_trace = _load()
    manifest = _manifest(sessions, by_trace)
    if any(b["id"] == "batch1" for b in manifest["batches"]):
        sys.exit("batch1 already exists in the manifest; refusing to redraw it")

    excluded = _excluded_sessions(manifest, by_trace)
    pool = sorted(t for t, hit in by_trace.items() if hit["session"]["session_id"] not in excluded)
    rng = random.Random(seed)
    uniform = uniform_picks(pool, k_uniform, rng, by_trace)

    taken = {by_trace[p["trace_id"]]["session"]["session_id"] for p in uniform}
    cluster_pool = [t for t in pool if by_trace[t]["session"]["session_id"] not in taken]
    cluster = cluster_picks(cluster_pool, k_cluster, seed, by_trace)

    picks = _with_siblings(uniform + cluster, by_trace)
    batch = {
        "id": "batch1",
        "name": "Batch 1 · uniform + cluster",
        "method": f"{k_uniform} uniform traces, then {k_cluster} cluster representatives (k-means on "
                  f"{', '.join(_FEATURES)}, turn position, tools called)",
        "target": k_uniform + k_cluster,
        "seed": seed,
        "selected_at": _utcnow(),
        "command": f"uv run python -m analysis.review_app.batches batch1 --seed {seed}",
        "pool_traces": len(pool),
        "trace_ids": [p["trace_id"] for p in picks],
        "picks": picks,
    }
    manifest["batches"].append(batch)
    _state.write_json(_state.state_path("sample_manifest.json"), manifest)
    return batch


def batch2(seed: int, per_role: int) -> dict[str, Any]:
    """Stratify on caller role, chosen before looking at outcomes: the roles
    have different permissions (AUTH-1) and jobs, and Batch 1 was mostly
    shopper traffic. Within each role the draw is uniform over traces."""
    sessions, by_trace = _load()
    manifest = _manifest(sessions, by_trace)
    if not any(b["id"] == "batch1" for b in manifest["batches"]):
        sys.exit("draw batch1 first")
    if any(b["id"] == "batch2" for b in manifest["batches"]):
        sys.exit("batch2 already exists in the manifest; refusing to redraw it")

    excluded = _excluded_sessions(manifest, by_trace)
    pool = sorted(t for t, hit in by_trace.items() if hit["session"]["session_id"] not in excluded)
    rng = random.Random(seed)
    picks, pool_sizes = [], {}
    for role in ("shopper", "merchant", "support"):
        stratum = [t for t in pool if by_trace[t]["session"]["role"] == role]
        pool_sizes[role] = len(stratum)
        for p in uniform_picks(stratum, per_role, rng, by_trace):
            p.update(method="stratified_role", reason=f"uniform draw within role '{role}' ({len(stratum)} traces in stratum)")
            picks.append(p)

    picks = _with_siblings(picks, by_trace)
    batch = {
        "id": "batch2",
        "name": "Batch 2 · stratified by caller role",
        "method": f"{per_role} traces per caller role (shopper, merchant, support), uniform within each role, "
                  f"at most one drawn trace per conversation",
        "dimension": "caller role",
        "dimension_rationale": "chosen before reviewing outcomes: roles differ in permissions (AUTH-1) and tasks, and "
                               "Batch 1 was 32 shopper / 11 merchant / 7 support traces",
        "target": per_role * 3,
        "seed": seed,
        "selected_at": _utcnow(),
        "command": f"uv run python -m analysis.review_app.batches batch2 --seed {seed}",
        "pool_traces": len(pool),
        "pool_traces_by_role": pool_sizes,
        "trace_ids": [p["trace_id"] for p in picks],
        "picks": picks,
    }
    manifest["batches"].append(batch)
    _state.write_json(_state.state_path("sample_manifest.json"), manifest)
    return batch


def _tool_results(trace: dict[str, Any], name: str | None = None):
    for m in trace["messages"]:
        if m.get("role") == "tool_result" and (name is None or m.get("name") == name):
            content = m.get("content")
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except ValueError:
                    pass
            if isinstance(content, dict):
                yield content


def _text(trace: dict[str, Any], role: str) -> str:
    return " ".join(m.get("text") or "" for m in trace["messages"] if m.get("role") == role)


DQ_PRODUCTS, DQ_ORDERS = {1, 2, 3, 4}, {8001, 8002, 8003}  # entity ids in data_quality_cases

# Batch 3 searches. Each selects on what the trace contains, never on whether
# it failed, so every search can return passes (close negatives) as well as
# failures. Order matters: a trace counts for the first search it matches.
SEARCHES: list[tuple[str, str, str, int, Any]] = [
    ("list_6_plus", "partial_list_presented_as_complete",
     "a tool returned 6 or more orders or products", 5,
     lambda t: any(len(c.get("orders") or c.get("products") or []) >= 6 for c in _tool_results(t))),
    ("no_id_multi_orders", "acts_on_assumed_target",
     "the user gave no order id and list_my_orders returned more than one order", 5,
     lambda t: not re.search(r"\d{3,}", _text(t, "user"))
     and any(len(c.get("orders") or []) > 1 for c in _tool_results(t, "list_my_orders"))),
    ("search_zero", "false_absence_claim",
     "search_products returned 0 results", 4,
     lambda t: any(c.get("count") == 0 for c in _tool_results(t, "search_products"))),
    ("refund_unasked", "unrequested_eligibility_verdict",
     "the user never mentions refunds or returns but the reply mentions a refund (selects on reply text)", 4,
     lambda t: not re.search(r"refund|return|money back", _text(t, "user"), re.I)
     and bool(re.search(r"refund", _text(t, "assistant"), re.I))),
    ("dq_entity", "invalid_record_presented_as_valid",
     "a tool result contains a data-quality record (products 1-4, orders 8001-8003)", 4,
     lambda t: any((c.get("order") or {}).get("order_id") in DQ_ORDERS or c.get("order_id") in DQ_ORDERS
                   or any(p.get("product_id") in DQ_PRODUCTS for p in c.get("products") or [])
                   for c in _tool_results(t))),
    ("dispute_words", "missed_escalation",
     "the user mentions a dispute, chargeback, wrong item or not-as-described", 3,
     lambda t: bool(re.search(r"disput|chargeback|not as described|didn.t match|wrong item", _text(t, "user"), re.I))),
]


def batch3(seed: int) -> dict[str, Any]:
    sessions, by_trace = _load()
    manifest = _manifest(sessions, by_trace)
    if not any(b["id"] == "batch2" for b in manifest["batches"]):
        sys.exit("draw batch2 first")
    if any(b["id"] == "batch3" for b in manifest["batches"]):
        sys.exit("batch3 already exists in the manifest; refusing to redraw it")

    excluded = _excluded_sessions(manifest, by_trace)
    pool = sorted(t for t, hit in by_trace.items() if hit["session"]["session_id"] not in excluded)
    rng = random.Random(seed)
    picks, used, searches = [], set(), []
    for name, mode, description, k, match in SEARCHES:
        hits = [t for t in pool if match(by_trace[t]["trace"])]
        available = [t for t in hits if by_trace[t]["session"]["session_id"] not in used]
        drawn = uniform_picks(available, k, rng, by_trace)
        for p in drawn:
            used.add(by_trace[p["trace_id"]]["session"]["session_id"])
            p.update(method="targeted_search", search=name, target_mode=mode,
                     reason=f"random draw among {len(available)} traces where {description}")
        picks += drawn
        searches.append({"name": name, "target_mode": mode, "description": description, "requested": k,
                         "matched_traces": len(hits), "available_after_earlier_searches": len(available),
                         "drawn": len(drawn)})

    picks = _with_siblings(picks, by_trace)
    batch = {
        "id": "batch3",
        "name": "Batch 3 · targeted searches",
        "method": "structural searches aimed at modes lacking positives or close negatives; random draw within "
                  "each search, at most one drawn trace per conversation, first matching search wins",
        "selection_note": "searches select on trace content, not on a predicted failure; refund_unasked reads "
                          "the reply text, which is closer to selecting on behaviour",
        "searches": searches,
        "target": sum(s[3] for s in SEARCHES),
        "seed": seed,
        "selected_at": _utcnow(),
        "command": f"uv run python -m analysis.review_app.batches batch3 --seed {seed}",
        "pool_traces": len(pool),
        "trace_ids": [p["trace_id"] for p in picks],
        "picks": picks,
    }
    manifest["batches"].append(batch)
    _state.write_json(_state.state_path("sample_manifest.json"), manifest)
    return batch


TOPUP_SEARCHES = ["refund_unasked", "dq_entity", "dispute_words"]


def batch3_topup(seed: int) -> dict[str, Any]:
    """Bring Batch 3 from 22 to the handout's 25 drawn conversations.

    no_id_multi_orders came up 3 short because its other matches were taken by
    list_6_plus. One extra draw comes from each of the three searches whose
    target modes have the fewest close negatives and that still have unused
    matches. Appended to batch3 (not a new batch) and logged as a top-up.
    """
    sessions, by_trace = _load()
    manifest = _manifest(sessions, by_trace)
    batch = next((b for b in manifest["batches"] if b["id"] == "batch3"), None)
    if batch is None:
        sys.exit("draw batch3 first")
    if batch.get("topup"):
        sys.exit("batch3 was already topped up; refusing to redraw")

    excluded = _excluded_sessions(manifest, by_trace)
    pool = sorted(t for t, hit in by_trace.items() if hit["session"]["session_id"] not in excluded)
    rng = random.Random(seed)
    searches = {s[0]: s for s in SEARCHES}
    picks, log = [], []
    for name in TOPUP_SEARCHES:
        _, mode, description, _, match = searches[name]
        available = [t for t in pool if match(by_trace[t]["trace"])
                     and by_trace[t]["session"]["session_id"] not in {by_trace[p["trace_id"]]["session"]["session_id"] for p in picks}]
        drawn = uniform_picks(available, 1, rng, by_trace)
        for p in drawn:
            p.update(method="targeted_search", search=name, target_mode=mode, topup=True,
                     reason=f"top-up: random draw among {len(available)} unused traces where {description}")
        picks += drawn
        log.append({"search": name, "available": len(available), "drawn": len(drawn)})

    picks = _with_siblings(picks, by_trace)
    batch["picks"] += picks
    batch["trace_ids"] += [p["trace_id"] for p in picks]
    batch["topup"] = {
        "reason": "no_id_multi_orders drew 2 of 5 (its other matches were taken by list_6_plus), leaving 22 drawn "
                  "conversations; topped up to the handout's 25",
        "searches": log,
        "trace_ids": [p["trace_id"] for p in picks],
        "seed": seed,
        "selected_at": _utcnow(),
        "command": f"uv run python -m analysis.review_app.batches batch3-topup --seed {seed}",
    }
    _state.write_json(_state.state_path("sample_manifest.json"), manifest)
    return batch


def batch4(seed: int, k: int) -> dict[str, Any]:
    """Stability check: k conversations drawn uniformly over conversations.

    Batches 1-3 drew over turns, which favours long conversations; this batch
    samples the conversation itself. Conversations already reviewed, annotated
    or surfaced by a Part D search are excluded, since search hits are not a
    random draw. Every turn of a drawn conversation is reviewed; the first
    (lowest-numbered) turn is the drawn pick and the rest are siblings.
    """
    sessions, by_trace = _load()
    manifest = _manifest(sessions, by_trace)
    if any(b["id"] == "batch4" for b in manifest["batches"]):
        sys.exit("batch4 already exists in the manifest; refusing to redraw it")

    excluded = _excluded_sessions(manifest, by_trace)
    excluded |= {s["session_id"] for s in _state.read_json(_state.state_path("suggestions.json"), []) if s.get("session_id")}
    pool = sorted(s["session_id"] for s in sessions if s["session_id"] not in excluded)
    if len(pool) < k:
        sys.exit(f"only {len(pool)} unreviewed conversations left; cannot draw {k}")
    drawn = random.Random(seed).sample(pool, k)
    by_session = {s["session_id"]: s for s in sessions}
    picks = _with_siblings([_pick(by_trace, by_session[sid]["turns"][0]["trace_id"], "uniform_conversation",
                                  "uniform random draw over unreviewed conversations") for sid in drawn], by_trace)
    batch = {
        "id": "batch4",
        "name": "Batch 4 · stability check (uniform over conversations)",
        "method": f"{k} conversations drawn uniformly from unreviewed conversations, excluding any surfaced by a "
                  "Part D search; all turns reviewed",
        "target": k,
        "seed": seed,
        "selected_at": _utcnow(),
        "command": f"uv run python -m analysis.review_app.batches batch4 --seed {seed}",
        "pool_conversations": len(pool),
        "pool_traces": sum(len(by_session[sid]["turns"]) for sid in pool),
        "trace_ids": [p["trace_id"] for p in picks],
        "picks": picks,
        "counts": {"drawn_conversations": k, "sibling_traces": len(picks) - k, "traces": len(picks), "conversations": k},
        "taxonomy_frozen_at": _state.read_json(_state.state_path("patterns.json"), {}).get("frozen_at"),
    }
    manifest["batches"].append(batch)
    _state.write_json(_state.state_path("sample_manifest.json"), manifest)
    return batch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    b1 = sub.add_parser("batch1", help="15 uniform traces and 15 cluster representatives")
    b1.add_argument("--seed", type=int, required=True)
    b1.add_argument("--uniform", type=int, default=15)
    b1.add_argument("--cluster", type=int, default=15)
    b2 = sub.add_parser("batch2", help="10 traces per caller role")
    b2.add_argument("--seed", type=int, required=True)
    b2.add_argument("--per-role", type=int, default=10)
    b3 = sub.add_parser("batch3", help="25 traces from targeted structural searches")
    b3.add_argument("--seed", type=int, required=True)
    b3t = sub.add_parser("batch3-topup", help="add 3 drawn conversations to batch3")
    b3t.add_argument("--seed", type=int, required=True)
    b4 = sub.add_parser("batch4", help="15 conversations drawn uniformly over conversations (stability check)")
    b4.add_argument("--seed", type=int, required=True)
    b4.add_argument("--conversations", type=int, default=15)
    args = parser.parse_args()

    if args.cmd == "batch3-topup":
        batch = batch3_topup(args.seed)
        added = set(batch["topup"]["trace_ids"])
        for p in batch["picks"]:
            if p["trace_id"] in added:
                print(f"  {p['method']:<15} {p['scenario_id']} t{p['turn']} {p['role']:<8}  {p['reason']}")
        return

    if args.cmd in ("batch1", "batch2", "batch3", "batch4"):
        if args.cmd == "batch4":
            batch = batch4(args.seed, args.conversations)
        elif args.cmd == "batch1":
            batch = batch1(args.seed, args.uniform, args.cluster)
        elif args.cmd == "batch2":
            batch = batch2(args.seed, args.per_role)
        else:
            batch = batch3(args.seed)
            for s in batch["searches"]:
                print(f"search {s['name']:<20} matched {s['matched_traces']:>3}, available {s['available_after_earlier_searches']:>3}, drawn {s['drawn']}")
        methods = Counter(p["method"] for p in batch["picks"])
        roles = Counter(p["role"] for p in batch["picks"])
        print(f"{batch['id']}: {len(batch['trace_ids'])} traces from a pool of {batch['pool_traces']} ({dict(methods)})")
        print(f"roles: {dict(roles)}; harness-affected: {sum(p['harness_affected'] for p in batch['picks'])}")
        for p in batch["picks"]:
            flag = " [exc]" if p["harness_affected"] else ""
            print(f"  {p['method']:<15} {p['scenario_id']} t{p['turn']} {p['role']:<8}{flag}  {p['reason']}")


if __name__ == "__main__":
    main()
