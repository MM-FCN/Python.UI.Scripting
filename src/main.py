import argparse
import json
import os
import re
import shutil
import subprocess
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

from src.config_loader import ConfigError, load_config
from src.logging_utils import setup_file_logging
from src.runtime_config import (
    get_deprecated_site_selenium_remote_url,
    load_global_config,
    resolve_selenium_remote_url,
)
from src.workflow import create_workflow_crawler


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
        help="Explicit Selenium Remote WebDriver URL override for this run",
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


def _load_runtime_site_config(
    config_path: str,
    *,
    global_config: Optional[dict[str, Any]] = None,
    selenium_remote_url_override: Optional[str] = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    deprecated_site_url = get_deprecated_site_selenium_remote_url(config)
    resolved_url, source = resolve_selenium_remote_url(
        explicit_override=selenium_remote_url_override,
        global_config=global_config,
    )
    config["selenium_remote_url"] = resolved_url
    config["__selenium_remote_url_source"] = source
    if deprecated_site_url:
        print(
            "[CONFIG] site-level selenium_remote_url is deprecated and ignored for "
            f"{config_path}: {deprecated_site_url}"
        )
    return config


def run_config(
    config_path: str,
    headless: bool,
    params: Optional[dict[str, str]] = None,
    base_url_override: Optional[str] = None,
    global_config: Optional[dict[str, Any]] = None,
    selenium_remote_url_override: Optional[str] = None,
) -> tuple[bool, list[dict[str, Any]]]:
    print(f"\n{'='*60}")
    print(f"[TASK] Config: {config_path}")
    print(f"{'='*60}")
    try:
        config = _load_runtime_site_config(
            config_path,
            global_config=global_config,
            selenium_remote_url_override=selenium_remote_url_override,
        )
        if base_url_override:
            config["base_url"] = base_url_override
            print(f"[TASK] Override base_url by input URI: {base_url_override}")
        crawler = create_workflow_crawler(config=config, headless=headless, params=params or {})
        records = crawler.run()
        print(f"[DONE] Crawl finished. Records: {len(records)}")
        return True, records
    except ConfigError as e:
        print(f"[CONFIG ERROR] {e}")
        return False, []
    except Exception as e:
        print(f"[ERROR] {e}")
        traceback.print_exc()
        return False, []


def run_config_batch_reuse_session(
    config_path: str,
    headless: bool,
    params_list: list[dict[str, str]],
    base_url_override: Optional[str] = None,
    global_config: Optional[dict[str, Any]] = None,
    selenium_remote_url_override: Optional[str] = None,
) -> tuple[bool, list[dict[str, Any]]]:
    print(f"\n{'='*60}")
    print(f"[TASK] Config (batch reuse): {config_path}")
    print(f"[TASK] Batch jobs: {len(params_list)}")
    print(f"{'='*60}")
    try:
        config = _load_runtime_site_config(
            config_path,
            global_config=global_config,
            selenium_remote_url_override=selenium_remote_url_override,
        )
        if base_url_override:
            config["base_url"] = base_url_override
            print(f"[TASK] Override base_url by input URI: {base_url_override}")

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
                job_results.append({"params": dict(crawler.params), "records": records})

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


def _read_log_retention_days(config_path: Optional[Path], default_days: int = 30) -> int:
    if not config_path or not config_path.exists():
        return default_days
    try:
        with config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("log", {}).get("retention_days", default_days)
        days = int(raw)
        if days < 1:
            print(f"[LOG] Invalid retention_days={raw}, fallback to {default_days}")
            return default_days
        return days
    except Exception as e:
        print(f"[LOG] Failed to read log.retention_days from {config_path}: {e}")
        return default_days


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


def _run_input_timer_mode(
    *,
    project_root: Path,
    input_root: Path,
    headless: bool,
    interval_seconds: int,
    base_runtime_params: dict[str, str],
    global_config: Optional[dict[str, Any]] = None,
    selenium_remote_url_override: Optional[str] = None,
    watch_sites: Optional[set[str]] = None,
    push_timeout_seconds: int = 30,
    push_retries: int = 0,
    push_verify_ssl: bool = True,
) -> None:
    interval = max(1, int(interval_seconds))
    seen_fingerprints: dict[str, tuple[int, int]] = {}
    prepared_startup_sites: set[str] = set()

    print(f"[WATCH] Timer mode started. input_root={input_root}, interval={interval}s")
    print("[WATCH] Scanning input/<site>/*.json; changed files will trigger corresponding site crawls.")
    if watch_sites:
        print(f"[WATCH] Site filter enabled: {sorted(watch_sites)}")

    done_root = project_root / "input_done"
    done_root.mkdir(parents=True, exist_ok=True)

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
            if os.name == "nt":
                if batch_path.suffix.lower() in {".bat", ".cmd"}:
                    launch_cmd = ["cmd", "/c", str(batch_path), *batch_args]
                else:
                    launch_cmd = [str(batch_path), *batch_args]
            else:
                if batch_path.suffix.lower() == ".sh":
                    launch_cmd = ["sh", str(batch_path), *batch_args]
                elif batch_path.suffix.lower() in {".bat", ".cmd"}:
                    print(
                        f"[WATCH] browser_startup.batch_file is a Windows script and cannot run on this platform: {batch_path.name}"
                    )
                    return False
                else:
                    launch_cmd = [str(batch_path), *batch_args]

            # Launch browser startup script without blocking watcher loop.
            subprocess.Popen(
                launch_cmd,
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
        shutil.move(str(src), str(dst))
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
                shutil.move(str(src), str(dst))
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
                runtime_params = dict(base_runtime_params)
                runtime_params.update(extracted_params)

                job_params_list: list[dict[str, str]] = []
                if batch:
                    batch_field, batch_values = batch
                    for item in batch_values:
                        one_params = dict(runtime_params)
                        one_params[batch_field] = item
                        job_params_list.append(one_params)
                    print(
                        f"[WATCH] Batch input detected: field={batch_field}, count={len(batch_values)}"
                    )
                else:
                    job_params_list.append(runtime_params)

                success = True
                push_jobs: list[dict[str, Any]] = []
                if batch and len(job_params_list) > 1:
                    print(
                        f"[WATCH] Batch file reuse mode enabled: file={file_path.name}, "
                        f"jobs={len(job_params_list)}"
                    )
                    ok, batch_results = run_config_batch_reuse_session(
                        str(cfg_path),
                        headless=headless,
                        params_list=job_params_list,
                        base_url_override=base_url_override,
                        global_config=global_config,
                        selenium_remote_url_override=selenium_remote_url_override,
                    )
                    if not ok:
                        success = False
                    elif callback_uri:
                        push_jobs.extend(batch_results)
                else:
                    for idx, job_params in enumerate(job_params_list, start=1):
                        print(
                            f"[WATCH] Trigger crawl: site={site_name}, file={file_path.name}, "
                            f"job={idx}/{len(job_params_list)}, params={sorted(job_params.keys())}, "
                            f"callback_uri={'yes' if callback_uri else 'no'}, "
                            f"base_url_override={'yes' if base_url_override else 'no'}"
                        )
                        ok, records = run_config(
                            str(cfg_path),
                            headless=headless,
                            params=job_params,
                            base_url_override=base_url_override,
                            global_config=global_config,
                            selenium_remote_url_override=selenium_remote_url_override,
                        )
                        if not ok:
                            success = False
                            break

                        if callback_uri:
                            push_jobs.append({"params": dict(job_params), "records": records})

                if success and callback_uri:
                    if len(push_jobs) > 1:
                        print(
                            f"[WATCH] Aggregated callback push for file={file_path.name}, "
                            f"jobs={len(push_jobs)}"
                        )
                    ok = _push_records_to_uri(
                        uri=callback_uri,
                        site_name=site_name,
                        input_file_name=file_path.name,
                        params=runtime_params,
                        records=[],
                        job_results=push_jobs,
                        timeout_seconds=push_timeout_seconds,
                        retries=push_retries,
                        verify_ssl=push_verify_ssl,
                    )
                    if not ok:
                        success = False
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
    global_cfg = load_global_config(project_root)
    watch_cfg = global_cfg.get("watch", {}) if isinstance(global_cfg.get("watch"), dict) else {}

    log_cfg_path: Optional[Path] = None
    if args.config:
        log_cfg_path = Path(args.config)
    elif args.site:
        log_cfg_path = Path("config/sites") / args.site / "config.json"
    elif args.all_sites:
        first_site_cfg = sorted(Path("config/sites").glob("*/config.json"))
        if first_site_cfg:
            log_cfg_path = first_site_cfg[0]

    retention_days = _read_log_retention_days(log_cfg_path, default_days=30)
    log_site_name = _resolve_log_site_name(args)

    log_manager = setup_file_logging(
        project_root=project_root,
        keep_days=retention_days,
        site_name=log_site_name,
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
        print(f"[LOG] Retention days: {retention_days}")
        print(f"[LOG] Site scope: {log_site_name}")
        if runtime_params:
            print(f"[PARAMS] Runtime params: {sorted(runtime_params.keys())}")

        watch_enabled_by_default = bool(watch_cfg.get("enabled_by_default", True))
        should_watch_input = args.watch_input or (
            watch_enabled_by_default and (not args.all_sites and not args.config and not args.site)
        )
        if should_watch_input:
            interval_seconds = int(args.watch_interval or watch_cfg.get("interval_seconds", 10))
            input_root_raw = args.input_root or watch_cfg.get("input_root", "input")
            push_timeout_seconds = int(watch_cfg.get("push_timeout_seconds", 30))
            push_retries = int(watch_cfg.get("push_retries", 0))
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
                global_config=global_cfg,
                selenium_remote_url_override=args.selenium_remote_url,
                watch_sites=watch_sites,
                push_timeout_seconds=push_timeout_seconds,
                push_retries=push_retries,
                push_verify_ssl=push_verify_ssl,
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
                    global_config=global_cfg,
                    selenium_remote_url_override=args.selenium_remote_url,
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
                global_config=global_cfg,
                selenium_remote_url_override=args.selenium_remote_url,
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
                global_config=global_cfg,
                selenium_remote_url_override=args.selenium_remote_url,
            )
        else:
            print("[ERROR] Please provide --site <name>, --all-sites, --config ..., or use --watch-input")
    finally:
        log_manager.close()


if __name__ == "__main__":
    main()
