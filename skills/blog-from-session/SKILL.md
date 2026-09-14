---
name: blog-from-session
description: >-
  Turn the working session you just did — or a topic the user hands you — into ONE
  practitioner field-note draft. First proposes 2-3 candidate angles (each with a
  one-line "who searches for this"); on the user's pick, writes a single markdown
  draft with title/date/description frontmatter into the repo's `blog.posts_dir`
  (default `docs/blog/`). Practitioner voice — what we tried, what broke, what we'd
  do differently — no marketing fluff. NEVER publishes, pushes, or commits on its
  own. Triggers: "blog this session", "write a field note from what we just did",
  "turn this into a post", "draft a post about X", "/blog-from-session". Repo/dev
  skill.
---

# blog-from-session

Turns raw material — the session you just did, or a topic the user states — into
**one** field-note draft, written in a practitioner voice and saved locally. Your
job is the two things the author can't easily do alone: **find the sharp angle**
and **write it honestly**. Don't just transcribe the session.

This skill produces a *draft file only*. It never publishes, commits, or pushes —
that stays a deliberate human step (see *Publishing* below).

## Per-repo configuration (`dev-workflow.yml`)

**Run this preamble ONCE at the start** to resolve the config reader and load the
keys this skill uses; the list below explains each. No `dev-workflow.yml` → the
default `docs/blog/` applies and there's no publish command.

**Set `DW_ROOT` before the preamble — on EVERY harness, Claude Code included.**
Neither Claude Code nor Codex exports a plugin-root variable into a skill's
shell, so without this the preamble cannot find the framework and falls through
to a relative path that does not exist in a target repo. Export `DW_ROOT` as the
absolute directory **two levels above this SKILL.md file** — write the path out
in full, quoted, from the location your harness showed you. Examples:
`export DW_ROOT="$HOME/.claude/plugins/cache/dev-workflow/dev-workflow/<version>"`
or `export DW_ROOT="$HOME/.codex/plugins/cache/dev-workflow/dev-workflow/<version>"`.
Skip it ONLY when you are working from a framework checkout, where the
relative fallback is the correct answer.

```bash
if uv run python3 -c pass >/dev/null 2>&1; then PY="uv run"; else PY="python3"; fi                  # uv is unusable where its cache is unwritable (a Codex sandbox)
if command -v dw-config >/dev/null 2>&1 && dw-config 2>&1 | grep -q -- '--batch'; then DW="dw-config"   # hardened install (PATH), only if --batch-capable
elif [ -f "${CLAUDE_PLUGIN_ROOT}/dev-workflow/dw-config.py" ]; then DW="$PY ${CLAUDE_PLUGIN_ROOT}/dev-workflow/dw-config.py"  # plugin install — test the FILE: the harness substitutes the path but not the guard
elif [ -f "${DW_ROOT}/dev-workflow/dw-config.py" ]; then DW="$PY ${DW_ROOT}/dev-workflow/dw-config.py"                        # plugin install (other harness)
else DW="$PY dev-workflow/dw-config.py"; fi                                                          # framework checkout
if [ -f dev-workflow.yml ]; then
  eval "$DW dev-workflow.yml --batch blog.posts_dir=docs/blog blog.publish" \
    || echo "ERROR: dev-workflow.yml exists but could not be read — STOP. Do NOT fall back to defaults; a wrong base branch can open a PR into prod."
else
  echo "no dev-workflow.yml — using the skill's missing-config fallbacks"
fi
```

- `blog.posts_dir` — where the draft is written. **Default `docs/blog/`** when
  unset.
- `blog.publish` — an optional publish command. This skill never runs it on its
  own; it only names it as the follow-up when the human explicitly asks to publish.

## First — is there anything worth writing? (honest gate)

Before proposing angles, judge **neutrally** whether the material holds a real
post. Be willing to conclude *no* — a post needs a concrete, true, non-obvious
learning a reader would care about, not routine work, a plain bug fix, or "we
shipped a thing."

- **Clearly yes** → continue to step 1.
- **Clearly no** (routine / internal-only / nothing non-obvious) → say so plainly
  and stop. Do **not** manufacture a post to look productive.
- **Borderline** → don't guess. Name the candidate learning in a line or two and
  **ask the user to confirm** before continuing.

(When the user hands you a topic directly, treat that as the "yes" — the gate is
mainly for session-derived material.)

## 1. Find the angle (do this first, out loud)

From the material, propose **2-3 candidate angles**, one line each, and for each a
one-line **"who searches for this"** — the real question or query a reader would
type that this post answers. Then let the user pick; don't skip to drafting.

A strong angle is:

- **True & defensible** — you actually did it / believe it. No invented results.
- **Anchored to a real question** — it answers something a practitioner would
  actually search. That question is the "who searches for this" line.
- **Specific** — a concrete take only someone who did the work would write, not a
  generic listicle.
- **One claim** — narrow enough to prove on a single page.

## 2. Write it (practitioner field-note voice)

- Voice: first-person, show-the-work, honest about what you're unsure of. The
  spine is **what we tried → what broke → what we'd do differently**. Concrete
  over abstract; name the real tools, commands, and numbers. **No marketing
  fluff** — no hype adjectives, no "revolutionary", no call-to-action selling.
- Shape it around the reader question from step 1: put it in the title and an H2,
  answer it plainly. That's also what makes it findable.
- ~500-900 words: a hook, 2-4 H2 sections, optionally one code block, list, or
  table, a short close. Markdown only.
- If prior drafts exist in `blog.posts_dir`, read one to match frontmatter shape
  and length before drafting.

## 3. Write the file

Write ONE file to `<blog.posts_dir>/<slug>.md` (default `docs/blog/<slug>.md`),
with `<slug>` a kebab-case version of the title, and this frontmatter:

```
---
title: <headline that answers the reader question>
slug: <kebab-case>            # match the filename
date: <today — run: date +%F>
description: <one line — the card excerpt / meta description>
tags: <comma, separated, 2-3>
---
```

Write the file and stop. Do **not** stage, commit, or push it.

## Publishing — a separate, explicit human step

This skill's output is a draft. Publishing is never automatic:

- If `blog.publish` is configured, **name it** as the follow-up command (e.g.
  "to publish, run `<blog.publish> <slug>`") and run it **only when the user
  explicitly asks** to publish.
- If `blog.publish` is unset, say the draft is ready for the repo's normal review
  path (a PR, or whatever the team uses) — and let the human take it from there.

## Done

- You proposed 2-3 angles, each with a "who searches for this", and the chosen one
  maps to a real reader question.
- A single `.md` exists at `<blog.posts_dir>/<slug>.md` with valid frontmatter,
  today's `date`, and `slug` == filename.
- Nothing was committed, pushed, or published.

## Never

- Publish, commit, or push — this skill writes a draft file and stops.
- Manufacture an angle for routine work — respect the honest gate.
- Invent results, names, or metrics that weren't in the source material.
- Write more than one draft per invocation, or anywhere other than
  `blog.posts_dir`.
