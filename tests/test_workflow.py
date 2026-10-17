"""End-to-end workflow with a fake API client (no network)."""

import json
import sys

import pytest

from auto_writer import cli
from auto_writer.bible import stale_bibles
from auto_writer.llm import Generator
from auto_writer.project import Project

PROSE = "\n\n".join(f"Paragraph {i} of the chapter, with a complete sentence." for i in range(40))

OUTLINE = """# Overview
| Ch | Title |

# Chapter 1: Arrival
**Summary:** She arrives.
**POV:** Her

## Scenes
### Scene 1: Bus
Things.

# Chapter 2: Departure
**Summary:** She leaves.

## Chapter-Specific Instructions

Keep it short.
"""

BIBLE = (
    "# Story So Far\n## Chapter N\nShe arrived on the bus and met the driver, Gary.\n\n"
    "# Characters\n| Name | Role |\n| Gary | Bus driver |\n\n# Facts Established\n- The bus is red and runs every Tuesday at noon.\n\n"
    "# Plan Deviations\nNone\n"
)

CHARACTERS_REPLY = """Here are the profiles:

```
### Gary

description: A burly bus driver.
base_tags: 1boy, male, short grey hair, burly
nude_tags: large penis

### Zelda
description: Not in the story.
**base_tags:** blonde hair, G-cup
```
"""


class FakeClient:
    """Answers each task based on markers in its prompt; records every call."""

    def __init__(self):
        self.calls = []
        self.last_http_debug = {}
        self.truncate_next_chapter = False

    def chat_completion(self, messages, thinking_log_path=None):
        self.calls.append(messages)
        system = messages[0]["content"] if messages[0]["role"] == "system" else ""
        last = messages[-1]["content"]
        reason = "stop"
        if messages[-1]["role"] == "user" and "previous reply was cut off" in last:
            text = "and then she finished the sentence.\n\nThe end."
        elif "ask the few questions" in system:
            text = "# Questions\n\n## 1. Ending\nHow?\nDefault if unanswered: happy\nAnswer:\n"
        elif "turn an author's rough idea" in system:
            text = "# Synopsis\nA story.\n\n# Target Word Count\n3000-4000\n\n# Fixed Facts\n- Sky is green\n"
        elif "chapter-by-chapter outline" in system:
            text = OUTLINE
        elif "demanding developmental editor" in system:
            text = "# Outline Review\n## Recommended Changes\n1. Nothing."
        elif "continuity editor" in system:
            text = BIBLE
        elif "writing a long-form story one chapter at a time" in system:
            n = "1" if "Chapter 1:" in last else "2"
            text = f"Chapter {n}: Title\n\n{PROSE}"
            if self.truncate_next_chapter:
                self.truncate_next_chapter = False
                text += "\n\nShe looked"
                reason = "length"
        elif "copy editor" in system:
            text = "Fixed line."
        elif "SDXL tag profiles" in system:
            text = CHARACTERS_REPLY
        else:
            raise AssertionError(f"unexpected prompt: {system[:80]!r}")
        self.last_http_debug = {"finish_reason": reason, "usage": None}
        return text


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr("auto_writer.llm.CONFIG_DIR", tmp_path / "no_user_config")
    monkeypatch.setattr("auto_writer.llm.time.sleep", lambda s: None)
    monkeypatch.chdir(tmp_path)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"api_base": "http://x", "model": "m", "max_tokens": 1000}))
    run(monkeypatch, "init", "My Story", "--profile", str(profile))
    root = tmp_path / "My Story"
    monkeypatch.chdir(root)
    client = FakeClient()
    monkeypatch.setattr(
        "auto_writer.cli.Generator",
        lambda proj: Generator(proj, profile=json.loads(profile.read_text()), client=client),
    )
    return Project(root), client


def run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["aw", *argv])
    cli.main()


def test_full_workflow(project, monkeypatch, capsys):
    proj, client = project
    assert (proj.root / "idea.md").exists()

    # Empty idea is rejected.
    with pytest.raises(SystemExit):
        run(monkeypatch, "plan")

    (proj.root / "idea.md").write_text("A woman on a bus.\n")
    run(monkeypatch, "plan")
    assert (proj.root / "questions.md").exists()
    assert not (proj.root / "plan.md").exists()

    # Unanswered questions block planning...
    with pytest.raises(SystemExit):
        run(monkeypatch, "plan")
    # ...until answered.
    q = proj.root / "questions.md"
    q.write_text(q.read_text().replace("Answer:", "Answer: bittersweet"))
    run(monkeypatch, "plan")
    assert "Sky is green" in (proj.root / "plan.md").read_text()
    assert "bittersweet" in client.calls[-1][-1]["content"]

    run(monkeypatch, "outline")
    run(monkeypatch, "review-outline")
    assert (proj.root / "outline_review.md").exists()
    run(monkeypatch, "prepare")
    assert proj.outline_nums() == [1, 2]
    assert "She arrives." in (proj.root / "story_arc.md").read_text()

    # Chapter 1 comes back truncated; the writer asks the model to continue.
    client.truncate_next_chapter = True
    run(monkeypatch, "write")
    ch1 = proj.story_file(1).read_text()
    assert "She looked and then she finished the sentence." in ch1
    assert proj.bible_file(1).exists()
    req = json.loads(proj.log_file("ch01.writer.request.json").read_text())
    assert req["continuations"] == 1 and req["prompt_origin"] == "bundled"

    # Chapter 2's prompt gets the plan, the bible, chapter 1 and its own instructions.
    run(monkeypatch, "write")
    prompt = proj.log_file("ch02.writer.prompt.md").read_text()
    assert "Sky is green" in prompt
    assert "# Story So Far" in prompt
    assert "Paragraph 39" in prompt
    assert "Keep it short." in prompt
    assert "Aim for 3,000–4,000 words" in prompt
    assert stale_bibles(proj) == []

    # Editing chapter 1 makes both bibles stale (staleness propagates).
    proj.story_file(1).write_text(ch1 + "\nAn added line.\n")
    assert [n for n, _ in stale_bibles(proj)] == [1, 2]
    run(monkeypatch, "bible")
    assert stale_bibles(proj) == []

    # Existing chapters are protected; reset removes chapter, bible and logs.
    run(monkeypatch, "write", "2")
    assert "already exists" in capsys.readouterr().out
    run(monkeypatch, "reset", "2")
    assert not proj.story_file(2).exists()
    assert not proj.bible_file(2).exists()
    assert not list(proj.path("logs_dir").glob("ch02.*"))


def test_no_questions_and_project_prompt_override(project, monkeypatch):
    proj, client = project
    (proj.root / "idea.md").write_text("Idea.\n")
    (proj.prompts_dir).mkdir(parents=True)
    (proj.prompts_dir / "plan.append.md").write_text("=== user ===\nEXTRA PROJECT RULE")
    run(monkeypatch, "plan", "--no-questions")
    sent = client.calls[-1][-1]["content"]
    assert "No questions were asked" in sent
    assert sent.rstrip().endswith("EXTRA PROJECT RULE")
    assert (proj.root / "plan.md").exists()
    with pytest.raises(SystemExit):
        run(monkeypatch, "plan", "--no-questions")  # exists; needs --force
    run(monkeypatch, "plan", "--no-questions", "--force")
    assert list((proj.meta_dir / "backup").glob("plan.*.md"))


def test_full_fix_rejects_short_output(project, monkeypatch):
    from auto_writer.writing import run_validation_and_maybe_fix

    proj, client = project
    proj.story_file(1).write_text("Chapter 1: X\n\n" + PROSE + "\n\nbroken line without end\n\nThe end.\n")
    gen = Generator(proj, profile={"model": "m"}, client=client)
    run_validation_and_maybe_fix(proj, gen, 1, fix_mode="full")
    # The fake fixer returns a single line, far shorter than the chapter: rejected.
    assert "Paragraph 39" in proj.story_file(1).read_text()
    assert proj.log_file("ch01.fixer_full.rejected.md").exists()


def test_profile_listing_and_names(project, monkeypatch, tmp_path, capsys):
    proj, _ = project
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "fast.json").write_text(json.dumps({"api_base": "http://y", "model": "fast-model"}))
    monkeypatch.setenv("LLM_PROFILES_DIR", str(profiles))

    run(monkeypatch, "profile")
    out = capsys.readouterr().out
    assert str(profiles) in out
    assert "fast" in out and "fast-model @ http://y, no output cap" in out
    assert "Current project profile" in out

    run(monkeypatch, "profile", "fast")
    assert json.loads(proj.profile_path.read_text())["model"] == "fast-model"
    with pytest.raises(SystemExit):
        run(monkeypatch, "profile", "nope")

    # Listing works outside a project too.
    monkeypatch.chdir(tmp_path)
    run(monkeypatch, "profile")
    assert "Current project" not in capsys.readouterr().out


def test_characters(project, monkeypatch, capsys):
    proj, client = project
    with pytest.raises(SystemExit):
        run(monkeypatch, "characters")  # no plan yet
    (proj.root / "plan.md").write_text("# Synopsis\nA bus story.\n")
    proj.story_file(1).write_text("Chapter 1: Bus\n\nGary drove the bus.\n")

    run(monkeypatch, "characters")
    out = capsys.readouterr().out
    assert proj.bible_file(1).exists()  # brought up to date first
    sent = client.calls[-1][-1]["content"]
    assert "Gary drove the bus." in sent and "Story text (all chapters)" in sent
    assert "# Story So Far" in sent

    path = proj.root / "characters.md"
    text = path.read_text()
    assert "Here are the profiles" not in text and "```" not in text
    assert text.index("Field reference") < text.index("### Gary")
    assert "2 characters: Gary, Zelda" in out
    assert "Zelda: uses a cup size" in out
    assert "Zelda: name not found" in out
    assert "Gary: name not found" not in out

    comfykit = pytest.importorskip("comfykit.characters")
    parsed = comfykit.parse_characters_file(path)
    assert list(parsed) == ["Gary", "Zelda"]
    assert parsed["Gary"]["nude_tags"] == "large penis"

    with pytest.raises(SystemExit):
        run(monkeypatch, "characters")  # exists; needs --force
    run(monkeypatch, "characters", "--force")
    assert list((proj.meta_dir / "backup").glob("characters.*.md"))
    run(monkeypatch, "status")
    assert "characters: ok" in capsys.readouterr().out
