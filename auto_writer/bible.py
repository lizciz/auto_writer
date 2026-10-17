"""Per-chapter story bible: bible/chNN.md is the story state after chapter NN.

Each bible is generated from the previous bible plus the chapter text, so
rewriting chapter 4 only ever sees what chapters 1–3 established. The inputs'
hashes are recorded; when a chapter or an earlier bible changes, every bible
downstream of it becomes stale and is regenerated before it is used again.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from .llm import Generator
from .project import Project, read_text, write_text
from .text import extract_chapter_title, get_section, split_outline_instructions, strip_comments

FIRST_CHAPTER_BIBLE = "(This is the first chapter. Nothing has been established yet beyond the plan.)"


def previous_chapter(project: Project, n: int) -> Optional[int]:
    """Highest written chapter number below n."""
    earlier = [k for k in project.written_nums() if k < n]
    return earlier[-1] if earlier else None


def bible_inputs(project: Project, n: int) -> List:
    inputs = [project.story_file(n)]
    prev = previous_chapter(project, n)
    if prev is not None:
        inputs.append(project.bible_file(prev))
    return inputs


def stale_reason(project: Project, n: int) -> Optional[str]:
    """Why bible n needs regenerating, or None if it is current."""
    path = project.bible_file(n)
    if not path.exists():
        return "missing"
    changed = project.changed_inputs(path)
    if changed is None:
        return None  # hand-written or pre-dates bookkeeping: trust it
    expected = {project.rel(p) for p in bible_inputs(project, n)}
    recorded = set((project.record_for(path) or {}).get("inputs", {}))
    if expected != recorded:
        return "earlier chapters were added or removed"
    if changed:
        return "changed since generated: " + ", ".join(changed)
    return None


def stale_bibles(project: Project, upto: Optional[int] = None) -> List[Tuple[int, str]]:
    """(chapter, reason) for every written chapter whose bible is missing or stale.

    Staleness propagates: once one bible is stale, all later ones are too.
    """
    out: List[Tuple[int, str]] = []
    for n in project.written_nums():
        if upto is not None and n > upto:
            break
        reason = stale_reason(project, n)
        if reason is None and out:
            reason = f"depends on stale bible for chapter {out[-1][0]}"
        if reason:
            out.append((n, reason))
    return out


def previous_bible_text(project: Project, n: int) -> str:
    prev = previous_chapter(project, n)
    if prev is None:
        return FIRST_CHAPTER_BIBLE
    return read_text(project.bible_file(prev)).strip() or FIRST_CHAPTER_BIBLE


def update_bible(project: Project, gen: Generator, n: int, plan: str, retries: int = 0) -> None:
    """Generate bible/chNN.md from the previous bible and chapter n."""
    outline, _ = split_outline_instructions(read_text(project.outline_file(n)).strip())
    chapter = read_text(project.story_file(n)).strip()
    variables = {
        "title": project.title,
        "story_title": project.title,
        "chapter_number": n,
        "chapter_title": extract_chapter_title(outline) or extract_chapter_title(chapter),
        "plan": plan,
        "previous_bible": previous_bible_text(project, n),
        "chapter_outline": outline or "(no outline)",
        "chapter_content": chapter,
    }
    print(f"Updating story bible for chapter {n}...")
    result = gen.run("bible", variables, f"ch{n:02d}", retries=retries, min_chars=200)
    path = project.bible_file(n)
    write_text(path, strip_comments(result.text).strip() + "\n")
    project.record(path, bible_inputs(project, n))
    deviations = get_section(result.text, "Plan Deviations")
    if deviations and deviations.strip().strip(".").lower() not in ("none", "- none", "(none)"):
        print(f"NOTE: chapter {n} deviates from the plan or earlier facts:\n{deviations}\n")


def ensure_bibles(project: Project, gen: Generator, upto: int, plan: str, retries: int = 0) -> None:
    """Bring every bible up to chapter `upto` up to date, oldest first."""
    for n, reason in stale_bibles(project, upto):
        print(f"Bible for chapter {n} needs regenerating ({reason}).")
        update_bible(project, gen, n, plan, retries=retries)
