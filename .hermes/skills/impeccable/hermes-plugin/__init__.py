"""impeccable-design-hook — the Impeccable design detector, wired into Hermes Agent's
plugin-hook systems.

Two passes, mirroring the reference Claude Code integration (`.claude/settings.json`)
on Hermes' hook surface:

- **Per-edit pass** (`transform_tool_result` on ``write_file`` / ``patch``): runs the
  Impeccable engine with a Claude-Code ``PostToolUse`` event and appends its findings
  block to the tool result, so the model sees design feedback immediately after each
  edit.
- **Stop pass** (`pre_verify`): at the turn's verify gate (fires only when the turn
  landed file edits), runs the engine's deep pass with the Codex ``Stop`` contract
  (``{"decision": "block", "reason": ...}``) and returns ``{"action": "continue", ...}``
  so the turn keeps going until the findings are addressed. The engine deduplicates
  anything the per-edit pass already surfaced (shared ``session_id``), so a clean Stop
  returns ``None`` and the turn finishes.

The engine binary is resolved per event: ``IMPECCABLE_DESIGN_HOOK_BIN`` first, then the
global Hermes skill install (``<hermes home>/skills/impeccable/scripts/impeccable``),
then a project-local skill install found by walking up from the edited file. The hook
respects the project's own detector config: ``impeccable hooks off`` (which writes
``hook.enabled: false`` to ``.impeccable/config.json``) silences it there, because the
engine checks that flag itself.

Gating: by default the engine only runs for writes inside a project that opted in — a
project-root ``.impeccable/`` directory or a project-local impeccable skill install.
Set ``IMPECCABLE_DESIGN_HOOK_SCOPE=always`` to scan writes in every project.
Kill switch: ``IMPECCABLE_DESIGN_HOOK_DISABLE=1``. Engine override:
``IMPECCABLE_DESIGN_HOOK_BIN``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SKILL_DIR_NAME = "impeccable"
_SKILL_SUBDIRS = (".hermes", ".claude", ".agents", ".cursor", ".codex")
_MARKER_DIRS = (".impeccable",) + tuple(f"{d}/skills/{_SKILL_DIR_NAME}" for d in _SKILL_SUBDIRS)
_LAUNCHER_REL = ("scripts", "impeccable")

_TRUTHY = {"1", "true", "yes", "on"}
_PER_EDIT_TIMEOUT_S = 10.0
_STOP_TIMEOUT_S = 25.0  # stay under the default 30s plugins.hook_callback_timeout

# Claude's PostToolUse contract output key.
_CONTEXT_KEY = "additionalContext"
# Codex Stop contract output keys (what pre_verify's parser understands natively).
_BLOCK_KEYS = ("decision", "reason")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in _TRUTHY


def _hermes_home() -> Path:
    """Profile-aware Hermes home (respects HERMES_HOME / profiles); falls back to ~/.hermes
    when hermes_constants is not importable (standalone runs outside the Hermes process)."""
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def _launcher_at(skill_dir: Path) -> Optional[Path]:
    launcher = skill_dir.joinpath(*_LAUNCHER_REL)
    if launcher.is_file() and os.access(launcher, os.X_OK):
        return launcher
    return None


def _skill_dir_for(file_path: str) -> Optional[tuple[Path, Path]]:
    """(project root, engine launcher) for the edited file: walks up to the project root
    and prefers a project-local skill launcher, falling back to the global Hermes skill.
    Returns None when the project did not opt in and scope is not ``always``."""
    try:
        current = Path(file_path).resolve().parent
    except (OSError, ValueError):
        return None
    root: Optional[Path] = None
    while True:
        if any((current / marker).exists() for marker in _MARKER_DIRS):
            root = current
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    always = os.environ.get("IMPECCABLE_DESIGN_HOOK_SCOPE", "").lower() == "always"
    if root is None:
        if not always or not file_path:
            return None
        try:
            root = Path(file_path).resolve().parent
        except (OSError, ValueError):
            return None
    # Project-local skill wins so a pinned project install shadows the global one.
    for base in (root, *_walk_ancestors(root)):
        for sub in _SKILL_SUBDIRS:
            launcher = _launcher_at(base / sub / "skills" / _SKILL_DIR_NAME)
            if launcher is not None:
                return root, launcher
    global_launcher = _launcher_at(_hermes_home() / "skills" / _SKILL_DIR_NAME)
    if global_launcher is not None:
        return root, global_launcher
    return None


def _walk_ancestors(start: Path):
    current = start
    while True:
        parent = current.parent
        if parent == current:
            return
        yield parent
        current = parent


def _engine_bin(file_path: str) -> Optional[tuple[Path, Path]]:
    """(cwd, engine entrypoint): env override first, then the skill launcher resolved
    for the edited file."""
    override = os.environ.get("IMPECCABLE_DESIGN_HOOK_BIN", "")
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            resolved = _skill_dir_for(file_path)
            cwd = resolved[0] if resolved else Path(file_path).resolve().parent
            return cwd, candidate
        return None
    resolved = _skill_dir_for(file_path)
    if resolved is None:
        return None
    return resolved[0], resolved[1]


def _run_engine(event: dict[str, object], harness: str, cwd: Path, launcher: Path,
                timeout: float) -> Optional[dict[str, object]]:
    """Pipe one hook event through the engine; returns its parsed stdout JSON or None."""
    env = dict(os.environ)
    env["IMPECCABLE_HOOK_HARNESS"] = harness
    skill_dir = launcher.parent.parent
    if skill_dir.is_dir():
        env.setdefault("IMPECCABLE_SKILL_DIR", str(skill_dir))
    try:
        proc = subprocess.run(
            [str(launcher), "hook"],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(cwd),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("impeccable-design-hook: engine run failed: %s", exc)
        return None
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return None
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        logger.warning("impeccable-design-hook: engine stdout was not JSON: %s", stdout[:200])
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract(payload: dict[str, object], wrapper_key: str, value_key: str) -> str:
    """Pull a text field out of an engine payload (``{wrapper: {value: str}}``)."""
    wrapper = payload.get(wrapper_key)
    if not isinstance(wrapper, dict):
        return ""
    text = wrapper.get(value_key)
    return text if isinstance(text, str) else ""


def _edit_path(args: object) -> str:
    """The written file's path from write_file/patch tool args (both use ``path``)."""
    if not isinstance(args, dict):
        return ""
    path = args.get("path")
    return path if isinstance(path, str) else ""


def _result_is_error(result: object) -> bool:
    """Don't decorate failed writes — the model already has bigger problems."""
    if not isinstance(result, str):
        return True
    try:
        parsed = json.loads(result)
    except (ValueError, TypeError):
        return False
    return isinstance(parsed, dict) and "error" in parsed and len(parsed) <= 2


def _on_transform_tool_result(
    tool_name: str = "", args: object = None, result: object = None, session_id: str = "", **_: object
) -> Optional[str]:
    """Per-edit pass: append design findings to a successful write_file/patch result."""
    if _env_flag("IMPECCABLE_DESIGN_HOOK_DISABLE"):
        return None
    if tool_name not in {"write_file", "patch"} or not isinstance(result, str):
        return None
    if _result_is_error(result):
        return None
    path = _edit_path(args)
    if not path:
        return None
    resolved = _engine_bin(path)
    if resolved is None:
        return None
    payload = _run_engine(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": path},
            "cwd": str(resolved[0]),
            "session_id": session_id,
        },
        harness="claude",
        cwd=resolved[0],
        launcher=resolved[1],
        timeout=_PER_EDIT_TIMEOUT_S,
    )
    if payload is None:
        return None
    context = _extract(payload, "hookSpecificOutput", _CONTEXT_KEY).strip()
    if not context:
        return None
    return result + "\n\n" + context


def _on_pre_verify(
    session_id: str = "", changed_paths: object = None, **_: object
) -> Optional[dict[str, str]]:
    """Stop pass: deep-check this turn's edited projects; continue the turn on findings."""
    if _env_flag("IMPECCABLE_DESIGN_HOOK_DISABLE"):
        return None
    if not isinstance(changed_paths, list):
        return None
    resolved: Optional[tuple[Path, Path]] = None
    for entry in changed_paths:
        if isinstance(entry, str):
            resolved = _engine_bin(entry)
            if resolved is not None:
                break
    if resolved is None:
        return None
    payload = _run_engine(
        {
            "hook_event_name": "Stop",
            "cwd": str(resolved[0]),
            "session_id": session_id,
        },
        harness="codex",
        cwd=resolved[0],
        launcher=resolved[1],
        timeout=_STOP_TIMEOUT_S,
    )
    if payload is None:
        return None
    # Codex Stop contract: findings that should continue the turn are a top-level
    # blocking decision; empty text means nothing new to report.
    decision = str(payload.get(_BLOCK_KEYS[0]) or "").strip().lower()
    reason = payload.get(_BLOCK_KEYS[1])
    if decision == "block" and isinstance(reason, str) and reason.strip():
        return {"action": "continue", "message": reason.strip()}
    return None


def register(ctx: object) -> None:
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
    ctx.register_hook("pre_verify", _on_pre_verify)
