# Local testing — run a working build in both harnesses

Use this to try a change to the framework in a real repo, on Claude Code and on
the Codex CLI, before the change merges.

The published marketplace serves the merged version. This guide points each
harness at your clone instead, so both run the same working code.

## Before you start

You need a clone of this repo and at least one repo that already has a
`dev-workflow.yml`. Every command below uses these two paths. Substitute yours.

    CLONE=~/repos/aws/dev-workflow
    TARGET=~/repos/aws/pubx-hil

Check out the branch you want to test in the clone. Both harnesses copy whatever
is checked out at install time.

## 1. Install into Claude Code

Claude Code reads one marketplace per name. The clone's marketplace is also
called `dev-workflow`, so remove the published one first.

    claude plugin uninstall dev-workflow@dev-workflow
    claude plugin marketplace remove dev-workflow
    claude plugin marketplace add $CLONE
    claude plugin install dev-workflow@dev-workflow

Confirm the source is your clone, not GitHub:

    claude plugin marketplace list

The entry must read `directory` and your clone's path.

## 2. Install into Codex

Codex allows a local marketplace beside the published one. Add it once:

    codex plugin marketplace add $CLONE
    codex plugin add dev-workflow@dev-workflow

The `@dev-workflow` suffix is required. The bare plugin name is rejected.

## 3. Use it

Open either harness in the target repo. Pass no flags:

    cd $TARGET
    claude
    # or
    codex

Both load the same eight skills and the same helpers.

**Invoking a skill differs.** In Claude Code you type `/standup`. In Codex you
**ask for the skill** — `run the dev-workflow:standup skill`. Codex's `/`
namespace holds its own built-ins, and there is no prompts directory a plugin can
register into, so `/standup` returns `Unrecognized command`.

**`DW_ROOT` differs.** Codex exports no plugin-root variable to a skill's shell, so
each skill preamble asks you to set `DW_ROOT` to the plugin path. Claude Code
sets `CLAUDE_PLUGIN_ROOT` itself, so nothing is needed there. `AGENTS.md`
describes the rule.

## 4. Refresh after you edit the clone

Neither harness reads the clone live. Both copy it at install time.

    claude plugin marketplace update dev-workflow

    codex plugin add dev-workflow@dev-workflow   # then start a NEW thread

`codex plugin marketplace upgrade` refreshes Git marketplaces only. It does
nothing for a local one.

## 5. Test the handoff across harnesses

This is the flow the handoff exists for. It takes about five minutes.

Write a note as an agent would, then record the git state:

    cd $TARGET
    H=$CLONE/dev-workflow/handoff.py
    python3 "$H" show                 # -> "no handoff for bq-<branch>"
    $EDITOR "$(python3 "$H" path)"    # write what you are doing, and why
    python3 "$H" checkpoint
    python3 "$H" show                 # -> the note, then "checkpoint is current (...)"

Open Claude Code in the target repo and run `/standup`. The brief must open with
a summary of the note.

Open Codex in the same repo and ask it to run the handoff step. It must report
the same intent, decision and next step, without you repeating them.

Delete the note when you finish:

    rm "$(python3 "$H" path)"

The note lives under `.local/`, which this repo and the configured target repos
already ignore. Confirm it in a new target repo before you trust it:

    git -C $TARGET check-ignore -q .local/handoff && echo ignored

## 6. Go back to the published version

Do this once your change merges. A local install tracks a working branch, which
goes stale as soon as other work lands.

    claude plugin uninstall dev-workflow@dev-workflow
    claude plugin marketplace remove dev-workflow
    claude plugin marketplace add singlas/dev-workflow
    claude plugin install dev-workflow@dev-workflow

    codex plugin marketplace remove dev-workflow
    codex plugin marketplace add singlas/dev-workflow
    codex plugin add dev-workflow@dev-workflow

## Testing one change without installing

To try a branch once, skip the install and point Claude Code at the clone:

    claude plugin disable dev-workflow@dev-workflow
    cd $TARGET
    claude --plugin-dir $CLONE
    # afterwards
    claude plugin enable dev-workflow@dev-workflow

Disable the installed copy first. Two copies of a skill make it ambiguous which
one answers.

Codex has no equivalent flag. Install the local marketplace instead.

## What to test, and what to leave alone

`/standup` is read-only. Run it freely.

These skills act outside your machine. Do not run them casually against a repo
that matters:

| Skill | What it does |
|---|---|
| `/cleanup` | Pushes your branch and opens a PR |
| `/release` | Targets the prod branch |
| `/worktree` | Deletes remote branches already merged into the trunk |
| `/ticket-loop` | Starts real work on real tickets |

To watch one of these without letting it act, ask the agent to run a single
named step and stop.

## Known traps

**`codex exec` ignores its prompt argument** when stdin is a pipe. It reads the
prompt from stdin instead, with no error. Always redirect:

    codex exec --sandbox read-only "<prompt>" < /dev/null

**A sandboxed agent cannot read `~/.claude/plugins`.** A probe that reports a
skill is missing a step may only be unable to read the skill's body. Check the
installed copy yourself before you believe it:

    grep -n handoff ~/.claude/plugins/cache/dev-workflow/dev-workflow/*/skills/standup/SKILL.md

**A skill's description is not its body.** `CLAUDE_PLUGIN_ROOT` is set only while
a skill runs, so it is empty in a plain shell. That is expected, and not a sign
the install is broken.
