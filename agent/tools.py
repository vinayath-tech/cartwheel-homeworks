"""Homework 1: the remaining commerce-agent tools.

The three lecture tools (`search_help_center`, `get_order`, `issue_refund`)
are implemented in agent/agent.py and are worked examples of the pattern:
check permissions first, go through agent/db.py for data, and return a
structured dict, never a prose error. The homework tools follow the same
pattern. agent/agent.py already wraps each function below as an SDK tool, so
once a function works here it works in chat with no further wiring.

Result convention (see agent/auth.py):
  - Success: a dict with "ok": True plus the payload fields named in each
    docstring.
  - Failure: {"ok": False, "error": <code>, "reason": <human-readable str>}.

Run the contract tests with: uv run pytest tests/test_hw_holes.py -k hw1
They are marked xfail and flip to passing as you implement each function.
"""

from __future__ import annotations

import re
from typing import Any

from agent import db
from agent.auth import AuthContext, can_cancel_order, can_view_order, permission_denied
from agent.config import load_facts
from agent.helpcenter import load_policy_docs
from agent.killswitch import kill_switch
from seed.eligibility import effective_return_window_days, is_refund_eligible

MAX_SEARCH_LIMIT = 25
DEFAULT_ORDER_LIMIT = 20
FIND_ORDER_LIMIT = 5

# Words in a find_order query that describe the purchase, not the product.
_FIND_ORDER_FILLER = {
    "a", "an", "the", "my", "me", "i", "it", "that", "this", "these", "those",
    "some", "of", "for", "with", "from", "in", "on", "and", "or", "to", "was",
    "bought", "buy", "ordered", "order", "orders", "purchased", "got", "get",
    "last", "week", "month", "year", "ago", "recently", "yesterday", "today",
    "where", "is", "find", "one",
}


def get_policy(ctx: AuthContext, policy_id: str) -> dict[str, Any]:
    """Fetch one policy doc by its exact id. Risk tier: read.

    Every role may read every policy doc (the corpus is public help-center
    content), so this tool needs no permission check.

    Args:
        ctx: The caller's auth context. Unused here, but every tool takes it.
        policy_id: An exact policy id, e.g. "cw-returns" or
            "store-juniper-home-goods-policy". Matching is exact and
            case-sensitive; ids are the `policy_id` front-matter field of the
            files in data/policies/.

    Returns:
        On success: {"ok": True, "policy_id": str, "title": str,
        "audience": str, "body": str} where body is the markdown body of the
        doc without the front matter.
        If no doc has that id: {"ok": False, "error": "not_found",
        "reason": ...} naming the id that was requested.

    Implementation notes:
        agent.helpcenter.load_policy_docs() returns every parsed doc.
    """
    for doc in load_policy_docs():
        if doc.policy_id == policy_id:
            return {
                "ok": True,
                "policy_id": doc.policy_id,
                "title": doc.title,
                "audience": doc.audience,
                "body": doc.body,
            }
    return {"ok": False, "error": "not_found", "reason": f"no policy with id '{policy_id}'"}


def search_products(
    ctx: AuthContext,
    query: str,
    store: str | None = None,
    max_price_usd: float | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Search the product catalog. Risk tier: read.

    Every role may search products. Matching is deterministic keyword
    matching, not semantic search: a product matches when every whitespace
    token of `query` appears case-insensitively as a substring of the
    product's title or description.

    Args:
        ctx: The caller's auth context.
        query: Free-text query. Must be non-empty after stripping whitespace;
            otherwise return {"ok": False, "error": "invalid_argument",
            "reason": ...}.
        store: Optional store filter. Matched with
            agent.db.get_store_by_name (case-insensitive name or slug). If
            given and no store matches, return {"ok": False, "error":
            "not_found", "reason": ...} naming the store string.
        max_price_usd: Optional inclusive price ceiling. If given and not
            strictly positive, return an "invalid_argument" error.
        limit: Maximum products to return. Clamp to the range
            [1, MAX_SEARCH_LIMIT]; do not error on out-of-range values.

    Returns:
        {"ok": True, "products": [...], "count": <len(products)>} where each
        product is {"product_id": int, "store_id": int, "title": str,
        "price_usd": float}. Sort matches by price_usd ascending, then by
        product_id ascending, and truncate to `limit`. No matches is still a
        success: {"ok": True, "products": [], "count": 0}.

    Implementation notes:
        agent.db.list_products(conn, store_id) gives the candidate set.
        Use `with db.connection() as conn:` to close the database automatically.
    """
    tokens = query.lower().split()
    if not tokens:
        return {"ok": False, "error": "invalid_argument", "reason": "empty query"}
    if max_price_usd is not None and max_price_usd <= 0:
        return {
            "ok": False,
            "error": "invalid_argument",
            "reason": f"max_price_usd must be positive, got {max_price_usd}",
        }
    limit = max(1, min(limit, MAX_SEARCH_LIMIT))
    with db.connection() as conn:
        store_id = None
        if store is not None:
            found = db.get_store_by_name(conn, store)
            if found is None:
                return {"ok": False, "error": "not_found", "reason": f"no store named '{store}'"}
            store_id = found.id
        candidates = db.list_products(conn, store_id)
    matches = [
        p
        for p in candidates
        if all(t in p.title.lower() or t in p.description.lower() for t in tokens)
        and (max_price_usd is None or p.price_usd <= max_price_usd)
    ]
    matches.sort(key=lambda p: (p.price_usd, p.id))
    products = [
        {"product_id": p.id, "store_id": p.store_id, "title": p.title, "price_usd": p.price_usd}
        for p in matches[:limit]
    ]
    return {"ok": True, "products": products, "count": len(products)}


def list_my_orders(ctx: AuthContext) -> dict[str, Any]:
    """List recent orders in the caller's own scope. Risk tier: read.

    Role behavior, straight from the access matrix in SPEC.md:
        - shopper: the caller's own orders.
        - merchant: the caller's store's orders (ctx.store_id).
        - support: support staff have no orders of their own and look up
          specific orders with get_order instead, so return {"ok": False,
          "error": "invalid_argument", "reason": ...} saying exactly that.

    Returns:
        For shopper and merchant: {"ok": True, "orders": [...],
        "count": <len(orders)>} where each order is
        agent.db.Order.to_public_dict() and the list holds at most
        DEFAULT_ORDER_LIMIT orders, newest first (agent.db.list_orders_for_user
        and list_orders_for_store already sort and limit this way).

    Implementation notes:
        No permission check is needed beyond the role dispatch, because the
        scope is baked into which query you run. That is the point of the
        tool: the model cannot ask for someone else's orders through it.
    """
    if ctx.role == "support":
        return {
            "ok": False,
            "error": "invalid_argument",
            "reason": "support staff have no orders of their own; look up a specific order with get_order",
        }
    with db.connection() as conn:
        if ctx.role == "shopper":
            orders = db.list_orders_for_user(conn, ctx.user_id, DEFAULT_ORDER_LIMIT)
        else:
            orders = db.list_orders_for_store(conn, ctx.store_id, DEFAULT_ORDER_LIMIT)
    return {"ok": True, "orders": [o.to_public_dict() for o in orders], "count": len(orders)}


def cancel_order(ctx: AuthContext, order_id: int, reason: str) -> dict[str, Any]:
    """Cancel an order. Risk tier: write.

    This is the homework's write tool, and it must enforce two independent
    rules in this order:

    1. The access matrix (scope): use agent.auth.can_cancel_order. Shoppers
       may cancel only their own orders, merchants only their own store's
       orders, support any order. On failure return
       agent.auth.permission_denied(...) with a reason naming the role and
       the order id. Scope is checked before the status rule so that an
       out-of-scope caller learns nothing about the order's state.
    2. The pre-shipment rule (facts.yaml `cancel_cutoff`): only orders whose
       status is exactly "placed" can be cancelled, for every role. If the
       order is in scope but its status is not "placed", return
       {"ok": False, "error": "not_eligible", "reason": ...} that names the
       current status and states that orders can be cancelled only before
       shipment.

    Args:
        ctx: The caller's auth context.
        order_id: The order to cancel.
        reason: Free-text reason from the user; not validated.

    Returns:
        If no order has this id: {"ok": False, "error": "not_found",
        "reason": ...}.
        On success: {"ok": True, "order_id": order_id, "status": "cancelled"}
        after persisting the new status with agent.db.set_order_status.

    Implementation notes:
        Fetch with agent.db.get_order. Note the argument order of
        can_cancel_order(ctx, order_user_id, order_store_id).

    The Module 4 kill switch is checked first (before the scope and
    status rules and before your code), so that a paused write tool touches
    nothing. It is provided; the default ("off") returns None and falls
    through to your implementation.
    """
    paused = kill_switch("cancel_order")
    if paused is not None:
        return {"ok": False, "error": "paused", "reason": paused}
    with db.connection() as conn:
        order = db.get_order(conn, order_id)
        if order is None:
            return {"ok": False, "error": "not_found", "reason": f"no order #{order_id}"}
        if not can_cancel_order(ctx, order.user_id, order.store_id):
            return permission_denied(
                f"role '{ctx.role}' (user {ctx.user_id}) may not cancel order #{order_id}"
            )
        if order.status != "placed":
            return {
                "ok": False,
                "error": "not_eligible",
                "reason": (
                    f"order #{order_id} has status '{order.status}'; "
                    f"orders can be cancelled only before shipment"
                ),
            }
        db.set_order_status(conn, order_id, "cancelled")
    return {"ok": True, "order_id": order_id, "status": "cancelled"}


def find_order(ctx: AuthContext, query: str) -> dict[str, Any]:
    """Search the caller's orders by product name. Risk tier: read.

    Takes a natural-language query (e.g., "earmuffs I bought last week")
    and searches the authenticated user's orders for products whose name
    matches. Use fuzzy string matching (e.g., thefuzz.fuzz.partial_ratio
    or case-insensitive substring matching) to find orders whose product name is close to the
    query.

    Access rules: a shopper searches only the shopper's own orders, a
    merchant searches orders from the merchant's store, and support staff
    can search any orders. Use agent.db.list_order_search_candidates with
    user_id=ctx.user_id for shoppers, store_id=ctx.store_id for merchants,
    or all_orders=True only for support. Derive the scope from ctx, never
    from the query; reject unsupported roles or missing required identity.
    Use agent.db.list_products to map product IDs to product titles.

    The helper returns the complete authorised scope, newest first with
    order ID descending as the tie-breaker. Match product names first,
    preserve that order, then return at most five matches. Do not search
    only the 20 most recent orders. Convert matches with to_public_dict().

    Args:
        ctx: The caller's auth context.
        query: A natural-language description of the product.

    Returns:
        {"ok": True, "orders": [...]} with a list of matching orders
        (at most 5), each as the dict returned by agent.db. If no orders
        match, return {"ok": True, "orders": []}.
    """
    if ctx.role == "shopper":
        scope = {"user_id": ctx.user_id}
    elif ctx.role == "merchant" and ctx.store_id is not None:
        scope = {"store_id": ctx.store_id}
    elif ctx.role == "support":
        scope = {"all_orders": True}
    else:
        return permission_denied(f"role '{ctx.role}' has no order search scope")

    words = [_stem(w) for w in re.findall(r"[a-z0-9]+", query.lower())]
    words = [w for w in words if len(w) > 2 and w not in _FIND_ORDER_FILLER]
    if not words:
        return {"ok": True, "orders": []}

    with db.connection() as conn:
        titles = {p.id: p.title for p in db.list_products(conn)}
        candidates = db.list_order_search_candidates(conn, **scope)

    # Score each order by how many query words its product title contains, then
    # keep only the best-scoring orders so "modern teapot" prefers modern teapots
    # over every modern item. Candidates arrive newest first; filtering keeps that.
    scored = [(_title_score(words, titles.get(o.product_id, "")), o) for o in candidates]
    best = max((score for score, _ in scored), default=0)
    if best == 0:
        return {"ok": True, "orders": []}
    matches = [o for score, o in scored if score == best]
    return {"ok": True, "orders": [o.to_public_dict() for o in matches[:FIND_ORDER_LIMIT]]}


def _stem(word: str) -> str:
    """Drop a plural "s" so "earmuffs" matches "Earmuff"."""
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _title_score(words: list[str], title: str) -> int:
    """Count query words that start some word of the product title."""
    title_words = [_stem(w) for w in re.findall(r"[a-z0-9]+", title.lower())]
    return sum(any(t.startswith(w) for t in title_words) for w in words)


# ---------------------------------------------------------------------------
# Additional tool (HW1 Part A): not part of the starter contract.
# ---------------------------------------------------------------------------


def check_return_eligibility(ctx: AuthContext, order_id: int) -> dict[str, Any]:
    """Explain whether an order can be returned, and under which policy. Risk tier: read.

    get_order reports only a refund_eligible flag. This tool adds the reason:
    the return window that applies (a store override takes precedence over
    the platform default), the policy doc that sets it, and the days elapsed
    and remaining since delivery. It reuses seed.eligibility, the same rule
    that stamps refund_eligible, so it cannot disagree with issue_refund.

    Access follows get_order: shoppers their own orders, merchants their
    store's orders, support any order. Scope is checked before anything about
    the order is returned.

    Returns:
        {"ok": True, "order_id", "store_name", "status", "delivered_at",
        "return_window_days", "window_policy_id", "days_since_delivery",
        "days_remaining", "eligible", "reason"}. For an order that is not yet
        delivered, eligible is False and the reason says the window starts on
        delivery. For contradictory or missing delivery data, eligible is False
        and the reason names the problem instead of guessing.
        Failures: "not_found" for an unknown order, "permission_denied" for an
        order outside the caller's scope.
    """
    with db.connection() as conn:
        order = db.get_order(conn, order_id)
        if order is None:
            return {"ok": False, "error": "not_found", "reason": f"no order #{order_id}"}
        if not can_view_order(ctx, order.user_id, order.store_id):
            return permission_denied(
                f"role '{ctx.role}' (user {ctx.user_id}) may not view order #{order_id}"
            )
        store = db.get_store(conn, order.store_id)
        as_of = db.world_asof(conn)

    platform_window = load_facts()["return_window_days"]
    override = store.return_window_days_override if store else None
    window = effective_return_window_days(platform_window, override)
    store_policy_id = f"store-{store.slug}-policy" if store else None
    if override is not None and store_policy_id in {d.policy_id for d in load_policy_docs()}:
        window_policy_id = store_policy_id
    else:
        window_policy_id = "cw-returns"

    result: dict[str, Any] = {
        "ok": True,
        "order_id": order.id,
        "store_name": store.name if store else None,
        "status": order.status,
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
        "return_window_days": window,
        "window_policy_id": window_policy_id,
        "days_since_delivery": None,
        "days_remaining": None,
        "eligible": False,
    }

    if order.status in ("placed", "shipped"):
        result["reason"] = (
            f"order is '{order.status}' and not delivered yet; the {window}-day return "
            f"window ({window_policy_id}) starts on the delivery date"
        )
        return result
    if order.status != "delivered":
        result["reason"] = f"order is '{order.status}', so it cannot be returned"
        return result
    if order.delivered_at is None:
        result["reason"] = "order is marked delivered but has no delivery date; the window cannot be computed"
        return result
    if order.shipped_at and order.delivered_at < order.shipped_at:
        result["reason"] = (
            f"order data is inconsistent: delivered {order.delivered_at} before it shipped "
            f"{order.shipped_at}; the window cannot be computed reliably"
        )
        return result

    age = (as_of - order.delivered_at).days
    eligible = is_refund_eligible(
        status=order.status, delivered_at=order.delivered_at, as_of=as_of, return_window_days=window
    )
    result["days_since_delivery"] = age
    result["days_remaining"] = max(window - age, 0) if age >= 0 else None
    result["eligible"] = eligible
    if eligible:
        result["reason"] = (
            f"delivered {age} days ago, within the {window}-day window ({window_policy_id}); "
            f"{window - age} days remain"
        )
    elif age < 0:
        result["reason"] = f"delivery date {order.delivered_at} is after today ({as_of}); the window cannot be computed"
        result["days_remaining"] = None
    else:
        result["reason"] = (
            f"delivered {age} days ago, outside the {window}-day window ({window_policy_id})"
        )
    return result
