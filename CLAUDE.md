# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

`auto_writer` is a semi-automatic long-form story writer for OpenAI-compatible APIs. The user writes a rough idea; the tool develops it into a plan and chapter outline (with the user reviewing each step), then writes chapters one at a time while maintaining a per-chapter story bible for continuity.

It depends on the sibling package `storykit` (`~/git/personal-repos/storykit`, not on PyPI) for the streaming LLM client (`storykit.llm.OpenAICompatClient`) and the prompt library (`storykit.prompts`).

## Commands

```bash
# Dev install (storykit must be installed from the local checkout)
pip install -e ../storykit -e ".[dev]"
pytest
ruff check .

# The user's `aw` is a pipx editable install:
pipx install --force --editable . --pip-args="--no-deps"
pipx inject auto-writer --editable ../storykit
```

User workflow (each step writes a file the user may edit before the next):

```bash
aw init "Title" [--profile NAME]     # creates idea.md
aw profile [NAME|PATH]   # no arg: list shared ~/.config/llm-profiles/*.json (storykit.profiles) + current; else copy into .story/profile.json
aw plan                  # 1st run: questions.md; after answering: plan.md
aw plan --no-questions   # skip the question round; model decides, lists # Assumptions
aw outline               # chapter_outline.md from plan.md
aw review-outline [--apply]          # outline_review.md; --apply revises the outline
aw prepare               # outline/chNN.md + story_arc.md
aw write [N] [--loop] [--max-writes N] [--fix-mode quick|full|none] [--force] [--retry N]
aw bible [N]             # regenerate stale/missing bibles (or bible N)
aw characters [--force]  # characters.md: SDXL tag profiles for narrate (after the story is written)
aw reset N|all
aw status
aw prompts [list | show TASK | use TASK VARIANT | fork TASK]
aw migrate               # convert legacy .story/system_prompt.md + user_prompt.md
vc story/ch*.md          # standalone validator (validate_chapter)
```

All commands find the project by walking up from CWD (or `-C PATH`).

## Architecture (`auto_writer/`)

- `cli.py` — argparse subcommands; thin wrappers around the modules below.
- `project.py` — `Project`: config (merged with `DEFAULT_CONFIG`), paths, and generated-file bookkeeping in `.story/state.json` (`record()` stores input/output hashes; `changed_inputs()` drives staleness warnings; `backup()` copies to `.story/backup/` before overwriting).
- `llm.py` — `Generator.run(task, variables, log_name, ...)`: resolves the task prompt, renders it, calls the API, logs `logs/<name>.<task>.{prompt.md,request.json,thinking.md}`. Continues responses that stop early (`finish_reason == "length"`, or a truncated-looking ending when the provider reports no reason), retries API errors and too-small outputs (refusals) with backoff.
- `planning.py` — `plan` (questions round by default), `outline`, `review_outline`, `prepare`.
- `writing.py` — `write_chapter`: ensures earlier bibles are current, builds variables (`chapter_variables`), generates, validates/fixes, updates the chapter's bible. The full-mode fixer's output is rejected if it is <80% of the original length. Truncated-ending issues are never sent to the fixer (adding a full stop would hide the truncation).
- `bible.py` — `bible/chNN.md` is the story state *after* chapter NN, generated from the previous bible + chapter text, so rewriting chapter 4 never sees facts from chapter 7. Staleness propagates down the chain (`stale_bibles`).
- `text.py` — pure helpers: outline parsing (`# Chapter N: Title`, digits or number words; preamble such as an overview table is ignored), `parse_sections` for `# Section` files, `ending_looks_truncated`, word ranges.
- `characters.py` — `generate_characters`: plan + latest bible + chapter text (from ch1, within `context.max_words`) → `characters.md` in narrate's format (`### Name` blocks with `description`/`base_tags`/`nude_tags`/`base_negative`). Output is cleaned (no preamble/fences) and checked against comfykit's parser rules (every `###` is a character and needs `base_tags`), cup sizes and names missing from the story.
- `validate.py` — regex validator (title format, quotes, foreign-script characters, punctuation, auto-wrapped blocks, truncated ending, ...).

## Prompts

One file per task with both messages (see `storykit/prompts.py` for the format):

```
---
description: ...
variables: [plan, bible]
---
=== system ===
...
=== user ===
...
```

Tasks: `plan_questions`, `plan`, `outline`, `outline_review`, `outline_revise`, `writer`, `bible`, `fixer_line`, `fixer_full`, `characters`.

Lookup: `.story/prompts/<task>.md` (project fork) → `~/.config/auto_writer/prompts/<task>/<variant>.md` → bundled `auto_writer/prompts/<task>/<variant>.md`. The variant comes from `config.json` `"prompts": {task: variant}` (default `default`). `.story/prompts/<task>.append.md` is appended to whichever prompt is used. Bundled prompts are generic across stories but tuned for long-form explicit erotic fiction (character defaults, per-scene prose styles, a sexual arc that escalates across chapters, consent as the single hard boundary, adults only); story specifics come from `plan.md` via `{plan}`. The bible's `# Sexual History` table is what lets later chapters escalate instead of repeating. Every logged request records the resolved prompt path and hash.

Writer variables: `{plan}` (whole plan), each plan section by normalized name (`{synopsis}`, `{character_profiles}`, `{fixed_facts}`, ...), `{story_arc}`, `{bible}`, `{recent_context}`, `{context_label}`, `{chapter_outline}`, `{chapter_specific_instructions}`, `{chapter_number}`, `{chapter_title}`, `{story_title}`, `{target_word_count}`, `{length_instruction}`; legacy aliases `{title}`, `{chapter_count}`, `{previous_summary}` (= bible), `{kinks}`.

Keep stable content (plan, arc) in the system message and previous chapters first in the user message so provider prefix caching works across chapters.

## Project layout

```
.story/config.json, profile.json, state.json, prompts/, backup/
idea.md → questions.md → plan.md → chapter_outline.md (+ outline_review.md)
outline/chNN.md, story_arc.md      # from `aw prepare`
story/chNN.md, bible/chNN.md, logs/
characters.md                      # from `aw characters`; narrate finds it by walking up from a story file
```

Legacy projects: `context.md` is used as the plan if `plan.md` is missing; old `summary/` files are ignored (bibles are generated on the next write).

Context modes (`config.json` `context.mode`): `budget` (default; most recent whole chapters within `max_words`, 60000), `last_n_chapters`, `last_n_words`.
