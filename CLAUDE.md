# CLAUDE.md

## Project Overview

`dev-workflow` is the framework — **CI, but for ticket work.** A generic runner +
Claude Code plugin read one per-repo `dev-workflow.yml` and work your board
(pick up tickets, ask questions in team chat, open one reviewable PR each)
inside guardrails a repo can tighten but never loosen. The framework leads;
the original standalone AI-prompt collections (context files, audits,
web-optimization, handover) are demoted to `extras/`.

## Repository Structure

```
dev-workflow/
├── dev-workflow/            # THE FRAMEWORK: per-repo config contract + validator + tracker seam
│   ├── README.md            # 3 zones, 2 boundary rules, baseline guardrails, distribution
│   ├── dev-workflow.example.yml  # annotated full config (generic values)
│   ├── validate.py          # schema + tighten-only validator (PyYAML)
│   ├── dw-config.py          # dotted-path config reader for shell callers
│   ├── queue-count.py       # Linear queue-depth pre-check (queue_count verb)
│   ├── test_validate.py     # unittest for validate.py
│   └── tracker-adapters.md  # canonical verbs → provider mapping (Linear impl)
├── skills/                  # Claude Code plugin skills — setup, worktree, standup, cleanup, release, ticket-loop
│   ├── setup/  worktree/  standup/  cleanup/  release/  # session skills, driven by dev-workflow.yml
│                            #   (worktree = the branch/slot skill over dev-process/scripts/worktree-reset.sh)
│   └── ticket-loop/         # autonomous agent + docker/ runner packaging
│       └── orchestrator/    # multi-project round-robin scheduler (roster.yml,
│                            #   adaptive pre-check + backoff, over the same runner)
├── dev-process/             # The narrative playbook behind the skills
│   ├── README.md            # branch model, worktrees, GitHub setup, daily loop, agent loop
│   └── scripts/             # worktree-reset.sh, ship-preflight.sh (ready to copy)
├── hooks/                   # Plugin SessionStart hook (silent outside configured repos)
├── scripts/                 # bump-version.sh — release version bump + drift check
├── .claude-plugin/          # Plugin manifest + marketplace (plugin name: dev-workflow)
├── extras/                  # LEGACY prompt collections, demoted — README.md there indexes them
│   ├── context-files/       # AI tool context-file generators
│   ├── codebase-audit-docs/ # 3-prompt multi-repo documentation + audit pipeline
│   ├── web-optimization/    # PageSpeed + SEO/GEO/AEO prompts
│   └── handover/            # project-handover.md checklist
├── site/                    # HTML guide page + static assets
└── README.md
```

## Content Guidelines

- Prompts should be copy-paste ready — no customization needed for basic use
- Use `[PLACEHOLDER]` syntax for values the user must fill in
- Never ask AI to guess — instruct it to write "Unknown" if something can't be determined
- Include security defaults (credential exclusion, protected areas)
- Structure prompts as Phase 1 (Audit) → Phase 2 (Implement) where applicable
- One commit per fix pattern for implementation prompts

## Conventions

- README.md is about the framework (pitch → tiers → quickstart → how it's put
  together); the prompt collections get one short "Extras" section pointing at
  `extras/README.md` — their detail tables live THERE, not in the root README;
  keep that split when editing
- All prompts are Markdown files organized into categorized subdirectories under
  `extras/`, indexed by `extras/README.md`
- `site/` contains the HTML guide page and is not a prompt directory
- `dev-workflow/` is a real framework, not prompts: the per-repo `dev-workflow.yml`
  contract, its PyYAML validator (`validate.py` — enforces boundary rule 1,
  config can only tighten, never loosen), the dotted-path reader (`dw-config.py`),
  and the tracker-adapter seam. Keep names in sync with the ticket-loop runner
  (`skills/ticket-loop/`) and the `.claude-plugin/plugin.json` (plugin name
  `dev-workflow`). New Python here must pass `python3 -m py_compile`; the
  validator has a `test_validate.py` (`python3 dev-workflow/test_validate.py`);
  the orchestrator brain and pre-check have `skills/ticket-loop/orchestrator/test_orch.py`
  and `dev-workflow/test_queue_count.py` (same `python3 <file>` idiom)
- The `skills/*/SKILL.md` root ladder and harness guard (Codex support) have
  their own tests: `bash skills/test_root_ladder.sh` and
  `bash skills/test_harness_guard.sh`. `AGENTS.md` lists the same two — keep
  both lists in sync
