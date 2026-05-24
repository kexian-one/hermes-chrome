from __future__ import annotations

import json
import os
from pathlib import Path


BUILTIN_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write a UTF-8 text file to disk. Use this to save CSV reports, "
                "JSON outputs, or any artifact produced by a skill. By default "
                "relative paths resolve into THIS task's output directory — the "
                "master then forwards any files written here back to the message "
                "channel that requested the task (if the channel supports file "
                "uploads). Absolute paths must stay inside the project root and "
                "are NOT auto-forwarded. Creates parent directories as needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Filename or relative path (e.g. "
                            "'1688_applying_invoices_summary.csv') — goes into the "
                            "task's output dir. Absolute paths inside project root "
                            "also accepted but won't be auto-uploaded to the channel."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "File text content (UTF-8).",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a UTF-8 text file from disk. Use this for skill input "
                "files like `chase_messages_batch1.md`, prior task CSVs, or "
                "any text artifact you need to consume. Paths are resolved "
                "relative to the project root (or the task's output dir if "
                "WORKER_OUTPUT_DIR is set, same as write_file). Absolute paths "
                "must stay inside project root. Returns the full file content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Filename or relative path (e.g. "
                            "'chase_messages_batch1.md'). Resolved against project "
                            "root by default."
                        ),
                    },
                    "max_bytes": {
                        "type": "integer",
                        "description": (
                            "Optional safety cap on returned content size "
                            "(default 1 MB). Files larger than this return an error."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_knowledge",
            "description": (
                "Read a knowledge topic from the local knowledge base. Use this "
                "when you hit a problem that prior tasks may have learned about — "
                "e.g. unexpected API responses, DOM patterns, slider triggers. "
                "Returns the curated version if available, else the local "
                "by-machine version. Returns 'not found' if topic doesn't exist."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Topic name (kebab-case, see list in system prompt)",
                    },
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_knowledge",
            "description": (
                "Record a piece of learned knowledge to the local knowledge base, "
                "tagged with a topic. Use when you discover something worth "
                "remembering for future tasks: API quirks, DOM patterns, hidden "
                "constraints, observed risk-control triggers, etc. Writes to this "
                "machine's by-machine namespace; a merger machine periodically "
                "consolidates all machines' notes into a curated version. "
                "Topic should be kebab-case (e.g. '1688-mtop-amount-unit', "
                "'slider-triggers'). Content is markdown, can be a single "
                "observation or multiple paragraphs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": (
                            "Kebab-case topic name, e.g. '1688-mtop-amount-unit'. "
                            "Same topic across machines will be merged later by the merger."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Markdown content of the knowledge entry. Include "
                            "context, the observation, and an example if useful."
                        ),
                    },
                },
                "required": ["topic", "content"],
            },
        },
    },
]

BUILTIN_TOOL_NAMES: frozenset[str] = frozenset(t["function"]["name"] for t in BUILTIN_TOOLS)


def is_builtin(tool_name: str) -> bool:
    return tool_name in BUILTIN_TOOL_NAMES


def _resolve_safe(path_str: str, project_root: Path, *, for_write: bool = True) -> Path:
    """Resolve a file path safely.

    Precedence for project root:
      1. WORKER_PROJECT_ROOT env (set by master from config) — wins because
         master is the source of truth; cwd may be set elsewhere by systemd.
      2. The `project_root` arg (passed by caller, defaults to Path.cwd()).

    Relative path resolution depends on `for_write`:
    - `for_write=True` (default, used by write_file): relative paths resolve
      INTO `WORKER_OUTPUT_DIR` if it's set — outputs land in the per-task
      dir so master can forward them via the IM channel.
    - `for_write=False` (read_file): relative paths resolve to project_root,
      so skills can read input files (e.g. `chase_messages_batch1.md`) the
      user dropped at project root.

    Absolute paths must always stay inside project root either way.
    """
    env_root = os.environ.get("WORKER_PROJECT_ROOT", "").strip()
    if env_root:
        project_root = Path(env_root)
    project_root = project_root.resolve()
    p = Path(path_str)
    if p.is_absolute():
        resolved = p.resolve()
    else:
        if for_write:
            output_dir = os.environ.get("WORKER_OUTPUT_DIR", "")
            base = Path(output_dir).resolve() if output_dir else project_root
        else:
            base = project_root
        resolved = (base / p).resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise ValueError(
            f"path {resolved} is outside project root {project_root}"
        ) from exc
    return resolved


def execute_builtin(
    tool_name: str,
    arguments_json: str,
    project_root: Path,
    *,
    machine_id: str = "unknown",
    knowledge_root: Path | None = None,
) -> str:
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"invalid JSON arguments: {exc}"})

    if tool_name == "write_file":
        return _write_file(args, project_root)
    if tool_name == "read_file":
        return _read_file(args, project_root)
    if tool_name == "append_knowledge":
        return _append_knowledge(args, machine_id, knowledge_root or (project_root / "knowledge"))
    if tool_name == "read_knowledge":
        return _read_knowledge(args, knowledge_root or (project_root / "knowledge"))
    return json.dumps({"error": f"unknown builtin tool: {tool_name}"})


# Paths/files write_file is forbidden to touch (project source + config + state)
_WRITE_FORBIDDEN_DIRS = frozenset({"agent", "tests", "deploy", "scripts", "DOC", "skills", "state", ".git"})
_WRITE_FORBIDDEN_FILES = frozenset({"config.yaml", "config.example.yaml", "pyproject.toml", ".gitignore",
                                     "README.md", "AGENTS.md"})
_WRITE_FORBIDDEN_SUFFIXES = frozenset({".py"})


def _write_path_allowed(rel: Path) -> tuple[bool, str]:
    """Return (allowed, reason). Forbids writes to source / config / state."""
    parts = rel.parts
    if parts and parts[0] in _WRITE_FORBIDDEN_DIRS:
        return False, f"top-level dir '{parts[0]}/' is read-only for builtin write_file"
    if str(rel) in _WRITE_FORBIDDEN_FILES:
        return False, f"'{rel}' is a protected config/doc file"
    if rel.suffix in _WRITE_FORBIDDEN_SUFFIXES:
        return False, f"writing {rel.suffix} files is not allowed (would clobber code)"
    return True, ""


def _write_file(args: dict, project_root: Path) -> str:
    path = args.get("path")
    content = args.get("content")
    if not isinstance(path, str) or not isinstance(content, str):
        return json.dumps({"error": "write_file requires path (str) and content (str)"})

    try:
        target = _resolve_safe(path, project_root)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    rel = target.relative_to(project_root.resolve())
    allowed, reason = _write_path_allowed(rel)
    if not allowed:
        return json.dumps({"error": f"write_file refused: {reason}"})

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return json.dumps({
        "ok": True,
        "path": str(rel),
        "bytes_written": len(content.encode("utf-8")),
    })


_READ_FILE_DEFAULT_MAX_BYTES = 1024 * 1024    # 1 MB cap on returned content


def _read_file(args: dict, project_root: Path) -> str:
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return json.dumps({"error": "read_file requires path (str)"})
    max_bytes_raw = args.get("max_bytes", _READ_FILE_DEFAULT_MAX_BYTES)
    try:
        max_bytes = int(max_bytes_raw)
    except (TypeError, ValueError):
        max_bytes = _READ_FILE_DEFAULT_MAX_BYTES
    if max_bytes <= 0:
        max_bytes = _READ_FILE_DEFAULT_MAX_BYTES

    # Same containment check as write_file (must stay under project root),
    # but relative paths resolve to project_root NOT output_dir — skills
    # need to read input files like chase_messages_batch1.md at root.
    try:
        target = _resolve_safe(path, project_root, for_write=False)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    if not target.is_file():
        return json.dumps({"error": f"file not found: {path}"})

    try:
        size = target.stat().st_size
    except OSError as exc:
        return json.dumps({"error": f"stat failed: {exc}"})

    if size > max_bytes:
        return json.dumps({
            "error": f"file too large ({size} bytes > {max_bytes} cap). "
                     f"Raise max_bytes to read it, or use a different approach.",
        })

    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return json.dumps({
            "error": f"file is not valid UTF-8 — read_file only handles text files",
        })
    except OSError as exc:
        return json.dumps({"error": f"read failed: {exc}"})

    rel = target.relative_to(project_root.resolve())
    return json.dumps({
        "ok": True,
        "path": str(rel),
        "size": size,
        "content": content,
    })


_TOPIC_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$")


def _read_knowledge(args: dict, knowledge_root: Path) -> str:
    topic = args.get("topic")
    if not isinstance(topic, str) or not topic:
        return json.dumps({"error": "read_knowledge requires topic (str)"})
    if not _TOPIC_RE.match(topic):
        return json.dumps({"error": f"topic {topic!r} must be kebab-case (a-z, 0-9, hyphens), no path traversal"})

    from agent.knowledge_store import KnowledgeStore
    store = KnowledgeStore(root=knowledge_root)
    curated = store.load_curated(topic)
    if curated:
        return json.dumps({"ok": True, "topic": topic, "source": "curated", "content": curated})

    views = store.list_machine_views(topic)
    if views:
        return json.dumps({
            "ok": True,
            "topic": topic,
            "source": "by-machine",
            "machine_views": views,
        })

    return json.dumps({"ok": False, "topic": topic, "error": "not found"})


def _append_knowledge(args: dict, machine_id: str, knowledge_root: Path) -> str:
    topic = args.get("topic")
    content = args.get("content")
    if not isinstance(topic, str) or not isinstance(content, str):
        return json.dumps({"error": "append_knowledge requires topic (str) and content (str)"})
    if not _TOPIC_RE.match(topic):
        return json.dumps({"error": f"topic {topic!r} must be kebab-case (a-z, 0-9, hyphens)"})
    if not content.strip():
        return json.dumps({"error": "content must not be empty"})

    from agent.knowledge_store import KnowledgeStore
    store = KnowledgeStore(root=knowledge_root)
    store.append(machine_id, topic, content)
    return json.dumps({
        "ok": True,
        "machine_id": machine_id,
        "topic": topic,
        "bytes_written": len(content.encode("utf-8")),
    })
