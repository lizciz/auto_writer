"""Pure text helpers: outline parsing, sectioned markdown, word counts, endings."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# ============================== Number words ==============================

_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}


def words_to_int(s: str) -> Optional[int]:
    """
    Convert simple English cardinal words up to ~199 (e.g., 'twenty-one', 'one hundred one').
    Returns None if it doesn't look like a number word.
    """
    s = s.strip().lower().replace("-", " ")
    parts = [p for p in s.split() if p != "and"]
    if not parts:
        return None
    cur = 0
    for w in parts:
        if w == "hundred":
            cur = (cur or 1) * 100
        elif w in _NUM_WORDS:
            cur += _NUM_WORDS[w]
        else:
            return None
    return cur


# ============================== Comments and sections ==============================


def is_comment(line: str) -> bool:
    s = line.strip()
    return s == "//" or s.startswith("// ")


def strip_comments(text: str) -> str:
    """Remove lines starting with '// ' (template guidance comments)."""
    return "\n".join(ln for ln in text.splitlines() if not is_comment(ln)).strip()


def normalize_key(heading: str) -> str:
    """'Character Profiles' -> 'character_profiles'."""
    return re.sub(r"[^a-z0-9]+", "_", heading.lower()).strip("_")


def parse_sections(content: str) -> Dict[str, str]:
    """Parse a sectioned markdown file into {normalized_heading: section_content}.

    Sections are delimited by top-level '# Heading' lines. '## ...' subheadings
    are content. Lines starting with '// ' are stripped as comments.
    """
    result: Dict[str, str] = {}
    current_key: Optional[str] = None
    buf: List[str] = []

    def flush() -> None:
        if current_key is not None:
            result[current_key] = "\n".join(buf).strip()

    for line in strip_comments(content).splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            flush()
            current_key = normalize_key(stripped[2:])
            buf = []
        else:
            buf.append(line)
    flush()
    return result


def get_section(markdown: str, heading: str) -> str:
    """Return the body of a '# Heading' or '## Heading' section (case-insensitive)."""
    m = re.search(
        rf"^(#{{1,2}})\s+{re.escape(heading)}\s*$(.*?)(?=^#{{1,2}}\s|\Z)",
        markdown,
        flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    return m.group(2).strip() if m else ""


# ============================== Outline parsing ==============================

_CHAPTER_H_RE = re.compile(
    r"^\s*#*\s*\**\s*Chapter\s+([A-Za-z0-9\-]+)\s*(?:[-:—]\s*(.*?)\s*(?:\(revised\))?\**)?\s*$",
    flags=re.IGNORECASE,
)


def parse_master_outline(master_md: str) -> Dict[int, str]:
    """
    Parse chapter_outline.md into {chapter_number: brief_markdown}.
    Content for each chapter is all lines until the next '# Chapter ...' heading.
    Anything before the first chapter heading (e.g. an overview table) is ignored.
    """
    chapters: Dict[int, str] = {}
    cur_num: Optional[int] = None
    cur_buf: List[str] = []

    def flush() -> None:
        if cur_num is not None:
            brief = "\n".join(cur_buf).strip()
            chapters[cur_num] = brief + "\n"

    for ln in master_md.splitlines():
        m = _CHAPTER_H_RE.match(ln)
        if not m:
            cur_buf.append(ln)
            continue
        flush()
        token = m.group(1).strip()
        title_tail = (m.group(2) or "").strip().strip("*").strip()
        num = int(token) if token.isdigit() else words_to_int(token)
        if num is None:
            raise ValueError(f"Cannot parse chapter number from header: {ln!r}, token={token!r}")
        cur_num = num
        cur_buf = [f"Chapter {cur_num}: {title_tail}" if title_tail else f"Chapter {cur_num}"]
    flush()
    return chapters


_SUMMARY_LINE_RE = re.compile(r"^\**\s*Summary\s*:?\s*\**\s*:?\s*(.+)$", re.IGNORECASE)
_LABEL_LINE_RE = re.compile(r"^\*\*[^*]+:\*\*")


def chapter_one_liner(outline_body: str) -> str:
    """Pick a one-line description of a chapter from its outline.

    Prefers a 'Summary:' line; otherwise the first plain prose line (skipping
    headings, list items and '**Label:** value' metadata lines).
    """
    lines = [ln.strip() for ln in outline_body.splitlines() if ln.strip()]
    for ln in lines:
        m = _SUMMARY_LINE_RE.match(ln)
        if m:
            return m.group(1).strip()
    for ln in lines:
        if ln.startswith(("#", "-", "*", "|", ">")) or _LABEL_LINE_RE.match(ln) or ln == "---":
            continue
        return ln
    # Bullet-only outlines: first bullet with real content beyond a bold label.
    for ln in lines:
        if ln.startswith(("- ", "* ")):
            item = _LABEL_LINE_RE.sub("", ln[2:].strip()).strip()
            if item:
                return item if len(item) <= 200 else item[:197].rstrip() + "..."
    return ""


def generate_story_arc(chapters: Dict[int, str]) -> str:
    """Generate a condensed one-line-per-chapter arc from parsed chapter data."""
    lines = []
    for num in sorted(chapters):
        content_lines = chapters[num].splitlines()
        heading = content_lines[0] if content_lines else f"Chapter {num}"
        one_liner = chapter_one_liner("\n".join(content_lines[1:]))
        lines.append(f"- **{heading}** — {one_liner}" if one_liner else f"- **{heading}**")
    return "\n".join(lines)


_INSTRUCTIONS_HEADING_RE = re.compile(
    r"##\s+Chapter[- ]Specific\s+Instructions[^\n]*\n", re.IGNORECASE
)


def split_outline_instructions(content: str) -> Tuple[str, str]:
    """Split outline content into (main_outline, chapter_specific_instructions).

    Instructions live in an optional '## Chapter-Specific Instructions' section
    at the bottom of the outline, optionally delimited by '---' rules.
    """
    m = _INSTRUCTIONS_HEADING_RE.search(content)
    if not m:
        return content.rstrip(), ""
    outline_part = re.sub(r"\n---+\s*$", "", content[: m.start()].rstrip()).rstrip()
    instr_part = re.sub(r"\n?---+\s*$", "", content[m.end():].strip()).strip()
    return outline_part, instr_part


def extract_chapter_title(outline_content: str) -> str:
    """Return the subtitle from a first line like 'Chapter N: Title', or ''."""
    stripped = outline_content.strip()
    first_line = stripped.splitlines()[0] if stripped else ""
    m = re.match(r"(?i)chapter\s+\S+?\s*[:\-—]\s*(.*)", first_line)
    return m.group(1).strip() if m else ""


# ============================== Words and length ==============================


def word_count(text: str) -> int:
    return len(text.split())


def parse_word_range(s: str) -> Optional[Tuple[int, int]]:
    """Parse '3000-4000', '3,000–4,000 words' or 'about 3500' into (low, high)."""
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", s or "")]
    nums = [n for n in nums if n >= 100]
    if not nums:
        return None
    return (min(nums[:2]), max(nums[:2]))


def length_instruction(target: str) -> str:
    rng = parse_word_range(target)
    if not rng:
        return ""
    lo, hi = rng
    if lo == hi:
        return f"Aim for about {lo:,} words. Do not exceed {int(hi * 1.15):,} words."
    return f"Aim for {lo:,}–{hi:,} words. Do not exceed {hi:,} words."


def tail_words(text: str, max_words: int) -> str:
    """Return roughly the last max_words words, starting at a sentence boundary."""
    if word_count(text) <= max_words:
        return text
    sentences = re.split(r"(?<=[.!?…]) +", text.strip())
    selected: List[str] = []
    count = 0
    for sentence in reversed(sentences):
        n = word_count(sentence)
        if count + n > max_words:
            break
        selected.insert(0, sentence)
        count += n
    if selected:
        return " ".join(selected)
    return " ".join(text.split()[-max_words:])


# ============================== Endings ==============================

_TERMINAL = (".", "!", "?", "…")
_CLOSERS = set("\"'”’)]}»›*_")


def ending_looks_truncated(text: str) -> bool:
    """Heuristic: does the last prose line look cut off mid-sentence?

    Flags a missing terminal punctuation mark, a trailing dash (interrupted
    speech is fine mid-chapter but not as the final line) and a trailing
    ellipsis.
    """
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return True
    s = lines[-1]
    while s and s[-1] in _CLOSERS:
        s = s[:-1].rstrip()
    if not s:
        return False
    if s.endswith(("...", "…", "—", "–", "-", ",", ";", ":")):
        return True
    return not s.endswith(_TERMINAL)
