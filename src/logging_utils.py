from __future__ import annotations

import io
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TextIO


DEFAULT_MAX_LOG_BYTES = 80 * 1024 * 1024
DEFAULT_LOG_RETENTION_DAYS = 7
DEFAULT_LOG_CLEANUP_INTERVAL_SECONDS = 60 * 60
TAG_LINE_PATTERN = re.compile(r"^\[[^\]\r\n]+\]")
TIMESTAMP_FORMAT = "%Y/%m/%d %H:%M:%S"


class TeeStream(io.TextIOBase):
    """Mirror writes to console and a log file."""

    def __init__(self, console: TextIO, log_file: TextIO) -> None:
        self._console = console
        self._log_file = log_file
        self._pending = ""

    def write(self, s: str) -> int:
        text = self._consume_text(s)
        if text:
            self._console.write(text)
            self._log_file.write(text)
        return len(s)

    def flush(self) -> None:
        remaining = self._flush_pending()
        if remaining:
            self._console.write(remaining)
            self._log_file.write(remaining)
        self._console.flush()
        self._log_file.flush()

    def _consume_text(self, s: str) -> str:
        if not s:
            return ""

        self._pending += s
        lines = self._pending.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._pending = lines.pop()
        else:
            self._pending = ""

        return "".join(self._format_one_line(line) for line in lines)

    def _flush_pending(self) -> str:
        if not self._pending:
            return ""
        pending = self._pending
        self._pending = ""
        return self._format_one_line(pending)

    def _format_one_line(self, line: str) -> str:
        if TAG_LINE_PATTERN.match(line):
            timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)
            return f"{timestamp} {line}"
        return line


class BoundedDailyLogFile(io.TextIOBase):
    """Keep a single daily log file within a fixed size window."""

    def __init__(
        self,
        site_dir: Path,
        max_bytes: int,
        keep_days: int,
        cleanup_interval_seconds: int,
        encoding: str = "utf-8",
        now_provider: Callable[[], datetime] | None = None,
        monotonic_provider: Callable[[], float] | None = None,
    ) -> None:
        self.site_dir = site_dir
        self.max_bytes = max(int(max_bytes), 1)
        self.keep_days = max(int(keep_days), 1)
        self.cleanup_interval_seconds = max(int(cleanup_interval_seconds), 1)
        self._encoding = encoding
        self._now_provider = now_provider or datetime.now
        self._monotonic_provider = monotonic_provider or time.monotonic
        self._lock = threading.RLock()
        self._file: TextIO | None = None
        self._current_path: Path | None = None
        self._current_size = 0
        self._next_cleanup_at = 0.0
        self.site_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_current_file(self._now_provider())
        self._run_cleanup()

    @property
    def log_path(self) -> Path:
        if self._current_path is None:
            raise RuntimeError("Log file path is not initialized")
        return self._current_path

    def write(self, s: str) -> int:
        if not s:
            return 0

        incoming_size = len(s.encode(self._encoding, errors="replace"))

        with self._lock:
            now = self._now_provider()
            self._ensure_current_file(now)
            self._maybe_run_cleanup()
            self._write_text(s, incoming_size)
        return len(s)

    def flush(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.flush()

    def close(self) -> None:
        with self._lock:
            self._close_file()

    def _write_text(self, s: str, incoming_size: int) -> None:
        if self._file is None:
            self._ensure_current_file(self._now_provider())
        if self._file is None:
            return

        if incoming_size >= self.max_bytes:
            self._rewrite_with_text(s)
            return

        if (self._current_size + incoming_size) <= self.max_bytes:
            self._file.write(s)
            self._current_size += incoming_size
            return

        retained_existing = self._read_tail_bytes(max(self.max_bytes - incoming_size, 0))
        retained_incoming = s.encode(self._encoding, errors="replace")
        retained = self._normalize_retained_bytes(retained_existing + retained_incoming)
        self._rewrite_with_bytes(retained)

    def _normalize_retained_bytes(self, data: bytes) -> bytes:
        trimmed = data[-self.max_bytes :]
        text = trimmed.decode(self._encoding, errors="ignore")
        if len(data) > self.max_bytes:
            newline_idx = text.find("\n")
            if 0 <= newline_idx < (len(text) - 1):
                text = text[newline_idx + 1 :]
        encoded = text.encode(self._encoding)
        if len(encoded) <= self.max_bytes:
            return encoded
        return encoded[-self.max_bytes :].decode(self._encoding, errors="ignore").encode(self._encoding)

    def _read_tail_bytes(self, limit: int) -> bytes:
        path = self.log_path
        if limit <= 0 or not path.exists():
            return b""
        self.flush()
        file_size = self._current_size
        with path.open("rb") as f:
            if file_size > limit:
                f.seek(file_size - limit)
            return f.read(limit)

    def _rewrite_with_text(self, s: str) -> None:
        retained = self._normalize_retained_bytes(s.encode(self._encoding, errors="replace"))
        self._rewrite_with_bytes(retained)

    def _rewrite_with_bytes(self, data: bytes) -> None:
        path = self.log_path
        self._close_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self._file = path.open("a", encoding=self._encoding)
        self._current_size = len(data)

    def _ensure_current_file(self, now: datetime) -> None:
        next_path = self._build_log_path(now)
        if self._current_path == next_path and self._file is not None:
            return
        self._close_file()
        next_path.parent.mkdir(parents=True, exist_ok=True)
        self._current_path = next_path
        self._shrink_existing_file_if_needed(next_path)
        self._file = next_path.open("a", encoding=self._encoding)
        self._current_size = self._read_current_size(next_path)

    def _build_log_path(self, now: datetime) -> Path:
        month_dir = self.site_dir / now.strftime("%Y-%m")
        return month_dir / f"run_{now.strftime('%Y-%m-%d')}.txt"

    def _shrink_existing_file_if_needed(self, path: Path) -> None:
        current_size = self._read_current_size(path)
        if current_size <= self.max_bytes:
            return
        retained = self._normalize_retained_bytes(path.read_bytes())
        path.write_bytes(retained)

    def _read_current_size(self, path: Path) -> int:
        try:
            return path.stat().st_size if path.exists() else 0
        except Exception:
            return 0

    def _close_file(self) -> None:
        if self._file is None:
            return
        try:
            self._file.flush()
            self._file.close()
        except Exception:
            pass
        finally:
            self._file = None

    def _maybe_run_cleanup(self) -> None:
        now = self._monotonic_provider()
        if now < self._next_cleanup_at:
            return
        self._run_cleanup()

    def _run_cleanup(self) -> None:
        try:
            cleanup_old_logs(
                log_root=self.site_dir,
                keep_days=self.keep_days,
                exclude_paths=[self.log_path] if self._current_path is not None else None,
            )
        finally:
            self._next_cleanup_at = self._monotonic_provider() + self.cleanup_interval_seconds


class LogManager:
    def __init__(self, original_stdout: TextIO, original_stderr: TextIO, log_file: TextIO) -> None:
        self._original_stdout = original_stdout
        self._original_stderr = original_stderr
        self._log_file = log_file

    @property
    def log_path(self) -> Path:
        return getattr(self._log_file, "log_path")

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


def cleanup_old_logs(
    log_root: Path,
    keep_days: int = DEFAULT_LOG_RETENTION_DAYS,
    exclude_paths: Iterable[Path] | None = None,
) -> None:
    if not log_root.exists():
        return

    cutoff = datetime.now() - timedelta(days=keep_days)
    excluded: set[Path] = set()
    for path in exclude_paths or []:
        try:
            excluded.add(path.resolve())
        except Exception:
            continue

    for path in log_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.resolve() in excluded:
                continue
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
    cleanup_interval_seconds: int = DEFAULT_LOG_CLEANUP_INTERVAL_SECONDS,
) -> LogManager:
    log_root = project_root / "log"
    log_root.mkdir(parents=True, exist_ok=True)

    migrate_legacy_log_layout(log_root)

    site_dir = log_root / _sanitize_site_name(site_name)
    effective_keep_days = max(int(keep_days), 1)
    effective_max_log_bytes = max(int(max_log_bytes), 1)
    effective_cleanup_interval_seconds = max(int(cleanup_interval_seconds), 1)

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    log_file = BoundedDailyLogFile(
        site_dir=site_dir,
        max_bytes=effective_max_log_bytes,
        keep_days=effective_keep_days,
        cleanup_interval_seconds=effective_cleanup_interval_seconds,
        encoding="utf-8",
    )

    sys.stdout = TeeStream(original_stdout, log_file)
    sys.stderr = TeeStream(original_stderr, log_file)

    return LogManager(
        original_stdout=original_stdout,
        original_stderr=original_stderr,
        log_file=log_file,
    )
