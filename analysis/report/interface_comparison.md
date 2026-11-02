# Interface comparison

The review interface is `analysis/review_app/`: `server.py` plus `static/index.html`, started with `uv run --env-file .env python -m analysis.review_app.server`. It is compared here with the reference interface (`analysis/server.py`, `analysis/ui/index.html`) and with the standard Langfuse annotation view used in Part A.

## Retained from the reference: quote-anchored notes in a margin, with AI suggestions kept apart

The reference anchors each note to the exact quoted text it concerns and lists the notes in a margin beside the conversation, with agent suggestions in their own tint. This design was kept because it makes every open code carry its evidence: a note sits next to the sentence it describes. It was extended in three ways:
- **Explicit decisions.** A suggestion card has accept and reject buttons, a rejection requires a reason naming the boundary that excludes the trace, and every earlier decision stays in `decision_history`.
- **Close-negative suggestions.** These cards are labelled as such, and accepting one records Pass rather than Fail.
- **Editable notes.** A human note is revised or retracted, never deleted, and keeps its earlier versions.

## Changed after inspecting the traces: one conversation per screen, with each tool call beside its result

Cartwheel writes one Langfuse trace per user turn. The standard view therefore shows a follow-up turn on its own, and the tool result sits apart from the reply that describes it. Several failures could only be seen across turns:
- In support-0054 t2 the agent declines a refund that failed in t1, without calling the tool again (`tool_failure_recovery_mishandled`). Reviewed in isolation, t2 looks like a reasonable refusal.
- In support-0245 t3 the agent acts on an order it picked by itself in t1 (`acts_on_assumed_target`).

The interface therefore groups traces by `cartwheel.session_id` and shows every turn in order. Each tool call is merged with its result into one card placed before the reply. Other changes driven by the traces:
- **A lock flag:** a conversation containing a harness exception is marked "exc" in the queue, and the failing tool card reads "EXCEPTION (harness)", so that under Option A the reviewer judges the handling rather than the lock.
- **A facts panel:** it shows the seeded database rows for the caller and for the conversation's orders, products and stores, so that data-quality cases (e.g. order 8003's store mismatch, product 4's negative price) can be checked without leaving the page.

## Limitation remaining: AI-written cards can anchor the reviewer

Suggestion and proposal cards are written by the coding agent, and their wording can steer the human decision. An early Part D search printed "Expected to fall outside the mode" on boundary probes, which told the reviewer what to conclude before they read the trace. That wording was removed: later searches state only evidence, such as the arguments, the result count and the user's words. Two weaker forms of the problem remain:
- A card's existence still signals that the agent thinks the trace is relevant.
- In Part E, a proposed value is shown on the Pass/Fail chip (dashed) before the reviewer decides, and bulk confirmation accepts proposals after a spot-check of 8 per mode rather than a full read.

The labels record each label's source (reviewer or `proposal_<source>`), so the effect can be audited, but the interface does not prevent anchoring. A blind mode, which hides the suggestion until the reviewer has written their own judgment, would address it.

Two smaller limitations:
- A quote can only be anchored inside message text. Notes about tool arguments are anchored to the user's request instead.
- The Part D retrieval signals are regular expressions over replies and tool results, so they miss paraphrases. For example, "try escalating again" was not matched as a retry offer.

## Trace source and Langfuse availability

The server loads traces from Langfuse by default. If Langfuse is unavailable it falls back to the Module 1 export (`traces/support_traces.json`), keeping the stable trace ids, and prints the reason. The page header shows which source is in use.

> **[Reviewer to confirm: was the fallback ever used during review? If so, state when and the reason the server printed.]**

Two Langfuse problems affected score writes, not trace loading:
1. **Silent write failures.** The SDK's batched `create_score` only logs a failed write, so failed writes were recorded locally as synced. Writes now use the direct API call (`api.score.create`), which raises on failure, with a 30-second timeout and two retries. A failed write is saved locally as unsynced, with the error, and the Progress tab can retry it.
2. **A storage outage of about 45 hours.** After a Docker restart, an older, unused Langfuse stack took the host ports of this stack's MinIO, and later its ClickHouse, so every score write failed inside Langfuse ("Failed to upload events to blob storage"). After the conflicting containers were stopped, every unsynced label was pushed and the full set was checked against Langfuse. All 1,248 labels match.

Local state writes also needed hardening on Windows: two note saves were lost while the page was polling the same file. Reads now share a lock with writes, writes retry on sharing violations, and the page keeps an unsaved note and retries it until the server confirms.
