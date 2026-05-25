from __future__ import annotations

import io
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import TextIO


DEFAULT_MAX_LOG_BYTES = 50 * 1024 * 1024
DEFAULT_LOG_RETENTION_DAYS = 30


class TeeStream(io.TextIOBase):
    """Mirror writes to console and a log file."""

    def __init__(self, console: TextIO, log_file: TextIO) -> None:
        self._console = console
        self._log_file = log_file

    def write(self, s: str) -> int:
        self._console.write(s)
        self._log_file.write(s)
        return len(s)

    def flush(self) -> None:
        self._console.flush()
        self._log_file.flush()


class RotatingDailyLogFile(io.TextIOBase):
    """Append to a daily log file and rotate when it exceeds max bytes."""

    def __init__(self, base_path: Path, max_bytes: int, encoding: str = "utf-8") -> None:
        self.base_path = base_path
        self.max_bytes = max_bytes
        self._encoding = encoding
        self.base_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.base_path.open("a", encoding=self._encoding)
        self._current_size = self._read_current_size()

    def write(self, s: str) -> int:
        if not s:
            return 0

        incoming_size = len(s.encode(self._encoding, errors="replace"))

        if self._should_rotate_for(incoming_size):
            self._rotate()

        self._file.write(s)
        self._current_size += incoming_size
        return len(s)

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        try:
            self._file.flush()
            self._file.close()
        except Exception:
            pass

    def _should_rotate_for(self, incoming_size: int) -> bool:
        return self._current_size > 0 and (self._current_size + incoming_size) > self.max_bytes

    def _rotate(self) -> None:
        try:
            self._file.flush()
            self._file.close()
        except Exception:
            pass

        highest = self._find_highest_suffix()
        for idx in range(highest, 0, -1):
            src = self._suffix_path(idx)
            dst = self._suffix_path(idx + 1)
            if src.exists():
                src.replace(dst)

        if self.base_path.exists():
            self.base_path.replace(self._suffix_path(1))

        self._file = self.base_path.open("a", encoding=self._encoding)
        self._current_size = 0

    def _read_current_size(self) -> int:
        try:
            return self.base_path.stat().st_size if self.base_path.exists() else 0
        except Exception:
            return 0

    def _find_highest_suffix(self) -> int:
        highest = 0
        pattern = re.compile(rf"^{re.escape(self.base_path.name)}\.(\d+)$")
        for child in self.base_path.parent.iterdir():
            if not child.is_file():
                continue
            match = pattern.match(child.name)
            if not match:
                continue
            try:
                highest = max(highest, int(match.group(1)))
            except ValueError:
                continue
        return highest

    def _suffix_path(self, idx: int) -> Path:
        return self.base_path.with_name(f"{self.base_path.name}.{idx}")


class LogManager:
    def __init__(self, log_path: Path, original_stdout: TextIO, original_stderr: TextIO, log_file: TextIO) -> None:
        self.log_path = log_path
        self._original_stdout = original_stdout
        self._original_stderr = original_stderr
        self._log_file = log_file

    def close(self) -> None:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr
        try:
            self._log_file.flush()
            self._log_file.close()
        except Exception:
            pass


def _is_date_dir(path: Path) -> bool:
    try:
        datetime.strptime(path.name, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def migrate_legacy_log_layout(log_root: Path) -> None:
    """Move legacy log/YYYY-MM-DD/* into log/general/YYYY-MM-DD/*."""
    general_root = log_root / "general"

    for child in log_root.iterdir() if log_root.exists() else []:
        if not child.is_dir() or not _is_date_dir(child):
            continue
        target_date_dir = general_root / child.name
        target_date_dir.mkdir(parents=True, exist_ok=True)

        for file_path in child.glob("*.txt"):
            target = target_date_dir / file_path.name
            if target.exists():
                target = target_date_dir / f"{file_path.stem}_legacy{file_path.suffix}"
            try:
                file_path.replace(target)
            except Exception:
                continue

        try:
            if not any(child.iterdir()):
                child.rmdir()
        except Exception:
            pass


def cleanup_old_logs(log_root: Path, keep_days: int = DEFAULT_LOG_RETENTION_DAYS) -> None:
    if not log_root.exists():
        return

    cutoff = datetime.now() - timedelta(days=keep_days)

    for path in log_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime)
            if modified < cutoff:
                path.unlink(missing_ok=True)
        except Exception:
            # Ignore cleanup failures so crawler can still run.
            continue

    # Remove empty date folders after old files are deleted.
    for folder in sorted([p for p in log_root.rglob("*") if p.is_dir()], key=lambda p: len(p.parts), reverse=True):
        try:
            if not any(folder.iterdir()):
                folder.rmdir()
        except Exception:
            continue


def _sanitize_site_name(site_name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", (site_name or "general").strip())
    return cleaned or "general"


def setup_file_logging(
    project_root: Path,
    keep_days: int = DEFAULT_LOG_RETENTION_DAYS,
    site_name: str = "general",
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
) -> LogManager:
    log_root = project_root / "log"
    log_root.mkdir(parents=True, exist_ok=True)

    migrate_legacy_log_layout(log_root)

    site_dir = log_root / _sanitize_site_name(site_name)
    # Keep at most one month of logs for the current site.
    effective_keep_days = min(max(int(keep_days), 1), DEFAULT_LOG_RETENTION_DAYS)
    cleanup_old_logs(log_root=site_dir, keep_days=effective_keep_days)

    month_dir = site_dir / datetime.now().strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)

    log_file_path = month_dir / f"run_{datetime.now().strftime('%Y-%m-%d')}.txt"

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    log_file = RotatingDailyLogFile(log_file_path, max_bytes=max_log_bytes, encoding="utf-8")

    sys.stdout = TeeStream(original_stdout, log_file)
    sys.stderr = TeeStream(original_stderr, log_file)

    return LogManager(
        log_path=log_file_path,
        original_stdout=original_stdout,
        original_stderr=original_stderr,
        log_file=log_file,
    )
