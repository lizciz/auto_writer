#!/usr/bin/env python
"""
validate_chapter.py

Usage:
    ./validate_chapter.py path/to/story/ch*.md
    ./validate_chapter.py --strip-asterisks path/to/story/ch*.md
"""

import argparse
import glob
import re
import sys
import unicodedata as ud
from pathlib import Path
from typing import List, Optional, Tuple

TITLE_RE = re.compile(r"^Chapter\s+(\d+):\s+(.+)$")
REPEATED_WORD_RE = re.compile(r"\b(\w+)\s+\1\b", re.IGNORECASE)
MIN_EXPECTED_LINES = 20  # configurable threshold
MAX_LINE_LENGTH = 1000  # configurable threshold


# --- Signature / sentence-tail heuristics ------------------------------------

_SIG_DELIMS = r"(?:-|–|—|/)"  # hyphen, en dash, em dash, slash

# Match a short "signature-ish" tail at end of line, e.g. " – Name Lastname"
_SIG_TAIL_RE = re.compile(
    rf"""
    \s*{_SIG_DELIMS}\s+      # delimiter + whitespace
    (?P<tail>.{{1,60}})      # short-ish tail (tune if needed)
    \s*$                     # end of line
    """,
    re.VERBOSE,
)

# Split sentences by end punctuation (incl ellipsis). Keeps it heuristic.
_SENT_END_SPLIT_RE = re.compile(r"[.!?…]+(?:['\"\)\]]+)?\s+")


def is_signature_tail(line: str) -> bool:
    return _SIG_TAIL_RE.search(line.strip()) is not None


def extract_last_sentence_segment(line: str) -> str:
    """
    Return the last sentence-like segment, ignoring any trailing signature tail.
    """
    s = line.strip()

    # Remove signature tail if present
    s_wo_sig = _SIG_TAIL_RE.sub("", s).rstrip()
    base = s_wo_sig if s_wo_sig else s

    parts = [p.strip() for p in _SENT_END_SPLIT_RE.split(base) if p.strip()]
    return parts[-1] if parts else base


def has_delimiter_short_tail(line: str) -> bool:
    """
    Detect "delimiter + short text" tails that commonly look like signatures
    or attributions, e.g. "— Name", "/ Notes", "- John".
    """
    m = _SIG_TAIL_RE.search(line.strip())
    if not m:
        return False

    tail = m.group("tail").strip()
    # Require at least one letter to avoid false positives like "— ---"
    return any(ch.isalpha() for ch in tail)


# --- Core checks -------------------------------------------------------------


def is_suspicious_char(ch: str) -> bool:
    """Return True if the character looks suspicious for English prose."""
    # Allow newline and tab
    if ch in ("\n", "\t"):
        return False

    code = ord(ch)

    # Control characters other than newline/tab
    if code < 32 or code == 127:
        return True

    # Letters outside A-Z/a-z are considered suspicious (non-English letters)
    cat = ud.category(ch)
    if cat.startswith("L") and ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ":
        return True

    # Everything else (punctuation, digits, etc.) is fine
    return False


def looks_like_sentence(line: str) -> bool:
    """Heuristic: does this line look like a prose sentence?"""
    s = line.strip()
    if not s:
        return False

    # Skip the chapter title line, markdown headings, lists, etc.
    if s.startswith(("Chapter ", "#", "-", "*", ">")):
        return False

    # Skip numbered list items: "1. Something", "1) Something"
    if re.match(r"^\d+[\.\)]\s", s):
        return False

    # Very short lines (e.g. "Yes", "No") – we don't complain about punctuation
    if len(s) < 10:
        return False

    return True


def check_title_lines(path: Path, lines: List[str]) -> List[Tuple[int, str]]:
    errors: List[Tuple[int, str]] = []

    # Find first non-empty line
    first_idx: Optional[int] = None
    first_line = ""
    for i, line in enumerate(lines, start=1):
        if line.strip():
            first_idx = i
            first_line = line.rstrip("\n")
            break

    if first_idx is None:
        errors.append((1, "Missing title (file is empty)"))
        return errors

    m = TITLE_RE.match(first_line)
    if not m:
        errors.append((first_idx, "Wrong title format (expected 'Chapter N: Subtitle')"))
        return errors

    # Optional: cross-check chapter number vs filename like ch01.md
    chap_num = int(m.group(1))
    m_file = re.search(r"ch(\d+)", path.name, re.IGNORECASE)
    if m_file:
        file_num = int(m_file.group(1))
        if file_num != chap_num:
            errors.append(
                (
                    first_idx,
                    f"Chapter number ({chap_num}) does not match filename ({file_num:02d})",
                )
            )

    return errors


def strip_markdown_asterisks(text: str) -> tuple[str, bool]:
    """
    Remove Markdown emphasis markers *like this* and **like this**.
    Returns (new_text, changed_flag).
    """
    changed = False
    new_text = text

    def repl_bold(match: re.Match) -> str:
        nonlocal changed
        changed = True
        return match.group(1)

    new_text = re.sub(r"\*\*(.+?)\*\*", repl_bold, new_text)

    def repl_italic(match: re.Match) -> str:
        nonlocal changed
        changed = True
        return match.group(1)

    new_text = re.sub(r"\*(.+?)\*", repl_italic, new_text)

    return new_text, changed


SENTENCE_ENDERS = (".", "?", "!", "…", "—")
TRAILING_CLOSERS = set("\"'" + "”’" + ")]}»›")


def has_sentence_ending_punct(line: str) -> bool:
    s = line.rstrip()
    # Peel off trailing closers (quotes/brackets/etc.)
    while s and s[-1] in TRAILING_CLOSERS:
        s = s[:-1].rstrip()
    return s.endswith(SENTENCE_ENDERS)


def check_line(line: str, lineno: int) -> list[tuple[int, str]]:
    errors: list[tuple[int, str]] = []
    stripped = line.rstrip("\n")

    # 1. Unbalanced straight double quotes on this line (odd count)
    if stripped.count('"') % 2 == 1:
        errors.append((lineno, 'Unbalanced double quote (")'))

    # 1b. Curly double quotes “ ”
    if stripped.count("“") != stripped.count("”"):
        errors.append((lineno, "Unbalanced curly double quotes (“ ”)"))

    # 2. Parenthesis balance on this line
    paren_depth = 0
    for ch in stripped:
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            if paren_depth == 0:
                errors.append((lineno, "Unmatched closing parenthesis ')'"))
            else:
                paren_depth -= 1
    if paren_depth > 0:
        errors.append((lineno, "Unclosed parenthesis '('"))

    # 3. Suspicious characters (e.g. non-ASCII letters, control chars)
    suspicious_found = 0
    for ch in stripped:
        if is_suspicious_char(ch):
            code = ord(ch)
            errors.append((lineno, f"Suspicious character U+{code:04X} ({repr(ch)[1:-1]})"))
            suspicious_found += 1
            if suspicious_found >= 3:
                break

    # 4. Sentence punctuation + signature-tail heuristics
    if looks_like_sentence(stripped):
        # New: check for and allow signature-like delimiter tail
        if not has_delimiter_short_tail(stripped):

            # New: check the last sentence-like segment length (ignoring a tail)
            last_seg = extract_last_sentence_segment(stripped)
            LAST_SEG_LEN_THRESHOLD = 140  # tune as needed

            if len(last_seg) >= LAST_SEG_LEN_THRESHOLD and not has_sentence_ending_punct(stripped):
                errors.append((lineno, "Long final sentence segment without ending punctuation"))

            # Existing punctuation rule
            if not has_sentence_ending_punct(stripped):
                errors.append((lineno, "Missing sentence-ending punctuation"))

    # 5. Simple repeated-word check: "the the"
    for word in set(REPEATED_WORD_RE.findall(stripped)):
        errors.append((lineno, f'Repeated word "{word}"'))

    # 6. Common AI placeholders / artefacts
    upper = stripped.upper()
    if "TODO" in upper:
        errors.append((lineno, "Contains 'TODO' placeholder"))
    if "INSERT SCENE" in upper or "INSERT HERE" in upper:
        errors.append((lineno, "Contains 'INSERT ...' placeholder text"))
    if "LOREM IPSUM" in upper:
        errors.append((lineno, "Contains 'Lorem ipsum' placeholder text"))

    # 7. Too long line
    if len(stripped) > MAX_LINE_LENGTH:
        errors.append((lineno, f"Line too long ({len(stripped)} characters). This may indicate missing line breaks or paragraphs merged into single lines. Consider splitting into multiple lines."))

    return errors


def check_auto_wrapped_blocks(lines: List[str]) -> List[Tuple[int, str]]:
    """
    Detect blocks of consecutive lines that appear to be improperly wrapped.

    Looks for 3+ consecutive non-empty lines where most don't end with
    sentence-ending punctuation, suggesting auto-wrapped paragraphs.

    Returns errors with each line in the problematic block reported.
    """
    errors: List[Tuple[int, str]] = []

    consecutive_lines = []
    block_start = 0

    for lineno, line in enumerate(lines, start=1):
        stripped = line.rstrip("\n")

        # Empty line resets the consecutive block
        if len(stripped) == 0:
            if len(consecutive_lines) >= 3:
                # Check if this block looks auto-wrapped
                if _is_auto_wrapped_block(consecutive_lines, lines, block_start):
                    # Report error on each line in the block
                    for block_lineno in range(block_start, block_start + len(consecutive_lines)):
                        errors.append(
                            (block_lineno, "Inconsistent line breaks (likely auto-wrapped text)")
                        )
            consecutive_lines = []
            block_start = 0
            continue

        # Skip headings, lists, very short lines
        if _should_skip_line_for_autowrap(stripped):
            # End current block
            if len(consecutive_lines) >= 3:
                if _is_auto_wrapped_block(consecutive_lines, lines, block_start):
                    for block_lineno in range(block_start, block_start + len(consecutive_lines)):
                        errors.append(
                            (block_lineno, "Inconsistent line breaks (likely auto-wrapped text)")
                        )
            consecutive_lines = []
            block_start = 0
            continue

        # Add to current block
        if not consecutive_lines:
            block_start = lineno
        consecutive_lines.append(stripped)

    # Check final block
    if len(consecutive_lines) >= 3:
        if _is_auto_wrapped_block(consecutive_lines, lines, block_start):
            for block_lineno in range(block_start, block_start + len(consecutive_lines)):
                errors.append((block_lineno, "Inconsistent line breaks (likely auto-wrapped text)"))

    return errors


def _should_skip_line_for_autowrap(line: str) -> bool:
    """Check if a line should be skipped for auto-wrap detection."""
    # Very short lines
    if len(line) < 10:
        return True

    # Headings
    if line.startswith("#"):
        return True

    # List items
    if line.lstrip().startswith(("-", "*")):
        return True

    # Numbered lists (e.g., "1. ", "2. ")
    if len(line) > 2 and line[0].isdigit() and line[1:3] in (". ", ") "):
        return True

    return False


def _is_auto_wrapped_block(consecutive_lines: List[str], all_lines: List[str], start_lineno: int) -> bool:
    """
    Check if a block of consecutive lines looks auto-wrapped or improperly formatted.

    The issue: consecutive non-empty lines without blank lines between them.
    They should either be merged into one paragraph or separated by blank lines.

    Returns True if the block looks like prose that needs re-paragraphing.
    """
    if len(consecutive_lines) < 3:
        return False

    # Check if most lines are "prose-like" (reasonable length, not super short)
    # This filters out intentional short lines like dialogue or poetry
    prose_like_count = 0
    for line in consecutive_lines:
        # Consider a line "prose-like" if it's longer than typical short lines
        # (dialogue, poetry, emphasis tend to be very short)
        if len(line) > 20:
            prose_like_count += 1

    # If most lines are prose-like (not super short), flag the block
    # This catches both:
    # - Auto-wrapped incomplete sentences (original case)
    # - Complete sentences that should be merged or separated (new case)
    if prose_like_count >= len(consecutive_lines) * 0.6:  # 60% threshold
        return True

    return False


def validate_file(path: Path, strip_asterisks: bool) -> list[tuple[int, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [(1, "File is not valid UTF-8")]

    if strip_asterisks:
        new_text, changed = strip_markdown_asterisks(text)
        if changed:
            path.write_text(new_text, encoding="utf-8")
            text = new_text

    lines = text.splitlines(keepends=True)
    errors: List[Tuple[int, str]] = []

    errors.extend(check_title_lines(path, lines))

    for lineno, line in enumerate(lines, start=1):
        errors.extend(check_line(line, lineno))

    # Check for auto-wrapped text blocks
    errors.extend(check_auto_wrapped_blocks(lines))

    total_lines = len(lines)
    if total_lines < MIN_EXPECTED_LINES:
        errors.append(
            (
                1,
                f"Too few lines ({total_lines}); this may indicate missing line breaks "
                "or paragraphs merged into single lines.",
            )
        )

    errors.sort(key=lambda e: e[0])
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate AI-generated chapter files for common issues."
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="Files or glob patterns to validate (e.g. path/to/story/ch*.md)",
    )
    parser.add_argument(
        "-s",
        "--strip-asterisks",
        action="store_true",
        help="Strip Markdown *italic* and **bold** markers from files (and overwrite).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    files: list[Path] = []
    for pattern in args.paths:
        matched = glob.glob(pattern)
        if not matched:
            files.append(Path(pattern))
        else:
            files.extend(Path(p) for p in matched)

    if not files:
        print("No files to validate.", file=sys.stderr)
        return 1

    exit_code = 0

    for path in files:
        if not path.exists():
            print(f"{path.name:<12} ERROR")
            print("- line 1: File does not exist")
            exit_code = 1
            continue

        errors = validate_file(path, strip_asterisks=args.strip_asterisks)

        status = "OK" if not errors else "ERROR"
        if errors:
            exit_code = 1

        print(f"{path.name:<12} {status}")
        for lineno, msg in errors:
            print(f"- line {lineno}: {msg}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
