🤖 How to work with this group

ADD WORK — just say it, no ticket needed first
• feature: <what you want>
    feature: add CSV export to the reports page
• bug: <what's broken>
    bug: checkout 500s when the cart is empty
• ticket: <anything else> — chore, refactor, docs
• flag: <title> — park it for a human; the agent will NOT build it

Lines after the first become the description — put acceptance criteria,
links, repro steps there. Attach a screenshot to any message; it's read as
evidence.

The prefixes are hints, not a grammar: a plain question is answered, a plain
request aimed at the agent is filed (it says so — reply "drop it" if not), and
case/spacing don't matter. The prefix just makes the routing certain.

A report IS the ask — there's no approve step. The agent files the ticket,
reads the code, then either builds it or comes back with ONE scoped question
or a short plan. It never asks permission just to go look.

Multi-repo group? Name the repo in brackets after the prefix:
    feature: [pt-web] sticky nav on mobile
Untagged, it asks which repo (or parks it in Intake for triage).

TICKETS ALREADY ON THE BOARD
• take ABC-123 (or "ABC-123 go") — hand an existing ticket to the agent
• flag ABC-123 — put it on the weekly human-review checklist
• A ticket carrying an excluded label is refused — it'll say so

WHEN THE AGENT ASKS YOU SOMETHING
• ❓ = it needs a decision before it can build
• 🧭 = it drafted a plan and wants one call made
• 🙋 = idle-board proposal → reply go/yes or skip/no
• Answer by replying to that message, or start your text with ABC-123.
  Either way it routes to the ticket, unblocks it, and is mirrored onto the
  ticket as a comment. Your answer IS the go — no separate "take" needed.
• Replying to a bot headline of the form `✅ ABC-12 — …` / `🔨 Starting ABC-12`
  / `❓ ABC-12 — …` / `👍 ABC-12 queued` (one ticket in the subject slot)
  routes to that ticket. A reply to the ▶️ status line, a 💬 answer or the
  digest is read in context — name the ticket if the message lists several.
• ▶️ "pass starting" is ONE line that updates in place, not a new message
  per pass.
• ⚠️ YOUR emoji reactions never reach the bot — a 👍 tap does nothing. Send text.
• 👀 on your message = the bot received it (seconds); 👍 = it acted on it.
  A reply follows the 👍. 👀 that never turns into 👍 = seen, nothing to do.

ASK ABOUT THE CODE (creates no ticket)
• question: <anything> — a read-only agent answers here with file:line refs
    question: [pt-api] where is the retry backoff configured?
• Works as a reply to any message too

WHAT COMES BACK
• One PR per ticket into the integration branch, title ending [agent]
• It then babysits that PR — red CI, review comments, merge conflicts — until
  it merges, and closes the ticket on merge
• Your review is the gate: the agent never merges its own PR
• Review comments ARE instructions — it pushes fixes and replies to each one

RELEASES
• release — cuts the base→prod release PR for this repo
• release <repo> — same, for one repo in a multi-repo group
• Merging that PR on GitHub is what deploys. The agent never merges it.

HOUSEKEEPING
• questions — list every ❓ still waiting on an answer
• prune questions — clear questions whose tickets were closed elsewhere
• A daily digest posts automatically: merged, awaiting your review, pending
  release, blocked, queued

WHAT IT WON'T DO (no matter who asks, here or in a ticket)
• Push to the base/prod branch, force-push, or merge any PR
• Read secrets/.env, edit CI workflows, or edit its own config
• Change dependencies or tooling, or ship an oversized diff — it stops and asks
