from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

import yaml


CONFIG_FILE_ENV = "ALL_IN_AI_CONFIG"
CONFIG_FILE_DEFAULT = Path("config.yaml")


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    model: str
    api_key: str


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str
    mcp_port: int
    llm_multimodal: LLMSettings
    llm_reasoning: LLMSettings
    mcp_server_js_path: Path = Path("./deploy/oicc-b1/host/mcp-server.js")
    skills_dir: Path = Path("./skills")
    log_dir: Path = Path("./logs")


@dataclass(frozen=True)
class BrowserSpec:
    """Per-worker browser metadata for restart_browser intent.

    Restart kills only processes whose .Path matches `executable`, so Edge
    stable (Program Files) and Edge Dev (AppData) are NOT confused — they
    have different install paths.
    """
    worker_id: str
    name: str          # chrome / edge / brave / vivaldi / opera
    executable: Path
    warmup_url: str = "https://work.1688.com"


@dataclass(frozen=True)
class KnowledgeConfig:
    enabled: bool = True
    root: Path = Path("./knowledge")
    is_merger: bool = False
    consolidate_cron: str = "0 2 * * *"
    repo_url: str = ""               # if set, auto git clone on first start
    pull_interval_secs: int = 300    # 5 min default


@dataclass(frozen=True)
class SkillsConfig:
    dir: Path = Path("./skills")
    repo_url: str = ""               # if set, auto git clone on first start
    pull_interval_secs: int = 1800   # 30 min default


@dataclass(frozen=True)
class BotConfig:
    app_id: str
    app_secret: str
    authorized_user_ids: tuple[str, ...]
    enabled: bool = True
    # alert routing: ALL proactive pushes (slider warnings, cron task-done,
    # worker crash) go to whichever bot has `is_alert_target: true`. At most
    # one bot per machine may have this flag. `alert_chat_id` is the actual
    # chat_id the alerts get pushed to (the Feishu API needs a chat_id; the
    # bot itself doesn't know which of its chats you want alerts in).
    is_alert_target: bool = False
    alert_chat_id: str = ""
    type: str = "feishu"       # "feishu" / "dingtalk" / "wecom" (only feishu implemented for now)


@dataclass(frozen=True)
class MasterConfig:
    workers: list[WorkerConfig]
    machine_name: str = "machine"   # global per-machine label, used in all card titles
    # default_factory so the cwd is captured at construction time, not at
    # class-definition import time (otherwise a chdir between import and
    # construction would silently change the project root).
    project_root: Path = field(default_factory=Path.cwd)
    cron_schedule: str = "0 9,15 * * *"
    log_dir: Path = Path("./logs")
    bots: tuple[BotConfig, ...] = ()
    knowledge: KnowledgeConfig = KnowledgeConfig()
    skills: SkillsConfig = SkillsConfig()
    browsers: dict[str, BrowserSpec] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validation runs for ANY MasterConfig construction (default path, tests,
        future programmatic callers). Without this, only `default_master_config()`
        enforced the rules, so a hand-built MasterConfig could silently violate
        invariants."""
        _validate_bots(self.bots)

    def active_bots(self) -> list[BotConfig]:
        """Bots to actually run. Returns only enabled bots."""
        return [b for b in self.bots if b.enabled]


def _validate_bots(bots: tuple[BotConfig, ...]) -> None:
    """Shared bot-list validation: unique app_id, at most one alert target,
    alert target has alert_chat_id. Zero alert targets is allowed (no proactive
    pushes — only @bot replies)."""
    seen_app_ids: set[str] = set()
    for b in bots:
        if b.app_id in seen_app_ids:
            raise ValueError(
                f"duplicate bot app_id {b.app_id!r} — each bot needs a unique app_id"
            )
        seen_app_ids.add(b.app_id)
    alert_targets = [b for b in bots if b.is_alert_target]
    if len(alert_targets) > 1:
        ids = ", ".join(b.app_id for b in alert_targets)
        raise ValueError(
            f"only one bot may have `is_alert_target: true`. Found {len(alert_targets)}: {ids}"
        )
    for b in alert_targets:
        if not b.alert_chat_id:
            raise ValueError(
                f"bot {b.app_id!r}: is_alert_target=true requires a non-empty alert_chat_id"
            )


def _resolve_config_path(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        return explicit
    env_path = os.environ.get(CONFIG_FILE_ENV)
    if env_path:
        return Path(env_path)
    if CONFIG_FILE_DEFAULT.is_file():
        return CONFIG_FILE_DEFAULT
    return None


def _config_base_dir() -> Path:
    """Directory containing config.yaml."""
    p = _resolve_config_path()
    if p is None:
        return Path.cwd()
    return p.resolve().parent


def _require_absolute(path_str: str, label: str) -> Path:
    """Config paths MUST be absolute. Relative paths break when master is
    started from a different cwd (e.g. via task scheduler vs terminal).

    Returns the Path. Raises ValueError if relative.
    """
    raw = str(path_str).strip()
    if not raw:
        raise ValueError(f"config '{label}' is empty — fill in an absolute path")
    p = Path(raw)
    if not p.is_absolute():
        raise ValueError(
            f"config '{label}' must be an absolute path, got {raw!r}. "
            f"Edit config.yaml and use a full path (Windows: 'C:\\\\Users\\\\you\\\\...'; "
            f"POSIX: '/home/you/...') instead of a relative one."
        )
    return p


def _require_positive_interval(value, label: str) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"config '{label}' must be a positive integer, got {value!r}")
    if v <= 0:
        raise ValueError(f"config '{label}' must be > 0, got {v}")
    return v


@lru_cache(maxsize=4)
def load_config_file(path: Path | None = None) -> dict:
    target = _resolve_config_path(path)
    if target is None or not target.is_file():
        return {}
    with target.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{target}: top level must be a mapping")
    return data


def _llm_from_dict(d: dict, label: str) -> LLMSettings:
    for key in ("base_url", "model", "api_key"):
        if key not in d:
            raise KeyError(f"llm.{label}.{key} missing in config")
    return LLMSettings(
        base_url=str(d["base_url"]),
        model=str(d["model"]),
        api_key=str(d["api_key"]),
    )


def _load_llm_pair(config: dict | None = None) -> tuple[LLMSettings, LLMSettings]:
    cfg = config if config is not None else load_config_file()
    llm = cfg.get("llm")
    if not isinstance(llm, dict):
        raise KeyError(
            f"config file missing top-level 'llm' section. "
            f"Copy config.example.yaml to {CONFIG_FILE_DEFAULT} and fill in your keys."
        )
    if "multimodal" not in llm or "reasoning" not in llm:
        raise KeyError("config 'llm' must contain both 'multimodal' and 'reasoning' sub-sections")
    return (
        _llm_from_dict(llm["multimodal"], "multimodal"),
        _llm_from_dict(llm["reasoning"], "reasoning"),
    )


def worker_config_from_file(worker_id: str, mcp_port: int | None = None) -> WorkerConfig:
    b_index = int(worker_id.lstrip("b"))
    port = mcp_port if mcp_port is not None else (18764 + b_index)
    multimodal, reasoning = _load_llm_pair()
    return WorkerConfig(
        worker_id=worker_id,
        mcp_port=port,
        llm_multimodal=multimodal,
        llm_reasoning=reasoning,
        mcp_server_js_path=Path(f"./deploy/oicc-{worker_id}/host/mcp-server.js").resolve(),
    )


def _knowledge_config_from_dict(d: dict) -> KnowledgeConfig:
    return KnowledgeConfig(
        enabled=bool(d.get("enabled", True)),
        root=_require_absolute(d.get("root", ""), "knowledge.root"),
        is_merger=bool(d.get("is_merger", False)),
        consolidate_cron=str(d.get("consolidate_cron", "0 2 * * *")),
        repo_url=str(d.get("repo_url", "")),
        pull_interval_secs=_require_positive_interval(
            d.get("pull_interval_secs", 300), "knowledge.pull_interval_secs"
        ),
    )


def _skills_config_from_dict(d: dict) -> SkillsConfig:
    return SkillsConfig(
        dir=_require_absolute(d.get("dir", ""), "skills.dir"),
        repo_url=str(d.get("repo_url", "")),
        pull_interval_secs=_require_positive_interval(
            d.get("pull_interval_secs", 1800), "skills.pull_interval_secs"
        ),
    )


_SUPPORTED_BOT_TYPES = {"feishu"}  # add "dingtalk", "wecom" when implemented


def _bot_config_from_dict(d: dict) -> BotConfig:
    bot_type = str(d.get("type", "feishu")).strip().lower() or "feishu"
    if bot_type not in _SUPPORTED_BOT_TYPES:
        raise ValueError(
            f"bot type {bot_type!r} not implemented yet. Supported: {sorted(_SUPPORTED_BOT_TYPES)}"
        )
    return BotConfig(
        enabled=bool(d.get("enabled", True)),
        app_id=str(d["app_id"]),
        app_secret=str(d["app_secret"]),
        authorized_user_ids=tuple(str(uid) for uid in d.get("authorized_user_ids", [])),
        is_alert_target=bool(d.get("is_alert_target", False)),
        alert_chat_id=str(d.get("alert_chat_id", "")),
        type=bot_type,
    )


def default_master_config() -> MasterConfig:
    cfg = load_config_file()
    multimodal, reasoning = _load_llm_pair(cfg)

    skills = SkillsConfig()
    if isinstance(cfg.get("skills"), dict):
        skills = _skills_config_from_dict(cfg["skills"])

    workers = [
        WorkerConfig(
            worker_id=f"b{i}",
            mcp_port=18764 + i,
            llm_multimodal=multimodal,
            llm_reasoning=reasoning,
            mcp_server_js_path=Path(f"./deploy/oicc-b{i}/host/mcp-server.js").resolve(),
            skills_dir=skills.dir,
        )
        for i in range(1, 7)
    ]
    bots_list: list[BotConfig] = []
    raw_bots = cfg.get("bots")
    if isinstance(raw_bots, list):
        for d in raw_bots:
            if isinstance(d, dict):
                bots_list.append(_bot_config_from_dict(d))
    # MasterConfig.__post_init__ runs `_validate_bots(...)` below; no need to
    # duplicate the validation here.
    knowledge = KnowledgeConfig()
    if isinstance(cfg.get("knowledge"), dict):
        knowledge = _knowledge_config_from_dict(cfg["knowledge"])

    browsers: dict[str, BrowserSpec] = {}
    if isinstance(cfg.get("browsers"), dict):
        for wid, d in cfg["browsers"].items():
            if not isinstance(d, dict):
                continue
            browsers[wid] = BrowserSpec(
                worker_id=wid,
                name=str(d.get("name", "")),
                executable=_require_absolute(
                    d.get("executable", ""), f"browsers.{wid}.executable"
                ),
                warmup_url=str(d.get("warmup_url", "https://work.1688.com")),
            )

    machine_name = str(cfg.get("machine_name", "")).strip() or "machine"
    project_root = _config_base_dir()

    return MasterConfig(
        workers=workers, machine_name=machine_name, project_root=project_root,
        bots=tuple(bots_list),
        knowledge=knowledge, skills=skills, browsers=browsers,
    )
