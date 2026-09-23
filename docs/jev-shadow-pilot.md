# Jev Telegram intent pilot (shadow only)

The local Telegram listener still uses its Claude router to decide and execute
workflow actions. The Jev observer only compares a second classification; it
cannot start an issue, alter a PR, or override a human decision.

## Enablement and data boundary

The observer is **off by default**. It runs only when both
`DW_JEV_SHADOW=1` and `TYPESAFE_API_KEY` are present in the listener process.
Do not put the API key in `dev-workflow.yml` or commit it. Do not enable this in
the Docker container until the owner approves sending Telegram message text to
TypeSafe AI's external API and the key is supplied through an approved secret
mechanism. No external request is made merely by deploying this code.

When enabled, one request can send the current message (up to 2,000 characters),
reply-to text (500), pending conversation context (500), and up to three active
issue numbers and phases. It deliberately excludes issue titles, PR contents,
review findings, run logs, and GitHub credentials. The message itself may still
contain sensitive data, so treat this as an external data transfer.

Jev answers two typed questions: a closed choice among the existing listener
actions, and whether the user explicitly rejected a review finding. It uses
`jev-1.13.0` for a reproducible pilot. Calls run in background daemon threads,
with a four-second HTTP timeout and a maximum of two simultaneous calls. A
failure or disagreement never changes the Claude route or delays its action.

## Evidence and evaluation

The observer appends to `runtime.state_dir/jev-shadow.jsonl` (or
`TICKET_LOOP_STATE_DIR` when set). Each line contains timestamp, Telegram
message ID, live action, Jev action/confidence/rejection score, and agreement,
or an error type. It does **not** record message text, reply text, API keys, or
HTTP error bodies. The log is created with owner-only `0600` permissions.
Summarize counts and disagreement message IDs locally with
`python3 dev-workflow/jev_shadow.py <state-dir>/jev-shadow.jsonl`.

Before considering Jev for active routing, replay a labeled set of actual
misroutes and ordinary commands, then inspect the disagreements manually.
Include fresh-review versus feedback, rejected findings, factual questions,
ambiguous references with multiple PRs, and casual messages. Evaluate target
resolution and human authorization separately: Jev confidence is not proof
that an issue/PR target is correct or that an action is permitted. Keep the
existing deterministic safety checks and ask for clarification when needed.
