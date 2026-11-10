# Release readiness: policy-answer LLM judge

As of 5 November 2026 · Judge `policy_answer_missing_specifics-v0` · Live version: https://claude.ai/artifact/H4WTAhgzknQjF1M6xABD9j

## Summary

**Recommendation: release the judge for a supervised pilot as a triage aid, not as an automated release gate.** On 28 held-out conversations it caught 11 of 13 faulty replies and passed 12 of 15 good ones.

The judge is an automated reviewer for the Cartwheel support assistant. It reads a conversation and decides whether the assistant's answer to a policy question gave the customer the specific number they needed, such as a return window or days remaining.

| Measure (held-out test, 28 conversations) | Result | 95% range |
| --- | --- | --- |
| Faulty replies caught (Fail agreement) | 85% (11 of 13) | 58% to 96% |
| Good replies passed (Pass agreement) | 80% (12 of 15) | 55% to 93% |
| Overall agreement with the human reviewer | 82% (23 of 28) | not computed |
| Cost per run | about $0.01 for 28 conversations | not applicable |

The headline rates are useful, but the ranges are wide because the test set is small. The judge also has one repeatable blind spot, described under Risks. Both are reasons to keep a person in the loop for now.

## What the judge checks

The judge checks one failure: **a policy answer that leaves out the deciding value.** It is one of eight failure types found in the earlier error analysis.

| Verdict | Example |
| --- | --- |
| Fail | "Your order is not eligible for a refund, as indicated in our policy." No window, no delivery date, no days elapsed. |
| Fail | A store with a 14-day window is quoted the 30-day platform default. |
| Fail | A refund is blocked by a system error and the reply never states the refund amount. |
| Pass | "Delivered 23 days ago; you have 7 days left in the 30-day window." |
| Pass | The customer asked no policy question, so no value was needed. |

Other failures are out of scope and need their own checks. These include a missing policy ID, poor handling of system errors, and a missed escalation. A missing policy ID is better caught by a simple code rule than by an AI judge.

## How it was validated

The judge was checked against a human reviewer's labels, and the final score comes from conversations it never saw during development.

1. **Labels.** One reviewer labelled 67 simulated support conversations as Pass or Fail under a written definition: 36 Pass and 31 Fail.
2. **Split.** The labels were split once, at random, into three sets that never overlap.
   - Training (13): the source of the worked examples inside the judge's instructions.
   - Development (26): used to compare the judge with the reviewer and improve the instructions.
   - Test (28): held back until the final version was locked.
3. **Model.** gpt-4o-mini, run in batches through the DocETL pipeline. The judge writes its reasoning before each verdict, so every decision can be audited.
4. **Lock, then test.** The chosen version was frozen on 4 November 2026 before any test result was seen. Test results cannot be used to tune it.

The conversations come from simulated scenarios, not live customer traffic.

## Test results

The locked judge agreed with the reviewer on 23 of 28 held-out conversations. It missed 2 faulty replies and wrongly flagged 3 good ones.

| | Reviewer: Pass (15) | Reviewer: Fail (13) |
| --- | --- | --- |
| **Judge: Pass** | 12 correct | 2 faulty replies missed |
| **Judge: Fail** | 3 good replies flagged | 11 correct |

The 95% ranges show what the true rates could be, given only 13 to 15 examples per class. Plan around the low end: the judge may catch as few as 58% of faulty replies.

The test rates are lower than development, where the judge scored 92% on both measures. The ranges overlap almost entirely, so the drop is within what a sample this small can produce.

**What the 5 test errors have in common:**

- **2 faulty replies missed.** One said an order was eligible and gave only its date and total, with no return window; the same blind spot appeared in development. The other was a refund blocked by a system error, where the final reply never stated the amount.
- **3 good replies flagged.** In each case the judge was stricter than the reviewer about which values had to appear.

## Development history

The first version was kept: two rewrites aimed at its errors scored lower, so revision stopped at the limit of two.

Agreement with the reviewer on 26 development conversations (95% range in brackets):

| Version | Good replies passed | Faulty replies caught |
| --- | --- | --- |
| v0 (chosen, after label corrections) | 92% (67% to 99%) | 92% (67% to 99%) |
| v1 (rewrite 1) | 62% (36% to 82%) | 77% (50% to 92%) |
| v2 (rewrite 2) | 85% (58% to 96%) | 77% (50% to 92%) |

- **Most first-round disagreements were labelling errors, not judge errors.** Of v0's 7 disagreements, the reviewer corrected 5 labels, some carried over from earlier work. The judge was right in those cases.
- **The two rewrites did not fix their targets.** Both remaining errors persisted in all three versions, while the longer instructions caused new mistakes elsewhere.
- **Run-to-run variation is large.** Six of v1's eight changed verdicts were on cases the rewrite did not touch. With this setup the same instructions can give different answers on borderline cases.

The reviewer chose v0 on its development results and aligned the written definition with it before testing.

## Risks and known limitations

The largest risk is a known blind spot that lets some faulty refund answers through; the rest come from the small sample and the model's variability.

| Risk | Evidence | Effect if released | Mitigation |
| --- | --- | --- | --- |
| Blind spot: "eligible" plus a date or total is treated as complete | Wrong in all 3 versions in development and again in test | Some faulty refund answers go unreported | A simple code rule that flags "eligible" replies with no number of days |
| Small test sample | 13 to 15 conversations per class; ranges about 35 points wide | True performance may be well below the headline | Label 100 or more further conversations and retest |
| Run-to-run variation | Rewrites changed verdicts on untouched cases | The same conversation can get different verdicts | Fix the model's randomness setting before production use |
| Malformed answers | 3 of about 110 calls returned no usable verdict; those batches had to be rerun | Missing or delayed results | Automatic retry; the pipeline already rejects malformed answers instead of guessing |
| Simulated data | All conversations come from designed scenarios | Live traffic may differ in mix and difficulty | Validate on a random sample of live conversations |
| Single reviewer | One person wrote the definition and all labels | Labels reflect one person's judgment | Have a second reviewer label a sample and compare |

## Recommendation and next steps

**Proposed: a supervised pilot, with the judge flagging conversations for human review, not blocking releases.** It is accurate enough to point reviewers at likely problems, but not proven enough to act on alone.

| Use | Ready? | Why |
| --- | --- | --- |
| Triage: flag conversations for a human to review | Yes, as a pilot | Catches about 85% of faulty replies in test, for about $0.01 per 28 conversations |
| Weekly quality trend reporting | Not yet | Needs a correction for the judge's own error rate, and a random live sample |
| Automatic release gate | No | The low end of the range is 58% of faulty replies caught, with a known blind spot |

Conditions before wider use:

- [ ] Add the code rule for "eligible" replies with no number of days, covering the blind spot
- [ ] Fix the model's randomness setting and confirm verdicts repeat across runs
- [ ] Label 100 or more further conversations, including live ones, and retest the locked judge
- [ ] Have a second reviewer label a sample to check the definition is applied consistently
- [ ] Report the error-corrected failure rate on a random sample (scoped, currently paused)

## Appendix: evidence in the repository

Every number in this report can be recomputed from saved files, without calling the model again.

| Evidence | Location |
| --- | --- |
| Pass/Fail definition and boundary decisions | `analysis/state/hw5_definition.json` |
| Human labels with reasons | `analysis/state/hw5_labels/policy_answer_missing_specifics.jsonl` |
| Train, development and test split | `analysis/state/splits.json` |
| Exact judge inputs | `analysis/state/hw5_trace_inputs.json` |
| Judge instructions, all versions | `analysis/prompts/policy_answer_missing_specifics-v0.txt`, `-v1.txt`, `-v2.txt` |
| Verdicts and reasoning per conversation | `analysis/state/judges/policy_answer_missing_specifics-v0.json` (and `-v1`, `-v2`) |
| Development and test metrics | `analysis/report/dev-*.json`, `analysis/report/test-policy_answer_missing_specifics-v0.json` |
| Pipeline code | `analysis/run_judges.py` |
