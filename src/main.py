import argparse
import errno
import json
import math
import platform
import re
import shutil
import sqlite3
import subprocess
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

from src.config_loader import ConfigError, load_config
from src.logging_utils import (
    DEFAULT_LOG_CLEANUP_INTERVAL_SECONDS,
    DEFAULT_LOG_RETENTION_DAYS,
    DEFAULT_MAX_LOG_BYTES,
    cleanup_old_logs,
    setup_file_logging,
)
from src.workflow import create_workflow_crawler


DEFAULT_MAX_STATE_DB_BYTES = 200 * 1024 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Workflow-based website crawler")

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--config",
        help="Path to a site config JSON under config/sites/<name>/config.json",
    )
    group.add_argument(
        "--site",
        help="Site name under config/sites/<name>/config.json",
    )
    group.add_argument(
        "--all-sites",
        action="store_true",
        help="Run all sites found under config/sites/*/config.json",
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode",
    )
    parser.add_argument(
        "--mawb",
        help="External value for {{MAWB}} placeholders",
    )
    parser.add_argument(
        "--container-no",
        dest="container_no",
        help="External value for {{ContainerNo}} placeholders",
    )
    parser.add_argument(
        "--hawno",
        help="External value for {{HAWNO}} placeholders",
    )
    parser.add_argument(
        "--watch-input",
        action="store_true",
        help="Timer mode: poll input/<site>/*.json and trigger site crawls when files change",
    )
    parser.add_argument(
        "--watch-interval",
        type=int,
        help="Polling interval seconds for --watch-input mode (default from global config or 10)",
    )
    parser.add_argument(
        "--input-root",
        help="Input root folder for timer mode (default from global config or input)",
    )
    parser.add_argument(
        "--watch-sites",
        help="Comma-separated site folders to process in timer mode, e.g. cargo,cargonavi",
    )
    parser.add_argument(
        "--selenium-remote-url",
        action="store_true",
        help="Enable remote Selenium mode; URL is resolved from global config with platform fallback",
    )
    return parser.parse_args()


def _parse_watch_sites(raw_watch_sites: Optional[Any]) -> Optional[set[str]]:
    if raw_watch_sites is None:
        return None
    if isinstance(raw_watch_sites, str):
        items = [item.strip() for item in raw_watch_sites.split(",")]
    elif isinstance(raw_watch_sites, (list, tuple, set)):
        items = [str(item).strip() for item in raw_watch_sites]
    else:
        return None

    sites = {item.lower() for item in items if item}
    return sites or None


def _load_global_config(project_root: Path) -> dict[str, Any]:
    config_path = project_root / "config" / "global.json"
    if not config_path.exists():
        return {}

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[CONFIG] Failed to load global config {config_path}: {e}")
        return {}

    if not isinstance(payload, dict):
        print(f"[CONFIG] Global config root must be JSON object: {config_path}")
        return {}

    return payload


def _coerce_positive_int(raw: Any, default_value: int, label: str) -> int:
    try:
        value = int(raw)
    except Exception:
        print(f"[LOG] Invalid {label}={raw!r}, fallback to {default_value}")
        return default_value

    if value < 1:
        print(f"[LOG] Invalid {label}={raw!r}, fallback to {default_value}")
        return default_value
    return value


def _read_runtime_log_settings(global_cfg: dict[str, Any]) -> dict[str, int]:
    watch_cfg = global_cfg.get("watch", {}) if isinstance(global_cfg.get("watch"), dict) else {}
    watch_log_cfg = watch_cfg.get("log", {}) if isinstance(watch_cfg.get("log"), dict) else {}
    legacy_log_cfg = global_cfg.get("log", {}) if isinstance(global_cfg.get("log"), dict) else {}
    log_cfg = watch_log_cfg or legacy_log_cfg
    default_max_mb = DEFAULT_MAX_LOG_BYTES // (1024 * 1024)
    max_mb = _coerce_positive_int(log_cfg.get("max_mb", default_max_mb), default_max_mb, "log.max_mb")
    retention_days = _coerce_positive_int(
        log_cfg.get("retention_days", DEFAULT_LOG_RETENTION_DAYS),
        DEFAULT_LOG_RETENTION_DAYS,
        "log.retention_days",
    )
    cleanup_interval_seconds = _coerce_positive_int(
        log_cfg.get("cleanup_interval_seconds", DEFAULT_LOG_CLEANUP_INTERVAL_SECONDS),
        DEFAULT_LOG_CLEANUP_INTERVAL_SECONDS,
        "log.cleanup_interval_seconds",
    )
    return {
        "max_mb": max_mb,
        "max_bytes": max_mb * 1024 * 1024,
        "retention_days": retention_days,
        "cleanup_interval_seconds": cleanup_interval_seconds,
    }


def _read_watch_db_size_settings(global_cfg: dict[str, Any]) -> dict[str, int]:
    watch_cfg = global_cfg.get("watch", {}) if isinstance(global_cfg.get("watch"), dict) else {}
    db_cfg = watch_cfg.get("db_config", {}) if isinstance(watch_cfg.get("db_config"), dict) else {}
    default_max_mb = DEFAULT_MAX_STATE_DB_BYTES // (1024 * 1024)
    max_size_mb = _coerce_positive_int(
        db_cfg.get("max_size_mb", default_max_mb),
        default_max_mb,
        "db_config.max_size_mb",
    )
    return {
        "max_size_mb": max_size_mb,
        "max_size_bytes": max_size_mb * 1024 * 1024,
    }


def _resolve_selenium_remote_url(global_cfg: dict[str, Any]) -> str:
    raw = ""
    if isinstance(global_cfg, dict):
        raw = str(global_cfg.get("selenium_remote_url", "")).strip()
    if raw:
        return raw

    system_name = platform.system().strip().lower()
    if system_name == "windows":
        return "http://szh2vm0372.apac.bosch.com:4444/wd/hub"
    return "http://172.17.0.1:4444/wd/hub"


def run_config(
    config_path: str,
    headless: bool,
    params: Optional[dict[str, str]] = None,
    base_url_override: Optional[str] = None,
    defer_output_save: bool = False,
    selenium_remote_url: Optional[str] = None,
) -> tuple[bool, list[dict[str, Any]], list[dict[str, Any]]]:
    print(f"\n{'='*60}")
    print(f"[TASK] Config: {config_path}")
    print(f"{'='*60}")
    try:
        config = load_config(config_path)
        if selenium_remote_url:
            config["selenium_remote_url"] = selenium_remote_url
        if base_url_override:
            config["base_url"] = base_url_override
            print(f"[TASK] Override base_url by input URI: {base_url_override}")
        if defer_output_save:
            config["__defer_output_save"] = True
            config["__deferred_output_items"] = []
        crawler = create_workflow_crawler(config=config, headless=headless, params=params or {})
        records = crawler.run()
        deferred_items = crawler.config.get("__deferred_output_items", [])
        if not isinstance(deferred_items, list):
            deferred_items = []
        print(f"[DONE] Crawl finished. Records: {len(records)}")
        return True, records, deferred_items
    except ConfigError as e:
        print(f"[CONFIG ERROR] {e}")
        return False, [], []
    except Exception as e:
        print(f"[ERROR] {e}")
        traceback.print_exc()
        return False, [], []


def run_config_batch_reuse_session(
    config_path: str,
    headless: bool,
    params_list: list[dict[str, str]],
    base_url_override: Optional[str] = None,
    defer_output_save: bool = False,
    selenium_remote_url: Optional[str] = None,
) -> tuple[bool, list[dict[str, Any]]]:
    print(f"\n{'='*60}")
    print(f"[TASK] Config (batch reuse): {config_path}")
    print(f"[TASK] Batch jobs: {len(params_list)}")
    print(f"{'='*60}")
    try:
        config = load_config(config_path)
        if selenium_remote_url:
            config["selenium_remote_url"] = selenium_remote_url
        if base_url_override:
            config["base_url"] = base_url_override
            print(f"[TASK] Override base_url by input URI: {base_url_override}")
        if defer_output_save:
            config["__defer_output_save"] = True
            config["__deferred_output_items"] = []

        crawler = create_workflow_crawler(config=config, headless=headless, params={})

        print("[BATCH] Starting shared browser session...")
        crawler._start_browser()
        try:
            startup_navigate = bool(crawler.config.get("startup_navigate", True))
            if startup_navigate:
                print("[BATCH] Initial navigation to base URL...")
                crawler._navigate_to_base_url()
            else:
                print("[BATCH] Startup navigation skipped by config.")

            print("[BATCH] Executing login once for shared session...")
            crawler._login()

            main_handle = None
            try:
                handles = crawler.driver.window_handles if crawler.driver else []
                if handles:
                    main_handle = handles[0]
            except Exception:
                main_handle = None

            tasks = crawler.config.get("tasks")
            job_results: list[dict[str, Any]] = []
            for idx, one_params in enumerate(params_list, start=1):
                if idx > 1 and crawler.driver:
                    # Reuse one browser session but reset to the main tab between jobs.
                    try:
                        handles = list(crawler.driver.window_handles)
                    except Exception:
                        handles = []

                    if not main_handle and handles:
                        main_handle = handles[0]

                    if main_handle and handles:
                        for h in list(handles):
                            if h == main_handle:
                                continue
                            try:
                                crawler.driver.switch_to.window(h)
                                crawler.driver.close()
                                print(f"[BATCH] Closed extra tab before job {idx}: {h}")
                            except Exception:
                                continue

                        try:
                            crawler.driver.switch_to.window(main_handle)
                            print(f"[BATCH] Switched back to main tab before job {idx}")
                        except Exception:
                            # If main handle became invalid, fallback to first remaining handle.
                            try:
                                fallback_handles = list(crawler.driver.window_handles)
                                if fallback_handles:
                                    crawler.driver.switch_to.window(fallback_handles[0])
                                    main_handle = fallback_handles[0]
                                    print(f"[BATCH] Main tab handle refreshed before job {idx}")
                            except Exception:
                                pass

                crawler.params = dict(one_params or {})
                crawler.config["__deferred_output_items"] = []
                print(
                    f"[BATCH] Job {idx}/{len(params_list)} start, "
                    f"params={sorted(crawler.params.keys())}"
                )
                if tasks:
                    records = crawler._run_tasks(tasks)
                else:
                    crawler.popup_data = []
                    crawler._perform_steps(crawler.config.get("navigation_steps", []))
                    records = crawler._scrape_records()
                    crawler._save_records(records)

                print(f"[BATCH] Job {idx}/{len(params_list)} done. Records: {len(records)}")
                deferred_items = crawler.config.get("__deferred_output_items", [])
                if not isinstance(deferred_items, list):
                    deferred_items = []
                job_results.append({
                    "params": dict(crawler.params),
                    "records": records,
                    "deferred_output_items": deferred_items,
                })

            print("[BATCH] Shared-session batch completed.")
            return True, job_results
        finally:
            if crawler.driver:
                keep_open = bool(crawler.config.get("keep_browser_open", getattr(crawler, "_attached_existing_browser", False)))
                if keep_open:
                    print("[BATCH] keep_browser_open=true, leaving browser/session open.")
                else:
                    print("[BATCH] Closing shared browser session...")
                    crawler.driver.quit()
    except ConfigError as e:
        print(f"[CONFIG ERROR] {e}")
        return False, []
    except Exception as e:
        print(f"[ERROR] {e}")
        traceback.print_exc()
        return False, []


def _persist_deferred_output_items(
    *,
    config_path: str,
    headless: bool,
    params: dict[str, str],
    base_url_override: Optional[str],
    deferred_items: list[dict[str, Any]],
    selenium_remote_url: Optional[str] = None,
) -> bool:
    if not deferred_items:
        return True

    try:
        config = load_config(config_path)
        if selenium_remote_url:
            config["selenium_remote_url"] = selenium_remote_url
        if base_url_override:
            config["base_url"] = base_url_override

        config["__defer_output_save"] = False
        crawler = create_workflow_crawler(config=config, headless=headless, params=params)

        grouped_records: dict[str, dict[str, Any]] = {}

        for item in deferred_items:
            if not isinstance(item, dict):
                continue
            output_cfg = item.get("output_cfg", {})
            records = item.get("records", [])
            if not isinstance(output_cfg, dict) or not isinstance(records, list):
                continue

            # Multiple parallel jobs can target the same output path in the same second.
            # Group and merge first so we write once per output config and avoid overwrite.
            try:
                group_key = json.dumps(output_cfg, sort_keys=True, ensure_ascii=False)
            except Exception:
                group_key = repr(output_cfg)

            bucket = grouped_records.setdefault(
                group_key,
                {
                    "output_cfg": dict(output_cfg),
                    "records": [],
                },
            )
            bucket_records = bucket.get("records", [])
            if isinstance(bucket_records, list):
                bucket_records.extend(records)

        for grouped in grouped_records.values():
            grouped_output_cfg = grouped.get("output_cfg", {})
            grouped_rows = grouped.get("records", [])
            if not isinstance(grouped_output_cfg, dict) or not isinstance(grouped_rows, list):
                continue
            if not grouped_rows:
                continue
            crawler.config["output"] = grouped_output_cfg
            crawler.popup_data = []
            crawler._save_records(grouped_rows)
        return True
    except Exception as e:
        print(f"[OUTPUT] Failed to persist deferred records for {config_path}: {type(e).__name__}: {e}")
        traceback.print_exc()
        return False


def _persist_output_records_from_history(
    *,
    config_path: str,
    headless: bool,
    params: dict[str, str],
    base_url_override: Optional[str],
    records: list[dict[str, Any]],
) -> bool:
    if not records:
        return True

    try:
        config = load_config(config_path)
        if base_url_override:
            config["base_url"] = base_url_override

        crawler = create_workflow_crawler(config=config, headless=headless, params=params)
        tasks = config.get("tasks", [])

        output_cfgs: list[dict[str, Any]] = []
        if isinstance(tasks, list):
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                one_output = task.get("output", {})
                if isinstance(one_output, dict) and one_output:
                    output_cfgs.append(dict(one_output))

        if not output_cfgs:
            root_output = config.get("output", {})
            if isinstance(root_output, dict) and root_output:
                output_cfgs.append(dict(root_output))

        if not output_cfgs:
            return True

        for one_output_cfg in output_cfgs:
            crawler.config["output"] = one_output_cfg
            crawler.popup_data = []
            crawler._save_records(records)
        return True
    except Exception as e:
        print(f"[OUTPUT] Failed to persist historical records for {config_path}: {type(e).__name__}: {e}")
        traceback.print_exc()
        return False


def _push_records_to_uri(
    *,
    uri: str,
    site_name: str,
    input_file_name: str,
    params: dict[str, str],
    records: list[dict[str, Any]],
    job_results: Optional[list[dict[str, Any]]] = None,
    timeout_seconds: int,
    retries: int,
    verify_ssl: bool,
) -> bool:
    target = (uri or "").strip()
    if not target:
        return True

    # Tag each record with request identifiers so batch inputs can be disambiguated downstream.
    request_keys = ("ContainerNo", "MAWB", "HAWNO")

    def _tag_rows(source_records: list[dict[str, Any]], source_params: dict[str, str]) -> list[dict[str, Any]]:
        tagged: list[dict[str, Any]] = []
        for item in source_records:
            row = dict(item) if isinstance(item, dict) else {"value": item}
            for key in request_keys:
                value = source_params.get(key)
                if value and key not in row:
                    row[key] = value
            tagged.append(row)
        return tagged

    tagged_records: list[dict[str, Any]] = []
    params_list: list[dict[str, str]] = []
    if job_results:
        for job in job_results:
            job_params = job.get("params", {})
            if not isinstance(job_params, dict):
                job_params = {}
            job_records = job.get("records", [])
            if not isinstance(job_records, list):
                job_records = []
            params_list.append(dict(job_params))
            tagged_records.extend(_tag_rows(job_records, job_params))
    else:
        tagged_records = _tag_rows(records, params)

    payload = {
        "site": site_name,
        "input_file": input_file_name,
        "record_count": len(tagged_records),
        "params": params,
        "params_list": params_list,
        "job_count": len(job_results or []),
        "records": tagged_records,
        "pushed_at": datetime.now().isoformat(timespec="seconds"),
    }

    max_attempts = max(1, int(retries) + 1)
    timeout = max(1, int(timeout_seconds))
    if not verify_ssl:
        print(f"[PUSH] SSL verification disabled for target: {target}")

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(target, json=payload, timeout=timeout, verify=verify_ssl)
            if 200 <= response.status_code < 300:
                print(
                    f"[PUSH] Success: {target}, status={response.status_code}, "
                    f"records={len(tagged_records)}, jobs={len(job_results or []) or 1}, "
                    f"attempt={attempt}/{max_attempts}"
                )
                return True

            body = (response.text or "").strip().replace("\n", " ")
            print(
                f"[PUSH] Failed: {target}, status={response.status_code}, "
                f"jobs={len(job_results or []) or 1}, attempt={attempt}/{max_attempts}, response={body[:300]}"
            )
        except Exception as e:
            print(
                f"[PUSH] Exception while pushing to {target}, "
                f"jobs={len(job_results or []) or 1}, attempt={attempt}/{max_attempts}: {type(e).__name__}: {e}"
            )

        if attempt < max_attempts:
            time.sleep(1)

    return False


def _resolve_log_site_name(args: argparse.Namespace) -> str:
    if args.watch_input:
        return "input-watcher"
    if args.site:
        return args.site
    if args.config:
        return Path(args.config).resolve().parent.name
    if args.all_sites:
        return "all-sites"
    return "general"


def _extract_params_uri_and_batch(
    payload: dict[str, Any],
) -> tuple[dict[str, str], Optional[str], Optional[str], Optional[tuple[str, list[str]]]]:
    params: dict[str, str] = {}
    callback_uri: Optional[str] = None
    base_url_override: Optional[str] = None
    batch_field: Optional[str] = None
    batch_values: list[str] = []

    for raw_key, raw_val in payload.items():
        key = str(raw_key).strip()
        if not key:
            continue

        normalized = key.lower().replace("-", "").replace("_", "")

        if normalized in {"uri", "url", "callbackuri", "webhook", "webhookurl"}:
            if raw_val is not None:
                callback_uri = str(raw_val).strip()
            continue

        if normalized in {"baseurl", "crawlurl", "queryurl", "targeturl"}:
            if raw_val is not None:
                base_url_override = str(raw_val).strip()
            continue

        if isinstance(raw_val, list):
            cleaned = [str(v).strip() for v in raw_val if str(v).strip()]
            if not cleaned:
                continue
            if normalized in {"containerno", "containernoh"}:
                batch_field = "ContainerNo"
                batch_values = cleaned
                continue
            if normalized == "mawb":
                batch_field = "MAWB"
                batch_values = cleaned
                continue
            if normalized == "hawno":
                batch_field = "HAWNO"
                batch_values = cleaned
                continue
            continue

        if raw_val is None or isinstance(raw_val, dict):
            continue

        value = str(raw_val).strip()
        if normalized == "mawb":
            params["MAWB"] = value
            continue
        if normalized in {"containerno", "containernoh"}:
            params["ContainerNo"] = value
            continue
        if normalized == "hawno":
            params["HAWNO"] = value
            continue

        params[key] = value

    batch = (batch_field, batch_values) if batch_field and batch_values else None
    return params, callback_uri, base_url_override, batch


def _ensure_state_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS crawl_item_state (
                site_name TEXT NOT NULL,
                request_uri TEXT NOT NULL DEFAULT '',
                item_field TEXT NOT NULL,
                item_value TEXT NOT NULL,
                status TEXT NOT NULL,
                record_count INTEGER NOT NULL DEFAULT 0,
                source_file TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (site_name, request_uri, item_field, item_value)
            )
            """
        )

        # Migrate legacy schema (PK without request_uri) so same item can be tracked per URI.
        table_info = conn.execute("PRAGMA table_info(crawl_item_state)").fetchall()
        column_names = [str(row[1]) for row in table_info if len(row) > 1]
        pk_columns = [
            str(row[1])
            for row in sorted(table_info, key=lambda r: int(r[5]) if len(r) > 5 else 0)
            if len(row) > 5 and int(row[5]) > 0
        ]
        expected_pk = ["site_name", "request_uri", "item_field", "item_value"]
        if ("request_uri" not in column_names) or (pk_columns != expected_pk):
            conn.execute("ALTER TABLE crawl_item_state RENAME TO crawl_item_state_legacy")
            conn.execute(
                """
                CREATE TABLE crawl_item_state (
                    site_name TEXT NOT NULL,
                    request_uri TEXT NOT NULL DEFAULT '',
                    item_field TEXT NOT NULL,
                    item_value TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_count INTEGER NOT NULL DEFAULT 0,
                    source_file TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (site_name, request_uri, item_field, item_value)
                )
                """
            )
            if "request_uri" in column_names:
                conn.execute(
                    """
                    INSERT INTO crawl_item_state(
                        site_name, request_uri, item_field, item_value, status, record_count, source_file, updated_at
                    )
                    SELECT
                        site_name,
                        COALESCE(request_uri, ''),
                        item_field,
                        item_value,
                        status,
                        COALESCE(record_count, 0),
                        source_file,
                        updated_at
                    FROM crawl_item_state_legacy
                    """
                )
            else:
                conn.execute(
                    """
                    INSERT INTO crawl_item_state(
                        site_name, request_uri, item_field, item_value, status, record_count, source_file, updated_at
                    )
                    SELECT
                        site_name,
                        '',
                        item_field,
                        item_value,
                        status,
                        COALESCE(record_count, 0),
                        source_file,
                        updated_at
                    FROM crawl_item_state_legacy
                    """
                )
            conn.execute("DROP TABLE crawl_item_state_legacy")
        conn.commit()
    finally:
        conn.close()


def _reset_state_db_if_oversized(db_path: Path, *, max_size_bytes: int) -> bool:
    if max_size_bytes < 1:
        return False

    try:
        current_size_bytes = db_path.stat().st_size
    except FileNotFoundError:
        return False
    except Exception as e:
        print(f"[WATCH] Failed to stat state DB {db_path}: {e}")
        return False

    if current_size_bytes <= max_size_bytes:
        return False

    try:
        db_path.unlink()
        print(
            f"[WATCH] State DB exceeded size limit and was reset: path={db_path}, "
            f"size_mb={current_size_bytes / (1024 * 1024):.2f}, "
            f"limit_mb={max_size_bytes / (1024 * 1024):.2f}"
        )
        return True
    except Exception as e:
        print(f"[WATCH] Failed to reset oversized state DB {db_path}: {e}")
        return False


def _normalize_item_value(item_field: str, value: Any) -> str:
    normalized = str(value).strip()
    if not normalized:
        return ""

    # Treat common tracking identifiers case-insensitively for dedup/state matching.
    if str(item_field).strip() in {"ContainerNo", "MAWB", "HAWNO"}:
        return normalized.upper()
    return normalized


def _load_successful_items(
    db_path: Path,
    *,
    site_name: str,
    request_uri: str,
    item_field: str,
    item_values: list[str],
) -> set[str]:
    normalized_values = [
        _normalize_item_value(item_field, value)
        for value in item_values
        if _normalize_item_value(item_field, value)
    ]
    if not normalized_values:
        return set()
    _ensure_state_db(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        placeholders = ",".join("?" for _ in normalized_values)
        sql = (
            "SELECT item_value FROM crawl_item_state "
            "WHERE site_name=? AND request_uri=? AND item_field=? AND status='success' "
            f"AND item_value IN ({placeholders})"
        )
        rows = conn.execute(sql, [site_name, request_uri, item_field, *normalized_values]).fetchall()
        return {
            _normalize_item_value(item_field, row[0])
            for row in rows
            if row and row[0] is not None and _normalize_item_value(item_field, row[0])
        }
    finally:
        conn.close()


def _upsert_item_results(
    db_path: Path,
    *,
    site_name: str,
    request_uri: str,
    item_field: str,
    results: list[dict[str, Any]],
    source_file: str,
) -> None:
    if not results:
        return
    _ensure_state_db(db_path)
    now_ts = datetime.now().isoformat(timespec="seconds")

    conn = sqlite3.connect(str(db_path))
    try:
        rows_to_upsert: list[tuple[str, str, str, str, str, int, str, str]] = []
        for item in results:
            normalized_value = _normalize_item_value(item_field, item.get("item_value", ""))
            if not normalized_value:
                continue
            rows_to_upsert.append(
                (
                    site_name,
                    request_uri,
                    item_field,
                    normalized_value,
                    str(item.get("status", "error")).strip() or "error",
                    max(0, int(item.get("record_count", 0))),
                    source_file,
                    now_ts,
                )
            )

        conn.executemany(
            """
            INSERT INTO crawl_item_state(
                site_name, request_uri, item_field, item_value, status, record_count, source_file, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(site_name, request_uri, item_field, item_value)
            DO UPDATE SET
                status=excluded.status,
                record_count=excluded.record_count,
                source_file=excluded.source_file,
                updated_at=excluded.updated_at
            """,
            rows_to_upsert,
        )
        conn.commit()
    finally:
        conn.close()


def _collect_site_output_json_candidates(project_root: Path, site_name: str) -> list[Path]:
    cfg_path = project_root / "config" / "sites" / site_name / "config.json"
    if not cfg_path.exists():
        return []

    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    candidate_output_paths: list[str] = []
    output_cfg = cfg.get("output", {}) if isinstance(cfg.get("output", {}), dict) else {}
    output_path = str(output_cfg.get("path", "")).strip()
    if output_path:
        candidate_output_paths.append(output_path)

    tasks = cfg.get("tasks", [])
    if isinstance(tasks, list):
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_output = task.get("output", {}) if isinstance(task.get("output", {}), dict) else {}
            task_output_path = str(task_output.get("path", "")).strip()
            if task_output_path:
                candidate_output_paths.append(task_output_path)

    files: list[Path] = []
    seen: set[str] = set()
    for raw in candidate_output_paths:
        p = Path(raw)
        if not p.is_absolute():
            p = (project_root / p).resolve()
        suffix = p.suffix.lower()
        if suffix != ".json":
            continue

        parent = p.parent
        stem = p.stem
        if not parent.exists():
            continue

        patterns = [f"{stem}.json", f"{stem}_*.json"]
        for pattern in patterns:
            for one in parent.glob(pattern):
                key = str(one.resolve())
                if key in seen:
                    continue
                seen.add(key)
                files.append(one)

    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return files


def _load_historical_records_map(
    project_root: Path,
    *,
    site_name: str,
    item_field: str,
    item_values: list[str],
    max_files: int = 200,
) -> dict[str, list[dict[str, Any]]]:
    normalized_targets = {
        _normalize_item_value(item_field, v)
        for v in item_values
        if _normalize_item_value(item_field, v)
    }
    if not normalized_targets:
        return {}

    candidates = _collect_site_output_json_candidates(project_root, site_name)
    if max_files > 0:
        candidates = candidates[:max_files]

    matched: dict[str, list[dict[str, Any]]] = {}
    pending = set(normalized_targets)

    def _extract_rows_with_context(payload_obj: Any) -> list[dict[str, Any]]:
        if isinstance(payload_obj, list):
            return [dict(r) for r in payload_obj if isinstance(r, dict)]

        if not isinstance(payload_obj, dict):
            return []

        raw_rows = payload_obj.get("records", [])
        if not isinstance(raw_rows, list):
            raw_rows = [payload_obj]

        top_level_context: dict[str, Any] = {
            "ContainerNo": payload_obj.get("ContainerNo", ""),
            "MAWB": payload_obj.get("MAWB", ""),
            "HAWNO": payload_obj.get("HAWNO", ""),
        }
        value_for_field = payload_obj.get(item_field, "")
        if value_for_field:
            top_level_context[item_field] = value_for_field

        rows_with_context: list[dict[str, Any]] = []
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            merged = dict(row)
            for key, value in top_level_context.items():
                if value and key not in merged:
                    merged[key] = value
            rows_with_context.append(merged)
        return rows_with_context

    for file_path in candidates:
        if not pending:
            break
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        rows = _extract_rows_with_context(payload)
        if not rows:
            continue

        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            value = _normalize_item_value(item_field, row.get(item_field, ""))
            if not value or value not in pending:
                continue
            grouped.setdefault(value, []).append(dict(row))

        for value, rows in grouped.items():
            if value in pending and rows:
                matched[value] = rows
                pending.remove(value)

    return matched


def _split_into_chunks(items: list[dict[str, str]], worker_count: int, chunk_size: int) -> list[list[dict[str, str]]]:
    if not items:
        return []
    if chunk_size <= 0:
        chunk_size = int(math.ceil(len(items) / max(1, worker_count)))
    chunk_size = max(1, chunk_size)
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]


def _load_site_no_data_rule(project_root: Path, site_name: str) -> dict[str, Any]:
    cfg_path = project_root / "config" / "sites" / site_name / "config.json"
    if not cfg_path.exists():
        return {}
    try:
        payload = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    fallback_cfg = payload.get("fallback", {})
    if not isinstance(fallback_cfg, dict):
        return {}
    no_data_cfg = fallback_cfg.get("no_data", {})
    if not isinstance(no_data_cfg, dict):
        return {}
    return no_data_cfg


def _records_have_effective_data(records: list[dict[str, Any]], no_data_rule: dict[str, Any]) -> bool:
    if not isinstance(records, list) or not records:
        return False

    require_any_fields = no_data_rule.get("require_any_fields", [])
    if not isinstance(require_any_fields, list):
        require_any_fields = []
    required_fields = [str(x).strip() for x in require_any_fields if str(x).strip()]

    text_contains = no_data_rule.get("text_contains", [])
    if not isinstance(text_contains, list):
        text_contains = []
    deny_texts = [str(x).strip().lower() for x in text_contains if str(x).strip()]

    if required_fields:
        for row in records:
            if not isinstance(row, dict):
                continue
            for key in required_fields:
                value = row.get(key)
                if value is not None and str(value).strip():
                    return True
        return False

    if deny_texts:
        all_values: list[str] = []
        for row in records:
            if isinstance(row, dict):
                all_values.extend(str(v).strip().lower() for v in row.values() if v is not None)
            elif row is not None:
                all_values.append(str(row).strip().lower())
        joined = "\n".join(all_values)
        if any(marker in joined for marker in deny_texts):
            return False

    return True


def _run_container_fallback_chain(
    *,
    project_root: Path,
    container_no: str,
    sites_chain: list[str],
    headless: bool,
    base_runtime_params: dict[str, str],
    base_url_override: Optional[str],
    prepare_browser_startup,
    site_error_retries: int = 2,
    defer_output_save: bool = False,
    selenium_remote_url: Optional[str] = None,
) -> tuple[bool, dict[str, str], list[dict[str, Any]], list[dict[str, Any]]]:
    params = dict(base_runtime_params)
    params["ContainerNo"] = container_no
    tried_sites: list[str] = []
    max_attempts = max(1, int(site_error_retries) + 1)

    for one_site in sites_chain:
        site_name = str(one_site).strip().lower()
        if not site_name:
            continue
        cfg_path = project_root / "config" / "sites" / site_name / "config.json"
        if not cfg_path.exists():
            print(f"[FALLBACK] Site config missing: {cfg_path}, skip")
            tried_sites.append(site_name)
            continue

        if not prepare_browser_startup(site_name, cfg_path):
            print(f"[FALLBACK] Browser startup prepare failed for site '{site_name}', skip")
            tried_sites.append(site_name)
            continue

        ok = False
        records: list[dict[str, Any]] = []
        selected_deferred_items: list[dict[str, Any]] = []
        for attempt in range(1, max_attempts + 1):
            print(
                f"[FALLBACK] Try site={site_name}, ContainerNo={container_no}, "
                f"attempt={attempt}/{max_attempts}"
            )
            ok, records, deferred_items = run_config(
                str(cfg_path),
                headless=headless,
                params=params,
                base_url_override=base_url_override,
                defer_output_save=defer_output_save,
                selenium_remote_url=selenium_remote_url,
            )
            if ok:
                selected_deferred_items = deferred_items
                break
            if attempt < max_attempts:
                print(
                    f"[FALLBACK] Site '{site_name}' execution error. "
                    "Restarting browser and retrying current site..."
                )
                time.sleep(1)

        tried_sites.append(site_name)
        if not ok:
            error_record = {
                "ContainerNo": container_no,
                "source_site": site_name,
                "status": "site_error",
                "tried_sites": ",".join(tried_sites),
            }
            print(
                f"[FALLBACK] Site '{site_name}' failed after {max_attempts} attempts; "
                "stop chain for this container"
            )
            return False, params, [error_record], []

        no_data_rule = _load_site_no_data_rule(project_root, site_name)
        has_data = _records_have_effective_data(records, no_data_rule)
        if not has_data:
            print(f"[FALLBACK] No data on site={site_name}, continue next site")
            continue

        normalized: list[dict[str, Any]] = []
        for row in records:
            item = dict(row) if isinstance(row, dict) else {"value": row}
            item.setdefault("ContainerNo", container_no)
            item.setdefault("source_site", site_name)
            normalized.append(item)
        print(f"[FALLBACK] Data found on site={site_name}, records={len(normalized)}")
        return True, params, normalized, selected_deferred_items

    not_found_record = {
        "ContainerNo": container_no,
        "source_site": "",
        "status": "not_found",
        "tried_sites": ",".join(tried_sites),
    }
    return True, params, [not_found_record], []


def _run_input_timer_mode(
    *,
    project_root: Path,
    input_root: Path,
    headless: bool,
    interval_seconds: int,
    base_runtime_params: dict[str, str],
    watch_sites: Optional[set[str]] = None,
    push_timeout_seconds: int = 30,
    push_retries: int = 0,
    push_verify_ssl: bool = True,
    cargo_fallback_cfg: Optional[dict[str, Any]] = None,
    parallel_cfg: Optional[dict[str, Any]] = None,
    input_done_retention_days: int = 30,
    selenium_remote_url: Optional[str] = None,
) -> None:
    interval = max(1, int(interval_seconds))
    seen_fingerprints: dict[str, tuple[int, int]] = {}
    prepared_startup_sites: set[str] = set()

    print(f"[WATCH] Timer mode started. input_root={input_root}, interval={interval}s")
    print("[WATCH] Scanning input/<site>/*.json; changed files will trigger corresponding site crawls.")
    if watch_sites:
        print(f"[WATCH] Site filter enabled: {sorted(watch_sites)}")

    fallback_cfg = cargo_fallback_cfg if isinstance(cargo_fallback_cfg, dict) else {}
    fallback_enabled = bool(fallback_cfg.get("enabled", False))
    fallback_trigger_site = str(fallback_cfg.get("trigger_site", "cargo")).strip().lower() or "cargo"
    raw_chain = fallback_cfg.get("sites_chain", ["cargo", "cma", "hapag"])
    if isinstance(raw_chain, str):
        fallback_chain = [s.strip().lower() for s in raw_chain.split(",") if s.strip()]
    elif isinstance(raw_chain, list):
        fallback_chain = [str(s).strip().lower() for s in raw_chain if str(s).strip()]
    else:
        fallback_chain = ["cargo", "cma", "hapag"]
    site_error_retries = int(fallback_cfg.get("site_error_retries", 2))
    if site_error_retries < 0:
        site_error_retries = 0
    reuse_trigger_site_session = bool(fallback_cfg.get("reuse_trigger_site_session", True))
    if fallback_enabled:
        print(
            f"[WATCH] Cargo fallback enabled: trigger_site={fallback_trigger_site}, "
            f"chain={fallback_chain}, site_error_retries={site_error_retries}"
        )

    parallel_cfg = parallel_cfg if isinstance(parallel_cfg, dict) else {}
    parallel_enabled = bool(parallel_cfg.get("enabled", False))
    parallel_max_workers = max(1, int(parallel_cfg.get("max_workers", 4)))
    parallel_chunk_size = int(parallel_cfg.get("chunk_size", 0))
    db_cfg_raw = global_cfg.get("watch", {}).get("db_config", {}) if isinstance(global_cfg.get("watch", {}), dict) else {}
    db_cfg = db_cfg_raw if isinstance(db_cfg_raw, dict) else {}
    db_size_settings = _read_watch_db_size_settings(global_cfg)
    skip_successful_items = bool(db_cfg.get("skip_successful_items", parallel_cfg.get("skip_successful_items", True)))
    force_output_on_skipped_success = bool(
        db_cfg.get("force_output_on_skipped_success", parallel_cfg.get("force_output_on_skipped_success", True))
    )
    recrawl_skipped_without_history = bool(
        db_cfg.get("recrawl_skipped_without_history", parallel_cfg.get("recrawl_skipped_without_history", True))
    )
    state_db_raw = str(db_cfg.get("state_db_path", parallel_cfg.get("state_db_path", "state/crawl_item_state.db"))).strip()
    if not state_db_raw:
        state_db_raw = "state/crawl_item_state.db"
    state_db_path = Path(state_db_raw)
    if not state_db_path.is_absolute():
        state_db_path = (project_root / state_db_path).resolve()
    state_db_max_size_bytes = db_size_settings["max_size_bytes"]

    if parallel_enabled:
        print(
            f"[WATCH] Parallel batch mode enabled: max_workers={parallel_max_workers}, "
            f"chunk_size={'auto' if parallel_chunk_size <= 0 else parallel_chunk_size}, "
            f"skip_successful_items={skip_successful_items}, "
            f"force_output_on_skipped_success={force_output_on_skipped_success}, "
            f"recrawl_skipped_without_history={recrawl_skipped_without_history}, "
            f"state_db={state_db_path}"
        )

    done_root = project_root / "input_done"
    done_root.mkdir(parents=True, exist_ok=True)
    effective_input_done_retention_days = max(1, int(input_done_retention_days))
    cleanup_old_logs(done_root, keep_days=effective_input_done_retention_days)
    print(f"[WATCH] input_done retention days: {effective_input_done_retention_days}")

    def prepare_browser_startup(site_name: str, cfg_path: Path) -> bool:
        try:
            cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[WATCH] Failed to load site config for browser startup ({cfg_path}): {e}")
            return False

        startup_cfg = cfg_data.get("browser_startup", {})
        if not isinstance(startup_cfg, dict):
            return True

        mode = str(startup_cfg.get("mode", "system")).strip().lower()
        run_once = bool(startup_cfg.get("run_once", True))
        startup_key = site_name.strip().lower()
        if run_once and startup_key in prepared_startup_sites:
            return True

        if mode in {"", "system", "auto"}:
            if run_once:
                prepared_startup_sites.add(startup_key)
            print(f"[WATCH] Browser startup mode for site '{site_name}': system")
            return True

        if mode != "batch_attach":
            print(
                f"[WATCH] Unsupported browser_startup.mode='{mode}' for site '{site_name}'. "
                f"Supported: system, batch_attach"
            )
            return False

        batch_file = str(startup_cfg.get("batch_file", "")).strip()
        if not batch_file:
            print(f"[WATCH] browser_startup.batch_file is required for site '{site_name}' in batch_attach mode")
            return False

        batch_path = Path(batch_file)
        if not batch_path.is_absolute():
            batch_path = (project_root / batch_path).resolve()
        if not batch_path.exists():
            print(f"[WATCH] browser_startup.batch_file not found: {batch_path}")
            return False

        raw_args = startup_cfg.get("args", [])
        if isinstance(raw_args, list):
            batch_args = [str(item) for item in raw_args]
        elif raw_args is None:
            batch_args = []
        else:
            batch_args = [str(raw_args)]

        wait_seconds = float(startup_cfg.get("wait_seconds", 2))
        print(
            f"[WATCH] Browser startup mode for site '{site_name}': batch_attach, "
            f"launcher={batch_path.name}, args={len(batch_args)}"
        )

        try:
            # Launch batch script without blocking watcher loop.
            subprocess.Popen(
                ["cmd", "/c", str(batch_path), *batch_args],
                cwd=str(project_root),
                shell=False,
            )
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            if run_once:
                prepared_startup_sites.add(startup_key)
            return True
        except Exception as e:
            print(f"[WATCH] Failed to launch browser startup batch for site '{site_name}': {e}")
            return False

    def archive_input_file(src: Path, site_name: str) -> Path:
        site_done_dir = done_root / site_name
        site_done_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = site_done_dir / f"{src.stem}_{ts}{src.suffix}"
        try:
            src.replace(dst)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            shutil.move(str(src), str(dst))
        cleanup_old_logs(site_done_dir, keep_days=effective_input_done_retention_days)
        return dst

    def rename_failed_input_for_retry(src: Path) -> Path:
        # Keep filename sortable by time so latest retries are at the end of folder listing.
        stem_prefix = src.stem
        match = re.match(r"^(.*)_\d{8}(?:_\d{6}(?:_\d{6})?)$", src.stem)
        if match:
            stem_prefix = match.group(1)

        folder = src.parent
        latest_mtime = 0.0
        for p in folder.glob("*.json"):
            try:
                latest_mtime = max(latest_mtime, p.stat().st_mtime)
            except Exception:
                continue

        candidate_dt = datetime.now()
        if latest_mtime > 0:
            latest_dt = datetime.fromtimestamp(latest_mtime)
            if latest_dt >= candidate_dt:
                candidate_dt = latest_dt

        while True:
            ts = candidate_dt.strftime("%Y%m%d_%H%M%S_%f")
            dst = folder / f"{stem_prefix}_{ts}{src.suffix}"
            if not dst.exists():
                src.replace(dst)
                return dst
            candidate_dt = datetime.fromtimestamp(candidate_dt.timestamp() + 1)

    while True:
        try:
            files = sorted(input_root.glob("*/*.json"))
            active_paths: set[str] = set()

            for file_path in files:
                key = str(file_path.resolve())
                active_paths.add(key)
                site_name = file_path.parent.name
                normalized_site_name = site_name.strip().lower()
                if watch_sites and normalized_site_name not in watch_sites:
                    continue

                try:
                    stat = file_path.stat()
                except Exception as e:
                    print(f"[WATCH] Skip unreadable file {file_path}: {e}")
                    continue

                fingerprint = (int(stat.st_mtime_ns), int(stat.st_size))
                if seen_fingerprints.get(key) == fingerprint:
                    continue

                _reset_state_db_if_oversized(state_db_path, max_size_bytes=state_db_max_size_bytes)

                cfg_path = project_root / "config" / "sites" / site_name / "config.json"

                if not cfg_path.exists():
                    print(f"[WATCH] No config for site '{site_name}': {cfg_path}. Skip.")
                    continue

                if not prepare_browser_startup(site_name, cfg_path):
                    print(f"[WATCH] Browser startup prepare failed for site '{site_name}', skip this round.")
                    continue

                try:
                    payload = json.loads(file_path.read_text(encoding="utf-8"))
                except Exception as e:
                    print(f"[WATCH] Invalid JSON in {file_path}: {e}")
                    continue

                if not isinstance(payload, dict):
                    print(f"[WATCH] JSON root must be object in {file_path}. Skip.")
                    continue

                extracted_params, callback_uri, base_url_override, batch = _extract_params_uri_and_batch(payload)
                state_request_uri = (callback_uri or "").strip()
                runtime_params = dict(base_runtime_params)
                runtime_params.update(extracted_params)

                job_params_list: list[dict[str, str]] = []
                batch_field = ""
                batch_values: list[str] = []
                state_track_field = ""
                if batch:
                    batch_field, batch_values = batch
                    state_track_field = batch_field
                    original_count = len(batch_values)
                    deduped_values: list[str] = []
                    seen_values: set[str] = set()
                    for raw in batch_values:
                        value = str(raw).strip()
                        normalized_value = _normalize_item_value(batch_field, value)
                        if not normalized_value or normalized_value in seen_values:
                            continue
                        seen_values.add(normalized_value)
                        deduped_values.append(normalized_value)
                    batch_values = deduped_values

                    if len(batch_values) != original_count:
                        print(
                            f"[WATCH] Batch input deduplicated: field={batch_field}, "
                            f"original={original_count}, unique={len(batch_values)}"
                        )

                    for item in batch_values:
                        one_params = dict(runtime_params)
                        one_params[batch_field] = item
                        job_params_list.append(one_params)
                    print(
                        f"[WATCH] Batch input detected: field={batch_field}, count={len(batch_values)}"
                    )
                else:
                    job_params_list.append(runtime_params)
                    for candidate_field in ("ContainerNo", "MAWB", "HAWNO"):
                        candidate_value = _normalize_item_value(
                            candidate_field,
                            runtime_params.get(candidate_field, ""),
                        )
                        if candidate_value:
                            state_track_field = candidate_field
                            break

                skipped_success_values: set[str] = set()
                attempted_values: list[str] = []
                skipped_jobs_for_push: list[dict[str, Any]] = []
                if state_track_field and skip_successful_items:
                    if batch and batch_field:
                        raw_values = [
                            _normalize_item_value(state_track_field, v)
                            for v in batch_values
                            if _normalize_item_value(state_track_field, v)
                        ]
                    else:
                        raw_values = []
                        for p in job_params_list:
                            normalized_value = _normalize_item_value(
                                state_track_field,
                                p.get(state_track_field, ""),
                            )
                            if normalized_value:
                                raw_values.append(normalized_value)

                    skipped_success_values = _load_successful_items(
                        state_db_path,
                        site_name=normalized_site_name,
                        request_uri=state_request_uri,
                        item_field=state_track_field,
                        item_values=raw_values,
                    )
                    if skipped_success_values:
                        print(
                            f"[WATCH] Skip already-success items: field={state_track_field}, "
                            f"skipped={len(skipped_success_values)}"
                        )
                        job_params_list = [
                            p for p in job_params_list
                            if _normalize_item_value(
                                state_track_field,
                                p.get(state_track_field, ""),
                            ) not in skipped_success_values
                        ]

                if state_track_field and skipped_success_values and (
                    callback_uri or force_output_on_skipped_success
                ):
                    historical_map = _load_historical_records_map(
                        project_root,
                        site_name=normalized_site_name,
                        item_field=state_track_field,
                        item_values=sorted(skipped_success_values),
                    )
                    requeued_missing_count = 0

                    for item_value in sorted(skipped_success_values):
                        one_params = dict(runtime_params)
                        one_params[state_track_field] = item_value
                        historical_rows = historical_map.get(item_value, [])
                        if historical_rows:
                            skipped_jobs_for_push.append({
                                "params": one_params,
                                "records": historical_rows,
                            })
                            continue

                        if recrawl_skipped_without_history:
                            exists_in_queue = any(
                                _normalize_item_value(state_track_field, p.get(state_track_field, "")) == item_value
                                for p in job_params_list
                                if isinstance(p, dict)
                            )
                            if not exists_in_queue:
                                job_params_list.append(one_params)
                            requeued_missing_count += 1
                            continue

                        skipped_jobs_for_push.append({
                            "params": one_params,
                            "records": [{
                                state_track_field: item_value,
                                "source_site": normalized_site_name,
                                "status": "cached_success_skipped",
                            }],
                        })

                    historical_hit_count = sum(
                        1 for v in skipped_success_values if historical_map.get(v)
                    )
                    if historical_hit_count:
                        print(
                            f"[WATCH] Reusing historical records for callback push: "
                            f"field={state_track_field}, hits={historical_hit_count}/{len(skipped_success_values)}"
                        )
                    elif not recrawl_skipped_without_history:
                        print(
                            f"[WATCH] No historical records found for skipped items; "
                            "fallback to cached_success_skipped payload"
                        )
                    else:
                        print(
                            f"[WATCH] Missing historical records were re-queued for fresh crawl: "
                            f"field={state_track_field}, count={requeued_missing_count}"
                        )

                    if recrawl_skipped_without_history and requeued_missing_count > 0:
                        print(
                            f"[WATCH] Re-queued skipped items without history for fresh crawl: "
                            f"field={state_track_field}, count={requeued_missing_count}"
                        )

                if state_track_field:
                    attempted_values = [
                        _normalize_item_value(state_track_field, p.get(state_track_field, ""))
                        for p in job_params_list
                        if _normalize_item_value(state_track_field, p.get(state_track_field, ""))
                    ]

                if state_track_field and not job_params_list:
                    print(
                        f"[WATCH] All batch items already successful, skip crawling. "
                        f"field={state_track_field}, file={file_path.name}"
                    )
                    all_skipped_ok = True
                    if callback_uri and skipped_jobs_for_push:
                        print(
                            f"[WATCH] Callback push for skipped-success items: "
                            f"field={state_track_field}, jobs={len(skipped_jobs_for_push)}"
                        )
                        all_skipped_ok = _push_records_to_uri(
                            uri=callback_uri,
                            site_name=site_name,
                            input_file_name=file_path.name,
                            params=runtime_params,
                            records=[],
                            job_results=skipped_jobs_for_push,
                            timeout_seconds=push_timeout_seconds,
                            retries=push_retries,
                            verify_ssl=push_verify_ssl,
                        )

                    if all_skipped_ok and force_output_on_skipped_success and skipped_jobs_for_push:
                        print(
                            f"[WATCH] Persisting output for skipped-success items: "
                            f"field={state_track_field}, jobs={len(skipped_jobs_for_push)}"
                        )
                        for one_job in skipped_jobs_for_push:
                            one_params = one_job.get("params", {})
                            one_records = one_job.get("records", [])
                            ok_save = _persist_output_records_from_history(
                                config_path=str(cfg_path),
                                headless=headless,
                                params=one_params if isinstance(one_params, dict) else {},
                                base_url_override=base_url_override,
                                records=one_records if isinstance(one_records, list) else [],
                            )
                            if not ok_save:
                                all_skipped_ok = False
                                break

                    if file_path.exists():
                        if all_skipped_ok:
                            archived = archive_input_file(file_path, site_name)
                            seen_fingerprints[key] = fingerprint
                            print(f"[WATCH] Archived processed input: {archived}")
                        else:
                            renamed = rename_failed_input_for_retry(file_path)
                            print(f"[WATCH] Renamed failed input for retry: {renamed.name}")
                    continue

                success = True
                push_jobs: list[dict[str, Any]] = []
                deferred_output_jobs: list[dict[str, Any]] = []
                processed_jobs: list[dict[str, Any]] = []
                has_container_input = (
                    (batch is not None and str(batch[0]) == "ContainerNo")
                    or ("ContainerNo" in runtime_params and str(runtime_params.get("ContainerNo", "")).strip() != "")
                )
                use_fallback_chain = (
                    fallback_enabled
                    and normalized_site_name == fallback_trigger_site
                    and has_container_input
                )
                if use_fallback_chain:
                    fallback_job_params_list: list[dict[str, str]] = []
                    for one_params in job_params_list:
                        if not isinstance(one_params, dict):
                            continue
                        container_value = _normalize_item_value(
                            "ContainerNo",
                            one_params.get("ContainerNo", ""),
                        )
                        if not container_value:
                            continue
                        fallback_one_params = dict(one_params)
                        fallback_one_params["ContainerNo"] = container_value
                        fallback_job_params_list.append(fallback_one_params)

                    container_values = [p["ContainerNo"] for p in fallback_job_params_list]

                    if not container_values:
                        print("[FALLBACK] No ContainerNo found in input, fallback chain skipped")
                        success = False
                    else:
                        print(
                            f"[FALLBACK] Processing file={file_path.name} with container count={len(container_values)}"
                        )
                        can_reuse_trigger_site = (
                            reuse_trigger_site_session
                            and len(container_values) > 1
                            and bool(fallback_chain)
                            and fallback_chain[0] == fallback_trigger_site
                        )

                        if can_reuse_trigger_site:
                            trigger_site = fallback_trigger_site
                            trigger_cfg_path = project_root / "config" / "sites" / trigger_site / "config.json"
                            trigger_params_list = [dict(p) for p in fallback_job_params_list]

                            print(
                                f"[FALLBACK] Reusing one '{trigger_site}' browser session for "
                                f"{len(trigger_params_list)} containers"
                            )
                            ok_trigger, trigger_results = run_config_batch_reuse_session(
                                str(trigger_cfg_path),
                                headless=headless,
                                params_list=trigger_params_list,
                                base_url_override=base_url_override,
                                defer_output_save=bool(callback_uri),
                            )
                            if not ok_trigger:
                                success = False
                            else:
                                trigger_no_data_rule = _load_site_no_data_rule(project_root, trigger_site)
                                trigger_result_by_container: dict[str, dict[str, Any]] = {}
                                for item in trigger_results:
                                    if not isinstance(item, dict):
                                        continue
                                    item_params = item.get("params", {})
                                    if not isinstance(item_params, dict):
                                        continue
                                    container_key = _normalize_item_value(
                                        "ContainerNo",
                                        item_params.get("ContainerNo", ""),
                                    )
                                    if container_key and container_key not in trigger_result_by_container:
                                        trigger_result_by_container[container_key] = item

                                downstream_chain = fallback_chain[1:]
                                for idx, container_no in enumerate(container_values, start=1):
                                    print(
                                        f"[FALLBACK] Container job {idx}/{len(container_values)}: {container_no}"
                                    )
                                    container_key = _normalize_item_value("ContainerNo", container_no)
                                    one_params = dict(runtime_params)
                                    one_params["ContainerNo"] = container_no

                                    trigger_job = trigger_result_by_container.get(container_key)
                                    trigger_records = []
                                    trigger_deferred_items: list[dict[str, Any]] = []
                                    if trigger_job:
                                        trigger_records = (
                                            trigger_job.get("records", [])
                                            if isinstance(trigger_job.get("records", []), list)
                                            else []
                                        )
                                        trigger_deferred_items = (
                                            trigger_job.get("deferred_output_items", [])
                                            if isinstance(trigger_job.get("deferred_output_items", []), list)
                                            else []
                                        )

                                    has_trigger_data = _records_have_effective_data(trigger_records, trigger_no_data_rule)
                                    if has_trigger_data:
                                        normalized_records: list[dict[str, Any]] = []
                                        for row in trigger_records:
                                            normalized_row = dict(row) if isinstance(row, dict) else {"value": row}
                                            normalized_row.setdefault("ContainerNo", container_no)
                                            normalized_row.setdefault("source_site", trigger_site)
                                            normalized_records.append(normalized_row)

                                        processed_jobs.append({"params": dict(one_params), "records": normalized_records})
                                        if callback_uri:
                                            push_jobs.append({"params": dict(one_params), "records": normalized_records})
                                            if trigger_deferred_items:
                                                deferred_output_jobs.append({
                                                    "config_path": str(trigger_cfg_path),
                                                    "params": dict(one_params),
                                                    "deferred_output_items": trigger_deferred_items,
                                                })
                                        continue

                                    if not downstream_chain:
                                        no_hit_records = [{
                                            "ContainerNo": container_no,
                                            "source_site": "",
                                            "status": "not_found",
                                            "tried_sites": trigger_site,
                                        }]
                                        processed_jobs.append({"params": dict(one_params), "records": no_hit_records})
                                        if callback_uri:
                                            push_jobs.append({"params": dict(one_params), "records": no_hit_records})
                                        continue

                                    ok_one, fb_params, fb_records, fb_deferred_items = _run_container_fallback_chain(
                                        project_root=project_root,
                                        container_no=container_no,
                                        sites_chain=downstream_chain,
                                        headless=headless,
                                        base_runtime_params=runtime_params,
                                        base_url_override=base_url_override,
                                        prepare_browser_startup=prepare_browser_startup,
                                        site_error_retries=site_error_retries,
                                        defer_output_save=bool(callback_uri),
                                        selenium_remote_url=selenium_remote_url,
                                    )
                                    processed_jobs.append({"params": dict(fb_params), "records": fb_records})
                                    if not ok_one:
                                        success = False
                                        break
                                    if callback_uri:
                                        push_jobs.append({"params": dict(fb_params), "records": fb_records})
                                        if fb_deferred_items:
                                            source_site = str(fb_records[0].get("source_site", "")).strip().lower() if fb_records else ""
                                            if source_site:
                                                source_cfg_path = project_root / "config" / "sites" / source_site / "config.json"
                                            else:
                                                source_cfg_path = cfg_path
                                            deferred_output_jobs.append({
                                                "config_path": str(source_cfg_path),
                                                "params": dict(fb_params),
                                                "deferred_output_items": fb_deferred_items,
                                            })
                        else:
                            run_parallel = parallel_enabled and len(container_values) > 1
                            if run_parallel:
                                worker_count = min(parallel_max_workers, len(container_values))
                                chunk_lists = _split_into_chunks(
                                    fallback_job_params_list,
                                    worker_count=worker_count,
                                    chunk_size=parallel_chunk_size,
                                )
                                print(
                                    f"[FALLBACK] Parallel mode: workers={worker_count}, chunks={len(chunk_lists)}"
                                )

                                def _run_fallback_chunk(chunk: list[dict[str, str]]) -> list[dict[str, Any]]:
                                    chunk_results: list[dict[str, Any]] = []
                                    for one in chunk:
                                        one_container = _normalize_item_value(
                                            "ContainerNo",
                                            one.get("ContainerNo", ""),
                                        )
                                        if not one_container:
                                            continue
                                        ok_one, one_params, one_records, one_deferred_items = _run_container_fallback_chain(
                                            project_root=project_root,
                                            container_no=one_container,
                                            sites_chain=fallback_chain,
                                            headless=headless,
                                            base_runtime_params=runtime_params,
                                            base_url_override=base_url_override,
                                            prepare_browser_startup=prepare_browser_startup,
                                            site_error_retries=site_error_retries,
                                            defer_output_save=bool(callback_uri),
                                            selenium_remote_url=selenium_remote_url,
                                        )
                                        chunk_results.append({
                                            "ok": ok_one,
                                            "params": one_params,
                                            "records": one_records,
                                            "deferred_output_items": one_deferred_items,
                                        })
                                    return chunk_results

                                fallback_job_results: list[dict[str, Any]] = []
                                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                                    futures = [executor.submit(_run_fallback_chunk, chunk) for chunk in chunk_lists]
                                    for fut in as_completed(futures):
                                        try:
                                            fallback_job_results.extend(fut.result())
                                        except Exception as e:
                                            print(f"[FALLBACK] Worker failed: {type(e).__name__}: {e}")
                                            success = False

                                for one in fallback_job_results:
                                    ok_one = bool(one.get("ok", False))
                                    one_params = one.get("params", {}) if isinstance(one.get("params", {}), dict) else {}
                                    one_records = one.get("records", []) if isinstance(one.get("records", []), list) else []
                                    one_deferred_items = one.get("deferred_output_items", [])
                                    if not ok_one:
                                        success = False
                                    processed_jobs.append({"params": dict(one_params), "records": one_records})
                                    if callback_uri and ok_one:
                                        push_jobs.append({"params": dict(one_params), "records": one_records})
                                        if isinstance(one_deferred_items, list) and one_deferred_items:
                                            source_site = str(one_records[0].get("source_site", "")).strip().lower() if one_records else ""
                                            if source_site:
                                                source_cfg_path = project_root / "config" / "sites" / source_site / "config.json"
                                            else:
                                                source_cfg_path = cfg_path
                                            deferred_output_jobs.append({
                                                "config_path": str(source_cfg_path),
                                                "params": dict(one_params),
                                                "deferred_output_items": one_deferred_items,
                                            })
                            else:
                                for idx, one_job_params in enumerate(fallback_job_params_list, start=1):
                                    container_no = one_job_params["ContainerNo"]
                                    print(
                                        f"[FALLBACK] Container job {idx}/{len(container_values)}: {container_no}"
                                    )
                                    ok, one_params, one_records, one_deferred_items = _run_container_fallback_chain(
                                        project_root=project_root,
                                        container_no=container_no,
                                        sites_chain=fallback_chain,
                                        headless=headless,
                                        base_runtime_params=runtime_params,
                                        base_url_override=base_url_override,
                                        prepare_browser_startup=prepare_browser_startup,
                                        site_error_retries=site_error_retries,
                                        defer_output_save=bool(callback_uri),
                                        selenium_remote_url=selenium_remote_url,
                                    )
                                    processed_jobs.append({"params": dict(one_params), "records": one_records})
                                    if not ok:
                                        success = False
                                        break
                                    if callback_uri:
                                        push_jobs.append({"params": dict(one_params), "records": one_records})
                                        if one_deferred_items:
                                            source_site = str(one_records[0].get("source_site", "")).strip().lower() if one_records else ""
                                            if source_site:
                                                source_cfg_path = project_root / "config" / "sites" / source_site / "config.json"
                                            else:
                                                source_cfg_path = cfg_path
                                            deferred_output_jobs.append({
                                                "config_path": str(source_cfg_path),
                                                "params": dict(one_params),
                                                "deferred_output_items": one_deferred_items,
                                            })
                elif batch and len(job_params_list) > 1:
                    run_parallel = parallel_enabled and len(job_params_list) > 1
                    if run_parallel:
                        worker_count = min(parallel_max_workers, len(job_params_list))
                        chunks = _split_into_chunks(
                            job_params_list,
                            worker_count=worker_count,
                            chunk_size=parallel_chunk_size,
                        )
                        print(
                            f"[WATCH] Batch parallel mode: file={file_path.name}, jobs={len(job_params_list)}, "
                            f"workers={worker_count}, chunks={len(chunks)}"
                        )
                        batch_results: list[dict[str, Any]] = []
                        with ThreadPoolExecutor(max_workers=worker_count) as executor:
                            futures = [
                                executor.submit(
                                    run_config_batch_reuse_session,
                                    str(cfg_path),
                                    headless,
                                    chunk,
                                    base_url_override,
                                    bool(callback_uri),
                                    selenium_remote_url,
                                )
                                for chunk in chunks
                            ]
                            for fut in as_completed(futures):
                                try:
                                    ok_one, one_results = fut.result()
                                    if not ok_one:
                                        success = False
                                    if isinstance(one_results, list):
                                        batch_results.extend(one_results)
                                except Exception as e:
                                    print(f"[WATCH] Parallel batch worker failed: {type(e).__name__}: {e}")
                                    success = False

                        processed_jobs.extend([
                            {
                                "params": dict(job.get("params", {})) if isinstance(job.get("params", {}), dict) else {},
                                "records": job.get("records", []) if isinstance(job.get("records", []), list) else [],
                            }
                            for job in batch_results
                        ])
                        if callback_uri:
                            push_jobs.extend(batch_results)
                            for job in batch_results:
                                deferred_items = job.get("deferred_output_items", [])
                                if isinstance(deferred_items, list) and deferred_items:
                                    deferred_output_jobs.append({
                                        "config_path": str(cfg_path),
                                        "params": dict(job.get("params", {})) if isinstance(job.get("params", {}), dict) else {},
                                        "deferred_output_items": deferred_items,
                                    })
                    else:
                        print(
                            f"[WATCH] Batch file reuse mode enabled: file={file_path.name}, "
                            f"jobs={len(job_params_list)}"
                        )
                        ok, batch_results = run_config_batch_reuse_session(
                            str(cfg_path),
                            headless=headless,
                            params_list=job_params_list,
                            base_url_override=base_url_override,
                            defer_output_save=bool(callback_uri),
                            selenium_remote_url=selenium_remote_url,
                        )
                        if not ok:
                            success = False
                        processed_jobs.extend([
                            {
                                "params": dict(job.get("params", {})) if isinstance(job.get("params", {}), dict) else {},
                                "records": job.get("records", []) if isinstance(job.get("records", []), list) else [],
                            }
                            for job in batch_results
                        ])
                        if callback_uri:
                            push_jobs.extend(batch_results)
                            for job in batch_results:
                                deferred_items = job.get("deferred_output_items", [])
                                if isinstance(deferred_items, list) and deferred_items:
                                    deferred_output_jobs.append({
                                        "config_path": str(cfg_path),
                                        "params": dict(job.get("params", {})) if isinstance(job.get("params", {}), dict) else {},
                                        "deferred_output_items": deferred_items,
                                    })
                else:
                    for idx, job_params in enumerate(job_params_list, start=1):
                        print(
                            f"[WATCH] Trigger crawl: site={site_name}, file={file_path.name}, "
                            f"job={idx}/{len(job_params_list)}, params={sorted(job_params.keys())}, "
                            f"callback_uri={'yes' if callback_uri else 'no'}, "
                            f"base_url_override={'yes' if base_url_override else 'no'}"
                        )
                        ok, records, deferred_items = run_config(
                            str(cfg_path),
                            headless=headless,
                            params=job_params,
                            base_url_override=base_url_override,
                            defer_output_save=bool(callback_uri),
                            selenium_remote_url=selenium_remote_url,
                        )
                        if not ok:
                            success = False
                            break

                        if callback_uri:
                            push_jobs.append({"params": dict(job_params), "records": records})
                            if deferred_items:
                                deferred_output_jobs.append({
                                    "config_path": str(cfg_path),
                                    "params": dict(job_params),
                                    "deferred_output_items": deferred_items,
                                })
                        processed_jobs.append({"params": dict(job_params), "records": records})

                if success and callback_uri:
                    aggregated_push_jobs = list(push_jobs)
                    if skipped_jobs_for_push:
                        aggregated_push_jobs.extend(skipped_jobs_for_push)

                    if len(aggregated_push_jobs) > 1:
                        print(
                            f"[WATCH] Aggregated callback push for file={file_path.name}, "
                            f"jobs={len(aggregated_push_jobs)}"
                        )
                    ok = _push_records_to_uri(
                        uri=callback_uri,
                        site_name=site_name,
                        input_file_name=file_path.name,
                        params=runtime_params,
                        records=[],
                        job_results=aggregated_push_jobs,
                        timeout_seconds=push_timeout_seconds,
                        retries=push_retries,
                        verify_ssl=push_verify_ssl,
                    )
                    if not ok:
                        success = False
                    elif deferred_output_jobs:
                        print(
                            f"[WATCH] Persisting deferred output files after callback success: "
                            f"jobs={len(deferred_output_jobs)}"
                        )
                        grouped_deferred_by_cfg: dict[str, dict[str, Any]] = {}
                        for item in deferred_output_jobs:
                            one_cfg = str(item.get("config_path", "")).strip()
                            if not one_cfg:
                                continue
                            one_params = item.get("params", {})
                            one_deferred_items = item.get("deferred_output_items", [])

                            bucket = grouped_deferred_by_cfg.setdefault(
                                one_cfg,
                                {
                                    "params": dict(one_params) if isinstance(one_params, dict) else {},
                                    "deferred_output_items": [],
                                },
                            )
                            bucket_items = bucket.get("deferred_output_items", [])
                            if isinstance(bucket_items, list) and isinstance(one_deferred_items, list):
                                bucket_items.extend(one_deferred_items)

                        for one_cfg, grouped in grouped_deferred_by_cfg.items():
                            grouped_params = grouped.get("params", {})
                            grouped_items = grouped.get("deferred_output_items", [])
                            ok_save = _persist_deferred_output_items(
                                config_path=one_cfg,
                                headless=headless,
                                params=grouped_params if isinstance(grouped_params, dict) else {},
                                base_url_override=base_url_override,
                                deferred_items=grouped_items if isinstance(grouped_items, list) else [],
                                selenium_remote_url=selenium_remote_url,
                            )
                            if not ok_save:
                                success = False
                                break

                if state_track_field and attempted_values:
                    record_count_by_value: dict[str, int] = {}
                    for job in processed_jobs:
                        params_obj = job.get("params", {}) if isinstance(job.get("params", {}), dict) else {}
                        value = _normalize_item_value(state_track_field, params_obj.get(state_track_field, ""))
                        if not value:
                            continue
                        records_obj = job.get("records", []) if isinstance(job.get("records", []), list) else []
                        record_count_by_value[value] = len(records_obj)

                    state_rows: list[dict[str, Any]] = []
                    for value in attempted_values:
                        if value in record_count_by_value:
                            status = "success" if success else "crawl_ok_push_failed"
                            state_rows.append({
                                "item_value": value,
                                "status": status,
                                "record_count": record_count_by_value.get(value, 0),
                            })
                        else:
                            state_rows.append({
                                "item_value": value,
                                "status": "error",
                                "record_count": 0,
                            })

                    try:
                        _upsert_item_results(
                            state_db_path,
                            site_name=normalized_site_name,
                            request_uri=state_request_uri,
                            item_field=state_track_field,
                            results=state_rows,
                            source_file=file_path.name,
                        )
                        success_count = sum(1 for row in state_rows if str(row.get("status")) == "success")
                        print(
                            f"[WATCH] Item state updated: field={state_track_field}, total={len(state_rows)}, "
                            f"success={success_count}"
                        )
                    except Exception as e:
                        print(f"[WATCH] Failed to update state DB: {type(e).__name__}: {e}")

                if success and file_path.exists():
                    try:
                        archived = archive_input_file(file_path, site_name)
                        seen_fingerprints[key] = fingerprint
                        print(f"[WATCH] Archived processed input: {archived}")
                    except Exception as e:
                        print(f"[WATCH] Failed to archive input file {file_path}: {e}")
                elif not success and file_path.exists():
                    try:
                        renamed = rename_failed_input_for_retry(file_path)
                        print(f"[WATCH] Renamed failed input for retry: {renamed.name}")
                    except Exception as e:
                        # If rename fails, mark fingerprint to avoid tight-loop retries.
                        seen_fingerprints[key] = fingerprint
                        print(f"[WATCH] Failed to rename failed input file {file_path}: {e}")

            removed = [p for p in list(seen_fingerprints.keys()) if p not in active_paths]
            for p in removed:
                seen_fingerprints.pop(p, None)

            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[WATCH] Timer mode stopped by user.")
            return
        except Exception as e:
            print(f"[WATCH] Unexpected error in timer loop: {e}")
            traceback.print_exc()
            time.sleep(interval)


def main() -> None:
    load_dotenv()
    args = parse_args()
    project_root = Path.cwd()
    site_root = (project_root / "config" / "sites").resolve()
    global_cfg = _load_global_config(project_root)
    watch_cfg = global_cfg.get("watch", {}) if isinstance(global_cfg.get("watch"), dict) else {}
    resolved_selenium_remote_url: Optional[str] = None
    if args.selenium_remote_url:
        resolved_selenium_remote_url = _resolve_selenium_remote_url(global_cfg)

    runtime_log_settings = _read_runtime_log_settings(global_cfg)
    log_site_name = _resolve_log_site_name(args)

    log_manager = setup_file_logging(
        project_root=project_root,
        keep_days=runtime_log_settings["retention_days"],
        site_name=log_site_name,
        max_log_bytes=runtime_log_settings["max_bytes"],
        cleanup_interval_seconds=runtime_log_settings["cleanup_interval_seconds"],
    )
    runtime_params: dict[str, str] = {}
    if args.mawb:
        runtime_params["MAWB"] = args.mawb
    if args.container_no:
        runtime_params["ContainerNo"] = args.container_no
    if args.hawno:
        runtime_params["HAWNO"] = args.hawno

    try:
        print(f"[LOG] Writing runtime logs to: {log_manager.log_path}")
        print(f"[LOG] Mode: bounded ring buffer")
        print(f"[LOG] Retention days: {runtime_log_settings['retention_days']}")
        print(f"[LOG] Max file size: {runtime_log_settings['max_mb']} MB")
        print(f"[LOG] Cleanup interval seconds: {runtime_log_settings['cleanup_interval_seconds']}")
        print(f"[LOG] Site scope: {log_site_name}")
        if runtime_params:
            print(f"[PARAMS] Runtime params: {sorted(runtime_params.keys())}")
        if resolved_selenium_remote_url:
            print(f"[BROWSER] Selenium remote mode enabled: {resolved_selenium_remote_url}")

        watch_enabled_by_default = bool(watch_cfg.get("enabled_by_default", True))
        should_watch_input = args.watch_input or (
            watch_enabled_by_default and (not args.all_sites and not args.config and not args.site)
        )
        if should_watch_input:
            interval_seconds = int(args.watch_interval or watch_cfg.get("interval_seconds", 10))
            input_root_raw = args.input_root or watch_cfg.get("input_root", "input")
            push_timeout_seconds = int(watch_cfg.get("push_timeout_seconds", 30))
            push_retries = int(watch_cfg.get("push_retries", 0))
            input_done_retention_days = int(watch_cfg.get("input_done_retention_days", 30))
            raw_push_verify_ssl = watch_cfg.get("push_verify_ssl", True)
            if isinstance(raw_push_verify_ssl, str):
                push_verify_ssl = raw_push_verify_ssl.strip().lower() not in {
                    "0",
                    "false",
                    "no",
                    "off",
                }
            else:
                push_verify_ssl = bool(raw_push_verify_ssl)
            input_root = Path(str(input_root_raw)).resolve()
            input_root.mkdir(parents=True, exist_ok=True)
            watch_sites = _parse_watch_sites(args.watch_sites)
            if watch_sites is None:
                watch_sites = _parse_watch_sites(watch_cfg.get("sites"))
            _run_input_timer_mode(
                project_root=project_root,
                input_root=input_root,
                headless=args.headless,
                interval_seconds=interval_seconds,
                base_runtime_params=runtime_params,
                watch_sites=watch_sites,
                push_timeout_seconds=push_timeout_seconds,
                push_retries=push_retries,
                push_verify_ssl=push_verify_ssl,
                cargo_fallback_cfg=watch_cfg.get("cargo_fallback", {}),
                parallel_cfg=watch_cfg.get("parallel", {}),
                input_done_retention_days=input_done_retention_days,
                selenium_remote_url=resolved_selenium_remote_url,
            )
            return

        if args.all_sites:
            site_configs = sorted(Path("config/sites").glob("*/config.json"))
            if not site_configs:
                print("[ERROR] No site configs found under config/sites/*/config.json")
                return
            print(f"[INFO] Found {len(site_configs)} site(s): {[p.parent.name for p in site_configs]}")
            for cfg_path in site_configs:
                run_config(
                    str(cfg_path),
                    args.headless,
                    runtime_params,
                    selenium_remote_url=resolved_selenium_remote_url,
                )
        elif args.config:
            cfg_path = Path(args.config)
            if not cfg_path.exists():
                print(f"[ERROR] Config file not found: {cfg_path}")
                return
            try:
                relative = cfg_path.resolve().relative_to(site_root)
            except ValueError:
                print("[ERROR] --config only supports files under config/sites/*/config.json")
                return
            if relative.name != "config.json":
                print("[ERROR] --config only supports files named config.json under config/sites/<site>/")
                return
            run_config(
                str(cfg_path),
                args.headless,
                runtime_params,
                selenium_remote_url=resolved_selenium_remote_url,
            )
        elif args.site:
            cfg_path = Path("config/sites") / args.site / "config.json"
            if not cfg_path.exists():
                print(f"[ERROR] Site config not found: {cfg_path}")
                return
            run_config(
                str(cfg_path),
                args.headless,
                runtime_params,
                selenium_remote_url=resolved_selenium_remote_url,
            )
        else:
            print("[ERROR] Please provide --site <name>, --all-sites, --config ..., or use --watch-input")
    finally:
        log_manager.close()


if __name__ == "__main__":
    main()
