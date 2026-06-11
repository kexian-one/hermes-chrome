from __future__ import annotations

import json
import os
import subprocess
import sys
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
    {
        "type": "function",
        "function": {
            "name": "run_ecom_script",
            "description": (
                "Run an approved helper script from skills/ecom-best-source/scripts. "
                "Use this for ecom-best-source workflows such as extracting JD product "
                "metadata, checking masked ecom config, fetching 1688 candidates, "
                "building keywords, and applying sourcing rules. The command runs with "
                "the current Python executable in the project root and returns stdout, "
                "stderr, and the exit code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "enum": [
                            "ecom_config.py",
                            "jd_product.py",
                            "keyword_builder.py",
                            "fetch_candidates.py",
                            "sourcing_rules.py",
                        ],
                        "description": "Approved script filename.",
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Command-line arguments, excluding Python and script path.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Timeout in seconds, default 120, max 300.",
                    },
                },
                "required": ["script"],
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


def _is_ecom_worker() -> bool:
    return os.environ.get("WORKER_SKILL_NAME", "").strip() == "ecom-best-source"


def _output_dir() -> Path | None:
    raw = os.environ.get("WORKER_OUTPUT_DIR", "").strip()
    return Path(raw).resolve() if raw else None


def _ecom_scratch_dir(project_root: Path) -> Path:
    output_dir = _output_dir()
    if output_dir:
        return output_dir / ".ecom-scratch"
    return project_root.resolve() / "outputs" / ".ecom-scratch"


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
    if tool_name == "run_ecom_script":
        return _run_ecom_script(args, project_root)
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

    if _is_ecom_worker() and target.suffix.lower() != ".csv":
        target = (_ecom_scratch_dir(project_root) / Path(path).name).resolve()

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

    # Same containment check as write_file (must stay under project root).
    # For ecom-best-source, intermediate JSON lives under a hidden scratch dir
    # inside the task output directory so it is readable but not uploaded.
    try:
        target = _resolve_safe(path, project_root, for_write=False)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    if not target.is_file():
        if _is_ecom_worker() and not Path(path).is_absolute():
            candidates = [_ecom_scratch_dir(project_root) / path]
            output_dir = _output_dir()
            if output_dir:
                candidates.append(output_dir / path)
            for candidate in candidates:
                if candidate.is_file():
                    target = candidate.resolve()
                    break
            else:
                return json.dumps({"error": f"file not found: {path}"})
        else:
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


_ECOM_SCRIPT_ALLOWLIST = frozenset({
    "ecom_config.py",
    "jd_product.py",
    "keyword_builder.py",
    "fetch_candidates.py",
    "sourcing_rules.py",
})
_ECOM_SCRIPT_INPUT_FLAGS = frozenset({"--input", "-i"})
_ECOM_SCRIPT_OUTPUT_FLAGS = frozenset({"--output", "-o"})
_ECOM_SCRIPT_STREAM_LIMIT = 20000


def _truncate_text(text: str, limit: int = _ECOM_SCRIPT_STREAM_LIMIT) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"\n...[truncated {omitted} chars]"


def _validate_ecom_script_args(argv: list[str], project_root: Path) -> str | None:
    """Keep script file output inside project root when callers pass --output."""
    project_root = project_root.resolve()
    for idx, arg in enumerate(argv):
        if arg not in _ECOM_SCRIPT_OUTPUT_FLAGS:
            continue
        if idx + 1 >= len(argv):
            return f"{arg} requires a path value"
        value = argv[idx + 1]
        target = Path(value)
        if target.is_absolute():
            resolved = target.resolve()
        else:
            output_dir = os.environ.get("WORKER_OUTPUT_DIR", "").strip()
            base = Path(output_dir).resolve() if output_dir else project_root
            resolved = (base / target).resolve()
        try:
            resolved.relative_to(project_root)
        except ValueError:
            return f"output path {resolved} is outside project root {project_root}"
    return None


def _resolve_ecom_runtime_path(value: str, project_root: Path, *, for_output: bool) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)

    project_root = project_root.resolve()
    if for_output:
        output_dir = _output_dir()
        base = (output_dir or project_root) if path.suffix.lower() == ".csv" else _ecom_scratch_dir(project_root)
        base.mkdir(parents=True, exist_ok=True)
        return str((base / path.name).resolve())

    output_dir = _output_dir()
    candidates = [
        _ecom_scratch_dir(project_root) / value,
        output_dir / value if output_dir else None,
        project_root / value,
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return str(candidate.resolve())
    return str((project_root / value).resolve())


def _rewrite_ecom_script_args(argv: list[str], project_root: Path) -> list[str]:
    out = list(argv)
    for idx, arg in enumerate(out[:-1]):
        if arg in _ECOM_SCRIPT_OUTPUT_FLAGS:
            out[idx + 1] = _resolve_ecom_runtime_path(out[idx + 1], project_root, for_output=True)
        elif arg in _ECOM_SCRIPT_INPUT_FLAGS:
            out[idx + 1] = _resolve_ecom_runtime_path(out[idx + 1], project_root, for_output=False)
    return out


def _run_ecom_script(args: dict, project_root: Path) -> str:
    script = args.get("script")
    argv = args.get("args", [])
    if not isinstance(script, str) or not script:
        return json.dumps({"error": "run_ecom_script requires script (str)"})
    script_name = Path(script).name
    if script_name != script or script_name not in _ECOM_SCRIPT_ALLOWLIST:
        return json.dumps({"error": f"script {script!r} is not allowed"})
    if not isinstance(argv, list) or not all(isinstance(v, str) for v in argv):
        return json.dumps({"error": "run_ecom_script args must be a list of strings"})

    try:
        timeout = int(args.get("timeout_seconds", 120))
    except (TypeError, ValueError):
        timeout = 120
    timeout = max(1, min(timeout, 300))

    env_root = os.environ.get("WORKER_PROJECT_ROOT", "").strip()
    if env_root:
        project_root = Path(env_root)
    project_root = project_root.resolve()
    script_path = project_root / "skills" / "ecom-best-source" / "scripts" / script_name
    if not script_path.is_file():
        return json.dumps({"error": f"script not found: {script_name}"})

    arg_error = _validate_ecom_script_args(argv, project_root)
    if arg_error:
        return json.dumps({"error": arg_error})

    env = dict(os.environ)
    output_dir = env.get("WORKER_OUTPUT_DIR", "").strip()
    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
    argv = _rewrite_ecom_script_args(argv, project_root)

    try:
        proc = subprocess.run(
            [sys.executable, str(script_path), *argv],
            cwd=project_root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return json.dumps({
            "ok": False,
            "script": script_name,
            "timeout_seconds": timeout,
            "error": "timeout",
            "stdout": _truncate_text(exc.stdout or ""),
            "stderr": _truncate_text(exc.stderr or ""),
        }, ensure_ascii=False)
    except OSError as exc:
        return json.dumps({"ok": False, "script": script_name, "error": str(exc)}, ensure_ascii=False)

    return json.dumps({
        "ok": proc.returncode == 0,
        "script": script_name,
        "returncode": proc.returncode,
        "stdout": _truncate_text(proc.stdout),
        "stderr": _truncate_text(proc.stderr),
    }, ensure_ascii=False)
