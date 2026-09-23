# Jev-assisted local issue workflow

## Decision and boundaries

Jev is an active semantic input for conversational Telegram messages, not the
executor or authorization authority. The coordinator reads Jev's typed result
**and** the original message and current run context. A deterministic gate then
checks the target and allowed transition before any worker starts. Explicit
commands (`start`, `status`, `pause`, `resume`, and pending model selection) stay
on their existing fast path. Automatic issue pickup remains off.

Jev's confidence is diagnostic, never permission to edit. A rejected review
finding remains rejected even when another finding is accepted. A request to
merge or deploy must not be turned into review-feedback work. No agent merges
or deploys based solely on a conversational classification.

## Stage 1 — live semantic routing (this change)

1. `DW_JEV_LIVE=1` plus `TYPESAFE_API_KEY` enables one bounded Jev request
   before the tool-free Claude router. The router sees the result as a hint.
   Missing key, timeout, or invalid Jev response falls back to the existing
   router. Shadow mode is not called a second time when live mode is enabled.
2. For a work-starting route, a Jev/coordinator action disagreement asks the
   operator to clarify. With multiple recorded issues, the request must name
   the selected issue or PR; Jev confidence does not resolve target ambiguity.
   Deferrals and merge/deploy wording do not start code work.
3. Record only message ID, labels, confidence, error type, and final action in
   the local mode-0600 decision log. Never record Telegram text, reply text,
   PR bodies, secrets, or HTTP error bodies.

**Release gate:** focused unit tests, adversarial local routing cases, and a
Docker listener smoke test. Enable live mode only for a manually triggered
pilot after the code is merged and the listener can be restarted without
interrupting an active run. Observe a small set of real conversations, then
disable `DW_JEV_LIVE` immediately if any wrong-target or forbidden action is
seen. The current router is the rollback path.

## Stage 2 — durable conversation and target resolution

- Persist a pending question with its message ID, issue/PR target, allowed
  replies, and expiry. A standalone answer is consumed only by the matching
  question; `status`, `pause`, and other commands remain interruptible.
- Represent a hold or deferral explicitly rather than as `start_issues` or a
  model choice. “Next week” does not queue or launch work without a later
  affirmative start.
- Resolve targets against fresh GitHub issue/PR state, not just the most recent
  progress file. A reply-to reference may select one target; multiple plausible
  targets produce one concise clarification question. Support requests with
  multiple actions as an ordered plan, not one lossy intent label.
- Extend Jev's typed questions for speech act, hold/deferral, and explicit
  review-finding rejection. Validate the output schema and preserve the raw
  human instruction for the coordinator and implementer.

**Acceptance:** replay the Telegram evaluation plus new two/three-PR and
pending-dialog cases. No answer is consumed by the wrong question; no ambiguous
target starts a worker; no rejected finding is implemented. Add end-to-end
tests for reply, restart, duplicate message, and Jev outage behavior.

## Stage 3 — evidence-gated issue lifecycle

1. Manual `start <issues>` selects up to three issues and models; each issue
   has a distinct run identity, worktree, progress record, and Telegram thread
   context. Do not launch a second pass against the same issue/PR.
2. Coordinator delegates implementation, collects actual test evidence, and
   opens or updates one draft PR per issue. A **fresh independent reviewer**
   inspects the full diff and runs available checks. Reviewer findings have
   explicit FIX/REJECT/LEAVE dispositions; implementer fixes only FIX items.
3. The coordinator verifies the updated diff, CI, review responses, and PR
   state before reporting “ready for human review.” A green assertion without
   a recorded command/result is not accepted as test evidence.
4. Human controls merge. Deployment uses the repository script from clean,
   synchronized main after the merge and any required migration. Production
   smoke checks and issue movement to testing/done are separate evidence gates.
   Jev may interpret a request for a stage transition; it never substitutes
   for the stage's proof or the required human decision.

**Acceptance:** one issue, then two concurrent issues, then three. Test
separate-target replies, reviewer independence, CI failure, migration gate,
failed deployment, and truthful Telegram completion notices. The listener must
report each run's state rather than only “active/idle.”

## Promotion metrics

- **Safety:** zero wrong-target starts, forbidden actions, rejected-finding
  edits, or duplicate PRs in the curated and new adversarial suite.
- **Quality:** measure intent accuracy separately from target resolution and
  end-to-end task completion; log Jev/coordinator disagreements for review.
- **Reliability:** measure Jev timeout/error rate, Telegram acknowledgment
  latency, duplicate-message handling, worker failures, and CI/review cycle
  time. A classification score alone is not a release criterion.
- **Privacy:** send only the bounded context documented in
  `docs/jev-shadow-pilot.md`; keep the TypeSafe key out of Git and logs.

This plan deliberately separates the small, reversible live-routing switch
from conversation persistence and worker lifecycle changes so failures can be
attributed and rolled back without abandoning the full workflow.
