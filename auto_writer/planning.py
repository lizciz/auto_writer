"""Planning stage: idea -> questions -> plan -> chapter outline -> review."""

from __future__ import annotations

import re
from pathlib import Path

from .llm import Generator
from .project import Project, die, read_text, write_text
from .text import generate_story_arc, parse_master_outline, strip_comments

NO_QUESTIONS = (
    "(No questions were asked. Make every open decision yourself and list the important "
    "ones under # Assumptions.)"
)


def _require(project: Project, key: str, hint: str) -> str:
    path = project.plan_path if key == "plan" else project.path(key)
    text = strip_comments(read_text(path))
    if not text:
        die(f"{project.rel(path)} is missing or empty. {hint}")
    return text


def _guard_overwrite(project: Project, path: Path, force: bool, command: str) -> None:
    if not path.exists():
        return
    if not force:
        die(f"{project.rel(path)} already exists. Re-run with `aw {command} --force` to regenerate it.")
    backup = project.backup(path)
    if backup:
        print(f"Backed up {project.rel(path)} to {project.rel(backup)}")


def _write_generated(project: Project, path: Path, text: str, inputs) -> None:
    write_text(path, text.strip() + "\n")
    project.record(path, inputs)
    print(f"Wrote {project.rel(path)}")


def _warn_if_stale(project: Project, path: Path) -> None:
    changed = project.changed_inputs(path)
    if changed:
        print(f"WARNING: {project.rel(path)} is out of date; changed since it was generated: {', '.join(changed)}")


_ANSWER_RE = re.compile(r"^[ \t]*\**Answer[ \t]*:?\**[ \t]*:?[ \t]*(.*)$", re.IGNORECASE | re.MULTILINE)


def count_answers(questions_md: str) -> int:
    return sum(1 for m in _ANSWER_RE.finditer(questions_md) if m.group(1).strip())


def plan(project: Project, gen: Generator, *, ask: bool = True, force: bool = False, retries: int = 0) -> None:
    """Develop idea.md into plan.md, by default via a round of questions."""
    idea = _require(project, "idea", "Write your story idea there first.")
    plan_path = project.path("plan")
    questions_path = project.path("questions")
    base_vars = {"title": project.title, "story_title": project.title, "idea": idea}

    if plan_path.exists() and not force:
        die(f"{project.rel(plan_path)} already exists. Re-run with `aw plan --force` to regenerate it.")

    if ask:
        if not questions_path.exists():
            result = gen.run("plan_questions", base_vars, "plan", retries=retries)
            _write_generated(project, questions_path, result.text, [project.path("idea")])
            print(
                f"\nAnswer the questions in {project.rel(questions_path)} (leave any blank to accept the "
                "suggested default), then run `aw plan` again.\n"
                "To skip questions and let the model decide: `aw plan --no-questions`."
            )
            return
        questions = read_text(questions_path)
        if not project.edited_since_generated(questions_path) and count_answers(questions) == 0:
            die(
                f"{project.rel(questions_path)} has no answers yet. Answer the questions and run `aw plan` "
                "again, or use `aw plan --no-questions` to let the model choose."
            )
        if project.changed_inputs(questions_path):
            print(
                "WARNING: idea.md changed after the questions were generated. Delete "
                f"{project.rel(questions_path)} and run `aw plan` to get new questions."
            )
        variables = {**base_vars, "questions": strip_comments(questions)}
        inputs = [project.path("idea"), questions_path]
    else:
        variables = {**base_vars, "questions": NO_QUESTIONS}
        inputs = [project.path("idea")]

    _guard_overwrite(project, plan_path, force, "plan")
    result = gen.run("plan", variables, "plan", retries=retries)
    _write_generated(project, plan_path, result.text, inputs)
    print("Review and edit the plan (especially # Assumptions), then run `aw outline`.")


def outline(project: Project, gen: Generator, *, force: bool = False, retries: int = 0) -> None:
    """Generate chapter_outline.md from plan.md."""
    plan_text = _require(project, "plan", "Run `aw plan` (or write plan.md yourself) first.")
    out = project.path("outline_master")
    _guard_overwrite(project, out, force, "outline")
    result = gen.run(
        "outline", {"title": project.title, "story_title": project.title, "plan": plan_text},
        "outline", retries=retries,
    )
    _write_generated(project, out, result.text, [project.plan_path])
    chapters = parse_master_outline(read_text(out))
    print(f"Outline has {len(chapters)} chapters. Next: `aw review-outline`, or edit it and run `aw prepare`.")


def review_outline(project: Project, gen: Generator, *, apply: bool = False, retries: int = 0) -> None:
    """Critique chapter_outline.md against plan.md; with apply, also revise the outline."""
    plan_text = _require(project, "plan", "Run `aw plan` first.")
    outline_path = project.path("outline_master")
    outline_text = _require(project, "outline_master", "Run `aw outline` first.")
    review_path = project.path("outline_review")
    base_vars = {
        "title": project.title, "story_title": project.title,
        "plan": plan_text, "chapter_outline": outline_text,
    }
    result = gen.run("outline_review", base_vars, "outline", retries=retries)
    if review_path.exists():
        project.backup(review_path)
    _write_generated(project, review_path, result.text, [project.plan_path, outline_path])
    if not apply:
        print(
            f"Read {project.rel(review_path)}. Edit the outline yourself, or run "
            "`aw review-outline --apply` to have the model apply the recommended changes."
        )
        return

    revised = gen.run(
        "outline_revise", {**base_vars, "outline_review": result.text}, "outline", retries=retries
    )
    if not parse_master_outline(revised.text):
        die("The revised outline has no '# Chapter N' headings; keeping the original.")
    backup = project.backup(outline_path)
    _write_generated(project, outline_path, revised.text, [project.plan_path])
    print("Revised outline written" + (f" (previous version: {project.rel(backup)})." if backup else "."))


def prepare(project: Project) -> None:
    """Split chapter_outline.md into outline/chNN.md and build story_arc.md."""
    master = project.path("outline_master")
    _warn_if_stale(project, master)
    chapters = parse_master_outline(read_text(master))
    if not chapters:
        die(f"No chapters found in {project.rel(master)} (check '# Chapter N' headings).")
    outline_dir = project.path("outline_dir")
    for stale in outline_dir.glob("ch*.md"):
        stale.unlink()
    for num, brief in sorted(chapters.items()):
        write_text(project.outline_file(num), brief)
    arc_path = project.path("story_arc")
    write_text(arc_path, generate_story_arc(chapters) + "\n")
    print(f"Prepared {len(chapters)} chapter outlines in {project.rel(outline_dir)} and {project.rel(arc_path)}.")
