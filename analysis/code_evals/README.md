# Code-based evaluators

A code evaluator reads a trace and returns Pass or Fail using fixed rules
instead of a model. Same input, same answer, no cost. It is still validated
against the human labels, like an LLM judge.

## search_arguments_misexpress_request

Definition: `analysis/state/patterns.json` (HW4 taxonomy, frozen 2026-11-01).
Labels: `analysis/state/labels/search_arguments_misexpress_request.jsonl` (1 = Fail).

A trace Fails when any rule fires on any `search_products` call.
A trace with no `search_products` call is Pass.

| Rule | Fires when | Definition clause |
|---|---|---|
| A. `store_name_in_query` | `store` is empty and `query` contains a store name | (a) |
| B. `store_id_as_name` | `store` is a number instead of a store name | (b) |
| C. `category_as_query` | `store` is empty and `query` is only a catalog category | (c) |
| D. `unstated_price_cap` | `max_price_usd` is not a number the user wrote in this conversation | (d) |

### Reviewer decisions (2026-11-05, Lesson 1)

1. Rule C fires only when `store` is empty: a category word while browsing a
   named store is allowed (support-0120 t0, Pass).
2. support-0117 t0 makes the same mistake as 0117 t1 (store name in query,
   store null), so the reviewer relabelled t0 Fail. The frozen `patterns.json`
   still lists it as a close negative.
3. support-0030 t1 has no `search_products` call, so the reviewer relabelled it
   Pass for this mode. The reviewer's note on it describes a separate problem:
   `search_help_center` returned another store's policy.
4. Rule D: the cap must equal a number the user stated, in any turn so far.

Text is normalised before comparing (lowercase; `_ / - &` become spaces; the
word "and" is dropped), so "home kitchen" matches the category
`home_and_kitchen` (support-0115).

### Known gap

Clause (c) also covers topic words ("gardening", "gift"). Code checks only the
11 catalog categories; topic words need an LLM judge.
