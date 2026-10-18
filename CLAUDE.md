# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

`auto_writer` is a semi-automatic story writer for OpenAI-compatible APIs. The user writes a rough idea; the tool develops it into a plan and chapter outline (with the user reviewing each step), then writes chapters one at a time while maintaining a per-chapter story bible for continuity. Projects created with `--short` (`"form": "short"`) are standalone short stories (~3–7k words): idea → plan (with a `# Scenes` list) → the whole story in one generation, no outline or bible.

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
aw init "Title" [--profile NAME] [--set SET] [--short]   # creates idea.md
aw profile [NAME|PATH]   # no arg: list shared ~/.config/llm-profiles/*.json (storykit.profiles) + current; else copy into .story/profile.json
aw plan                  # 1st run: questions.md; after answering: plan.md
aw plan --no-questions   # skip the question round; model decides, lists # Assumptions
aw outline               # chapter_outline.md from plan.md
aw review-outline [--apply]          # outline_review.md; --apply revises the outline
aw prepare               # outline/chNN.md + story_arc.md
aw write [N] [--loop] [--max-writes N] [--fix-mode quick|full|none] [--force] [--retry N]   # short: writes the story
aw oneshot [IDEA] [--force] [--fix-mode ...]   # short only: idea -> plan (no questions) -> story, redoing only what is out of date
aw bible [N]             # regenerate stale/missing bibles (or bible N)
aw characters [--force]  # characters.md: SDXL tag profiles for narrate (after the story is written)
aw reset N|all
aw status
aw prompts [list | show TASK | use SET [TASK] | fork TASK]
aw migrate               # convert legacy .story/system_prompt.md + user_prompt.md
vc [--short] story/ch*.md   # standalone validator (validate_chapter); --short: first line is the story title
```

All commands find the project by walking up from CWD (or `-C PATH`).

## Architecture (`auto_writer/`)

- `cli.py` — argparse subcommands; thin wrappers around the modules below.
- `project.py` — `Project`: config (merged with `DEFAULT_CONFIG`), paths, and generated-file bookkeeping in `.story/state.json` (`record()` stores input/output hashes; `changed_inputs()` drives staleness warnings; `backup()` copies to `.story/backup/` before overwriting).
- `llm.py` — `Generator.run(task, variables, log_name, ...)`: resolves the task prompt, renders it, calls the API, logs `logs/<name>.<task>.{prompt.md,request.json,thinking.md}`. Continues responses that stop early (`finish_reason == "length"`, or a truncated-looking ending when the provider reports no reason), retries API errors and too-small outputs (refusals) with backoff.
- `planning.py` — `plan` (questions round by default; `short_questions`/`short_plan` for short stories), `outline`, `review_outline`, `prepare`.
- `writing.py` — `write_chapter`: ensures earlier bibles are current, builds variables (`chapter_variables`), generates and validates/fixes (`generate_story_file`, shared with `write_short_story`), updates the chapter's bible. `write_short_story` writes `story/ch01.md` from `short_story_variables` (plan sections + `{length_instruction}`, default target 3500–4500) and records it against plan.md so `aw oneshot` knows when it is stale. The full-mode fixer's output is rejected if it is <80% of the original length. Truncated-ending issues are never sent to the fixer (adding a full stop would hide the truncation).
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

Tasks: `plan_questions`, `plan`, `outline`, `outline_review`, `outline_revise`, `writer`, `bible`, `fixer_line`, `fixer_full`, `characters`; short stories use `short_questions`, `short_plan`, `short_writer` (plus the fixers and `characters`).

Prompts are grouped into *prompt sets* (one directory per style: `prompts/<set>/<task>.md`). `default/` is complete; other sets hold only the tasks that differ and inherit the rest from `default` (`Generator.run` prints a note the first time a task is inherited). Lookup: `.story/prompts/<task>.md` (project fork) → `~/.config/auto_writer/prompts/<set>/<task>.md` → bundled `auto_writer/prompts/<set>/<task>.md` → the same for `default`. The set comes from `config.json` `"prompt_set"` (default `default`); `"prompts": {task: set}` overrides single tasks. `.story/prompts/<task>.append.md` is appended to whichever prompt is used. Form-specific tasks get their own task name rather than their own directory: a task is defined by the variables it receives. The bundled `default` set is generic across stories but tuned for long-form explicit erotic fiction (character defaults, per-scene prose styles, a sexual arc that escalates across chapters, consent as the single hard boundary, adults only); story specifics come from `plan.md` via `{plan}`. The bible's `# Sexual History` table is what lets later chapters escalate instead of repeating. Every logged request records the set, the set the file came from, the resolved path and hash.

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
