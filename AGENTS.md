# AGENTS.md

This file provides guidance to agentic coding agents (such as Claude Code, GitHub Copilot, or other AI assistants) when working with code in this repository.

## Overview

`auto_writer` is a semi-automatic long-form story writing tool that uses OpenAI-compatible APIs to generate fiction chapters. It follows a git-like command interface and maintains story state in a `.story/` directory within each project.

The codebase consists of two main modules:
- `auto_writer.py`: Core story generation and management
- `validate_chapter.py`: Chapter validation and quality checking

After each chapter write, the system automatically generates a summary that tracks current status, relationship status, and key plot points.

## Build/Lint/Test Commands

### Installation and Setup
```bash
# Install the package in development mode (makes aw/auto_writer and vc/validate_chapter available)
pip install -e .

# Verify installation
aw --help
vc --help
```

### Testing
Currently there are no automated tests in this repository. Manual testing is performed by:
1. Creating story projects with `auto_writer --init`
2. Running the full workflow: `--prepare`, `--write`, validation
3. Inspecting generated chapters and logs

### Linting and Code Quality
No automated linting tools are currently configured. Code quality is maintained through:
- Manual code review
- Type checking with mypy (if available)
- Following established Python conventions

### Running Individual Components
```bash
# Validate chapters
validate_chapter story/ch*.md

# Initialize story project
auto_writer --init --title "My Story"

# Prepare and write chapters
auto_writer --prepare
auto_writer --write --chapter 1
auto_writer --write --loop

# Configure summary settings
auto_writer --summary-max-words 200
auto_writer --summary-system-prompt /path/to/custom/prompt.md
auto_writer --summary-user-prompt /path/to/custom/prompt.md
```

## Code Style Guidelines

### General Principles
- **Clarity over cleverness**: Code should be readable and maintainable
- **Type safety**: Use type hints consistently
- **Error resilience**: Handle exceptions gracefully with appropriate fallbacks
- **Documentation**: Include docstrings for all public functions

### Python Version and Dependencies
- Python >= 3.9 required
- Core dependencies: `requests` only
- Use modern Python features (f-strings, pathlib, type hints)

### Imports
```python
# Standard library imports first
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Third-party imports
import requests

# Local imports
import validate_chapter as vc
```

### Naming Conventions
- **Functions**: `snake_case` (e.g., `load_json`, `parse_master_outline`)
- **Variables**: `snake_case` (e.g., `chapter_path`, `api_base`)
- **Constants**: `UPPER_CASE` (e.g., `APP_VERSION`, `SYSTEM_PROMPT_FILENAME`)
- **Classes**: `PascalCase` (rare in this codebase)
- **Modules**: `snake_case` (e.g., `auto_writer`, `validate_chapter`)

### Type Hints
```python
# Function signatures always include return type
def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""

# Use Optional for nullable types
def words_to_int(s: str) -> Optional[int]:
    # Implementation
```

### Error Handling
```python
# Graceful fallbacks for file operations
def load_json(p: Path, default: Any) -> Any:
    txt = read_text(p)
    if not txt.strip():
        return default
    try:
        return json.loads(txt)
    except Exception:
        return default
```

### File Operations
```python
# Use pathlib.Path consistently
def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""

def write_text(p: Path, s: str) -> None:
    p.write_text(s, encoding="utf-8")
```

### String Formatting
```python
# Use f-strings for all string formatting
chapter_title = f"Chapter {chapter_num}: {subtitle}"
url = f"{api_base.rstrip('/')}/chat/completions"
```

### Constants and Configuration
```python
# Define constants at module level
APP_VERSION = "2.1.0"
SYSTEM_PROMPT_FILENAME = "system_prompt.md"

# Use configuration dictionaries
profile = {
    "api_base": "https://api.example.com/v1",
    "model": "gpt-4",
    "parameters": {"temperature": 0.7, "max_tokens": 4000}
}
```

## Development Workflow

### File Organization
```
auto_writer/
├── auto_writer.py          # Main application logic
├── validate_chapter.py     # Validation and fixing logic
├── pyproject.toml          # Build configuration
├── AGENTS.md              # This file - agent guidance
├── CLAUDE.md              # Claude-specific guidance
└── README.md              # User documentation (future)
```

Project structure after initialization:
```
project/
├── .story/
│   ├── config.json           # Story metadata, paths, context settings
│   ├── profile.json          # API config (api_base, model, api_key_env, parameters)
│   ├── system_prompt.md      # System prompt template for writer
│   ├── user_prompt.md        # User prompt template for writer
│   ├── summary_system_prompt.md    # System prompt template for summarizer
│   └── summary_user_prompt.md      # User prompt template for summarizer
├── chapter_outline.md        # Master outline (edit this)
├── outline/
│   └── ch01.md, ch02.md...   # Per-chapter outlines (generated by --prepare)
├── story/
│   └── ch01.md, ch02.md...   # Generated chapters
├── summary/
│   └── ch01.md, ch02.md...   # Generated chapter summaries
└── logs/
    ├── chNN.prompt.md        # Full prompts sent to API
    ├── chNN.request.json     # Request metadata
    ├── chNN.summary.prompt.md    # Summary prompts
    └── chNN.summary.request.json # Summary metadata
```

### Adding New Features
1. Understand the existing architecture (see CLAUDE.md for details)
2. Follow established patterns for file operations, error handling, and type hints
3. Add appropriate docstrings and comments
4. Test manually with the full story generation workflow
5. Update this AGENTS.md if introducing new conventions

### Code Review Checklist
- [ ] Type hints present on all function signatures
- [ ] Docstrings for public functions
- [ ] Error handling with appropriate fallbacks
- [ ] Consistent naming conventions
- [ ] No hardcoded paths; use Path objects
- [ ] Imports organized correctly
- [ ] Manual testing with story generation workflow

## Architecture Notes

### Core Components
- **Story Management**: Project initialization, configuration, metadata
- **Chapter Generation**: API integration, prompt templating, content generation
- **Validation**: Quality checks, auto-fixing, error reporting

### Data Flow
1. User initializes project with `--init`
2. User writes `chapter_outline.md` master outline
3. `--prepare` parses outline into individual chapter files
4. `--write` generates chapters using API, with automatic validation and fixing
5. If writing chapter N and chapter N-1 exists but has no summary, summary is generated first
6. After each chapter write, a summary is automatically generated
7. Logs capture all API interactions for debugging

### Key Design Decisions
- **Git-like CLI**: Familiar interface for version control users
- **Local State**: `.story/` directory keeps project self-contained
- **API Agnostic**: Works with any OpenAI-compatible endpoint
- **Validation First**: Quality checks run automatically after generation</content>
<parameter name="filePath">/home/mattias/git/personal-repos/auto_writer/AGENTS.md