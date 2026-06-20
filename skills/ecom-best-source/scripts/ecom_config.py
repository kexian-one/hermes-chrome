from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


SECTION = "ecom_best_source"


@dataclass(frozen=True)
class EcomConfig:
    data_source: str
    onebound: dict[str, Any]
    alphashop_mcp: dict[str, Any]

    def apply_env(self) -> None:
        if self.onebound.get("key"):
            os.environ["OB_KEY"] = str(self.onebound["key"])
        if self.onebound.get("secret"):
            os.environ["OB_SECRET"] = str(self.onebound["secret"])
        if self.alphashop_mcp.get("ak"):
            os.environ["ALPHASHOP_AK"] = str(self.alphashop_mcp["ak"])
        if self.alphashop_mcp.get("sk"):
            os.environ["ALPHASHOP_SK"] = str(self.alphashop_mcp["sk"])

    def masked_status(self) -> dict[str, Any]:
        return {
            "data_source": self.data_source,
            "onebound": {
                "base": self.onebound.get("base", ""),
                "key": _mask(self.onebound.get("key", "")),
                "secret_configured": bool(self.onebound.get("secret")),
                "configured": bool(self.onebound.get("key") and self.onebound.get("secret")),
            },
            "alphashop_mcp": {
                "endpoint": self.alphashop_mcp.get("endpoint", ""),
                "ak": _mask(self.alphashop_mcp.get("ak", "")),
                "sk_configured": bool(self.alphashop_mcp.get("sk")),
                "configured": bool(self.alphashop_mcp.get("ak") and self.alphashop_mcp.get("sk")),
            },
        }


def load_ecom_config(project_root: Path | None = None) -> EcomConfig:
    root = project_root or _find_project_root()
    path = root / "config.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"config.yaml not found at {path}")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    section = cfg.get(SECTION)
    if not isinstance(section, dict):
        raise KeyError(f"config.yaml missing top-level {SECTION!r} section")
    return EcomConfig(
        data_source=str(section.get("data_source") or "hybrid"),
        onebound=dict(section.get("onebound") or {}),
        alphashop_mcp=dict(section.get("alphashop_mcp") or {}),
    )


def _find_project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "skills").is_dir():
            return parent
    return Path.cwd()


def _mask(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}...{text[-4:]}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Load ecom-best-source private config")
    parser.add_argument("--status", action="store_true", help="Print masked credential status")
    parser.add_argument("--apply-env", action="store_true", help="Load credentials into this process env")
    args = parser.parse_args()
    cfg = load_ecom_config()
    if args.apply_env:
        cfg.apply_env()
    if args.status or not args.apply_env:
        print(json.dumps(cfg.masked_status(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
