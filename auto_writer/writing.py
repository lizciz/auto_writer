"""Chapter writing: context assembly, generation, validation and fixing."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import validate as vc
from .bible import ensure_bibles, previous_bible_text, update_bible
from .llm import Generator
from .project import Project, read_text, write_text
from .text import (
    ending_looks_truncated,
    extract_chapter_title,
    length_instruction,
    parse_sections,
    parse_word_range,
    split_outline_instructions,
    strip_comments,
    tail_words,
    word_count,
)

# Outputs smaller than this are assumed to be errors/refusals.
MIN_CHAPTER_SIZE = 500

# A full-mode fix shorter than this fraction of the original is rejected.
MIN_FULL_FIX_RATIO = 0.8

# Short-story length when plan.md has no # Target Word Count.
DEFAULT_SHORT_TARGET = "3500-4500"


# ============================== Context ==============================


def load_plan(project: Project) -> str:
    return strip_comments(read_text(project.plan_path))


def build_recent_context(project: Project, n: int) -> Tuple[str, str]:
    """Return (context_block, context_label) of previous chapter text for chapter n."""
    earlier = [k for k in project.written_nums() if k < n]
    if not earlier:
        return "(No previous chapters; this is the first one.)", "Previous chapters"

    ctx = project.config["context"]
    mode = ctx.get("mode", "budget")
    if mode == "last_n_words":
        max_words = int(ctx.get("last_n_words", 500))
        text = read_text(project.story_file(earlier[-1])).strip()
        return tail_words(text, max_words), f"End of the previous chapter (last ~{max_words} words)"

    if mode == "last_n_chapters":
        chosen = earlier[-int(ctx.get("last_n_chapters", 3)):]
    else:  # budget
        budget = int(ctx.get("max_words", 60000))
        chosen, used = [], 0
        for k in reversed(earlier):
            words = word_count(read_text(project.story_file(k)))
            if chosen and used + words > budget:
                break
            chosen.insert(0, k)
            used += words

    blocks = [read_text(project.story_file(k)).strip() for k in chosen]
    if len(chosen) == len(earlier):
        label = "All previous chapters"
    else:
        label = (
            f"The most recent {len(chosen)} of {len(earlier)} previous chapters "
            "(earlier chapters are covered by the story bible)"
        )
    return "\n\n\n---\n\n\n".join(blocks), label


def chapter_variables(project: Project, n: int) -> Dict[str, Any]:
    """All template variables available to the writer prompt for chapter n."""
    outline_raw = read_text(project.outline_file(n)).strip()
    chapter_outline, instructions = split_outline_instructions(outline_raw)
    plan = load_plan(project)
    sections = parse_sections(plan)
    recent_context, context_label = build_recent_context(project, n)
    bible = previous_bible_text(project, n)
    title = project.title
    target = sections.get("target_word_count", "")

    variables: Dict[str, Any] = {
        # Every '# Section' of plan.md is available by its normalized name.
        **sections,
        "title": title,
        "story_title": title,
        "chapter_number": n,
        "chapter_count": n,
        "chapter_title": extract_chapter_title(chapter_outline),
        "plan": plan or "(No plan provided.)",
        "story_arc": strip_comments(read_text(project.path("story_arc"))) or "(No story arc provided.)",
        "bible": bible,
        "previous_summary": bible,  # legacy alias
        "chapter_outline": chapter_outline or "(empty)",
        "chapter_specific_instructions": instructions or "(none)",
        "recent_context": recent_context,
        "context_label": context_label,
        "target_word_count": target,
        "length_instruction": length_instruction(target),
    }
    # Legacy context.md variable names used by older prompts.
    for key in ("genre", "tone", "writing_style", "pov_approach", "themes", "continuity_notes"):
        variables.setdefault(key, "")
    variables.setdefault("kinks", sections.get("content_elements", ""))
    variables.setdefault("synopsis", "(No synopsis provided)")
    variables.setdefault("character_profiles", "(No character profiles provided)")
    return variables


def short_story_variables(project: Project) -> Dict[str, Any]:
    """Template variables for the short_writer prompt."""
    plan = load_plan(project)
    sections = parse_sections(plan)
    target = sections.get("target_word_count", "") or DEFAULT_SHORT_TARGET
    return {
        **sections,
        "title": project.title,
        "story_title": project.title,
        "plan": plan,
        "target_word_count": target,
        "length_instruction": length_instruction(target),
    }


def _what(project: Project, n: int) -> str:
    return "the story" if project.is_short else f"chapter {n}"


# ============================== Writing ==============================


def write_chapter(
    project: Project,
    gen: Generator,
    n: int,
    *,
    fix_mode: str = "quick",
    retries: int = 0,
    force: bool = False,
) -> bool:
    """Write chapter n, validate/fix it and update its bible. Returns success."""
    outline_path = project.outline_file(n)
    if not outline_path.exists():
        print(f"ERROR: Outline not found: {outline_path} (run `aw prepare`).")
        return False
    story_path = project.story_file(n)
    if story_path.exists() and not force:
        print(f"ERROR: Chapter {n} already exists. Use `aw reset {n}` or `aw write {n} --force`.")
        return False

    later = [k for k in project.written_nums() if k > n]
    if later:
        print(
            f"NOTE: chapters {', '.join(map(str, later))} already exist and were written against the "
            f"old chapter {n}. They may need rewriting; their bibles will be regenerated."
        )

    plan = load_plan(project)
    ensure_bibles(project, gen, n - 1, plan, retries=retries)
    print(f"Writing chapter {n}...")
    generate_story_file(project, gen, "writer", chapter_variables(project, n), n, fix_mode=fix_mode, retries=retries)
    update_bible(project, gen, n, plan, retries=retries)
    print(f"Chapter {n} done: {project.rel(story_path)}")
    return True


def write_short_story(
    project: Project, gen: Generator, *, fix_mode: str = "quick", retries: int = 0, force: bool = False
) -> bool:
    """Write the whole short story (story/ch01.md) from plan.md and validate/fix it."""
    if not load_plan(project):
        print(f"ERROR: {project.rel(project.plan_path)} is missing or empty. Run `aw plan` first.")
        return False
    story_path = project.story_file(1)
    if story_path.exists() and not force:
        print("ERROR: The story is already written. Use --force to rewrite it (the old version is backed up).")
        return False
    print("Writing the story...")
    generate_story_file(project, gen, "short_writer", short_story_variables(project), 1, fix_mode=fix_mode, retries=retries)
    project.record(story_path, [project.plan_path])
    print(f"Story done: {project.rel(story_path)}")
    return True


def generate_story_file(
    project: Project, gen: Generator, task: str, variables: Dict[str, Any], n: int, *,
    fix_mode: str = "quick", retries: int = 0,
) -> None:
    """Generate story/chNN.md with `task`, continuing a truncated reply, then validate/fix it."""
    gen.warn_if_no_output_cap()
    result = gen.run(
        task,
        variables,
        f"ch{n:02d}",
        max_continuations=int(project.config["generation"].get("max_continuations", 2)),
        should_continue=lambda text, reason: reason == "length"
        or (not reason and ending_looks_truncated(text)),
        retries=retries,
        min_chars=MIN_CHAPTER_SIZE,
    )

    story_path = project.story_file(n)
    if story_path.exists():
        project.backup(story_path)
    write_text(story_path, result.text.lstrip("#").strip() + "\n")
    report_length(story_path, variables.get("target_word_count", ""), "Story" if project.is_short else "Chapter")
    what = _what(project, n)
    if result.finish_reason == "length" or ending_looks_truncated(result.text):
        print(
            f"WARNING: {what} still looks truncated (finish_reason={result.finish_reason}). "
            "Check the ending; consider raising max_completion_tokens in the profile."
        )

    if fix_mode != "none":
        try:
            run_validation_and_maybe_fix(project, gen, n, fix_mode=fix_mode, retries=retries)
        except RuntimeError as e:
            print(f"WARNING: fixing {what} failed ({e}); keeping the unfixed text.")


def report_length(story_path: Path, target: str, label: str = "Chapter") -> None:
    text = read_text(story_path)
    words = word_count(text)
    line = f"{label} length: {words:,} words, {len(text.splitlines())} lines"
    rng = parse_word_range(target)
    if rng:
        lo, hi = rng
        line += f" (target {lo:,}–{hi:,})"
        if words > hi * 1.2:
            line += " — OVER target"
        elif words < lo * 0.8:
            line += " — UNDER target"
    print(line)


# ============================== Validation and fixing ==============================

# Messages about the final line's punctuation are symptoms of truncation; adding
# a full stop would hide the problem, so they are never sent to the fixer.
_UNFIXABLE = (vc.TRUNCATED_ENDING,)
_STRUCTURAL_PREFIXES = ("Too few lines",)


def _fixable_errors(errors: List[Tuple[int, str]]) -> List[Tuple[int, str]]:
    truncated_lines = {ln for ln, msg in errors if msg == vc.TRUNCATED_ENDING}
    return [
        (ln, msg) for ln, msg in errors
        if msg not in _UNFIXABLE
        and not (ln in truncated_lines and "punctuation" in msg.lower())
    ]


def _should_group_issues_together(issues1: List[str], issues2: List[str]) -> bool:
    """Whether two consecutive problem lines belong in one fix block."""
    for issue in issues1 + issues2:
        low = issue.lower()
        if "auto-wrapped" in low or "inconsistent line breaks" in low:
            return True
    return any("line too long" in i.lower() for i in issues1) and any(
        "line too long" in i.lower() for i in issues2
    )


def group_fixes_into_blocks(
    errors: List[Tuple[int, str]], lines: List[str]
) -> List[Tuple[range, List[str], str]]:
    """Group consecutive problematic lines into (line_range, issues, original_text) blocks."""
    issues_by_line: Dict[int, List[str]] = {}
    for lineno, msg in errors:
        issues_by_line.setdefault(lineno, []).append(msg)
    sorted_lines = sorted(issues_by_line)
    blocks: List[Tuple[range, List[str], str]] = []
    i = 0
    while i < len(sorted_lines):
        start = end = sorted_lines[i]
        while (
            i + 1 < len(sorted_lines)
            and sorted_lines[i + 1] == end + 1
            and _should_group_issues_together(issues_by_line[end], issues_by_line[end + 1])
        ):
            end += 1
            i += 1
        line_range = range(start, end + 1)
        all_issues = [msg for ln in line_range for msg in issues_by_line[ln]]
        original = "\n".join(lines[ln - 1].rstrip("\n") for ln in line_range)
        blocks.append((line_range, all_issues, original))
        i += 1
    return blocks


def run_validation_and_maybe_fix(
    project: Project, gen: Generator, n: int, *, fix_mode: str = "quick", retries: int = 0
) -> None:
    """Validate chapter n and send fixable issues to the fixer prompt.

    quick: only the offending lines are rewritten.
    full:  the whole chapter is rewritten (rejected if it comes back much shorter).
    """
    chapter_path = project.story_file(n)
    what = _what(project, n)
    all_errors = vc.validate_file(chapter_path, True, short=project.is_short)
    if not all_errors:
        print(f"No validation issues found in {what}.")
        return

    print(f"Validation found {len(all_errors)} potential issue(s) in {what}.")
    for lineno, msg in all_errors[:10]:
        print(f"  - line {lineno}: {msg}")
    if len(all_errors) > 10:
        print(f"  ... and {len(all_errors) - 10} more.")

    errors = _fixable_errors(all_errors)
    if not errors:
        return

    text = read_text(chapter_path)
    lines = text.splitlines(keepends=True)
    structural = any(msg.startswith(_STRUCTURAL_PREFIXES) for _, msg in errors)

    if fix_mode == "quick" and not structural:
        blocks = group_fixes_into_blocks(errors, lines)
        parts = []
        for idx, (line_range, issues, original) in enumerate(blocks, start=1):
            desc = (
                f"line {line_range.start}" if len(line_range) == 1
                else f"lines {line_range.start}-{line_range.stop - 1}"
            )
            parts.append(f"[{idx}] {desc}: issues: {'; '.join(dict.fromkeys(issues))}\nOriginal:\n{original}")
        result = gen.run(
            "fixer_line", {"blocks": "\n\n".join(parts), "block_count": len(blocks)},
            f"ch{n:02d}", retries=retries,
        )
        fixes = [f.strip() for f in result.text.split("---FIX---")] if len(blocks) > 1 else [result.text.strip()]
        if len(fixes) != len(blocks):
            print(f"WARNING: Expected {len(blocks)} fix(es) but got {len(fixes)}; not applying fixes.")
            return
        new_lines: List[str] = []
        last = 0
        for (line_range, _, _), fix in zip(blocks, fixes):
            new_lines.extend(lines[last: line_range.start - 1])
            new_lines.extend(ln + "\n" for ln in fix.split("\n"))
            last = line_range.stop - 1
        new_lines.extend(lines[last:])
        write_text(chapter_path, "".join(new_lines))
        print(f"Applied {len(blocks)} fix(es) to {what}.")
        return

    report = "\n".join(f"line {ln}: {msg}" for ln, msg in errors)
    result = gen.run(
        "fixer_full", {"validation_report": report, "chapter_text": text}, f"ch{n:02d}", retries=retries
    )
    fixed = result.text.lstrip("#").strip() + "\n"
    if len(fixed) < len(text) * MIN_FULL_FIX_RATIO:
        rejected = project.log_file(f"ch{n:02d}.fixer_full.rejected.md")
        write_text(rejected, fixed)
        print(
            f"WARNING: full fix returned {len(fixed):,} chars vs {len(text):,} originally; "
            f"keeping the original. Rejected output: {project.rel(rejected)}"
        )
        return
    project.backup(chapter_path)
    write_text(chapter_path, fixed)
    print(f"Applied full-text fixes to {what}.")
