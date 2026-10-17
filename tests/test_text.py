from auto_writer.text import (
    chapter_one_liner,
    ending_looks_truncated,
    extract_chapter_title,
    generate_story_arc,
    get_section,
    length_instruction,
    parse_master_outline,
    parse_sections,
    parse_word_range,
    split_outline_instructions,
    tail_words,
    words_to_int,
)


def test_words_to_int():
    assert words_to_int("twenty-three") == 23
    assert words_to_int("one hundred and one") == 101
    assert words_to_int("seven") == 7
    assert words_to_int("banana") is None


def test_parse_master_outline_ignores_overview_and_parses_words():
    md = """# Chapter Outline: "A Story"
# Overview
| Ch | Title |
| 1 | Start |

# Chapter One: The Start
**Summary:** She arrives.

# Chapter 2 — Middle
Body two.
"""
    chapters = parse_master_outline(md)
    assert sorted(chapters) == [1, 2]
    assert chapters[1].startswith("Chapter 1: The Start\n")
    assert chapters[2].startswith("Chapter 2: Middle")


def test_story_arc_skips_metadata_lines():
    chapters = {
        1: "Chapter 1: A\n**POV:** Her\n**Summary:** She arrives in town.\n",
        2: "Chapter 2: B\n**POV:** Him\n\nHe leaves.\n",
    }
    arc = generate_story_arc(chapters)
    assert "- **Chapter 1: A** — She arrives in town." in arc
    assert "- **Chapter 2: B** — He leaves." in arc
    assert "POV" not in arc
    assert chapter_one_liner("## Scenes") == ""
    assert chapter_one_liner("- **Core Plot Points:**\n  - Introduce Britt at work.") == "Introduce Britt at work."


def test_split_outline_instructions():
    outline, instr = split_outline_instructions(
        "Chapter 1: X\nbody\n\n---\n\n## Chapter-Specific Instructions (Optional)\n\nBe brief.\n\n---\n"
    )
    assert outline == "Chapter 1: X\nbody"
    assert instr == "Be brief."
    assert split_outline_instructions("plain") == ("plain", "")


def test_extract_chapter_title():
    assert extract_chapter_title("Chapter 3: The Meeting\nbody") == "The Meeting"
    assert extract_chapter_title("Chapter 3") == ""


def test_parse_sections_and_get_section():
    md = "# Synopsis\n// comment\nText.\n\n# Character Profiles\n## Alex\nHero.\n"
    sections = parse_sections(md)
    assert sections == {"synopsis": "Text.", "character_profiles": "## Alex\nHero."}
    assert get_section("# A\nx\n## Plan Deviations\nNone\n# B\ny", "Plan Deviations") == "None"


def test_word_range_and_length_instruction():
    assert parse_word_range("3,000–4,000 words") == (3000, 4000)
    assert parse_word_range("about 3500") == (3500, 3500)
    assert parse_word_range("") is None
    assert "Do not exceed 4,000" in length_instruction("3000-4000")
    assert length_instruction("") == ""


def test_tail_words_respects_sentences():
    text = "One two three. Four five six. Seven eight nine."
    assert tail_words(text, 6) == "Four five six. Seven eight nine."
    assert tail_words(text, 100) == text


def test_ending_looks_truncated():
    assert not ending_looks_truncated("Para.\n\nThe end.")
    assert not ending_looks_truncated('He said, "Goodbye."')
    assert not ending_looks_truncated("Really?”")
    # Real endings from a truncated story:
    assert ending_looks_truncated("She looked")
    assert ending_looks_truncated('"Whatever you did—"')
    assert ending_looks_truncated("cleared some of the accumulation that...\"")
    assert ending_looks_truncated("The surface was covered with papers, maps,")


def test_strip_asterisks_keeps_scene_breaks():
    from auto_writer.validate import strip_markdown_asterisks

    text, changed = strip_markdown_asterisks("She was *very* **wet**.\n\n***\n\nLater.\n")
    assert changed
    assert text == "She was very wet.\n\n***\n\nLater.\n"
