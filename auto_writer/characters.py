"""SDXL tag profiles for every character (characters.md), in the format narrate reads."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

from .bible import ensure_bibles
from .llm import Generator
from .planning import _guard_overwrite, _write_generated
from .project import Project, die, read_text, write_text
from .text import word_count
from .writing import load_plan

# Same rules as comfykit.characters.parse_characters_file, which narrate uses:
# every '###' line starts a character, and each character needs base_tags.
_HEADER_RE = re.compile(r"^###\s*(.+)")
_FIELD_RE = re.compile(
    r"^[-*]?\s*(base_tags|base_negative|nude_tags|description|loras)\s*:\s*(.*)", re.IGNORECASE
)
_CUP_RE = re.compile(r"\b[a-k]{1,3}[- ]?cups?\b", re.IGNORECASE)

FIELD_REFERENCE = """\
Field reference:
    description:   Natural-language description for LLM context only (not used in prompts).
    base_tags:     Always-visible physical traits, prepended to every image prompt. No clothing, no anatomy.
    nude_tags:     Optional. Anatomy, used when nude, adapted for partial exposure, omitted when clothed.
    base_negative: Optional. Traits to exclude from every prompt."""


def parse_character_blocks(text: str) -> Dict[str, Dict[str, str]]:
    """Parse '### Name' blocks into {name: {field: value}}."""
    characters: Dict[str, Dict[str, str]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        m = _HEADER_RE.match(line)
        if m:
            current = m.group(1).strip()
            characters[current] = {}
            continue
        if current is None:
            continue
        m = _FIELD_RE.match(line.replace("**", ""))
        if m:
            characters[current][m.group(1).lower()] = m.group(2).strip()
    return characters


def clean_output(text: str) -> str:
    """Keep only the character blocks: drop code fences and anything before the first '###'."""
    lines = [ln for ln in text.strip().splitlines() if not ln.strip().startswith("```")]
    for i, ln in enumerate(lines):
        if _HEADER_RE.match(ln.strip()):
            return "\n".join(lines[i:]).strip()
    return ""


def story_text_within_budget(project: Project, max_words: int) -> Tuple[str, str, List[Path]]:
    """Written chapters from the first one on, as many as fit in max_words.

    Returns (text, label, chapter_paths). Early chapters come first because
    they are where characters are introduced and described.
    """
    parts: List[str] = []
    paths: List[Path] = []
    used = 0
    written = project.written_nums()
    for n in written:
        path = project.story_file(n)
        text = read_text(path).strip()
        words = word_count(text)
        if parts and used + words > max_words:
            break
        parts.append(text)
        paths.append(path)
        used += words
    if not parts:
        return "(No chapters written yet; base the profiles on the plan.)", "Story text", []
    if len(paths) == len(written):
        label = "Story text (all chapters)"
    else:
        label = f"Story text (chapters {written[0]}–{written[len(paths) - 1]} of {written[-1]}; the bible covers the rest)"
    return "\n\n***\n\n".join(parts), label, paths


def check_characters(blocks: Dict[str, Dict[str, str]], known_text: str) -> List[str]:
    """Problems narrate would hit, or that break the tagging rules."""
    problems = []
    for name, fields in blocks.items():
        if not fields.get("base_tags"):
            problems.append(f"{name}: no base_tags (narrate refuses to load the file until this is fixed)")
        tags = " ".join(fields.get(k, "") for k in ("base_tags", "nude_tags"))
        if _CUP_RE.search(tags):
            problems.append(f"{name}: uses a cup size; use small|medium|large|huge|massive|gigantic breasts")
        if not re.search(r"\b" + re.escape(name) + r"\b", known_text, re.IGNORECASE):
            problems.append(f"{name}: name not found in the story, so narrate may never match it")
    return problems


def generate_characters(project: Project, gen: Generator, *, force: bool = False, retries: int = 0) -> None:
    """Write characters.md: SDXL tag profiles from the plan, the latest bible and the story text."""
    plan = load_plan(project)
    if not plan:
        die(f"{project.rel(project.plan_path)} is missing or empty. Run `aw plan` first.")
    out = project.path("characters")
    if out.exists() and not force:
        die(f"{project.rel(out)} already exists. Re-run with `aw characters --force` to regenerate it.")

    written = project.written_nums()
    inputs: List[Path] = [project.plan_path]
    if written:
        last = written[-1]
        ensure_bibles(project, gen, last, plan, retries=retries)
        bible = read_text(project.bible_file(last)).strip()
        inputs.append(project.bible_file(last))
    else:
        print("WARNING: no chapters written yet; the profiles will be based on the plan alone.")
        bible = "(No chapters written yet.)"

    max_words = int(project.config["context"].get("max_words", 60000))
    story_text, story_label, story_paths = story_text_within_budget(project, max_words)
    inputs.extend(story_paths)
    if written and len(story_paths) < len(written):
        print(f"Story is longer than context.max_words ({max_words:,}); sending {story_label.lower()}.")

    result = gen.run(
        "characters",
        {
            "title": project.title, "story_title": project.title, "plan": plan, "bible": bible,
            "story_text": story_text, "story_label": story_label,
        },
        "characters", retries=retries, min_chars=50,
    )
    body = clean_output(result.text)
    blocks = parse_character_blocks(body)
    if not blocks:
        failed = project.log_file("characters.failed_output.md")
        write_text(failed, result.text)
        die(f"The response has no '### Name' character blocks. Saved it to {project.rel(failed)}.")

    _guard_overwrite(project, out, force, "characters")
    header = (
        f'# Characters: "{project.title}"\n\n'
        "Generated by `aw characters` for narrate (SDXL tag profiles). Edit freely; every '###' "
        "heading is a character and must be the name the story uses.\n\n"
        f"{FIELD_REFERENCE}\n\n"
    )
    _write_generated(project, out, header + body, inputs)
    print(f"{len(blocks)} characters: {', '.join(blocks)}")
    problems = check_characters(blocks, "\n".join((plan, bible, story_text)))
    for p in problems:
        print(f"WARNING: {p}")
    print(f"Review {project.rel(out)}. narrate finds it for any story file in this folder or below it.")
