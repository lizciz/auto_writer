"""Command-line interface: `aw <command>`."""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata as ud
from pathlib import Path
from typing import Optional

from storykit.profiles import PROFILES_DIR_ENV, ProfileEntry, ProfileError, list_profiles, profiles_dir, resolve_profile
from storykit.prompts import PromptError, format_prompt_file

from . import __version__
from .bible import stale_bibles, update_bible
from .characters import generate_characters
from .llm import Generator, prompt_library
from .planning import count_answers, outline, plan, prepare, review_outline
from .project import (
    PACKAGE_DIR,
    Project,
    die,
    global_config,
    load_json,
    profile_reference,
    read_text,
    set_global_config,
    write_text,
)
from .text import parse_master_outline, strip_comments, word_count
from .writing import load_plan, write_chapter, write_short_story


def sanitize_dirname(s: str) -> str:
    """Sanitize a string to be safe for use as a directory name."""
    s = ud.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", s)
    return re.sub(r" +", " ", s).strip()


# ============================== Commands ==============================


def cmd_init(args: argparse.Namespace) -> None:
    base = Path(args.path).resolve() if args.path else Path.cwd()
    root = base / sanitize_dirname(args.dir or args.title)
    if args.set and args.set not in prompt_library().sets():
        die(f"Unknown prompt set '{args.set}'. Available: {', '.join(prompt_library().sets())}")
    project = Project.create(root, args.title)
    if args.short:
        project.set_config("form", "short")
    if args.set:
        project.set_config("prompt_set", args.set)
        print(f"Using prompt set '{args.set}'.")
    idea = project.path("idea")
    if not idea.exists():
        template = "idea_short.md" if project.is_short else "idea.md"
        write_text(idea, read_text(PACKAGE_DIR / "templates" / template))
    print(f"Initialized {'short story' if project.is_short else 'story'} project at: {root}")
    default = global_config().get("default_profile")
    if args.profile:
        project.set_profile(args.profile)
    elif default:
        project.set_profile(default, " (the global default)")
    else:
        print(
            "No profile set yet. Set one with `aw profile <name>`, or set a default for new projects "
            "with `aw profile --global <name>`."
        )
    then = "`aw oneshot` (or `aw plan`)" if project.is_short else "`aw plan`"
    print(f"Next: write your idea in {project.rel(idea)}, then run {then}.")


def show_profiles(project: Optional[Project]) -> None:
    if project is not None:
        if project.profile_path.exists():
            source = project.config.get("profile_source")
            current = ProfileEntry("current", project.profile_path, load_json(project.profile_path, None))
            print(f"Current project profile: {project.profile_path}")
            print(f"  {current.summary()}" + (f"  (copied from {source})" if source else ""))
        else:
            print(f"Current project ({project.root}) has no profile set.")
        print()
    default = global_config().get("default_profile")
    print(f"Global default (for new projects): {default or '(none)'}")
    entries = list_profiles()
    print(f"\nAvailable profiles in {profiles_dir()} (shared by all tools; override with ${PROFILES_DIR_ENV}):")
    if not entries:
        print("  (none) Put profile JSON files there, e.g.:")
        print('  {"api_base": "https://...", "api_key_env": "API_KEY", "model": "...", "max_completion_tokens": 32000}')
    for e in entries:
        mark = "*" if e.name == default else " "
        print(f"{mark} {e.name:<24} {e.summary()}")
    print(
        "\nSet one with: aw profile <name>   (a path to any JSON file also works)\n"
        "Default for new projects: aw profile --global <name>"
    )


def set_default_profile(source: Optional[str], unset: bool) -> None:
    if unset:
        set_global_config("default_profile", None)
        print("Removed the global default profile.")
        return
    if not source:
        print(f"Global default profile: {global_config().get('default_profile') or '(none)'}")
        return
    try:
        ref = profile_reference(resolve_profile(source))
    except ProfileError as e:
        die(str(e))
    set_global_config("default_profile", ref)
    print(f"Global default profile: {ref}. New projects (and projects without a profile) use it.")


def cmd_profile(args, project: Optional[Project]) -> None:
    if args.unset and not args.is_global:
        die("--unset only applies to the global default: aw profile --global --unset")
    if args.is_global:
        set_default_profile(args.source, args.unset)
        return
    if not args.source:
        show_profiles(project)
        return
    if project is None:
        die("Not in a story project. Run `aw init` first, or use `aw -C <project> profile ...`.")
    project.set_profile(args.source)


def cmd_title(args, project: Project) -> None:
    old = project.title
    project.set_config("title", args.text)
    print(f"Updated title from {old!r} to {args.text!r}.")


def cmd_plan(args, project: Project) -> None:
    plan(project, Generator(project), ask=not args.no_questions, force=args.force, retries=args.retry)


def require_full(project: Project, command: str) -> None:
    if project.is_short:
        die(
            f"`aw {command}` is not used for short stories (\"form\": \"short\" in .story/config.json). "
            "Short stories go straight from plan.md to `aw write`."
        )


def cmd_outline(args, project: Project) -> None:
    require_full(project, "outline")
    outline(project, Generator(project), force=args.force, retries=args.retry)


def cmd_review_outline(args, project: Project) -> None:
    require_full(project, "review-outline")
    review_outline(project, Generator(project), apply=args.apply, retries=args.retry)


def cmd_prepare(args, project: Project) -> None:
    require_full(project, "prepare")
    prepare(project)


def cmd_write(args, project: Project) -> None:
    if project.is_short:
        if args.chapter not in (None, 1):
            die("A short story is a single text; there is no chapter " + str(args.chapter) + ".")
        try:
            write_short_story(project, Generator(project), fix_mode=args.fix_mode, retries=args.retry, force=args.force)
        except RuntimeError as e:
            die(str(e))
        return
    outline_nums = project.outline_nums()
    if not outline_nums:
        die("No chapter outlines found. Run `aw prepare` first.")
    if args.chapter is not None:
        if args.chapter not in outline_nums:
            die(f"No outline for chapter {args.chapter}.")
        start = args.chapter
    else:
        written = set(project.written_nums())
        pending = [n for n in outline_nums if n not in written]
        if not pending:
            print("All chapters already written. Nothing to do.")
            return
        start = pending[0]

    gen = Generator(project)
    todo = [n for n in outline_nums if n >= start] if args.loop else [start]
    done = 0
    for n in todo:
        if args.loop and n != start and project.story_file(n).exists():
            continue
        try:
            ok = write_chapter(
                project, gen, n, fix_mode=args.fix_mode, retries=args.retry,
                force=args.force and n == start,
            )
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            ok = False
        if not ok:
            break
        done += 1
        if args.max_writes is not None and done >= args.max_writes:
            break
    print(f"Wrote {done} chapter(s).")


def cmd_oneshot(args, project: Project) -> None:
    """Short stories: idea -> plan -> story without stopping, redoing only what is out of date."""
    if not project.is_short:
        die("`aw oneshot` is for short stories. Create one with `aw init --short \"Title\"`.")
    idea_path = project.path("idea")
    if args.idea and args.idea.strip() != strip_comments(read_text(idea_path)):
        if strip_comments(read_text(idea_path)):
            backup = project.backup(idea_path)
            print(f"Replacing {project.rel(idea_path)} (previous version: {project.rel(backup)}).")
        write_text(idea_path, args.idea.strip() + "\n")

    gen = Generator(project)
    plan_path = project.path("plan")
    replan = args.force or not plan_path.exists() or bool(project.changed_inputs(plan_path))
    if replan:
        plan(project, gen, ask=False, force=True, retries=args.retry, hint=False)
    else:
        print(f"Using the existing {project.rel(plan_path)} (it is up to date with the idea).")

    story_path = project.story_file(1)
    rewrite = args.force or replan or bool(project.changed_inputs(story_path))
    if story_path.exists() and not rewrite:
        print(f"{project.rel(story_path)} is up to date with the plan. Use `aw oneshot --force` to redo everything.")
        return
    try:
        write_short_story(project, gen, fix_mode=args.fix_mode, retries=args.retry, force=True)
    except RuntimeError as e:
        die(str(e))


def cmd_bible(args, project: Project) -> None:
    require_full(project, "bible")
    gen = Generator(project)
    plan_text = load_plan(project)
    if args.chapter is not None:
        update_bible(project, gen, args.chapter, plan_text, retries=args.retry)
        return
    stale = stale_bibles(project)
    if not stale:
        print("All bibles are up to date.")
    for n, reason in stale:
        print(f"Chapter {n}: {reason}")
        update_bible(project, gen, n, plan_text, retries=args.retry)


def cmd_characters(args, project: Project) -> None:
    generate_characters(project, Generator(project), force=args.force, retries=args.retry)


def cmd_reset(args, project: Project) -> None:
    target = args.target.strip().lower()
    if target == "all":
        nums = project.written_nums()
    else:
        if not target.isdigit():
            die(f"Invalid chapter number: {args.target!r}")
        nums = [int(target)]
    logs = project.path("logs_dir")
    removed = 0
    for n in nums:
        for p in (project.story_file(n), project.bible_file(n), *logs.glob(f"ch{n:02d}.*")):
            if p.exists():
                p.unlink()
                project.forget(p)
                removed += 1
    later = [k for k in project.written_nums() if nums and k > max(nums)]
    print(f"Reset chapter(s) {', '.join(map(str, nums)) or '-'} ({removed} files removed).")
    if later:
        print(
            f"NOTE: chapters {', '.join(map(str, later))} were written after the removed chapter; "
            "their bibles will be regenerated on the next write."
        )


def cmd_status(args, project: Project) -> None:
    def state(path: Path) -> str:
        if not path.exists() or not strip_comments(read_text(path)):
            return "missing"
        changed = project.changed_inputs(path)
        if changed:
            return "OUT OF DATE (changed: " + ", ".join(changed) + ")"
        if project.edited_since_generated(path):
            return "ok (edited by you)"
        return "ok"

    print(f"{project.title}  [{project.root}]" + ("  (short story)" if project.is_short else ""))
    print(f"  idea:     {state(project.path('idea'))}")
    q = project.path("questions")
    if q.exists():
        print(f"  questions: {count_answers(read_text(q))} answered")
    print(f"  plan:     {state(project.plan_path)}  ({project.rel(project.plan_path)})")
    if project.is_short:
        story = project.story_file(1)
        words = f"  ({word_count(read_text(story)):,} words)" if story.exists() else ""
        print(f"  story:    {state(story)}{words}  ({project.rel(story)})")
        chars = project.path("characters")
        if chars.exists():
            print(f"  characters: {state(chars)}  ({project.rel(chars)})")
        return
    master = project.path("outline_master")
    n_outline = len(parse_master_outline(read_text(master))) if master.exists() else 0
    print(f"  outline:  {state(master)}  ({n_outline} chapters, {len(project.outline_nums())} prepared)")
    written = project.written_nums()
    print(f"  written:  {len(written)} chapter(s): {', '.join(map(str, written)) or '-'}")
    stale = stale_bibles(project)
    print(f"  bibles:   {'all current' if not stale else f'{len(stale)} need regenerating'}")
    for n, reason in stale:
        print(f"    ch{n:02d}: {reason}")
    chars = project.path("characters")
    if chars.exists():
        print(f"  characters: {state(chars)}  ({project.rel(chars)})")


def cmd_prompts(args, project: Project) -> None:
    lib = prompt_library()
    overrides = dict(project.config.get("prompts", {}))
    project_set = project.config.get("prompt_set") or "default"
    action = args.action or "list"
    names = args.names

    if action == "list":
        print(f"Prompt set:          {project_set}   (available: {', '.join(lib.sets())})")
        print(f"Bundled sets:        {lib.bundled_dir}")
        print(f"User sets:           {lib.user_dir}")
        print(f"Project prompts dir: {project.prompts_dir}\n")
        for task in lib.tasks():
            prompt = lib.resolve(task, project.prompt_set(task), project.prompts_dir)
            if prompt.origin == "project":
                source = "project fork"
            else:
                source = f"{prompt.source_set} ({prompt.origin})"
                if prompt.inherited:
                    source += f", inherited: '{prompt.prompt_set}' has none"
            if task in overrides:
                source += "  [per-task override]"
            print(f"{task:<16} {source}")
            print(f"{'':<16} {prompt.path}")
            if prompt.addendum_path:
                print(f"{'':<16} + addendum {prompt.addendum_path}")
        print("\nChange with: aw prompts use SET [TASK]")
        return

    if action == "use":
        if not names:
            die(f"`aw prompts use` needs a set name. Sets: {', '.join(lib.sets())}")
        new_set, task = names[0], (names[1] if len(names) > 1 else None)
        if task is None:
            if new_set not in lib.sets():
                die(f"Unknown prompt set '{new_set}'. Available: {', '.join(lib.sets())}")
            project.set_config("prompt_set", new_set)
            print(f"Project now uses prompt set '{new_set}'.")
            if overrides:
                print("Per-task overrides still apply: " + ", ".join(f"{t}={v}" for t, v in overrides.items()))
            return
        prompt = lib.resolve(task, new_set)  # validates set and task
        if new_set == project_set:
            overrides.pop(task, None)
        else:
            overrides[task] = new_set
        project.set_config("prompts", overrides)
        print(f"Project now uses set '{new_set}' for '{task}'" + (" (inherited from default)." if prompt.inherited else "."))
        return

    if not names:
        die(f"`aw prompts {action}` needs a task name. Tasks: {', '.join(lib.tasks())}")
    task = names[0]
    if action == "show":
        prompt = lib.resolve(task, project.prompt_set(task), project.prompts_dir)
        print(f"# {prompt.path} ({prompt.origin})")
        if prompt.addendum_path:
            print(f"# + {prompt.addendum_path}")
        print(f"# variables used: {', '.join(prompt.used_variables())}\n")
        print(f"=== system ===\n{prompt.system}\n\n=== user ===\n{prompt.user}")
    elif action == "fork":
        try:
            dest = lib.fork(task, project.prompts_dir, project.prompt_set(task))
        except PromptError as e:
            die(str(e))
        print(f"Copied to {dest}. Edit it there; it now overrides the shared prompt for this project.")


def cmd_migrate(args, project: Project) -> None:
    """Convert legacy per-project prompt files into the new layout."""
    legacy = project.meta_dir
    system, user = legacy / "system_prompt.md", legacy / "user_prompt.md"
    if not (system.exists() or user.exists()):
        print("No legacy .story/system_prompt.md / user_prompt.md found.")
    else:
        dest = project.prompts_dir / "writer.md"
        if dest.exists():
            die(f"{dest} already exists.")
        write_text(
            dest,
            format_prompt_file(
                read_text(system), read_text(user),
                {"description": "Migrated from legacy .story/system_prompt.md + user_prompt.md"},
            ),
        )
        print(f"Wrote {project.rel(dest)}.")
    for name in ("system_prompt.md", "user_prompt.md", "summary_system_prompt.md", "summary_user_prompt.md"):
        p = legacy / name
        if p.exists():
            p.rename(p.with_suffix(".md.legacy"))
            print(f"Renamed {project.rel(p)} -> {p.name}.legacy")
    if (project.root / "context.md").exists() and not project.path("plan").exists():
        print("Note: context.md is used as the plan. Rename it to plan.md when convenient.")


def _warn_legacy_prompts(project: Project) -> None:
    if (project.meta_dir / "system_prompt.md").exists() and not (project.prompts_dir / "writer.md").exists():
        print(
            "NOTE: this project has legacy prompts in .story/system_prompt.md / user_prompt.md, which "
            "are no longer used. Run `aw migrate` to convert them into .story/prompts/writer.md."
        )


# ============================== Parser ==============================


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aw",
        description="Semi-automatic story writer: long-form chapter by chapter, or short stories in one go.",
        epilog=(
            "Typical flow: init -> (write idea.md) -> plan -> (answer questions.md) -> plan -> "
            "outline -> review-outline -> prepare -> write --loop -> characters.  "
            "Short story: init --short -> (write idea.md) -> oneshot -> characters"
        ),
    )
    p.add_argument("--version", action="version", version=f"auto_writer {__version__}")
    p.add_argument("-C", "--project", help="Project directory (default: nearest one at or above CWD)")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def add(name, func, help_text):
        sp = sub.add_parser(name, help=help_text, description=help_text)
        sp.set_defaults(func=func)
        return sp

    def add_retry(sp):
        sp.add_argument("--retry", type=int, default=2, help="Retries on API errors or refusals (default: 2)")

    sp = add("init", cmd_init, "Create a new story project")
    sp.add_argument("title")
    sp.add_argument("--dir", help="Directory name (default: sanitized title)")
    sp.add_argument("--path", help="Parent directory (default: CWD)")
    sp.add_argument(
        "--profile", help="API profile: a name from ~/.config/llm-profiles or a path (default: the global default)"
    )
    sp.add_argument("--set", help="Prompt set (default: default); see `aw prompts`")
    sp.add_argument("--short", action="store_true", help="A standalone short story written in one go")

    sp = add(
        "profile", cmd_profile,
        "Set the API profile by name (from ~/.config/llm-profiles) or path; without an argument, list profiles",
    )
    sp.add_argument("source", nargs="?")
    sp.add_argument(
        "--global", dest="is_global", action="store_true",
        help="Set (or show) the default profile for new projects instead",
    )
    sp.add_argument("--unset", action="store_true", help="With --global: remove the default profile")

    sp = add("title", cmd_title, "Change the story title")
    sp.add_argument("text")

    sp = add("plan", cmd_plan, "Develop idea.md into plan.md (asks questions first by default)")
    sp.add_argument("--no-questions", action="store_true", help="Skip the question round; the model decides")
    sp.add_argument("--force", action="store_true", help="Regenerate plan.md even if it exists")
    add_retry(sp)

    sp = add("outline", cmd_outline, "Generate chapter_outline.md from plan.md")
    sp.add_argument("--force", action="store_true", help="Regenerate even if it exists")
    add_retry(sp)

    sp = add("review-outline", cmd_review_outline, "Critique the outline against the plan")
    sp.add_argument("--apply", action="store_true", help="Also revise the outline from the review")
    add_retry(sp)

    add("prepare", cmd_prepare, "Split chapter_outline.md into outline/chNN.md and story_arc.md")

    sp = add(
        "oneshot", cmd_oneshot,
        "Short stories: plan (no questions) and write the story in one go, redoing only what is out of date",
    )
    sp.add_argument("idea", nargs="?", help="The idea text (default: idea.md)")
    sp.add_argument("--fix-mode", choices=["quick", "full", "none"], default="quick")
    sp.add_argument("--force", action="store_true", help="Regenerate the plan and the story")
    add_retry(sp)

    sp = add("write", cmd_write, "Write the next unwritten chapter (or CHAPTER); short stories: the story")
    sp.add_argument("chapter", nargs="?", type=int)
    sp.add_argument("--loop", action="store_true", help="Keep writing following unwritten chapters")
    sp.add_argument("--max-writes", type=int, help="Stop after this many chapters")
    sp.add_argument("--fix-mode", choices=["quick", "full", "none"], default="quick")
    sp.add_argument("--force", action="store_true", help="Overwrite CHAPTER if it already exists")
    add_retry(sp)

    sp = add("bible", cmd_bible, "Regenerate missing/stale story bibles (or the one for CHAPTER)")
    sp.add_argument("chapter", nargs="?", type=int)
    add_retry(sp)

    sp = add("characters", cmd_characters, "Generate characters.md: SDXL tag profiles for narrate")
    sp.add_argument("--force", action="store_true", help="Regenerate even if it exists")
    add_retry(sp)

    sp = add("reset", cmd_reset, "Remove a written chapter (or 'all'), its bible and logs")
    sp.add_argument("target")

    add("status", cmd_status, "Show planning and writing progress")

    sp = add(
        "prompts", cmd_prompts,
        "List prompts, show or fork TASK, or select a prompt set (use SET, or use SET TASK for one task)",
    )
    sp.add_argument("action", nargs="?", choices=["list", "show", "use", "fork"])
    sp.add_argument("names", nargs="*", metavar="SET|TASK")

    add("migrate", cmd_migrate, "Convert a project's legacy prompt files to the new layout")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return
    if args.func is cmd_init:
        cmd_init(args)
        return
    if args.func is cmd_profile and (args.is_global or not args.source):
        cmd_profile(args, Project.find(args.project, required=False))
        return
    project = Project.find(args.project)
    if args.func not in (cmd_migrate, cmd_prompts):
        _warn_legacy_prompts(project)
    try:
        args.func(args, project)
    except PromptError as e:
        die(str(e))

