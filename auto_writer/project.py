"""Story project layout, config and generated-file bookkeeping."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, NoReturn, Optional, overload

from . import __version__

CONFIG_DIR = Path.home() / ".config" / "auto_writer"
PACKAGE_DIR = Path(__file__).parent

DEFAULT_PATHS = {
    "idea": "idea.md",
    "questions": "questions.md",
    "plan": "plan.md",
    "outline_master": "chapter_outline.md",
    "outline_review": "outline_review.md",
    "story_arc": "story_arc.md",
    "characters": "characters.md",
    "outline_dir": "outline",
    "story_dir": "story",
    "bible_dir": "bible",
    "logs_dir": "logs",
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "context": {
        # budget: as many of the most recent whole chapters as fit in max_words
        # last_n_chapters: the last N whole chapters
        # last_n_words: the last N words of the previous chapter
        "mode": "budget",
        "max_words": 60000,
        "last_n_chapters": 3,
        "last_n_words": 500,
    },
    "generation": {
        "max_continuations": 2,
    },
    # Prompt set for every task; "prompts" maps task -> set to override single tasks.
    "prompt_set": "default",
    "prompts": {},
}


def now_iso() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def write_text(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


def load_json(p: Path, default: Any) -> Any:
    txt = read_text(p)
    if not txt.strip():
        return default
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return default


def save_json(p: Path, data: Any) -> None:
    write_text(p, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def file_sha(p: Path) -> str:
    """Short content hash; '' for a missing file."""
    if not p.exists():
        return ""
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def die(msg: str) -> NoReturn:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def chapter_file(n: int) -> str:
    return f"ch{n:02d}.md"


def chapter_num_from_name(name: str) -> int:
    m = re.search(r"ch(\d+)", name, flags=re.I)
    return int(m.group(1)) if m else 0


def _merge_defaults(cfg: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(defaults)
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(defaults.get(k), dict):
            out[k] = _merge_defaults(v, defaults[k])
        else:
            out[k] = v
    return out


class Project:
    """A story project directory containing .story/config.json."""

    def __init__(self, root: Path):
        self.root = root
        self.meta_dir = root / ".story"
        self.config_path = self.meta_dir / "config.json"
        self.state_path = self.meta_dir / "state.json"
        self.prompts_dir = self.meta_dir / "prompts"
        self.profile_path = self.meta_dir / "profile.json"
        raw = load_json(self.config_path, {})
        self.raw_config = raw
        self.config = _merge_defaults(raw, {**DEFAULT_CONFIG, "paths": DEFAULT_PATHS})

    # ---------- discovery ----------

    @overload
    @classmethod
    def find(cls, start: Optional[str] = None, required: Literal[True] = True) -> "Project": ...

    @overload
    @classmethod
    def find(cls, start: Optional[str] = None, *, required: bool) -> Optional["Project"]: ...

    @classmethod
    def find(cls, start: Optional[str] = None, required: bool = True) -> Optional["Project"]:
        """Locate the project at or above `start` (default: CWD)."""
        here = Path(start).resolve() if start else Path.cwd()
        for d in (here, *here.parents):
            if (d / ".story" / "config.json").exists():
                return cls(d)
        if not required:
            return None
        die(f"Not a story project (no .story/config.json at or above {here}). Run `aw init` first.")

    @classmethod
    def create(cls, root: Path, title: str) -> "Project":
        if (root / ".story" / "config.json").exists():
            die(f"Story project already initialized at: {root}")
        cfg = {
            "title": title,
            "created": now_iso(),
            "version": __version__,
            "paths": dict(DEFAULT_PATHS),
            **json.loads(json.dumps(DEFAULT_CONFIG)),
        }
        save_json(root / ".story" / "config.json", cfg)
        project = cls(root)
        for key in ("outline_dir", "story_dir", "bible_dir", "logs_dir"):
            project.path(key).mkdir(parents=True, exist_ok=True)
        return project

    # ---------- config ----------

    @property
    def title(self) -> str:
        return self.config.get("title", "Untitled Story")

    def set_config(self, key: str, value: Any) -> None:
        self.raw_config[key] = value
        save_json(self.config_path, self.raw_config)
        self.config = _merge_defaults(self.raw_config, {**DEFAULT_CONFIG, "paths": DEFAULT_PATHS})

    def path(self, key: str) -> Path:
        return self.root / self.config["paths"].get(key, DEFAULT_PATHS[key])

    def rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.root))
        except ValueError:
            return str(p)

    @property
    def plan_path(self) -> Path:
        """plan.md, falling back to a legacy context.md if that is all there is."""
        plan = self.path("plan")
        legacy = self.root / "context.md"
        if not plan.exists() and legacy.exists():
            return legacy
        return plan

    def prompt_set(self, task: str) -> str:
        """The prompt set to use for a task: a per-task override, else the project's set."""
        return self.config.get("prompts", {}).get(task) or self.config.get("prompt_set") or "default"

    # ---------- chapters ----------

    def story_file(self, n: int) -> Path:
        return self.path("story_dir") / chapter_file(n)

    def outline_file(self, n: int) -> Path:
        return self.path("outline_dir") / chapter_file(n)

    def bible_file(self, n: int) -> Path:
        return self.path("bible_dir") / chapter_file(n)

    def log_file(self, name: str) -> Path:
        return self.path("logs_dir") / name

    def _nums(self, key: str) -> List[int]:
        d = self.path(key)
        return sorted(chapter_num_from_name(p.name) for p in d.glob("ch*.md")) if d.exists() else []

    def outline_nums(self) -> List[int]:
        return self._nums("outline_dir")

    def written_nums(self) -> List[int]:
        return self._nums("story_dir")

    # ---------- generated-file bookkeeping ----------

    def _state(self) -> Dict[str, Any]:
        return load_json(self.state_path, {"generated": {}})

    def record(self, output: Path, inputs: Iterable[Path]) -> None:
        """Remember which input contents a generated file was built from."""
        state = self._state()
        state.setdefault("generated", {})[self.rel(output)] = {
            "ts": now_iso(),
            "output": file_sha(output),
            "inputs": {self.rel(p): file_sha(p) for p in inputs},
        }
        save_json(self.state_path, state)

    def forget(self, output: Path) -> None:
        state = self._state()
        if state.get("generated", {}).pop(self.rel(output), None) is not None:
            save_json(self.state_path, state)

    def record_for(self, output: Path) -> Optional[Dict[str, Any]]:
        return self._state().get("generated", {}).get(self.rel(output))

    def changed_inputs(self, output: Path) -> Optional[List[str]]:
        """Inputs that changed since `output` was generated.

        Returns None if there is no record (e.g. the file was written by hand).
        """
        rec = self.record_for(output)
        if rec is None:
            return None
        return [
            name for name, sha in rec.get("inputs", {}).items()
            if file_sha(self.root / name) != sha
        ]

    def edited_since_generated(self, output: Path) -> bool:
        rec = self.record_for(output)
        return rec is not None and rec.get("output") != file_sha(output)

    def backup(self, p: Path) -> Optional[Path]:
        """Copy an existing file to .story/backup/ before it is overwritten."""
        if not p.exists():
            return None
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = self.meta_dir / "backup" / f"{p.stem}.{stamp}{p.suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
        return dest
