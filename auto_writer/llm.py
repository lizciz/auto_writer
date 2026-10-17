"""Prompt resolution and API calls: one `run()` per task, with logging and retries."""

from __future__ import annotations

import datetime as dt
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from storykit.llm import OpenAICompatClient
from storykit.prompts import Prompt, PromptLibrary

from .project import CONFIG_DIR, PACKAGE_DIR, Project, die, load_json, now_iso, save_json, write_text

# Profile keys that belong to auto_writer and must never be sent to the API.
_INTERNAL_PROFILE_KEYS = {"api_key"}

CONTINUE_MESSAGE = (
    "Your previous reply was cut off. Continue exactly where it stopped, "
    "mid-sentence if necessary. Do not repeat any text and do not add any preamble."
)


def prompt_library() -> PromptLibrary:
    return PromptLibrary(PACKAGE_DIR / "prompts", CONFIG_DIR / "prompts")


def resolve_prompt(project: Project, task: str) -> Prompt:
    return prompt_library().resolve(task, project.prompt_variant(task), project.prompts_dir)


def load_profile(project: Project) -> Dict[str, Any]:
    if not project.profile_path.exists():
        die("No profile set. List available profiles with `aw profile`, then set one with `aw profile <name>`.")
    prof = load_json(project.profile_path, {})
    if not prof:
        die(f"Profile is empty or invalid: {project.profile_path}")
    prof = dict(prof)
    prof.setdefault("api_base", os.getenv("API_BASE"))
    prof.setdefault("model", os.getenv("MODEL"))
    prof.setdefault("api_key_env", "API_KEY")
    if not os.getenv(prof["api_key_env"]) and os.getenv("API_KEY"):
        prof["api_key_env"] = "API_KEY"
    if not (prof["api_base"] and prof["model"]):
        die(
            "API config missing from profile or environment.\n"
            'Profile should include at least:\n  {"api_base": "...", "api_key_env": "API_KEY", "model": "..."}'
        )
    prof.setdefault("timeout_seconds", 300)
    return prof


@dataclass
class Result:
    text: str
    prompt: Prompt
    finish_reason: Optional[str] = None
    continuations: int = 0
    usage: List[Any] = field(default_factory=list)


class Generator:
    """Renders task prompts, calls the API and logs every request."""

    def __init__(self, project: Project, profile: Optional[Dict[str, Any]] = None, client: Any = None):
        self.project = project
        self.profile = profile if profile is not None else load_profile(project)
        self.client = client or OpenAICompatClient(self.profile, exclude_keys=_INTERNAL_PROFILE_KEYS)
        self._warned_tokens = False

    @property
    def model(self) -> str:
        return self.profile.get("model", "")

    def warn_if_no_output_cap(self) -> None:
        """Warn once when the profile leaves the output budget to provider defaults."""
        if self._warned_tokens:
            return
        self._warned_tokens = True
        if not any(k in self.profile for k in ("max_tokens", "max_completion_tokens")):
            print(
                "WARNING: profile sets neither max_tokens nor max_completion_tokens. Provider "
                "defaults are often too small for a full chapter (reasoning tokens count too), "
                "which truncates output. Set e.g. \"max_completion_tokens\": 32000."
            )

    def run(
        self,
        task: str,
        variables: Mapping[str, Any],
        log_name: str,
        *,
        max_continuations: int = 0,
        should_continue: Optional[Callable[[str, Optional[str]], bool]] = None,
        retries: int = 0,
        min_chars: int = 1,
    ) -> Result:
        """Run one task prompt.

        Args:
            task: Prompt task name (e.g. "writer").
            variables: Template variables.
            log_name: Base name for log files (e.g. "ch03" -> logs/ch03.writer.*).
            max_continuations: How many times to ask the model to continue a
                response that stopped early.
            should_continue: (text, finish_reason) -> bool. Defaults to
                finish_reason == "length".
            retries: Retries on API errors or outputs shorter than min_chars.
            min_chars: Outputs shorter than this count as failures (refusals/errors).
        """
        prompt = resolve_prompt(self.project, task)
        missing = prompt.missing_variables(variables)
        if missing:
            print(f"WARNING: prompt '{task}' ({prompt.path}) uses unknown variables: {', '.join(missing)}")
        messages = prompt.messages(variables)
        base = f"{log_name}.{task}"
        write_text(
            self.project.log_file(f"{base}.prompt.md"),
            "\n\n".join(f"===== {m['role']} =====\n{m['content']}" for m in messages) + "\n",
        )
        should_continue = should_continue or (lambda _text, reason: reason == "length")

        attempt = 0
        while True:
            try:
                result = self._complete(prompt, messages, base, max_continuations, should_continue)
                if len(result.text) >= min_chars:
                    return result
                write_text(self.project.log_file(f"{base}.failed_output.md"), result.text)
                error = (
                    f"output too small ({len(result.text)} chars < {min_chars}); likely a refusal or "
                    f"error. Saved to {self.project.rel(self.project.log_file(base + '.failed_output.md'))}"
                )
            except RuntimeError as e:
                error = str(e)
            attempt += 1
            if attempt > retries:
                raise RuntimeError(f"{task} failed: {error}")
            wait = 5 * attempt
            print(f"WARNING: {task} failed ({error}). Retrying in {wait}s ({attempt}/{retries})...")
            time.sleep(wait)

    def _complete(self, prompt, messages, base, max_continuations, should_continue) -> Result:
        t_start = dt.datetime.now()
        thinking_log = self.project.log_file(f"{base}.thinking.md")
        text = self.client.chat_completion(messages, thinking_log_path=thinking_log)
        debug = getattr(self.client, "last_http_debug", {}) or {}
        reason = debug.get("finish_reason")
        usage = [debug.get("usage")]
        continuations = 0
        while continuations < max_continuations and should_continue(text, reason):
            continuations += 1
            print(f"Response stopped early (finish_reason={reason}); continuing ({continuations}/{max_continuations})...")
            follow_up = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": CONTINUE_MESSAGE},
            ]
            more = self.client.chat_completion(follow_up, thinking_log_path=thinking_log)
            text = join_continuation(text, more)
            debug = getattr(self.client, "last_http_debug", {}) or {}
            reason = debug.get("finish_reason")
            usage.append(debug.get("usage"))
        elapsed = (dt.datetime.now() - t_start).total_seconds()
        save_json(
            self.project.log_file(f"{base}.request.json"),
            {
                "ts": now_iso(),
                "task": prompt.task,
                "prompt_variant": prompt.variant,
                "prompt_path": str(prompt.path),
                "prompt_origin": prompt.origin,
                "prompt_addendum": str(prompt.addendum_path) if prompt.addendum_path else None,
                "prompt_sha": prompt.sha,
                "model": self.model,
                "api_base": self.profile.get("api_base"),
                "finish_reason": reason,
                "continuations": continuations,
                "usage": usage,
                "elapsed_s": round(elapsed, 1),
                "chars": len(text),
            },
        )
        print(f"{prompt.task}: {elapsed:.1f}s, {len(text):,} chars, finish_reason={reason}")
        return Result(text=text, prompt=prompt, finish_reason=reason, continuations=continuations, usage=usage)


def join_continuation(text: str, more: str) -> str:
    """Join a continuation to the text it continues, avoiding doubled or missing spaces."""
    if not more:
        return text
    if text.endswith(("\n", " ")) or more.startswith(("\n", " ")):
        return text + more
    # Mid-word or mid-sentence continuation: join with a space unless it
    # continues punctuation (e.g. "...word" + ", and").
    if more[0] in ",.;:!?)'\"”’—–-":
        return text + more
    return text + " " + more
