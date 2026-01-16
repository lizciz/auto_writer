#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
auto_writer.py — simplified semi-automatic long-form story writer.

Git-like commands:

  --init    PATH --title "Title"
      Initialize a story directory with .story metadata.

  --prepare [PATH]
      Parse chapter_outline.md -> outline/chNN.md files.

  --write   [PATH]
      Write next (or specific) chapter using an OpenAI-compatible API.
      With --loop, keeps writing subsequent unwritten chapters until
      all are done or --max-writes is reached.

  --reset   all|N
      Remove generated chapter(s), including logs.

  --profile PATH
      Copy a JSON profile (api_base, api_key_env, model, ...) into .story/profile.json.

  --system-prompt PATH
      Copy a system prompt markdown file into .story/system_prompt.md.

  --user-prompt PATH
      Copy a user prompt markdown file into .story/user_prompt.md.

  --title "New title"
      Update the story title in .story/config.json.

Layout (per project directory):

  .story/
      config.json           # title, created, version, paths, context settings
      profile.json          # API config (api_base, api_key_env, model, ...)
      system_prompt.md      # system message template
      user_prompt.md        # user message template

  chapter_outline.md        # master outline file (edited by you)
  outline/
      ch01.md, ch02.md, ... # per-chapter outline snippets (from --prepare)
  story/
      ch01.md, ch02.md, ... # generated chapters
  logs/
      chNN.prompt.md        # full prompts for debugging
      chNN.request.json     # metadata for each write request
      chNN.fix.prompt.md    # full fixer prompts
      chNN.fix.request.json # metadata for each fixer request
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import unicodedata as ud

import validate_chapter as vc


# ============================== Utilities ==============================

APP_VERSION = "2.1.0"

SYSTEM_PROMPT_FILENAME = "system_prompt.md"
USER_PROMPT_FILENAME = "user_prompt.md"
SUMMARY_SYSTEM_PROMPT_FILENAME = "summary_system_prompt.md"
SUMMARY_USER_PROMPT_FILENAME = "summary_user_prompt.md"
PROFILE_FILENAME = "profile.json"


def now_iso() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def write_text(p: Path, s: str) -> None:
    p.write_text(s, encoding="utf-8")


def load_json(p: Path, default: Any) -> Any:
    txt = read_text(p)
    if not txt.strip():
        return default
    try:
        return json.loads(txt)
    except Exception:
        return default


def save_json(p: Path, data: Any) -> None:
    write_text(p, json.dumps(data, ensure_ascii=False, indent=2))


def list_chapter_files(chapter_dir: Path) -> List[Path]:
    return sorted(chapter_dir.glob("ch*.md"))


def find_first_chapter_num(chapter_dir: Path) -> int:
    chapter_files = list_chapter_files(chapter_dir)
    if not chapter_files:
        return 1
    nums = [chapter_num_from_name(p.name) for p in chapter_files]
    return min(nums) if nums else 1


def chapter_num_from_name(name: str) -> int:
    m = re.search(r"ch(\d+)", name, flags=re.I)
    return int(m.group(1)) if m else 0


# ============================== Number words ==============================

_NUM_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}


def words_to_int(s: str) -> Optional[int]:
    """
    Convert simple English cardinal words up to ~199 (e.g., 'twenty-one', 'one hundred one').
    Returns None if it doesn't look like a number word.
    """
    s = s.strip().lower()
    if not s:
        return None
    s = s.replace("-", " ")
    parts = [p for p in s.split() if p not in {"and"}]
    if not parts:
        return None
    total = 0
    cur = 0
    seen = False
    for w in parts:
        if w == "hundred":
            if cur == 0:
                cur = 1
            cur *= 100
            seen = True
        elif w in _NUM_WORDS:
            cur += _NUM_WORDS[w]
            seen = True
        else:
            return None
    total += cur
    return total if seen else None


# ============================== API client ==============================


def chat_completion(
    api_base: str,
    api_key: str,
    model: str,
    messages: List[Dict[str, str]],
    parameters: Optional[Dict[str, Any]] = None,
    timeout: int = 1800,
) -> str:
    """
    Thin wrapper over /chat/completions compatible APIs.
    """
    url = api_base.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload: Dict[str, Any] = {"model": model, "messages": messages}
    if parameters:
        payload.update(parameters)
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"API error {resp.status_code}: {resp.text[:900]}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        raise RuntimeError(f"Unexpected API response: {data}")


# ============================== Prompts ==============================

SYSTEM_WRITER = """You are a meticulous long-form fiction WRITER.
Write immersive, coherent chapters that build naturally from prior events.
Use the provided chapter outline and previous chapters as context.
Keep voice and tone consistent with the existing story.
Output pure Markdown prose; do not include OOC notes or explanations.
"""

SYSTEM_LINE_FIXER = """You are a meticulous copy editor for long-form English fiction.
You will be given a list of individual lines or multi-line blocks from a story,
each with a short description of potential validation issues.

Your job:
- For each item, produce a corrected version.
- Replace or remove any non-English, corrupted, or obviously wrong characters.
- Preserve the original meaning, tone, and voice as much as possible.
- Fix unbalanced quotes and parentheses.
- Fix repeated words like "the the" when they are clearly mistakes.
- Remove repeated punctuation like "!!" or ",,".
- Add missing sentence-ending punctuation where appropriate, but do NOT add
  punctuation in places where a fragment is clearly intentional (e.g., short
  interjections, dialogue tags, or stylistic fragments).
- Remove any Markdown formatting such as:
  - Bold: **text**
  - Italic: *text*
  - Headings: lines starting with one or more '#' characters
  Convert these to plain prose without Markdown markers.

Handling different types of fixes:

For single-line issues:
- Output the corrected line.

For multi-line blocks (often improperly formatted text):
CRITICAL - Understanding proper paragraph structure:
- A paragraph is a group of related sentences about a single idea/topic.
- Each paragraph should be written as CONTINUOUS text (no hard line breaks within it).
- Paragraphs are separated by ONE blank line (not single line breaks).
- Typical paragraph length: 3-7 sentences, or 100-300 words.

When fixing multi-line blocks:
1. Analyze the content and identify distinct ideas/topics/beats.
2. Group related sentences into paragraphs based on content and context.
3. Write each paragraph as continuous flowing text (all sentences on the same line, no line breaks within the paragraph).
4. Separate paragraphs with a single blank line.
5. Do NOT put single line breaks between sentences - that's the error you're fixing!
6. Do NOT create one giant paragraph from everything - break into logical paragraph units.

Examples of proper paragraph formatting:
GOOD: Continuous paragraph text.

      New paragraph after blank line.

BAD:  Sentence one.
      Sentence two.
      (Single line breaks between sentences = wrong!)

BAD:  Everything in one giant paragraph with no breaks at all for very long text.

Output rules:
- Return ONLY the corrected text, in the same order as given.
- When given multiple fixes to make:
  - Separate each fix with the delimiter: ---FIX---
  - Do NOT include the delimiter if there's only one fix.
- Do NOT include any numbering, labels, explanations, or extra text.
"""

SYSTEM_FIXER = """You are a meticulous copy editor for long-form English fiction.
You will be given:
- One chapter of a story, as plain text.
- A list of validation issues (line numbers and messages), which may include
  some false positives.

Your job:
- Fix genuine issues while preserving the original style, tone, voice, and
  meaning as much as possible.
- Replace or remove any non-English, corrupted, or obviously wrong characters.
- Fix unbalanced quotes and parentheses.
- Fix repeated words like "the the" when they are clearly mistakes.
- Add missing sentence-ending punctuation where appropriate, but do NOT add
  punctuation in places where a fragment is clearly intentional (e.g., short
  interjections, dialogue tags, or stylistic fragments).
- Remove any Markdown formatting such as:
  - Bold: **text**
  - Italic: *text*
  - Headings: lines starting with one or more '#' characters
  Convert these to plain prose without Markdown markers.
- Ensure sensible line breaks:
  - Do not return the entire chapter as a single very long line.
  - Use line breaks to keep lines at a reasonable length.
  - Keep logical paragraph breaks, and insert blank lines between paragraphs
    where this makes the text more readable.
  - Do not arbitrarily split a single dialogue line or sentence across
    multiple lines in the middle of a word or sentence.
- Chapter structure:
  - Each chapter should start with a heading like: "Chapter N: Subtitle".
  - If there is an additional story title heading before that, remove it.
- Be conservative:
  - The validation report may contain false positives. Only change something
    if it is clearly wrong or clearly an improvement in correctness or
    readability.
  - Do NOT add new scenes, remove existing scenes, or change plot events.

Output rules:
- Return ONLY the corrected chapter text, as plain prose.
- Do NOT include any explanations, comments, or additional metadata.
"""


DEFAULT_USER_WRITE_TEMPLATE = """Write chapter {chapter_count} of the story "{title}".
Use the provided chapter outline, and keep the new chapter consistent with
the previous chapter summary and recent context in tone, style, voice, and canon.

If something in the outline conflicts with what already happened, resolve it
in a way that feels natural and believable.

# Chapter outline
{chapter_outline}

# Previous chapter summary
{previous_summary}

# Recent context (last ~{last_n_words} words of the previous chapter)
{recent_context}
"""

SUMMARY_SYSTEM_WRITER = """You are a meticulous story summarizer for long-form adult fiction.
Your task is to create concise, structured summaries that track the story's progress.
Focus on key developments that affect the plot, character relationships, and story status.
The goal is that the summary can be used to give context when writing the next story chapter.

"""

DEFAULT_SUMMARY_USER_TEMPLATE = """Create a summary of chapter {chapter_count} of the story "{title}".

Requirements:
- Current status: What is the main character's current situation?
- Relationship status: Key relationships and their current state
- Key plot point reminder: Important events or decisions that affect future chapters

Keep the summary to {max_words} words or less.

# Chapter content
{chapter_content}

# Previous chapter summary (if any)
{previous_summary}
"""


def set_default_system_prompt(project: Path) -> None:
    sp_path = project / ".story" / SYSTEM_PROMPT_FILENAME
    if sp_path.exists():
        print("WARNING: System prompt already exists; not overwriting.")
        return
    write_text(sp_path, SYSTEM_WRITER)


def set_default_user_prompt(project: Path) -> None:
    up_path = project / ".story" / USER_PROMPT_FILENAME
    if up_path.exists():
        print("WARNING: User prompt already exists; not overwriting.")
        return
    write_text(up_path, DEFAULT_USER_WRITE_TEMPLATE)


def set_default_summary_system_prompt(project: Path) -> None:
    ssp_path = project / ".story" / SUMMARY_SYSTEM_PROMPT_FILENAME
    if ssp_path.exists():
        print("WARNING: Summary system prompt already exists; not overwriting.")
        return
    write_text(ssp_path, SUMMARY_SYSTEM_WRITER)


def set_default_summary_user_prompt(project: Path) -> None:
    sup_path = project / ".story" / SUMMARY_USER_PROMPT_FILENAME
    if sup_path.exists():
        print("WARNING: Summary user prompt already exists; not overwriting.")
        return
    write_text(sup_path, DEFAULT_SUMMARY_USER_TEMPLATE)


def load_system_prompt(project: Path) -> str:
    sp_path = project / ".story" / SYSTEM_PROMPT_FILENAME
    system_prompt = read_text(sp_path).strip()
    if not system_prompt:
        print(
            "ERROR: System prompt is empty.\n"
            "Set a new system prompt with:\n"
            "  auto_writer --system-prompt=<path>\n"
            "or restore to default with:\n"
            "  auto_writer --system-prompt=default",
            file=sys.stderr,
        )
        sys.exit(1)
    return system_prompt


def load_user_prompt(project: Path) -> str:
    up_path = project / ".story" / USER_PROMPT_FILENAME
    user_prompt = read_text(up_path).strip()
    if not user_prompt:
        print(
            "ERROR: User prompt is empty.\n"
            "Set a new user prompt with:\n"
            "  auto_writer --user-prompt=<path>\n"
            "or restore to default with:\n"
            "  auto_writer --user-prompt=default",
            file=sys.stderr,
        )
        sys.exit(1)
    return user_prompt


def load_summary_system_prompt(project: Path) -> str:
    ssp_path = project / ".story" / SUMMARY_SYSTEM_PROMPT_FILENAME
    summary_system_prompt = read_text(ssp_path).strip()
    if not summary_system_prompt:
        print(
            "ERROR: Summary system prompt is empty.\n"
            "Set a new summary system prompt with:\n"
            "  auto_writer --summary-system-prompt=<path>\n"
            "or restore to default with:\n"
            "  auto_writer --summary-system-prompt=default",
            file=sys.stderr,
        )
        sys.exit(1)
    return summary_system_prompt


def load_summary_user_prompt(project: Path) -> str:
    sup_path = project / ".story" / SUMMARY_USER_PROMPT_FILENAME
    summary_user_prompt = read_text(sup_path).strip()
    if not summary_user_prompt:
        print(
            "ERROR: Summary user prompt is empty.\n"
            "Set a new summary user prompt with:\n"
            "  auto_writer --summary-user-prompt=<path>\n"
            "or restore to default with:\n"
            "  auto_writer --summary-user-prompt=default",
            file=sys.stderr,
        )
        sys.exit(1)
    return summary_user_prompt


class _SafeFormatDict(dict):
    """
    Dict that leaves unknown {placeholders} intact when using str.format_map.
    """

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def format_template(template: str, **values: Any) -> str:
    return template.format_map(_SafeFormatDict(values))


# ============================== Outline parsing ==============================

_CHAPTER_H_RE = re.compile(
    r"(?i)^\s*#*\s*\**\s*Chapter\s+([A-Za-z0-9\-]+)\s*(?:[-:]\s*(.*?)\s*(?:\(revised\))?\**)?\s*$",
    flags=re.IGNORECASE,
)


def parse_master_outline(master_md: str) -> Dict[int, str]:
    """
    Parse chapter_outline.md into {chapter_number: brief_markdown}.
    Content for each chapter is all lines until the next '# Chapter ...' heading.
    Chapter number can be digits or basic English words.
    """
    lines = master_md.splitlines()
    chapters: Dict[int, str] = {}
    cur_num: Optional[int] = None
    cur_buf: List[str] = []

    def flush() -> None:
        nonlocal cur_num, cur_buf
        if cur_num is not None:
            brief = "\n".join(cur_buf).strip()
            chapters[cur_num] = (brief + "\n") if brief else "\n"

    for ln in lines:
        m = _CHAPTER_H_RE.match(ln)
        if m:
            # new chapter header
            flush()
            token = (m.group(1) or "").strip()
            title_tail = (m.group(2) or "").strip()
            # prefer digits
            if token.isdigit():
                num: Optional[int] = int(token)
            else:
                try:
                    num = int(token)
                except ValueError:
                    num = words_to_int(token)
            if num is None:
                raise ValueError(
                    f"Cannot parse chapter number from header: {ln!r}, token={token!r}"
                )
            cur_num = num
            cur_buf = []
            # Keep a simple first line for the outline
            if title_tail:
                cur_buf.append(f"Chapter {cur_num}: {title_tail}")
            else:
                cur_buf.append(f"Chapter {cur_num}")
        else:
            cur_buf.append(ln)
    flush()
    return chapters


# ============================== Core helpers ==============================


def detect_project_dir(path_arg: Optional[str]) -> Path:
    if path_arg:
        return Path(path_arg).resolve()
    cwd = Path.cwd()
    if (cwd / ".story").exists():
        return cwd
    return cwd


def load_profile(project: Path) -> Dict[str, Any]:
    profile_path = project / ".story" / PROFILE_FILENAME
    if not profile_path.exists():
        print(
            "ERROR: No profile set.\n"
            "Set a profile with:\n"
            "  auto_writer --profile=<path>",
            file=sys.stderr,
        )
        sys.exit(1)

    prof = load_json(profile_path, {})
    if not prof:
        print(
            "ERROR: Profile is empty or invalid.\n"
            "Set a new profile with:\n"
            "  auto_writer --profile=<path>",
            file=sys.stderr,
        )
        sys.exit(1)

    api_base = prof.get("api_base") or os.getenv("API_BASE")
    model = prof.get("model") or os.getenv("MODEL")
    api_key_env = prof.get("api_key_env", "API_KEY")
    api_key = os.getenv(api_key_env) or os.getenv("API_KEY")

    if not (api_base and model and api_key):
        print(
            "ERROR: API config missing from profile or environment.\n"
            "Profile should include at least:\n"
            '  {"api_base": "...", "api_key_env": "API_KEY", "model": "..."}\n'
            f"(Profile path: {profile_path})",
            file=sys.stderr,
        )
        sys.exit(1)

    parameters = {
        k: v
        for k, v in prof.items()
        if k not in {"api_base", "api_key_env", "api_key", "model"}
    }

    return {
        "api_base": api_base,
        "api_key": api_key,
        "model": model,
        "parameters": parameters,
    }


def _truncate_to_word_limit_with_sentence_boundary(text: str, max_words: int, from_end: bool = False) -> str:
    """
    Truncate text to approximately max_words words, but ensure we end/start at a sentence boundary.

    Args:
        text: The text to truncate
        max_words: Maximum number of words to include
        from_end: If True, take the last max_words words; if False, take the first max_words words
    """
    words = text.split()
    if len(words) <= max_words:
        return text

    if from_end:
        # For extracting from the end: split into sentences, then take the last sentences
        # that fit within max_words
        import re
        sentences = re.split(r'(?<=[.!?…]) +', text.strip())

        # Start from the end and accumulate sentences until we hit the word limit
        selected_sentences = []
        word_count = 0

        for sentence in reversed(sentences):
            sentence_words = sentence.split()
            if word_count + len(sentence_words) <= max_words:
                selected_sentences.insert(0, sentence)
                word_count += len(sentence_words)
            else:
                break

        if selected_sentences:
            return " ".join(selected_sentences)

        # Fallback: just take the last max_words words
        return " ".join(words[-max_words:])
    else:
        # Take the first max_words words (original behavior)
        truncated_words = words[:max_words]
        truncated_text = " ".join(truncated_words)

        # Find the last sentence-ending punctuation within our truncated text
        sentence_endings = [".", "!", "?", "...", "…"]
        last_sentence_end = -1

        for ending in sentence_endings:
            pos = truncated_text.rfind(ending)
            if pos > last_sentence_end:
                last_sentence_end = pos

        if last_sentence_end > 0:
            # Include the sentence-ending punctuation
            return truncated_text[:last_sentence_end + 1]

        # If no sentence boundary found, just return the word-limited text
        return truncated_text + "..."


def build_previous_chapters_block(
    *,
    story_dir: Path,
    current_chapter: int,
    max_previous: int,
    max_words: int,
) -> tuple[str, int]:
    """
    Return (markdown_block, num_prev_chapters) for the last N previous chapters,
    limited to approximately max_words words with sentence boundary awareness.
    """
    all_story_files = [
        p
        for p in list_chapter_files(story_dir)
        if chapter_num_from_name(p.name) < current_chapter
    ]
    if not all_story_files:
        return "(No previous chapters; this is the first one.)", 0

    all_story_files.sort(key=lambda p: chapter_num_from_name(p.name))

    # If max_previous is 0, use word-based limiting from the most recent chapter
    if max_previous == 0:
        chosen = all_story_files[-1:] if all_story_files else []
    else:
        chosen = all_story_files[-max_previous:]

    blocks: List[str] = []
    for p in chosen:
        n = chapter_num_from_name(p.name)
        text = read_text(p).strip()
        # only add chapter title if text doesn't already contain it
        if not re.match(r"^\s*#*\s*Chapter\s+", text, flags=re.IGNORECASE):
            blocks.append(f"# Chapter {n}\n\n")
        blocks.append(text)

    block_md = "\n\n\n---\n\n\n".join(blocks)

    # Apply word limit with sentence boundary awareness if max_words > 0
    if max_words > 0 and len(block_md.split()) > max_words:
        # When using word-based limiting (max_previous == 0), take from the end
        from_end = (max_previous == 0)
        block_md = _truncate_to_word_limit_with_sentence_boundary(block_md, max_words, from_end=from_end)

    return block_md, len(chosen)




def build_validation_summary(errors: List[Tuple[int, str]]) -> str:
    return "\n".join(f"line {lineno}: {msg}" for lineno, msg in errors)


def _should_group_issues_together(issues1: List[str], issues2: List[str]) -> bool:
    """
    Determine if two consecutive lines with issues should be grouped into a block.

    Returns True if the issues are compatible for block-based fixing.
    """
    all_issues = issues1 + issues2

    # Auto-wrapped text should always be grouped
    for issue in all_issues:
        if "auto-wrapped" in issue.lower() or "inconsistent line breaks" in issue.lower():
            return True

    # "Line too long" issues can be grouped (may be same paragraph)
    if any("line too long" in issue.lower() for issue in issues1) and any(
        "line too long" in issue.lower() for issue in issues2
    ):
        return True

    # Otherwise, don't group (keep as separate single-line fixes)
    return False


def group_fixes_into_blocks(
    errors: List[Tuple[int, str]], lines: List[str]
) -> List[Tuple[range, List[str], str]]:
    """
    Group consecutive problematic lines into fix blocks.

    Args:
        errors: List of (line_number, error_message) tuples
        lines: All lines from the file (with newlines)

    Returns:
        List of (line_range, issue_messages, original_text_block) tuples
        where line_range is a range object (e.g., range(42, 45) for lines 42-44).
    """
    # Build issues by line number
    issues_by_line: Dict[int, List[str]] = {}
    for lineno, msg in errors:
        issues_by_line.setdefault(lineno, []).append(msg)

    # Get sorted line numbers
    sorted_lines = sorted(issues_by_line.keys())
    if not sorted_lines:
        return []

    blocks: List[Tuple[range, List[str], str]] = []
    i = 0

    while i < len(sorted_lines):
        start = sorted_lines[i]
        end = start

        # Check if next lines are consecutive AND have compatible issues
        while i + 1 < len(sorted_lines) and sorted_lines[i + 1] == end + 1:
            # Check issue compatibility
            if _should_group_issues_together(
                issues_by_line[end], issues_by_line[sorted_lines[i + 1]]
            ):
                end = sorted_lines[i + 1]
                i += 1
            else:
                break

        # Create block for this range
        line_range = range(start, end + 1)
        all_issues: List[str] = []
        text_parts: List[str] = []

        for line_num in line_range:
            all_issues.extend(issues_by_line[line_num])
            text_parts.append(lines[line_num - 1].rstrip("\n"))

        original_text = "\n".join(text_parts)
        blocks.append((line_range, all_issues, original_text))
        i += 1

    return blocks


def run_validation_and_maybe_fix(
    *,
    chapter_path: Path,
    ch_num: int,
    profile: Dict[str, Any],
    logs_dir: Path,
    fix_mode: str = "quick",
) -> None:
    """
    Run validation on the chapter file. If any issues are found, either:
    - 'quick': send only the offending lines to the API and replace them, or
    - 'full': send the whole chapter and overwrite it with the corrected version.
    """
    errors = vc.validate_file(chapter_path, True)
    if not errors:
        print(f"No validation issues found for chapter {ch_num}.")
        return

    print(f"Validation found {len(errors)} potential issue(s) in chapter {ch_num}.")
    for lineno, msg in errors[:10]:
        print(f"  - line {lineno}: {msg}")
    if len(errors) > 10:
        print(f"  ... and {len(errors) - 10} more.")

    api_base = profile["api_base"]
    api_key = profile["api_key"]
    model = profile["model"]
    parameters = profile["parameters"]

    text = read_text(chapter_path)
    lines = text.splitlines(keepends=True)

    # Fil-breda / strukturella fel, t.ex. "Too few lines", har vi ingen
    # vettig quick-fix för. De hanteras bäst i full-läget.
    structural_prefixes = ("Too few lines",)
    has_structural_issues = any(
        any(msg.startswith(pref) for pref in structural_prefixes) for _, msg in errors
    )

    if fix_mode == "quick" and not has_structural_issues:
        # Group consecutive problematic lines into fix blocks
        blocks_to_fix = group_fixes_into_blocks(errors, lines)

        if not blocks_to_fix:
            print(
                "No line-local issues suitable for quick fix. "
                "Consider using --fix-mode full for structural issues."
            )
            return

        # Build user-prompt with all blocks and their issues
        lines_desc_parts: list[str] = []
        for idx, (line_range, issue_msgs, original_block) in enumerate(blocks_to_fix, start=1):
            # Format the line range description
            if len(line_range) == 1:
                desc = f"line {line_range.start}"
            else:
                desc = f"lines {line_range.start}-{line_range.stop - 1}"

            # Deduplicate issue messages (since we report same issue on each line in a block)
            unique_issues = list(dict.fromkeys(issue_msgs))
            issue_str = "; ".join(unique_issues)

            lines_desc_parts.append(
                f"[{idx}] {desc}: issues: {issue_str}\n" f"Original:\n{original_block}"
            )
        lines_desc = "\n\n".join(lines_desc_parts)

        # Check if we have multi-line blocks that need paragraph organization
        has_multi_line = any(len(r) > 1 for r, _, _ in blocks_to_fix)

        if has_multi_line:
            user_content = (
                "You will receive one or more text blocks from a story.\n"
                "For blocks with multiple lines, organize them into proper paragraphs:\n"
                "- Group related sentences by topic/idea into paragraphs\n"
                "- Write each paragraph as continuous text (no line breaks within it)\n"
                "- Separate paragraphs with blank lines\n"
                "- Do NOT put single line breaks between individual sentences!\n\n"
                "If there are multiple blocks, separate each fix with: ---FIX---\n"
                "Return the fixes in the same order as given.\n\n"
                "Blocks:\n"
                f"{lines_desc}"
            )
        else:
            user_content = (
                "You will receive one or more lines from a story.\n"
                "For each line, output the corrected version.\n"
                "If there are multiple lines, separate each fix with: ---FIX---\n"
                "Return the fixes in the same order as given.\n\n"
                "Lines:\n"
                f"{lines_desc}"
            )

        fix_messages = [
            {"role": "system", "content": SYSTEM_LINE_FIXER},
            {"role": "user", "content": user_content},
        ]

        ensure_dir(logs_dir)
        prompt_path = logs_dir / f"ch{ch_num:02d}.qfix.prompt.md"
        write_text(
            prompt_path,
            "System Prompt (quick fixer):\n"
            + SYSTEM_LINE_FIXER
            + "\n\nUser Message:\n"
            + user_content
            + "\n",
        )

        print("Sending quick-fix request to API...")
        t_start = dt.datetime.now()
        fixed_block = chat_completion(
            api_base=api_base,
            api_key=api_key,
            model=model,
            messages=fix_messages,
            parameters=parameters,
        )
        t_end = dt.datetime.now()
        print(
            f"Quick-fix API call took {(t_end - t_start).total_seconds():.1f} seconds."
        )

        # Split response by delimiter (if multiple blocks)
        if len(blocks_to_fix) > 1:
            fixes = fixed_block.split("---FIX---")
            fixes = [f.strip() for f in fixes]
        else:
            fixes = [fixed_block.strip()]

        if len(fixes) != len(blocks_to_fix):
            print(
                f"WARNING: Expected {len(blocks_to_fix)} fix(es) but got {len(fixes)}; "
                "skipping automatic application of fixes."
            )
            return

        # Build new file by replacing line ranges with fixed blocks
        new_lines = []
        last_processed = 0  # Track last line we've processed (0-indexed)

        for (line_range, _, _), fix_text in zip(blocks_to_fix, fixes):
            # Add unmodified lines before this block
            new_lines.extend(lines[last_processed : line_range.start - 1])

            # Convert fix_text to lines (split on newlines)
            fix_lines_content = fix_text.split("\n")

            # Add newlines to match original format
            fix_lines = [line + "\n" for line in fix_lines_content]
            new_lines.extend(fix_lines)

            # Update pointer to end of this block
            last_processed = line_range.stop - 1

        # Add any remaining unmodified lines
        new_lines.extend(lines[last_processed:])

        new_text = "".join(new_lines)
        write_text(chapter_path, new_text)
        print(f"Applied {len(blocks_to_fix)} block-level fix(es) to chapter {ch_num}.")

        # Determine if we used block-based fixing (multi-line blocks)
        has_multi_line_blocks = any(len(block_range) > 1 for block_range, _, _ in blocks_to_fix)

        fixlog = {
            "ts": now_iso(),
            "chapter": ch_num,
            "model": model,
            "api_base": api_base,
            "bytes": len(fixed_block),
            "issues": len(errors),
            "mode": "quick",
            "blocks_count": len(blocks_to_fix),
            "fix_mode": "block" if has_multi_line_blocks else "line",
        }
        save_json(logs_dir / f"ch{ch_num:02d}.qfix.request.json", fixlog)
        return

    # ----- full-läge: nuvarande beteende -----

    validation_summary = build_validation_summary(errors)
    original_text = text

    fix_messages = [
        {"role": "system", "content": SYSTEM_FIXER},
        {
            "role": "user",
            "content": (
                "Here is the chapter text and the validation report.\n\n"
                "Validation report:\n"
                f"{validation_summary}\n\n"
                "Chapter text:\n"
                f"{original_text}"
            ),
        },
    ]

    ensure_dir(logs_dir)
    fix_prompt_path = logs_dir / f"ch{ch_num:02d}.fix.prompt.md"
    write_text(
        fix_prompt_path,
        "System Prompt (fixer):\n"
        + SYSTEM_FIXER
        + "\n\nUser Message:\n"
        + fix_messages[1]["content"]
        + "\n",
    )

    print("Sending full-fix request to API...")
    t_start = dt.datetime.now()
    fixed_md = chat_completion(
        api_base=api_base,
        api_key=api_key,
        model=model,
        messages=fix_messages,
        parameters=parameters,
    )
    t_end = dt.datetime.now()
    print(f"Full-fix API call took {(t_end - t_start).total_seconds():.1f} seconds.")
    cleaned = fixed_md.lstrip("#").strip() + "\n"
    write_text(chapter_path, cleaned)
    print(f"Applied full-chapter fixes to chapter {ch_num}.")

    fixlog = {
        "ts": now_iso(),
        "chapter": ch_num,
        "model": model,
        "api_base": api_base,
        "bytes": len(fixed_md),
        "issues": len(errors),
        "mode": "full",
    }
    save_json(logs_dir / f"ch{ch_num:02d}.fix.request.json", fixlog)


# ============================== Commands ==============================


def cmd_init(args: argparse.Namespace) -> None:
    project = detect_project_dir(args.path)

    # check if already initialized
    if (project / ".story" / "config.json").exists():
        print(f"ERROR: Story repo already initialized at: {project}", file=sys.stderr)
        sys.exit(1)

    ensure_dir(project)
    ensure_dir(project / ".story")
    for d in ("outline", "story", "logs", "summary"):
        ensure_dir(project / d)

    # write config
    cfg_path = project / ".story" / "config.json"
    cfg: Dict[str, Any] = {
        "title": args.title or "Untitled Story",
        "created": now_iso(),
        "version": APP_VERSION,
        "paths": {
            "outline_master": "chapter_outline.md",
            "outline_dir": "outline",
            "story_dir": "story",
            "logs_dir": "logs",
            "summary_dir": "summary",
        },
        "context": {
            "mode": "last_n_chapters",
            "last_n_chapters": 0,
            "last_n_words": 300,
        },
        "summary": {
            "max_words": 300,
        },
    }
    save_json(cfg_path, cfg)

    # create skeleton files if missing
    master = project / "chapter_outline.md"
    if not master.exists():
        write_text(
            master,
            "# Chapter 1 - Opening\n"
            "* Inciting incident\n"
            "* First turn\n\n"
            "# Chapter 2 - Complication\n"
            "* Complication\n"
            "* Reaction\n",
        )

    characters = project / "characters.md"
    if not characters.exists():
        write_text(
            characters,
            "\n"
            "# Character Profiles\n"
            "\n"
            "Define your characters here for consistent image generation.\n"
            "Use physical descriptions only - SDXL doesn't understand names.\n"
            "\n"
            "## Example Format\n"
            "\n"
            "### Character Name (for your reference only)\n"
            "- Hair: long, wavy, dark brown\n"
            "- Eyes: green, almond-shaped\n"
            "- Skin: fair, light freckles\n"
            "- Body: slim, average height\n"
            "- Age: mid-20s\n"
            "- Distinguishing features: small scar on left cheek\n"
            "\n"
            "### Another Character\n"
            "- Hair: short, black, curly\n"
            "- Eyes: brown\n"
            "- Body: muscular, tall\n"
            "- Age: early 30s\n"
            "\n"
            "---\n"
            "\n"
            "Add your characters below:\n"
            "\n"
            "\n",
        )

    print(f"Initialized story repo at: {project}")

    # Profile and prompts
    if not args.profile:
        print(
            "WARNING: No profile provided. Set one with\n"
            "  auto_writer --profile <path>."
        )
    if not args.system_prompt:
        print("No system prompt set. Using default.")
        set_default_system_prompt(project)
    if not args.user_prompt:
        print("No user prompt set. Using default.")
        set_default_user_prompt(project)

    # Always set default summary prompts (they're new)
    print("Setting default summary prompts.")
    set_default_summary_system_prompt(project)
    set_default_summary_user_prompt(project)


def cmd_title(project: Path, cfg: Dict[str, Any], new_title: Optional[str]) -> None:
    cfg_path = project / ".story" / "config.json"
    old_title = cfg.get("title", "Untitled Story")
    cfg["title"] = new_title or old_title
    save_json(cfg_path, cfg)
    print(f"Updated title from {old_title!r} to {cfg['title']!r}.")


def cmd_reset(args: argparse.Namespace) -> None:
    project = detect_project_dir(args.path)
    cfg = load_json(project / ".story" / "config.json", {})
    if not cfg:
        print(
            "ERROR: Not a story repo (missing .story/config.json). Run --init first.",
            file=sys.stderr,
        )
        sys.exit(1)

    story_dir = project / cfg["paths"]["story_dir"]
    logs_dir = project / cfg["paths"]["logs_dir"]

    target = (args.reset or "").strip().lower()

    if target == "all":
        # Remove all chapters & logs
        for p in list_chapter_files(story_dir):
            p.unlink()
        for p in logs_dir.glob("ch*.prompt.md"):
            p.unlink()
        for p in logs_dir.glob("ch*.request.json"):
            p.unlink()
        for p in logs_dir.glob("ch*.fix.prompt.md"):
            p.unlink()
        for p in logs_dir.glob("ch*.fix.request.json"):
            p.unlink()
        print("Reset all chapters and logs.")
        return

    try:
        ch_num = int(target)
    except ValueError:
        print(f"ERROR: Invalid chapter number: {args.reset!r}", file=sys.stderr)
        sys.exit(1)

    story_path = story_dir / f"ch{ch_num:02d}.md"
    prompt_log = logs_dir / f"ch{ch_num:02d}.prompt.md"
    req_log = logs_dir / f"ch{ch_num:02d}.request.json"
    fix_prompt_log = logs_dir / f"ch{ch_num:02d}.fix.prompt.md"
    fix_req_log = logs_dir / f"ch{ch_num:02d}.fix.request.json"

    removed_any = False
    for p in (story_path, prompt_log, req_log, fix_prompt_log, fix_req_log):
        if p.exists():
            p.unlink()
            removed_any = True

    if removed_any:
        print(f"Reset chapter {ch_num}.")
    else:
        print(f"No files found for chapter {ch_num}.")


def cmd_prepare(args: argparse.Namespace) -> None:
    project = detect_project_dir(args.path)
    cfg = load_json(project / ".story" / "config.json", {})
    if not cfg:
        print(
            "ERROR: Not a story repo (missing .story/config.json). Run --init first.",
            file=sys.stderr,
        )
        sys.exit(1)

    master_path = project / cfg["paths"]["outline_master"]
    outline_dir = project / cfg["paths"]["outline_dir"]
    ensure_dir(outline_dir)

    chapters = parse_master_outline(read_text(master_path))
    if not chapters:
        print(
            "No chapters found in chapter_outline.md "
            "(check '# Chapter N' headings).",
            file=sys.stderr,
        )
        sys.exit(1)

    # write outline/chNN.md
    for num, brief in sorted(chapters.items(), key=lambda kv: kv[0]):
        write_text(outline_dir / f"ch{num:02d}.md", brief)

    # persist index for quick reference
    index = [{"chapter": n, "file": f"ch{n:02d}.md"} for n in sorted(chapters)]
    save_json(project / ".story" / "outline_index.json", index)
    print(f"Prepared {len(index)} outline files in {outline_dir}")


# --------- Writing helpers (single chapter and next unwritten) ---------


def _generate_chapter_summary(
    *,
    project: Path,
    cfg: Dict[str, Any],
    profile: Dict[str, Any],
    story_dir: Path,
    summary_dir: Path,
    logs_dir: Path,
    ch_num: int,
) -> None:
    """Generate and save a summary for the specified chapter."""
    story_path = story_dir / f"ch{ch_num:02d}.md"
    if not story_path.exists():
        print(f"ERROR: Chapter {ch_num} does not exist for summarization", file=sys.stderr)
        return

    summary_path = summary_dir / f"ch{ch_num:02d}.md"
    if summary_path.exists():
        print(f"Summary for chapter {ch_num} already exists.")
        return

    # Get chapter content
    chapter_content = read_text(story_path).strip()

    # Get previous summary if it exists
    prev_summary = ""
    if ch_num > 1:
        prev_summary_path = summary_dir / f"ch{ch_num-1:02d}.md"
        if prev_summary_path.exists():
            prev_summary = read_text(prev_summary_path).strip()

    # Load summary prompts
    summary_system_template = load_summary_system_prompt(project)
    summary_user_template = load_summary_user_prompt(project)

    # Get max words from config (default to 300)
    max_words = cfg.get("summary", {}).get("max_words", 300)

    title = cfg.get("title", "Untitled Story")
    context = {
        "title": title,
        "chapter_count": ch_num,
        "chapter_content": chapter_content,
        "previous_summary": prev_summary,
        "max_words": max_words,
    }

    summary_system_prompt = format_template(summary_system_template, **context)
    summary_user_prompt = format_template(summary_user_template, **context)

    messages = [
        {"role": "system", "content": summary_system_prompt},
        {"role": "user", "content": summary_user_prompt},
    ]

    # Log the summary prompt
    write_text(
        logs_dir / f"ch{ch_num:02d}.summary.prompt.md",
        "Summary System Prompt:\n"
        + summary_system_prompt
        + "\n\nSummary User Prompt:\n"
        + summary_user_prompt
        + "\n",
    )

    print(f"Generating summary for chapter {ch_num}...")

    # Generate summary
    api_base = profile["api_base"]
    api_key = profile["api_key"]
    model = profile["model"]
    parameters = profile["parameters"]

    t_start = dt.datetime.now()
    summary_text = chat_completion(
        api_base=api_base,
        api_key=api_key,
        model=model,
        messages=messages,
        parameters=parameters,
    )
    t_end = dt.datetime.now()

    print(f"Summary API call took {(t_end - t_start).total_seconds():.1f} seconds.")

    # Save summary
    write_text(summary_path, summary_text.strip())
    print(f"Summary saved to {summary_path}")

    # Log metadata
    reqlog = {
        "ts": now_iso(),
        "chapter": ch_num,
        "model": model,
        "api_base": api_base,
        "bytes": len(summary_text),
        "type": "summary",
    }
    save_json(logs_dir / f"ch{ch_num:02d}.summary.request.json", reqlog)


def _write_single_chapter(
    *,
    project: Path,
    cfg: Dict[str, Any],
    profile: Dict[str, Any],
    outline_dir: Path,
    story_dir: Path,
    summary_dir: Path,
    logs_dir: Path,
    ch_num: int,
    fix_mode: str,
) -> bool:
    outline_path = outline_dir / f"ch{ch_num:02d}.md"
    if not outline_path.exists():
        print(f"ERROR: Outline not found: {outline_path}", file=sys.stderr)
        return False

    chapter_outline = read_text(outline_path).strip()
    story_path = story_dir / outline_path.name

    print(f"Writing chapter {ch_num}")

    if story_path.exists():
        # Already written chapter – we still run validation/fix on it
        print(f"Chapter {ch_num} already written at: {story_path}")
    else:
        # Check for previous chapter and ensure it has a summary
        prev_chapter_num = ch_num - 1
        previous_summary = ""
        if prev_chapter_num > 0:
            prev_summary_path = summary_dir / f"ch{prev_chapter_num:02d}.md"
            if not prev_summary_path.exists():
                # Previous chapter exists but no summary - generate it first
                prev_story_path = story_dir / f"ch{prev_chapter_num:02d}.md"
                if prev_story_path.exists():
                    print(f"Previous chapter {prev_chapter_num} exists but has no summary. Generating summary first...")
                    _generate_chapter_summary(
                        project=project,
                        cfg=cfg,
                        profile=profile,
                        story_dir=story_dir,
                        summary_dir=summary_dir,
                        logs_dir=logs_dir,
                        ch_num=prev_chapter_num,
                    )
            # Load the summary (either just generated or pre-existing)
            if prev_summary_path.exists():
                previous_summary = read_text(prev_summary_path).strip()

        # Compute recent context (word-limited)
        context_cfg = cfg.get("context", {})
        last_n_words = int(context_cfg.get("last_n_words", 300))
        recent_context, _ = build_previous_chapters_block(
            story_dir=story_dir,
            current_chapter=ch_num,
            max_previous=0,  # Always use most recent chapter
            max_words=last_n_words,
        )

        # Build prompts
        system_template = load_system_prompt(project)
        user_template = load_user_prompt(project)

        title = cfg.get("title", "Untitled Story")
        context = {
            "title": title,
            "chapter_count": ch_num,
            "chapter_outline": chapter_outline or "(empty)",
            "previous_summary": previous_summary or "(No previous chapter)",
            "recent_context": recent_context,
            "last_n_words": last_n_words,
        }

        system_prompt = format_template(system_template, **context)
        user_prompt = format_template(user_template, **context)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # store entire prompt for reference/debugging
        print("Logging full prompt")
        write_text(
            logs_dir / f"ch{ch_num:02d}.prompt.md",
            "System Prompt:\n"
            + system_prompt
            + "\n\nUser Prompt:\n"
            + user_prompt
            + "\n",
        )

        # write chapter
        print("Sending write request to API...")
        api_base = profile["api_base"]
        api_key = profile["api_key"]
        model = profile["model"]
        parameters = profile["parameters"]

        t_start = dt.datetime.now()
        chapter_md = chat_completion(
            api_base=api_base,
            api_key=api_key,
            model=model,
            messages=messages,
            parameters=parameters,
        )
        t_end = dt.datetime.now()
        print(f"API call took {(t_end - t_start).total_seconds():.1f} seconds.")
        # remove leading '#' from chapter title if any
        cleaned = chapter_md.lstrip("#").strip() + "\n"
        write_text(story_path, cleaned)
        print(f"Received {len(chapter_md)} bytes from API.")

        # log (lightweight, omit full prompt for brevity)
        reqlog = {
            "ts": now_iso(),
            "chapter": ch_num,
            "model": model,
            "api_base": api_base,
            "bytes": len(chapter_md),
        }
        save_json(logs_dir / f"ch{ch_num:02d}.request.json", reqlog)

    print(f"Write complete for chapter {ch_num}.")

    # call the wc command if available and print lines, words and characters
    try:
        import subprocess

        wc_out = subprocess.check_output(["wc", story_path]).decode().strip()
        parts = wc_out.split()
        if len(parts) >= 4:
            lines_s, words_s, chars_s = parts[0], parts[1], parts[2]
            print("Chapter length:")
            print(f"  {lines_s} lines")
            print(f"  {words_s} words")
            print(f"  {chars_s} characters")
    except Exception:
        pass

    # Always validate and possibly auto-fix after a write command
    run_validation_and_maybe_fix(
        chapter_path=story_path,
        ch_num=ch_num,
        profile=profile,
        logs_dir=logs_dir,
        fix_mode=fix_mode,
    )

    # Generate chapter summary
    _generate_chapter_summary(
        project=project,
        cfg=cfg,
        profile=profile,
        story_dir=story_dir,
        summary_dir=summary_dir,
        logs_dir=logs_dir,
        ch_num=ch_num,
    )

    return True


def cmd_write(args: argparse.Namespace) -> None:
    project = detect_project_dir(args.path)
    cfg = load_json(project / ".story" / "config.json", {})
    if not cfg:
        print(
            "ERROR: Not a story repo (missing .story/config.json). Run --init first.",
            file=sys.stderr,
        )
        sys.exit(1)

    outline_dir = project / cfg["paths"]["outline_dir"]
    story_dir = project / cfg["paths"]["story_dir"]
    summary_dir = project / cfg["paths"]["summary_dir"]
    logs_dir = project / cfg["paths"]["logs_dir"]
    for d in (outline_dir, story_dir, summary_dir, logs_dir):
        ensure_dir(d)

    profile = load_profile(project)

    # Hämta alla kapitel som finns i outline/
    outline_files = list_chapter_files(outline_dir)
    if not outline_files:
        print("No outline chapters found in", outline_dir, file=sys.stderr)
        return

    # Sortera kapitelnummer från outlines
    outline_nums = sorted(chapter_num_from_name(p.name) for p in outline_files)

    # Bestäm startkapitel:
    if args.chapter is not None:
        start_ch = int(args.chapter)
        if start_ch not in outline_nums:
            print(
                f"ERROR: Outline for chapter {start_ch} not found in {outline_dir}",
                file=sys.stderr,
            )
            return
    else:
        # Hitta första outline där story saknas
        start_ch = None
        for p in outline_files:
            if not (story_dir / p.name).exists():
                start_ch = chapter_num_from_name(p.name)
                break
        if start_ch is None:
            print("All chapters already written. Nothing to do.")
            return

    # Vilka kapitel ska vi processa?
    if args.loop:
        # Alla kapitel från start_ch och uppåt (enligt outlines)
        chapters_to_process = [n for n in outline_nums if n >= start_ch]
    else:
        # Bara startkapitlet
        chapters_to_process = [start_ch]

    writes_done = 0

    for ch_num in chapters_to_process:
        ok = _write_single_chapter(
            project=project,
            cfg=cfg,
            profile=profile,
            outline_dir=outline_dir,
            story_dir=story_dir,
            summary_dir=summary_dir,
            logs_dir=logs_dir,
            ch_num=ch_num,
            fix_mode=args.fix_mode,
        )
        if not ok:
            break

        writes_done += 1
        if args.max_writes is not None and writes_done >= args.max_writes:
            break

    print(f"Performed {writes_done} write operation(s).")


def cmd_profile(project: Path, provided_path: str) -> None:
    prof_path = project / ".story" / PROFILE_FILENAME
    already_exists = prof_path.exists()

    if provided_path == "default":
        print("ERROR: No default profile is defined in this script.", file=sys.stderr)
        sys.exit(1)

    source_profile_path = Path(provided_path)
    if source_profile_path.exists():
        write_text(prof_path, read_text(source_profile_path))
    else:
        print(f"ERROR: Provided profile path not found:\n  {source_profile_path}")
        sys.exit(1)

    if already_exists:
        print("Replaced existing profile.")
    else:
        print("Created new profile.")


def cmd_system_prompt(project: Path, provided_path: str) -> None:
    sp_path = project / ".story" / SYSTEM_PROMPT_FILENAME
    already_exists = sp_path.exists()

    if provided_path == "default":
        set_default_system_prompt(project)
    else:
        source_sp_path = Path(provided_path)
        if source_sp_path.exists():
            write_text(sp_path, read_text(source_sp_path))
        else:
            print(
                f"ERROR: Provided system prompt path not found:\n  {source_sp_path}",
                file=sys.stderr,
            )
            sys.exit(1)

    if already_exists:
        print("Replaced existing system prompt.")
    else:
        print("Created new system prompt.")


def cmd_user_prompt(project: Path, provided_path: str) -> None:
    up_path = project / ".story" / USER_PROMPT_FILENAME
    already_exists = up_path.exists()

    if provided_path == "default":
        set_default_user_prompt(project)
    else:
        source_up_path = Path(provided_path)
        if source_up_path.exists():
            write_text(up_path, read_text(source_up_path))
        else:
            print(
                f"ERROR: Provided user prompt path not found:\n  {source_up_path}",
                file=sys.stderr,
            )
            sys.exit(1)

    if already_exists:
        print("Replaced existing user prompt.")
    else:
        print("Created new user prompt.")


def cmd_summary_system_prompt(project: Path, provided_path: str) -> None:
    ssp_path = project / ".story" / SUMMARY_SYSTEM_PROMPT_FILENAME
    already_exists = ssp_path.exists()

    if provided_path == "default":
        set_default_summary_system_prompt(project)
    else:
        source_ssp_path = Path(provided_path)
        if source_ssp_path.exists():
            write_text(ssp_path, read_text(source_ssp_path))
        else:
            print(
                f"ERROR: Provided summary system prompt path not found:\n  {source_ssp_path}",
                file=sys.stderr,
            )
            sys.exit(1)

    if already_exists:
        print("Replaced existing summary system prompt.")
    else:
        print("Created new summary system prompt.")


def cmd_summary_user_prompt(project: Path, provided_path: str) -> None:
    sup_path = project / ".story" / SUMMARY_USER_PROMPT_FILENAME
    already_exists = sup_path.exists()

    if provided_path == "default":
        set_default_summary_user_prompt(project)
    else:
        source_sup_path = Path(provided_path)
        if source_sup_path.exists():
            write_text(sup_path, read_text(source_sup_path))
        else:
            print(
                f"ERROR: Provided summary user prompt path not found:\n  {source_sup_path}",
                file=sys.stderr,
            )
            sys.exit(1)

    if already_exists:
        print("Replaced existing summary user prompt.")
    else:
        print("Created new summary user prompt.")


def cmd_summary_max_words(project: Path, cfg: Dict[str, Any], max_words: int) -> None:
    cfg_path = project / ".story" / "config.json"
    if "summary" not in cfg:
        cfg["summary"] = {}
    old_max_words = cfg["summary"].get("max_words", 300)
    cfg["summary"]["max_words"] = max_words
    save_json(cfg_path, cfg)
    print(f"Updated summary max words from {old_max_words} to {max_words}.")


# ============================== CLI ==============================


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Story auto-writer (git-like UX)")
    p.add_argument(
        "--init", dest="init", action="store_true", help="Initialize a story directory"
    )
    p.add_argument(
        "--prepare",
        dest="prepare",
        action="store_true",
        help="Parse chapter_outline.md into outline/chNN.md",
    )
    p.add_argument(
        "--write",
        dest="write",
        action="store_true",
        help="Write next (or specific) chapter",
    )
    p.add_argument("--path", help="Story directory (default: CWD)")
    p.add_argument("--title", help="Required for --init")
    p.add_argument(
        "--chapter",
        type=int,
        help=(
            "Start chapter number for --write. If omitted, writing starts at the next "
            "unwritten chapter."
        ),
    )

    p.add_argument("--system-prompt", help="Path to custom system prompt (for --init)")
    p.add_argument("--user-prompt", help="Path to custom user prompt (for --init)")
    p.add_argument("--summary-system-prompt", help="Path to custom summary system prompt (for --init)")
    p.add_argument("--summary-user-prompt", help="Path to custom summary user prompt (for --init)")
    p.add_argument("--summary-max-words", type=int, help="Maximum words for chapter summaries (default: 300)")
    p.add_argument(
        "--profile",
        help="Optional JSON profile (api_base, api_key_env, model, ...)",
    )
    p.add_argument(
        "--reset",
        help=(
            "Remove a generated chapter, including logs. "
            "Specify chapter number or 'all'."
        ),
    )
    p.add_argument(
        "--loop",
        action="store_true",
        help=(
            "When used with --write (without --chapter), keep writing subsequent "
            "chapters until all are done or --max-writes is reached."
        ),
    )
    p.add_argument(
        "--max-writes",
        type=int,
        help=(
            "Maximum number of write operations to perform in this run. "
            "Only meaningful together with --write."
        ),
    )
    p.add_argument(
        "--fix-mode",
        choices=["quick", "full"],
        default="quick",
        help=(
            "How to apply automatic fixes after validation: "
            "'quick' only rewrites offending lines; "
            "'full' rewrites the whole chapter. Default: quick."
        ),
    )

    return p


def main() -> None:
    args = build_parser().parse_args()

    if args.init:
        if not args.title:
            print("ERROR: --init requires --title", file=sys.stderr)
            sys.exit(1)
        cmd_init(args)
        return

    project = detect_project_dir(args.path)
    cfg_path = project / ".story" / "config.json"
    cfg = load_json(cfg_path, {})
    if not cfg:
        print(
            "ERROR: Not a story repo (missing .story/config.json). Run --init first.",
            file=sys.stderr,
        )
        sys.exit(1)

    did_something = False

    if args.title:
        did_something = True
        cmd_title(project, cfg, args.title)
    if args.profile:
        did_something = True
        cmd_profile(project, args.profile)
    if args.system_prompt:
        did_something = True
        cmd_system_prompt(project, args.system_prompt)
    if args.user_prompt:
        did_something = True
        cmd_user_prompt(project, args.user_prompt)
    if args.summary_system_prompt:
        did_something = True
        cmd_summary_system_prompt(project, args.summary_system_prompt)
    if args.summary_user_prompt:
        did_something = True
        cmd_summary_user_prompt(project, args.summary_user_prompt)
    if args.summary_max_words:
        did_something = True
        cmd_summary_max_words(project, cfg, args.summary_max_words)

    if args.reset:
        did_something = True
        cmd_reset(args)
        return
    if args.prepare:
        did_something = True
        cmd_prepare(args)
        return
    if args.write:
        did_something = True
        cmd_write(args)
        return

    if not did_something:
        print("Nothing to do. Use -h for help.")


if __name__ == "__main__":
    main()
