from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


class KnowledgeStore:
    def __init__(self, root: Path = Path("./knowledge")) -> None:
        self._root = Path(root)

    # ── internal paths ────────────────────────────────────────────────────────

    def _machine_dir(self, machine_id: str) -> Path:
        return self._root / "by-machine" / machine_id

    def _machine_file(self, machine_id: str, topic: str) -> Path:
        return self._machine_dir(machine_id) / f"{topic}.md"

    def _curated_file(self, topic: str) -> Path:
        return self._root / "curated" / f"{topic}.md"

    # ── write (machine-local only) ────────────────────────────────────────────

    def append(self, machine_id: str, topic: str, content: str) -> None:
        """OS-level append. No read-modify-write race; concurrent appends from
        different workers don't lose data (OS append is atomic for small writes
        on both POSIX and Windows)."""
        target = self._machine_file(machine_id, topic)
        target.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(tz=timezone.utc).isoformat()
        block = f"\n\n<!-- appended {ts} -->\n\n{content}"
        with target.open("a", encoding="utf-8") as f:
            f.write(block)

    # ── read ──────────────────────────────────────────────────────────────────

    def load_curated(self, topic: str) -> str | None:
        f = self._curated_file(topic)
        if f.is_file():
            return f.read_text(encoding="utf-8")
        return None

    def load_machine(self, machine_id: str, topic: str) -> str | None:
        f = self._machine_file(machine_id, topic)
        if f.is_file():
            return f.read_text(encoding="utf-8")
        return None

    def list_machine_views(self, topic: str) -> dict[str, str]:
        by_machine = self._root / "by-machine"
        result: dict[str, str] = {}
        if not by_machine.is_dir():
            return result
        for machine_dir in sorted(by_machine.iterdir()):
            if not machine_dir.is_dir():
                continue
            f = machine_dir / f"{topic}.md"
            if f.is_file():
                result[machine_dir.name] = f.read_text(encoding="utf-8")
        return result

    def list_topics(self) -> list[str]:
        topics: set[str] = set()
        by_machine = self._root / "by-machine"
        if by_machine.is_dir():
            for machine_dir in by_machine.iterdir():
                if not machine_dir.is_dir():
                    continue
                for f in machine_dir.iterdir():
                    if f.suffix == ".md":
                        topics.add(f.stem)
        curated = self._root / "curated"
        if curated.is_dir():
            for f in curated.iterdir():
                if f.suffix == ".md":
                    topics.add(f.stem)
        return sorted(topics)

    def all_machine_topics(self) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        by_machine = self._root / "by-machine"
        if not by_machine.is_dir():
            return pairs
        for machine_dir in sorted(by_machine.iterdir()):
            if not machine_dir.is_dir():
                continue
            for f in sorted(machine_dir.iterdir()):
                if f.suffix == ".md":
                    pairs.append((machine_dir.name, f.stem))
        return pairs

    # ── curated write (merger only) ───────────────────────────────────────────

    def write_curated(self, topic: str, content: str) -> None:
        """Atomic write: tempfile in same dir → os.replace.

        Direct write_text truncates the file before content is persisted;
        a crash mid-write leaves curated knowledge empty.
        """
        import os
        import tempfile
        target = self._curated_file(topic)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8",
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(content)
            tmp_name = tmp.name
        os.replace(tmp_name, target)
