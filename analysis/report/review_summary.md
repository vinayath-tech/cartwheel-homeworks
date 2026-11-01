# Homework 4 review summary

## Reviewed sample

The review set holds **156 distinct traces from 108 conversations**, out of 334 traces (250 conversations) in the Module 1 store. Cartwheel writes one Langfuse trace per user turn, so both units are reported. Every drawn conversation was reviewed in full: its other turns join the same batch as `session_sibling` picks and are never counted in a second batch.

| Batch | Selection | Drawn conversations | Traces | Roles (shopper / merchant / support) | Lock errors |
| --- | --- | --- | --- | --- | --- |
| Part A | Standard Langfuse view, before the interface existed | 8 | 11 | 5 / 1 / 5 | – |
| 1 | 15 uniform + 15 k-means cluster representatives (turn and tool features), seed 20261027 | 30 | 50 | 32 / 11 / 7 | 7 |
| 2 | Stratified by caller role, 10 per role, seed 20261028 | 30 | 44 | 18 / 13 / 13 | 9 |
| 3 | Structural depth searches for candidate modes and close negatives, seed 20261029 (+ top-up 20261030) | 25 | 34 | 24 / 5 / 5 | 6 |
| 4 | Stability check, uniform over unreviewed conversations, seed 20261101 | 15 | 17 | 11 / 3 / 3 | 3 |
| **Total** | | **108** | **156** | **90 / 33 / 33** | **25** |

- **Batch 2's dimension (caller role)** was chosen before looking at outcomes. Roles differ in permissions (AUTH-1) and tasks, and Batch 1 had come out 32 / 11 / 7.
- **Batch 3's searches select on what a trace contains, never on whether it failed.** Examples: list_my_orders returning 6 or more orders, a request with no order id from a caller with several orders, a search with zero results, a data-quality entity, dispute wording.
- **Sampling caveat:**
  - Batches 1–3 drew over turns, which slightly favours long conversations.
  - Batch 4 drew over conversations.
  - Batch 4 also excluded every conversation that a Part D search surfaced, so the stability check is a clean random draw.
- **Harness lock errors (Option A):** `database is locked` exceptions stay in the sample. The exception itself is never the failure; the agent's handling of it is judged. AgentDebug places this under *System: Tool Execution / Environment Error*, which supports the choice.
- **Annotations:** 167 active, made up of 72 open codes and 95 "no failure observed". By source: 148 written in the interface, 8 Part A Langfuse comments, and 11 accepted AI suggestions. Every edit keeps its earlier version, so the path from open code to category can be inspected.

## Final taxonomy (8 modes, frozen before Batch 4)

| Mode | Requirement | Evaluator | Positives / close negatives |
| --- | --- | --- | --- |
| `policy_answer_missing_specifics` | RESP-1, RESP-5 | LLM judge | 6 / 3 |
| `invalid_record_presented_as_valid` | RESP-3 | code | 4 / 5 |
| `missed_escalation` | ESC-3, ESC-4 | LLM judge | 5 / 4 |
| `acts_on_assumed_target` | ESC-4, RESP-3 | LLM judge | 4 / 7 |
| `unrequested_eligibility_verdict` | RESP-5 | LLM judge | 3 / 5 |
| `search_arguments_misexpress_request` | TOOLUSE-1 (revision) | code + LLM judge | 16 / 8 |
| `order_list_misreported` | RESP-3, TOOLUSE-4 (revision) | code | 5 / 7 |
| `tool_failure_recovery_mishandled` | TOOLUSE-2, TOOLUSE-3 (revision) | code + LLM judge | 10 / 4 |

`analysis/state/patterns.json` holds each mode's definition, boundary, originating annotations and change history, along with the five retired parent modes. Merges followed one test: two observations share a mode when one product change fixes both.

## Structured labels and sample fractions

Every one of the **1,248 trace × mode pairs** has a Pass or Fail label. Each label is a Langfuse score (score id = sha256 of `hw4:{trace}:{mode}`) with a matching record in `analysis/state/labels/<mode>.jsonl`. All 1,248 were checked against Langfuse after labelling and match.

A pair was labelled in one of two ways:
- **By the reviewer** (265 pairs).
- **From a proposal the reviewer confirmed** (`analysis/review_app/prefill.py`). Proposals came only from evidence the reviewer had already given (positives, close negatives, "no failure observed") or from a structural rule where the mode cannot apply. For example, `search_arguments_misexpress_request` cannot fail without a `search_products` call. Each label records which way it was made.

| Mode | Fail | Pass | Sample fraction |
| --- | --- | --- | --- |
| `policy_answer_missing_specifics` | 23 | 133 | 14.7% |
| `search_arguments_misexpress_request` | 19 | 137 | 12.2% |
| `tool_failure_recovery_mishandled` | 13 | 143 | 8.3% |
| `missed_escalation` | 6 | 150 | 3.8% |
| `order_list_misreported` | 6 | 150 | 3.8% |
| `acts_on_assumed_target` | 5 | 151 | 3.2% |
| `invalid_record_presented_as_valid` | 4 | 152 | 2.6% |
| `unrequested_eligibility_verdict` | 3 | 153 | 1.9% |

- These are **sample fractions, not prevalence estimates.** Batch 2's stratification, Batch 3's searches and the Part D searches deliberately changed the sample's composition. Homework 5 estimates prevalence on the full trace store.
- 88 traces fail no mode, 57 fail one, and 11 fail two.

## Stability: the final 15 conversations

**One of the final 15 conversations (one of 17 traces) showed a previously unseen consequential failure.**
- **The trace:** in support-0133 t1, `escalate_to_human` raised an exception, and the reply told the shopper "the support team will be notified", although nothing notified anyone. This breaks RESP-2.
- **Why it wasn't added as a mode:** one occurrence can't meet the three-positive minimum, a ninth mode would exceed the 5–8 range, and the taxonomy was frozen before Batch 4 so the batch could test it.
- **Where it's recorded:** `patterns.json → known_gaps` as `claims_unperformed_action_after_tool_failure`.
- **Conclusion:** one new mode in the final batch meets "reasonably stable", so no further batch was reviewed.

The other Batch 4 failures fell into existing modes: `policy_answer_missing_specifics` (0049, 0098), `search_arguments_misexpress_request` (0122, 0192) and `tool_failure_recovery_mishandled` (0087).

## One taxonomy revision

**`false_absence_claim` was replaced by `search_arguments_misexpress_request`.**
- The original mode named a symptom: the reply says nothing matched.
- Comparing the taxonomy with AgentDebug (*Action: Parameter Error*) showed that six of the reviewer's notes, filed under that mode, actually described the cause: bad `search_products` arguments.
  - A category word in `query`, which matches titles (0127, 0115).
  - A price cap the user never stated (0116, 0128).
  - A store id passed as the store name (0125, 0202).
- One product change, a clearer search interface, fixes all six. The new mode kept them, and the old one is kept under `retired_modes` with `replaced_by`.
- **Late-mode recheck (Part D run R1):** the definition was later revised to cover a store name placed in `query` with `store` left null, and a cap inferred from a vague word. Both changes came out of that recheck: all 14 earlier traces with a `search_products` call were re-reviewed, 10 accepted and 4 rejected.

## One rejected search suggestion

**Search B suggested support-0103 t1** for `item_in_results_reported_missing`, now part of `order_list_misreported`. The reviewer rejected it: "Reply refuses on permission grounds; nothing is reported missing."

The boundary that excludes it: *a permission refusal does not deny an order.* The trace became a close negative.

- Across Part D, 10 suggestions were rejected, each with a reason.
- After the definition was revised, the repeat search (run C) found 4 of 4 known positives and matched none of the previously rejected traces.

## SPEC.md revisions

`SPEC.md` gained section 7, *Tool-use requirements*, because three modes rested on behaviour the specification did not yet require.

| Requirement | Motivating annotations |
| --- | --- |
| **TOOLUSE-1:** search arguments carry only what the user stated, each in the field meant for it | `amvs5shge7057` (support-0127 t1: category used as a keyword); `a-pd-R1-c98bae48a576` (support-0205 t1: store name in `query`, `store` null) |
| **TOOLUSE-2:** report a tool exception plainly, and call the tool again when the user repeats the request | `amvsbt5gp5117`, `amvsowcfr5967` (support-0054 t2, t3: refund declined again without a retry) |
| **TOOLUSE-3:** when escalation fails for a support caller, do not direct them to customer support | `amvv1lxck706` (support-0138 t1) |
| **TOOLUSE-4:** say how many orders a lookup returned and whether the reply shows a subset; never deny an order the result contains | `amvscj04r9573` (support-0246 t1: 16 returned, 5 shown); `a-pd-v1-61a4ea8d2a40` (support-0142 t1) |

During Part E the reviewer considered reading TOOLUSE-3's boundary as "a suggestion to try again excuses a redirect". They chose to keep the requirement as written. 0138 t1, 0139 t1 and 0190 t2 stayed Fail, and the boundary now says that suggesting the caller try again later does not excuse a redirect.

## Preparing for Homework 5

Every mode has at least 133 Pass labels, but none has 30 Fail labels. Synthetic scenarios are needed for every mode:

| Mode | Fail now | Fail labels still needed |
| --- | --- | --- |
| `policy_answer_missing_specifics` | 23 | 7 |
| `search_arguments_misexpress_request` | 19 | 11 |
| `tool_failure_recovery_mishandled` | 13 | 17 |
| `missed_escalation` | 6 | 24 |
| `order_list_misreported` | 6 | 24 |
| `acts_on_assumed_target` | 5 | 25 |
| `invalid_record_presented_as_valid` | 4 | 26 |
| `unrequested_eligibility_verdict` | 3 | 27 |

The Batch 4 gap (`claims_unperformed_action_after_tool_failure`) is a candidate for the same scenario generation: tool exceptions on `escalate_to_human`, `cancel_order` and `issue_refund`.
