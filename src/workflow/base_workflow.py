# -*- coding: utf-8 -*-
import base64
import csv
import io
import json
import os
import re
import shutil
import tempfile
import time
import requests
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image
from selenium import webdriver
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchElementException,
    NoSuchWindowException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver import ActionChains, Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.select import Select
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.microsoft import EdgeChromiumDriverManager
from webdriver_manager.firefox import GeckoDriverManager

from src.logging_utils import cleanup_old_logs


BY_MAP = {
    "id": By.ID,
    "name": By.NAME,
    "css": By.CSS_SELECTOR,
    "xpath": By.XPATH,
    "class": By.CLASS_NAME,
    "tag": By.TAG_NAME,
    "link_text": By.LINK_TEXT,
    "partial_link_text": By.PARTIAL_LINK_TEXT,
}


class NoDataMatchedStop(Exception):
    """Raised when fallback.no_data condition is matched and workflow should stop early."""


class ManagedChallengeUnresolved(Exception):
    """Raised when managed challenge cannot be cleared automatically."""


class WorkflowCrawler:
    def __init__(self, config: Dict[str, Any], headless: bool = False, params: Dict[str, str] = None) -> None:
        self.config = config
        self.headless = headless
        self.params = params or {}
        self.driver: Optional[webdriver.Remote] = None
        self._browser_name: str = str(config.get("browser", "firefox")).strip().lower()
        self._attached_existing_browser: bool = False
        self._runtime_edge_profile_dir: str = ""
        self.default_timeout = int(config.get("default_timeout", 15))
        self._download_dir: str = str(
            Path(config.get("download_dir", "output/downloads")).resolve()
        )
        self.popup_data: List[Dict[str, Any]] = []  # 存储从弹出框爬取的数据
        self._current_task_name: str = ""
        self._active_scrape_cfg: Dict[str, Any] = {}
        self._challenge_debug_enabled: bool = False
        self._challenge_debug_file: Optional[Path] = None
        self._init_challenge_debug()

    def _init_challenge_debug(self) -> None:
        captcha_cfg = self.config.get("login", {}).get("captcha", {})
        if not isinstance(captcha_cfg, dict):
            return
        debug_cfg = captcha_cfg.get("debug", {})
        if not isinstance(debug_cfg, dict):
            return
        enabled = bool(debug_cfg.get("enabled", False))
        if not enabled:
            return

        root = Path(str(debug_cfg.get("dir", "output/challenge-debug"))).resolve()
        bucket = root / time.strftime("%Y-%m")
        bucket.mkdir(parents=True, exist_ok=True)
        file_name = f"challenge_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.jsonl"
        self._challenge_debug_file = bucket / file_name
        self._challenge_debug_enabled = True
        print(f"[CAPTCHA][DEBUG] Writing challenge debug events to: {self._challenge_debug_file}")

    def _challenge_token_len(self) -> int:
        if not self.driver:
            return 0
        try:
            token = self.driver.execute_script(
                """
                const el = document.querySelector("input[name='cf-turnstile-response'], input[id*='_response']");
                return el ? (el.value || '') : '';
                """
            )
            return len(token or "")
        except Exception:
            return 0

    def _challenge_iframe_count(self) -> int:
        if not self.driver:
            return 0
        try:
            selectors = [
                "iframe[src*='challenge-platform']",
                "iframe[src*='turnstile']",
                "iframe[src*='challenges.cloudflare.com']",
                "iframe[title*='challenge']",
            ]
            total = 0
            for sel in selectors:
                try:
                    total += len(self.driver.find_elements(By.CSS_SELECTOR, sel))
                except Exception:
                    continue
            return total
        except Exception:
            return 0

    def _is_managed_challenge_page(self) -> bool:
        if not self.driver:
            return False
        try:
            markers = [
                ".hal-container-header",
                "script[src*='challenge-platform']",
                "input[name='cf-turnstile-response']",
                "input[id*='_response']",
            ]
            for marker in markers:
                if self.driver.find_elements(By.CSS_SELECTOR, marker):
                    return True
            page_html = (self.driver.page_source or "").lower()
            if "managed challenge" in page_html or "_cf_chl_opt" in page_html:
                return True
            current_url = (self._safe_current_url() or "").lower()
            return "__cf_chl" in current_url
        except Exception:
            return False

    def _log_challenge_event(self, event: str, **data: Any) -> None:
        if not self._challenge_debug_enabled or not self._challenge_debug_file:
            return
        payload = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": event,
            "url": self._safe_current_url(),
            "managed_challenge": self._is_managed_challenge_page(),
            "token_len": self._challenge_token_len(),
            "iframe_count": self._challenge_iframe_count(),
        }
        payload.update(data)
        try:
            with self._challenge_debug_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _get_output_retention_days(self) -> int:
        output_cfg = self.config.get("output", {})
        if isinstance(output_cfg, dict) and output_cfg.get("retention_days") is not None:
            raw = output_cfg.get("retention_days")
        else:
            site_log_cfg = self.config.get("log", {})
            if isinstance(site_log_cfg, dict) and site_log_cfg.get("retention_days") is not None:
                raw = site_log_cfg.get("retention_days")
            else:
                raw = 30

        try:
            days = int(raw)
        except Exception:
            days = 30
        return max(1, days)

    def _cleanup_output_dir(self, folder: Path) -> None:
        try:
            cleanup_old_logs(folder, keep_days=self._get_output_retention_days())
        except Exception as e:
            print(f"[OUTPUT] Retention cleanup skipped for {folder}: {e}")

    def _safe_current_url(self) -> str:
        try:
            return self.driver.current_url if self.driver else "(no-driver)"
        except Exception:
            return "(unavailable)"

    def run(self) -> List[Dict[str, Any]]:
        print("[START] Crawler initializing...")
        print(f"[CONFIG] Base URL: {self.config['base_url']}")
        print("[STEP 1] Starting browser...")
        self._start_browser()
        try:
            startup_navigate = bool(self.config.get("startup_navigate", True))
            if startup_navigate:
                print(f"[STEP 2] Navigating to base URL...")
                self._navigate_to_base_url()
            else:
                print("[STEP 2] Startup navigation skipped by config.")
            self._bootstrap_flaresolverr_cookies()
            print("[STEP 3] Executing login...")
            self._login()

            tasks = self.config.get("tasks")
            if tasks:
                print(f"[STEP 4] Running {len(tasks)} task(s) after login...")
                records = self._run_tasks(tasks)
            else:
                print("[STEP 4] Performing navigation steps...")
                try:
                    self._active_scrape_cfg = self.config.get("scrape", {}) if isinstance(self.config.get("scrape", {}), dict) else {}
                    self._perform_steps(self.config.get("navigation_steps", []))
                    if self._is_fallback_no_data_matched():
                        print("[FALLBACK] no_data matched before scraping, skip scrape and save empty result.")
                        records = []
                    else:
                        print("[STEP 5] Scraping records...")
                        records = self._scrape_records()
                except NoDataMatchedStop as e:
                    print(f"[FALLBACK] {e}; stop flow early and save empty result.")
                    records = []
                finally:
                    self._active_scrape_cfg = {}
                print("[STEP 6] Saving records...")
                self._save_records(records)
            print("[SUCCESS] Crawler completed!")
            return records
        except Exception as e:
            print(f"[ERROR] Run failed: {type(e).__name__}: {e}")
            print(f"[ERROR] Task={self._current_task_name or '(none)'}, URL={self._safe_current_url()}")
            raise
        finally:
            if self.driver:
                keep_open = bool(self.config.get("keep_browser_open", self._attached_existing_browser))
                if keep_open:
                    print("[BROWSER] keep_browser_open=true, leaving browser/session open.")
                else:
                    print("[BROWSER] Closing browser session...")
                    self.driver.quit()
                    self._cleanup_runtime_edge_profile_dir()
            else:
                self._cleanup_runtime_edge_profile_dir()

    def _bootstrap_flaresolverr_cookies(self) -> None:
        cfg = self.config.get("flaresolverr", {})
        if not isinstance(cfg, dict) or not bool(cfg.get("enabled", False)):
            return

        endpoint = str(cfg.get("endpoint", "http://localhost:8191/v1")).strip()
        if not endpoint:
            print("[FS] FlareSolverr enabled but endpoint is empty, skip.")
            return

        target_url = str(cfg.get("url", self.config.get("base_url", ""))).strip()
        warmup_urls_cfg = cfg.get("warmup_urls", [])
        warmup_urls: List[str] = []
        if isinstance(warmup_urls_cfg, list):
            warmup_urls = [str(u).strip() for u in warmup_urls_cfg if str(u).strip()]
        elif isinstance(warmup_urls_cfg, str):
            warmup_urls = [u.strip() for u in warmup_urls_cfg.split(",") if u.strip()]

        solve_urls = warmup_urls + ([target_url] if target_url else [])
        if not solve_urls:
            print("[FS] FlareSolverr target URL is empty, skip.")
            return

        cmd = str(cfg.get("cmd", "request.get")).strip() or "request.get"
        max_timeout = max(1000, int(cfg.get("max_timeout", 60000) or 60000))
        request_timeout = max(1, int(cfg.get("request_timeout", 75) or 75))
        session_ttl = int(cfg.get("session_ttl_minutes", 0) or 0)
        session_name = str(cfg.get("session_id", "")).strip()
        session_enabled = bool(session_name) or session_ttl > 0
        if session_enabled and not session_name:
            session_name = f"crawler-{int(time.time())}"

        payload: Dict[str, Any] = {
            "cmd": cmd,
            "url": target_url,
            "maxTimeout": max_timeout,
        }

        if session_enabled:
            if not self._create_flaresolverr_session(endpoint, session_name, request_timeout):
                print("[FS] Session create failed, skip FlareSolverr bootstrap.")
                return
            payload["session"] = session_name
        if session_ttl > 0:
            payload["session_ttl_minutes"] = session_ttl

        cookies: List[Dict[str, Any]] = []
        html = ""
        solved_url = ""
        try:
            for idx, url in enumerate(solve_urls, start=1):
                one_payload = dict(payload)
                one_payload["url"] = url
                print(f"[FS] Solving challenge via FlareSolverr ({idx}/{len(solve_urls)}): {url}")
                try:
                    resp = requests.post(endpoint, json=one_payload, timeout=request_timeout)
                    result = resp.json() if resp.content else {}
                except Exception as e:
                    print(f"[FS] Request failed on {url}: {type(e).__name__}: {e}")
                    return

                if result.get("status") != "ok":
                    print(f"[FS] Solve failed on {url}: {result.get('message', 'unknown error')}")
                    return

                solution = result.get("solution", {}) if isinstance(result.get("solution", {}), dict) else {}
                cookies = solution.get("cookies", []) if isinstance(solution.get("cookies", []), list) else []
                html = str(solution.get("response", "") or "")
                solved_url = url
        finally:
            if session_enabled and bool(cfg.get("destroy_session_after_bootstrap", True)):
                self._destroy_flaresolverr_session(endpoint, session_name, request_timeout)

        if not cookies:
            print("[FS] Solve returned no cookies, skip cookie bootstrap.")
            return

        challenge_markers = [
            "_cf_chl_opt",
            "challenge-platform",
            "interactive challenge",
            "verify you are human",
        ]
        if html and any(marker in html.lower() for marker in challenge_markers):
            print("[FS] Warning: response still looks like challenge page; applying cookies anyway.")

        if self._apply_external_cookies(target_url, cookies):
            print(f"[FS] Applied {len(cookies)} cookies into browser session.")
            cookie_file = str(cfg.get("cookie_file", "")).strip()
            if cookie_file:
                self._save_external_cookie_file(cookie_file, cookies)
            if bool(cfg.get("navigate_after_apply", True)):
                try:
                    self.driver.get(target_url)
                    print(f"[FS] Navigated back to target URL after cookie apply: {target_url}")
                except Exception as e:
                    print(f"[FS] Failed to navigate to target URL after cookie apply: {type(e).__name__}: {e}")

    def _create_flaresolverr_session(self, endpoint: str, session_name: str, request_timeout: int) -> bool:
        payload = {
            "cmd": "sessions.create",
            "session": session_name,
        }
        try:
            resp = requests.post(endpoint, json=payload, timeout=request_timeout)
            result = resp.json() if resp.content else {}
        except Exception as e:
            print(f"[FS] Session create request failed: {type(e).__name__}: {e}")
            return False

        if result.get("status") == "ok":
            print(f"[FS] Session created: {session_name}")
            return True

        message = str(result.get("message", "") or "")
        if "already exists" in message.lower():
            print(f"[FS] Session already exists, reusing: {session_name}")
            return True

        print(f"[FS] Session create failed: {message or 'unknown error'}")
        return False

    def _destroy_flaresolverr_session(self, endpoint: str, session_name: str, request_timeout: int) -> None:
        payload = {
            "cmd": "sessions.destroy",
            "session": session_name,
        }
        try:
            resp = requests.post(endpoint, json=payload, timeout=request_timeout)
            result = resp.json() if resp.content else {}
            if result.get("status") == "ok":
                print(f"[FS] Session destroyed: {session_name}")
                return
            message = str(result.get("message", "") or "")
            if "does not exist" in message.lower():
                print(f"[FS] Session already gone: {session_name}")
                return
            print(f"[FS] Session destroy failed: {message or 'unknown error'}")
        except Exception as e:
            print(f"[FS] Session destroy request failed: {type(e).__name__}: {e}")

    def _apply_external_cookies(self, target_url: str, cookies: List[Dict[str, Any]]) -> bool:
        if not self.driver:
            return False

        parsed = urlparse(target_url)
        if not parsed.scheme or not parsed.netloc:
            return False
        origin = f"{parsed.scheme}://{parsed.netloc}/"

        try:
            self.driver.get(origin)
            self._override_navigator_webdriver()
        except Exception as e:
            print(f"[FS] Failed to open cookie origin {origin}: {type(e).__name__}: {e}")
            return False

        added = 0
        for raw in cookies:
            if not isinstance(raw, dict):
                continue
            cookie = dict(raw)
            # Selenium add_cookie accepts a strict subset.
            allowed_keys = {"name", "value", "path", "domain", "secure", "httpOnly", "expiry", "sameSite"}
            cookie = {k: v for k, v in cookie.items() if k in allowed_keys}
            if not cookie.get("name"):
                continue
            if "expiry" in cookie:
                try:
                    cookie["expiry"] = int(cookie["expiry"])
                except Exception:
                    cookie.pop("expiry", None)
            try:
                self.driver.add_cookie(cookie)
                added += 1
            except Exception:
                continue

        if added <= 0:
            print("[FS] No cookies were accepted by browser.")
            return False

        try:
            self.driver.refresh()
        except Exception:
            pass
        return True

    def _save_external_cookie_file(self, cookie_file: str, cookies: List[Dict[str, Any]]) -> None:
        try:
            path = Path(cookie_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[FS] Cookies saved to {cookie_file}")
        except Exception as e:
            print(f"[FS] Failed to save cookie file: {type(e).__name__}: {e}")

    def _run_tasks(self, tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """在同一登录会话中顺序执行多个页面任务。"""
        all_records: List[Dict[str, Any]] = []
        task_outputs: List[Dict[str, Any]] = []
        base_scrape = self.config.get("scrape", {})
        base_output = self.config.get("output", {})

        for idx, task in enumerate(tasks, start=1):
            task_name = task.get("name", f"task_{idx}")
            self._current_task_name = task_name
            print(f"[TASK {idx}] {task_name} started")

            self.popup_data = []

            self.config["scrape"] = task.get("scrape", base_scrape)
            self.config["output"] = task.get("output", base_output)
            self._active_scrape_cfg = self.config["scrape"] if isinstance(self.config["scrape"], dict) else {}

            nav_steps = task.get("navigation_steps", self.config.get("navigation_steps", []))
            print(f"[TASK {idx}] navigation steps: {len(nav_steps)}")
            try:
                self._perform_steps(nav_steps)

                if self._is_fallback_no_data_matched():
                    print("[FALLBACK] no_data matched before scraping, skip scrape and save empty result.")
                    records = []
                    self._save_records(records)
                    task_outputs.append({
                        "task_name": str(task_name),
                        "task_key": str(task.get("merge_key", "")).strip(),
                        "records": [],
                    })
                    continue

                records = self._scrape_records()
                self._save_records(records)

                task_records = self.popup_data if self.popup_data else records
                task_records = self._attach_request_identifiers(task_records)
                task_outputs.append({
                    "task_name": str(task_name),
                    "task_key": str(task.get("merge_key", "")).strip(),
                    "records": task_records,
                })
                all_records.extend(task_records)
                print(
                    f"[TASK {idx}] {task_name} completed, records={len(task_records)} "
                    f"(popup_records={len(self.popup_data)}, list_records={len(records)})"
                )
            except NoDataMatchedStop as e:
                print(f"[FALLBACK] {e}; stop current task early and save empty result.")
                records = []
                self._save_records(records)
                task_outputs.append({
                    "task_name": str(task_name),
                    "task_key": str(task.get("merge_key", "")).strip(),
                    "records": [],
                })
                continue
            except Exception as e:
                print(f"[ERROR] Task '{task_name}' failed: {type(e).__name__}: {e}")
                print(f"[ERROR] URL when task failed: {self._safe_current_url()}")
                raise

        self._current_task_name = ""
        self._active_scrape_cfg = {}

        self._save_merged_task_output(task_outputs)

        self.config["scrape"] = base_scrape
        self.config["output"] = base_output
        print(f"[TASK] All tasks finished, total aggregated records={len(all_records)}")
        return all_records

    def _save_merged_task_output(self, task_outputs: List[Dict[str, Any]]) -> None:
        merge_cfg = self.config.get("merged_output", {})
        if not isinstance(merge_cfg, dict) or not bool(merge_cfg.get("enabled", False)):
            return

        configured_keys = merge_cfg.get("global_identifier_keys", ["ContainerNo", "MAWB", "HAWNO"])
        if isinstance(configured_keys, list):
            tracked_keys = [str(k).strip() for k in configured_keys if str(k).strip()]
        else:
            tracked_keys = ["ContainerNo", "MAWB", "HAWNO"]

        payload: Dict[str, Any] = {}
        for key in tracked_keys:
            value = self.params.get(key)
            if value:
                payload[key] = value
        site_name = str(self.config.get("__site_name", "")).strip().lower()
        if site_name:
            payload.setdefault("source_site", site_name)

        for item in task_outputs:
            task_name = str(item.get("task_name", "")).strip()
            explicit_key = str(item.get("task_key", "")).strip()
            records = item.get("records", [])
            if not isinstance(records, list):
                records = []

            cleaned_records: List[Dict[str, Any]] = []
            for row in records:
                if isinstance(row, dict):
                    cleaned_records.append({k: v for k, v in row.items() if k not in tracked_keys})
                else:
                    cleaned_records.append({"value": row})

            if explicit_key:
                key = explicit_key
            else:
                key = re.sub(r"[^a-zA-Z0-9]+", "_", task_name).strip("_").lower() or "task"

            # Avoid key overwrite when duplicate task names/keys exist.
            base_key = key
            suffix = 2
            while key in payload:
                key = f"{base_key}_{suffix}"
                suffix += 1

            payload[key] = cleaned_records

        if bool(self.config.get("__defer_output_save", False)):
            deferred = self.config.setdefault("__deferred_output_items", [])
            if isinstance(deferred, list):
                deferred.append({
                    "kind": "merged_payload",
                    "records": [payload],
                })
            print("[OUTPUT] Deferred save enabled, queued merged payload for callback upload.")
            return

        output_path = Path(str(merge_cfg.get("path", "output/merged_tasks.json")))
        if bool(merge_cfg.get("append_timestamp", True)):
            output_path = self._append_timestamp_to_path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._cleanup_output_dir(output_path.parent)

        as_array = bool(merge_cfg.get("as_array", True))
        append_to_existing = bool(merge_cfg.get("append_to_existing", False))

        final_payload: Any = payload
        if as_array:
            if append_to_existing and output_path.exists():
                try:
                    existing = json.loads(output_path.read_text(encoding="utf-8"))
                except Exception:
                    existing = []
                if isinstance(existing, list):
                    existing.append(payload)
                    final_payload = existing
                else:
                    final_payload = [payload]
            else:
                final_payload = [payload]

        with output_path.open("w", encoding="utf-8") as f:
            json.dump(final_payload, f, ensure_ascii=False, indent=2)

        print(f"[OUTPUT] Saved merged task payload to {output_path}")

    def _start_browser(self) -> None:
        browser = str(self.config.get("browser", "firefox")).strip().lower()
        self._browser_name = browser
        selenium_remote_url = str(self.config.get("selenium_remote_url", "")).strip()
        remote_mode = bool(selenium_remote_url)
        page_load_strategy = str(self.config.get("page_load_strategy", "eager")).strip().lower()
        if page_load_strategy not in {"normal", "eager", "none"}:
            page_load_strategy = "eager"

        print(f"[BROWSER] Creating {browser} options...")
        print(f"[BROWSER] Setting download directory: {self._download_dir}")
        Path(self._download_dir).mkdir(parents=True, exist_ok=True)
        self._cleanup_output_dir(Path(self._download_dir))

        try:
            if browser == "edge":
                options = EdgeOptions()
                options.use_chromium = True
                options.page_load_strategy = page_load_strategy
                edge_cfg = self.config.get("edge", {})
                if not isinstance(edge_cfg, dict):
                    edge_cfg = {}
                edge_stealth_mode = bool(edge_cfg.get("stealth_mode", True))
                edge_headless_stealth = bool(edge_cfg.get("headless_stealth_enabled", True))
                edge_accept_language = str(
                    edge_cfg.get("accept_language", "en-US,en;q=0.9")
                ).strip()
                attach_existing = bool(
                    edge_cfg.get("attach_existing", self.config.get("edge_attach_existing", False))
                )
                edge_binary_location = str(
                    edge_cfg.get("binary_location", self.config.get("edge_binary_location", ""))
                ).strip()
                debugger_address = str(
                    edge_cfg.get("debugger_address", self.config.get("edge_debugger_address", ""))
                ).strip()
                edge_user_data_dir = str(
                    edge_cfg.get("user_data_dir", self.config.get("edge_user_data_dir", ""))
                ).strip()
                # New toggle: whether to reuse the user profile directory when launching Edge.
                # If false, the crawler will always use an isolated runtime profile even when
                # `user_data_dir` is configured.
                use_user_profile = bool(edge_cfg.get("use_user_profile", self.config.get("edge_use_user_profile", True)))
                if edge_user_data_dir:
                    edge_user_data_dir = os.path.expanduser(os.path.expandvars(edge_user_data_dir))
                edge_extra_args = edge_cfg.get("extra_args", [])
                if not isinstance(edge_extra_args, list):
                    edge_extra_args = []
                if self.headless:
                    options.add_argument("--headless=new")
                    options.add_argument("--window-size=1600,1000")
                    options.add_argument("--window-position=0,0")
                    options.add_argument("--force-device-scale-factor=1")
                    options.add_argument("--hide-scrollbars")
                else:
                    options.add_argument("--start-maximized")

                if edge_accept_language:
                    options.add_argument(f"--lang={edge_accept_language.split(',')[0].strip()}")

                if edge_binary_location and not remote_mode:
                    expanded_edge_binary = os.path.expanduser(os.path.expandvars(edge_binary_location))
                    if Path(expanded_edge_binary).exists():
                        options.binary_location = expanded_edge_binary
                        print(f"[BROWSER] Edge binary override: {expanded_edge_binary}")
                    else:
                        print(f"[BROWSER] Edge binary override not found, ignored: {expanded_edge_binary}")
                elif edge_binary_location and remote_mode:
                    print("[BROWSER] Edge binary_location ignored in remote Selenium mode.")

                # Keep direct-launch behavior closer to a regular user browser profile.
                # In attach mode, some experimental options are rejected by msedgedriver.
                if edge_stealth_mode and not attach_existing:
                    options.add_argument("--disable-blink-features=AutomationControlled")
                    options.add_argument("--disable-infobars")
                    options.add_argument("--disable-notifications")
                    options.add_argument("--no-default-browser-check")
                    options.add_argument("--no-first-run")
                    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
                    options.add_experimental_option("useAutomationExtension", False)

                if edge_user_data_dir and use_user_profile and not attach_existing and not remote_mode:
                    try:
                        Path(edge_user_data_dir).mkdir(parents=True, exist_ok=True)
                    except Exception:
                        pass
                    options.add_argument(f"--user-data-dir={edge_user_data_dir}")
                    print(f"[BROWSER] Edge direct mode using user data dir: {edge_user_data_dir}")
                elif not attach_existing and not remote_mode:
                    # Use an isolated temporary profile so automation does not attach to an existing Edge process.
                    runtime_profile = tempfile.mkdtemp(prefix="edge-crawler-", dir=str(Path(tempfile.gettempdir())))
                    self._runtime_edge_profile_dir = runtime_profile
                    options.add_argument(f"--user-data-dir={runtime_profile}")
                    print(f"[BROWSER] Edge direct mode using isolated runtime profile: {runtime_profile}")
                elif remote_mode and edge_user_data_dir:
                    print("[BROWSER] Edge user_data_dir ignored in remote Selenium mode.")

                for arg in edge_extra_args:
                    arg_str = str(arg).strip()
                    if arg_str:
                        options.add_argument(arg_str)

                edge_prefs = {
                    "download.prompt_for_download": False,
                    "download.directory_upgrade": True,
                    "safebrowsing.enabled": True,
                    "credentials_enable_service": False,
                    "profile.password_manager_enabled": False,
                    "intl.accept_languages": edge_accept_language,
                }
                if not remote_mode:
                    edge_prefs["download.default_directory"] = self._download_dir
                options.add_experimental_option("prefs", edge_prefs)
                if attach_existing:
                    if not debugger_address:
                        raise ValueError(
                            "Edge attach mode requires debugger address via edge.debugger_address or edge_debugger_address"
                        )
                    options.add_experimental_option("debuggerAddress", debugger_address)
                    print(f"[BROWSER] Edge attach mode enabled: debuggerAddress={debugger_address}")
                if selenium_remote_url:
                    if attach_existing:
                        raise ValueError("Edge attach mode is not supported when selenium_remote_url is enabled")
                    self._attached_existing_browser = False
                    print(f"[BROWSER] Selenium remote mode enabled: {selenium_remote_url}")
                    self.driver = webdriver.Remote(command_executor=selenium_remote_url, options=options)
                else:
                    self._attached_existing_browser = attach_existing
                    print("[BROWSER] Resolving msedgedriver...")
                    edge_exe = self._resolve_edgedriver_path()
                    service = EdgeService(edge_exe)
                    self.driver = webdriver.Edge(service=service, options=options)

                if self.headless and edge_stealth_mode and edge_headless_stealth and not attach_existing:
                    self._apply_edge_headless_overrides(edge_cfg)
            else:
                options = Options()
                options.page_load_strategy = page_load_strategy
                if self.headless:
                    options.add_argument("-headless")
                    options.add_argument("--window-size=1600,1000")
                else:
                    options.add_argument("--start-maximized")

                # Firefox keeps these args but may ignore Chromium-specific flags.
                options.add_argument("--disable-blink-features=AutomationControlled")
                options.add_argument("--disable-infobars")

                options.set_preference("browser.download.folderList", 2)
                if not remote_mode:
                    options.set_preference("browser.download.dir", self._download_dir)
                options.set_preference("browser.download.useDownloadDir", True)
                options.set_preference("browser.download.manager.showWhenStarting", False)
                options.set_preference(
                    "browser.helperApps.neverAsk.saveToDisk",
                    "application/octet-stream,application/pdf,text/csv,application/zip",
                )
                options.set_preference("pdfjs.disabled", True)
                options.set_preference("dom.webdriver.enabled", False)
                if selenium_remote_url:
                    print(f"[BROWSER] Selenium remote mode enabled: {selenium_remote_url}")
                    self.driver = webdriver.Remote(command_executor=selenium_remote_url, options=options)
                else:
                    print("[BROWSER] Resolving geckodriver...")
                    gecko_exe = self._resolve_geckodriver_path()
                    service = Service(gecko_exe)
                    self.driver = webdriver.Firefox(service=service, options=options)

            self._bootstrap_stealth_js()
            if not self.headless:
                try:
                    self.driver.maximize_window()
                    print("[BROWSER] Window maximized.")
                except Exception as e:
                    print(f"[BROWSER] Maximize window failed, continue with current size: {e}")
        except Exception as e:
            print(f"[BROWSER] Error starting {browser}: {e}")
            raise

        print(f"[BROWSER] {browser} started successfully!")

    def _cleanup_runtime_edge_profile_dir(self) -> None:
        if not self._runtime_edge_profile_dir:
            return
        try:
            shutil.rmtree(self._runtime_edge_profile_dir, ignore_errors=True)
            print(f"[BROWSER] Runtime Edge profile cleaned: {self._runtime_edge_profile_dir}")
        except Exception as e:
            print(f"[BROWSER] Runtime Edge profile cleanup skipped: {e}")
        finally:
            self._runtime_edge_profile_dir = ""

    def _bootstrap_stealth_js(self) -> None:
        """Inject stealth JS right after driver init, before any business page navigation."""
        if not self.driver:
            return
        if self._browser_name == "edge":
            self._install_cdp_stealth_script()
        if self._attached_existing_browser:
            print("[BROWSER] Attached existing browser, skip about:blank bootstrap.")
            self._override_navigator_webdriver()
            return
        bootstrap_open_base_url = bool(self.config.get("bootstrap_open_base_url", True))
        bootstrap_url = str(self.config.get("base_url", "")).strip() if bootstrap_open_base_url else ""

        # Only attempt bootstrap navigation when a real base_url is configured.
        if bootstrap_open_base_url and bootstrap_url:
            target_url = bootstrap_url
            attempts = max(1, int(self.config.get("bootstrap_nav_attempts", 2)))
            for attempt in range(1, attempts + 1):
                try:
                    # Default behavior opens base_url directly so the first visible page is the target site.
                    self.driver.get(target_url)
                    current_url = self._safe_current_url()
                    if current_url and not current_url.endswith("about:blank"):
                        print(f"[BROWSER] Bootstrap navigation success to {current_url} (attempt {attempt}/{attempts})")
                        break
                    else:
                        print(f"[BROWSER] Bootstrap navigation attempt {attempt}/{attempts} result: {current_url}")
                except Exception as e:
                    print(f"[BROWSER] Bootstrap navigation attempt {attempt}/{attempts} failed: {type(e).__name__}: {e}")

                if attempt < attempts:
                    time.sleep(0.5)
        else:
            print("[BROWSER] Skipping bootstrap navigation (bootstrap_open_base_url=false or base_url empty).")

        self._override_navigator_webdriver()

    def _resolve_external_stealth_js_path(self) -> Optional[Path]:
        edge_cfg = self.config.get("edge", {})
        configured_path = ""
        if isinstance(edge_cfg, dict):
            configured_path = str(edge_cfg.get("stealth_js_path", "")).strip()

        project_root = Path(__file__).resolve().parents[2]
        candidates: List[Path] = []
        if configured_path:
            p = Path(configured_path)
            if not p.is_absolute():
                p = (project_root / p).resolve()
            candidates.append(p)

        # Default to src/stealth.min.js used by test.py.
        candidates.append((Path(__file__).resolve().parents[1] / "stealth.min.js").resolve())

        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    def _load_external_stealth_js(self) -> str:
        path = self._resolve_external_stealth_js_path()
        if not path:
            return ""
        try:
            content = path.read_text(encoding="utf-8").strip()
            if content:
                print(f"[BROWSER] External stealth JS loaded: {path}")
                return content
        except Exception as e:
            print(f"[BROWSER] External stealth JS load skipped: {type(e).__name__}: {e}")
        return ""

    def _install_cdp_stealth_script(self) -> None:
        """Install CDP script so stealth overrides run before every new document."""
        if not self.driver:
            return
        built_in_script = """
            // webdriver
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
                configurable: true
            });

            // chromium runtime
            window.chrome = window.chrome || { runtime: {} };

            // plugins
            Object.defineProperty(navigator, 'plugins', {
                get: () => [
                    { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                    { name: 'Chromium PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                    { name: 'Microsoft Edge PDF Viewer', filename: 'internal-edge-pdf-viewer', description: '' }
                ],
                configurable: true
            });

            // languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
                configurable: true
            });

            // common fingerprint fields
            Object.defineProperty(navigator, 'platform', {
                get: () => 'Win32',
                configurable: true
            });
            Object.defineProperty(navigator, 'hardwareConcurrency', {
                get: () => 8,
                configurable: true
            });
            Object.defineProperty(navigator, 'deviceMemory', {
                get: () => 8,
                configurable: true
            });
            Object.defineProperty(navigator, 'maxTouchPoints', {
                get: () => 0,
                configurable: true
            });

            // permissions API patch used by many bot checks
            const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
            if (originalQuery) {
                window.navigator.permissions.query = (parameters) => (
                    parameters && parameters.name === 'notifications'
                        ? Promise.resolve({ state: Notification.permission })
                        : originalQuery(parameters)
                );
            }

            // avoid obvious headless geometry mismatch
            if (window.outerWidth === 0) {
                Object.defineProperty(window, 'outerWidth', {
                    get: () => window.innerWidth,
                    configurable: true
                });
            }
            if (window.outerHeight === 0) {
                Object.defineProperty(window, 'outerHeight', {
                    get: () => window.innerHeight + 80,
                    configurable: true
                });
            }
        """
        external_script = self._load_external_stealth_js()
        script = built_in_script
        if external_script:
            script = f"{external_script}\n\n{built_in_script}"
        try:
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": script},
            )
            print("[BROWSER] CDP stealth script installed.")
        except Exception as e:
            print(f"[BROWSER] CDP stealth install skipped: {type(e).__name__}: {e}")

    def _apply_edge_headless_overrides(self, edge_cfg: Dict[str, Any]) -> None:
        """Apply runtime overrides to make Edge headless fingerprint closer to normal desktop browsing."""
        if not self.driver:
            return

        accept_language = str(
            edge_cfg.get("accept_language", "en-US,en;q=0.9")
        ).strip()
        platform = str(edge_cfg.get("platform", "Win32")).strip() or "Win32"
        timezone = str(edge_cfg.get("timezone", "")).strip()
        ua_override = str(edge_cfg.get("user_agent", "")).strip()

        try:
            current_ua = str(self.driver.execute_script("return navigator.userAgent") or "")
        except Exception:
            current_ua = ""

        if not ua_override:
            ua_override = current_ua.replace("HeadlessChrome", "Chrome")

        try:
            self.driver.execute_cdp_cmd("Network.enable", {})
            self.driver.execute_cdp_cmd(
                "Network.setUserAgentOverride",
                {
                    "userAgent": ua_override,
                    "acceptLanguage": accept_language,
                    "platform": platform,
                },
            )
            print("[BROWSER] Edge headless UA/language/platform override applied.")
        except Exception as e:
            print(f"[BROWSER] Edge headless UA override skipped: {type(e).__name__}: {e}")

        if timezone:
            try:
                self.driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": timezone})
                print(f"[BROWSER] Edge headless timezone override applied: {timezone}")
            except Exception as e:
                print(f"[BROWSER] Edge headless timezone override skipped: {type(e).__name__}: {e}")

    def _override_navigator_webdriver(self) -> None:
        """Inject JS to override automation-related globals on current document."""
        if not self.driver:
            return
        script = """
            try {
                Object.defineProperty(Navigator.prototype, 'webdriver', {
                    get: () => undefined,
                    configurable: true
                });
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                    configurable: true
                });
            } catch (e) {
                try {
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => false,
                        configurable: true
                    });
                } catch (_) {}
            }

            try {
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en'],
                    configurable: true
                });
            } catch (_) {}

            try {
                Object.defineProperty(navigator, 'platform', {
                    get: () => 'Win32',
                    configurable: true
                });
            } catch (_) {}

            try {
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5],
                    configurable: true
                });
            } catch (_) {}
        """
        try:
            self.driver.execute_script(script)
        except Exception as e:
            print(f"[BROWSER] navigator.webdriver override skipped: {type(e).__name__}: {e}")

    def _restart_browser(self) -> None:
        """Restart browser session when startup navigation loses the browsing context."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            finally:
                self.driver = None
        self._start_browser()

    def _ensure_browser_context(self, context: str = "") -> None:
        """Ensure the current driver is attached to a valid browser window; auto-recover when configured."""
        if not self.driver:
            raise RuntimeError("Browser driver is not initialized")

        try:
            handles = self._get_window_handles_with_retry(retries=2, delay=0.2)
            if not handles:
                raise NoSuchWindowException("No browser window handles available")
            _ = self.driver.current_url
            return
        except Exception as e:
            auto_recover = bool(self.config.get("auto_recover_browser_context", True))
            marker = f" ({context})" if context else ""
            if not auto_recover:
                print(f"[BROWSER] Context check failed{marker}: {type(e).__name__}: {e}")
                raise

            print(
                f"[BROWSER] Context invalid{marker}: {type(e).__name__}: {e}. "
                f"Restarting browser session..."
            )
            self._restart_browser()
            handles = self._get_window_handles_with_retry(retries=2, delay=0.2)
            if not handles:
                raise RuntimeError("Browser context recovery failed: no window handles after restart")
            _ = self.driver.current_url

            if bool(self.config.get("recover_navigate_base_url", True)):
                try:
                    base_url = str(self.config.get("base_url", "")).strip()
                    if base_url:
                        self.driver.get(base_url)
                        self._override_navigator_webdriver()
                        print(f"[BROWSER] Context recovered{marker}; navigated to base URL.")
                except Exception as nav_err:
                    print(
                        f"[BROWSER] Context recovered{marker} but base navigation failed: "
                        f"{type(nav_err).__name__}: {nav_err}"
                    )

    def _navigate_to_base_url(self) -> None:
        """Navigate to base URL with retry for transient Firefox/geckodriver session loss."""
        url = self.config["base_url"]
        target_prefix = str(url).split("#", 1)[0]
        retry_count = int(self.config.get("startup_nav_retry", 1))
        max_attempts = max(1, retry_count + 1)
        page_load_timeout = int(self.config.get("page_load_timeout", 60))

        last_error: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                if not self.driver:
                    self._start_browser()

                current_url = self._safe_current_url()
                if current_url != "(unavailable)" and current_url.startswith(target_prefix):
                    self._override_navigator_webdriver()
                    print(f"[STEP 2] Base URL already open, skip duplicate navigation. URL={current_url}")
                    return

                self.driver.set_page_load_timeout(page_load_timeout)
                self.driver.get(url)
                self._override_navigator_webdriver()
                print(f"[STEP 2] Navigation success on attempt {attempt}/{max_attempts}")
                return
            except TimeoutException as e:
                last_error = e
                current_url = self._safe_current_url()
                ready_state = "(unknown)"
                try:
                    if self.driver:
                        ready_state = str(self.driver.execute_script("return document.readyState"))
                except Exception:
                    pass

                print(
                    f"[STEP 2] Navigation timeout on attempt {attempt}/{max_attempts}: {e}. "
                    f"URL={current_url}, readyState={ready_state}"
                )

                if current_url != "(unavailable)" and current_url.startswith(target_prefix):
                    print("[STEP 2] Target URL is already reached despite timeout, continue.")
                    return

                if attempt < max_attempts:
                    print("[STEP 2] Restarting browser and retrying navigation after timeout...")
                    self._restart_browser()
                    continue
                raise
            except (NoSuchWindowException, InvalidSessionIdException) as e:
                last_error = e
                print(
                    f"[STEP 2] Navigation session lost on attempt {attempt}/{max_attempts}: "
                    f"{type(e).__name__}: {e}"
                )
                if attempt < max_attempts:
                    print("[STEP 2] Restarting browser and retrying navigation...")
                    self._restart_browser()
                else:
                    raise
            except WebDriverException as e:
                # Some Firefox failures surface as generic WebDriverException with this message.
                msg = str(e)
                if "Browsing context has been discarded" in msg:
                    last_error = e
                    print(
                        f"[STEP 2] Browsing context discarded on attempt {attempt}/{max_attempts}: {e}"
                    )
                    if attempt < max_attempts:
                        print("[STEP 2] Restarting browser and retrying navigation...")
                        self._restart_browser()
                        continue
                raise

        if last_error:
            raise last_error

    def _resolve_geckodriver_path(self) -> str:
        """Resolve geckodriver path with local-first strategy for restricted networks."""
        candidates: List[Path] = []

        env_path = os.getenv("GECKODRIVER_PATH")
        if env_path:
            candidates.append(Path(env_path))

        path_hit = shutil.which("geckodriver")
        if path_hit:
            candidates.append(Path(path_hit))

        candidates.extend([
            Path("geckodriver"),
            Path("geckodriver.exe"),
            Path("drivers") / "geckodriver",
            Path("drivers") / "geckodriver.exe",
            Path.home() / ".wdm" / "drivers" / "geckodriver" / "linux64" / "geckodriver",
            Path.home() / ".wdm" / "drivers" / "geckodriver" / "win64" / "geckodriver.exe",
        ])

        cache_root = Path.home() / ".wdm" / "drivers" / "geckodriver"
        if cache_root.exists():
            for path in sorted(cache_root.glob("**/geckodriver*"), reverse=True):
                candidates.append(path)

        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except Exception:
                continue
            if resolved.exists() and resolved.is_file():
                print(f"[BROWSER] Using local geckodriver: {resolved}")
                return str(resolved)

        print("[BROWSER] No local geckodriver found, downloading via webdriver-manager...")
        return GeckoDriverManager().install()

    def _resolve_edgedriver_path(self) -> str:
        """Resolve msedgedriver path with local-first strategy for restricted networks."""
        candidates: List[Path] = []

        env_path = os.getenv("MSEDGEDRIVER_PATH")
        if env_path:
            candidates.append(Path(env_path))

        path_hit = shutil.which("msedgedriver")
        if path_hit:
            candidates.append(Path(path_hit))

        candidates.extend([
            Path("drivers") / "msedgedriver",
            Path("drivers") / "msedgedriver.exe",
            Path("msedgedriver"),
            Path("msedgedriver.exe"),
            Path.home() / ".wdm" / "drivers" / "edgedriver" / "linux64" / "msedgedriver",
            Path.home() / ".wdm" / "drivers" / "edgedriver" / "win64" / "msedgedriver.exe",
        ])

        cache_root = Path.home() / ".wdm" / "drivers" / "edgedriver"
        if cache_root.exists():
            for path in sorted(cache_root.glob("**/msedgedriver*"), reverse=True):
                candidates.append(path)

        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except Exception:
                continue
            if resolved.exists() and resolved.is_file():
                print(f"[BROWSER] Using local msedgedriver: {resolved}")
                return str(resolved)

        print("[BROWSER] No local msedgedriver found, downloading via webdriver-manager...")
        return EdgeChromiumDriverManager().install()

    def _login(self) -> None:
        login_cfg = self.config.get("login", {})
        if not login_cfg.get("enabled", True):
            print("[INFO] Login is disabled by config.")
            return

        # 尝试用已保存的 Cookie 跳过登录
        cookie_file = login_cfg.get("cookie_file")
        if cookie_file and self._load_cookies(login_cfg.get("url", self.config["base_url"]), cookie_file):
            return

        max_session_attempts = max(1, int(login_cfg.get("session_retry_attempts", 2)))

        def _is_session_lost_error(err: Exception) -> bool:
            if isinstance(err, (NoSuchWindowException, InvalidSessionIdException)):
                return True
            msg = str(err)
            markers = [
                "Browsing context has been discarded",
                "Failed to decode response from marionette",
                "Tried to run command without establishing a connection",
                "Browser session ended unexpectedly during login wait.",
            ]
            return any(m in msg for m in markers)

        for session_attempt in range(1, max_session_attempts + 1):
            try:
                print(f"[LOGIN] Session attempt {session_attempt}/{max_session_attempts}")
                print(f"[LOGIN] Navigating to login URL: {login_cfg.get('url')}")
                if login_cfg.get("url"):
                    self.driver.get(login_cfg["url"])
                    self._override_navigator_webdriver()

                self._wait_login_page_ready(login_cfg)

                field_delay = float(login_cfg.get("field_delay", 0.35))

                for field in login_cfg.get("fields", []):
                    value = field.get("value", "")
                    env_var = field.get("env")
                    if env_var:
                        value = os.getenv(env_var, "")
                    self._type_text(field["by"], field["selector"], value, clear_first=True)
                    time.sleep(field_delay)

                submit = login_cfg.get("submit")
                if submit:
                    self._click(submit["by"], submit["selector"])
                    # 点击登录后等待服务端响应并触发验证码弹窗
                    post_submit_delay = float(login_cfg.get("post_submit_delay", 2.0))
                    if post_submit_delay > 0:
                        print(f"[LOGIN] Waiting {post_submit_delay}s after submit for captcha to trigger...")
                        time.sleep(post_submit_delay)

                # 自动处理验证码
                captcha_cfg = login_cfg.get("captcha")
                if captcha_cfg:
                    self._wait_captcha_popup(captcha_cfg)
                    self._handle_captcha(captcha_cfg)

                # 调试：打印当前 URL，方便确认登录后跳转到哪里
                time.sleep(2)
                print(f"[LOGIN] Current URL after captcha: {self._safe_current_url()}")

                self._wait_login_success(login_cfg)

                # 登录成功后保存 Cookie
                if cookie_file:
                    self._save_cookies(cookie_file)
                return
            except Exception as e:
                if _is_session_lost_error(e) and session_attempt < max_session_attempts:
                    print(
                        f"[LOGIN] Session lost on attempt {session_attempt}/{max_session_attempts}: "
                        f"{type(e).__name__}: {e}"
                    )
                    print("[LOGIN] Restarting browser and retrying login...")
                    self._restart_browser()
                    continue
                raise

    def _wait_login_page_ready(self, login_cfg: Dict[str, Any]) -> None:
        """等待登录页 JS 和表单可交互，避免页面未初始化就输入。"""
        timeout = int(login_cfg.get("page_ready_timeout", self.default_timeout))
        WebDriverWait(self.driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

        submit = login_cfg.get("submit")
        if submit:
            self._wait_clickable(submit["by"], submit["selector"], timeout=timeout)

        # 给前端框架一次渲染周期，防止组件刚挂载时输入丢失
        time.sleep(float(login_cfg.get("post_ready_sleep", 0.6)))

    # ------------------------------------------------------------------ Cookie
    def _load_cookies(self, base_url: str, cookie_file: str) -> bool:
        path = Path(cookie_file)
        if not path.exists():
            print("[COOKIE] No cookie file, will do full login.")
            return False
        try:
            self.driver.get(base_url)
            self._override_navigator_webdriver()
            for cookie in json.loads(path.read_text(encoding="utf-8")):
                cookie.pop("sameSite", None)
                try:
                    self.driver.add_cookie(cookie)
                except Exception:
                    pass
            self.driver.refresh()
            print(f"[COOKIE] Loaded cookies from {cookie_file}")
            self._wait_login_success(self.config["login"])
            print("[COOKIE] Cookie login verified.")
            return True
        except Exception as e:
            print(f"[COOKIE] Cookie invalid ({e}), falling back to full login.")
            Path(cookie_file).unlink(missing_ok=True)
            return False

    def _save_cookies(self, cookie_file: str) -> None:
        path = Path(cookie_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.driver.get_cookies(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[COOKIE] Cookies saved to {cookie_file}")

    # --------------------------------------------------------------- Captcha
    # Shared captcha support lives in the base workflow so future sites can
    # reuse the same login/captcha pipeline through config only.
    def _cfg_selectors(self, cfg: Dict[str, Any], key: str) -> List[str]:
        """Normalize config selector value into a selector list."""
        val = cfg.get(key, "")
        if isinstance(val, list):
            return [str(s).strip() for s in val if str(s).strip()]
        if isinstance(val, str):
            return [s.strip() for s in val.split(",") if s.strip()]
        return []

    def _switch_to_captcha_iframe(self, cfg: Dict[str, Any], probe_selectors: Optional[List[str]] = None) -> bool:
        """Try switching into captcha iframe, defaulting to DataDome-style iframe selectors."""
        iframe_selectors = self._cfg_selectors(cfg, "iframe_selector")
        if not iframe_selectors:
            iframe_selectors = [
                "iframe[src*='captcha-delivery.com']",
                "iframe[title*='CAPTCHA']",
                "iframe[src*='captcha']",
            ]

        probe_selectors = [s for s in (probe_selectors or []) if s]

        try:
            self.driver.switch_to.default_content()
        except Exception:
            return False

        for sel in iframe_selectors:
            try:
                frames = self.driver.find_elements(By.CSS_SELECTOR, sel)
            except Exception:
                continue

            for frame in frames:
                try:
                    self.driver.switch_to.default_content()
                    try:
                        if not frame.is_displayed():
                            continue
                    except Exception:
                        pass
                    self.driver.switch_to.frame(frame)
                    if not probe_selectors:
                        print(f"[CAPTCHA] Switched to iframe via selector: {sel}")
                        return True
                    for p in probe_selectors:
                        elements = self.driver.find_elements(By.CSS_SELECTOR, p)
                        if any(el.is_displayed() for el in elements):
                            print(f"[CAPTCHA] Switched to captcha iframe via selector: {sel}")
                            return True
                except Exception:
                    continue

        if probe_selectors:
            try:
                all_frames = self.driver.find_elements(By.TAG_NAME, "iframe")
            except Exception:
                all_frames = []

            for idx, frame in enumerate(all_frames, start=1):
                try:
                    self.driver.switch_to.default_content()
                    self.driver.switch_to.frame(frame)
                    for p in probe_selectors:
                        if self.driver.find_elements(By.CSS_SELECTOR, p):
                            print(f"[CAPTCHA] Switched to captcha iframe by probing frame #{idx}")
                            return True
                except Exception:
                    continue

        try:
            self.driver.switch_to.default_content()
        except Exception:
            pass
        return False

    def _wait_captcha_popup(self, captcha_cfg: Dict[str, Any]) -> None:
        popup_selector = captcha_cfg.get("popup_selector")
        popup_selectors = self._cfg_selectors(captcha_cfg, "popup_selector")
        popup_timeout = int(captcha_cfg.get("popup_timeout", 15))
        popup_attempts = int(captcha_cfg.get("popup_attempts", 2))

        if not popup_selector:
            time.sleep(2)
            return

        for attempt in range(1, popup_attempts + 1):
            print(f"[CAPTCHA] Waiting for captcha popup: {popup_selector} (attempt {attempt}/{popup_attempts})")
            try:
                self._switch_to_captcha_iframe(captcha_cfg, probe_selectors=popup_selectors)
                self._wait_visible("css", popup_selector, timeout=popup_timeout)
                print("[CAPTCHA] Captcha popup detected!")
                time.sleep(0.8)
                try:
                    self.driver.switch_to.default_content()
                except Exception:
                    pass
                return
            except TimeoutException:
                try:
                    self.driver.switch_to.default_content()
                except Exception:
                    pass
                if attempt == popup_attempts:
                    print("[CAPTCHA] Popup not found after retries, proceeding to solver...")
                    return
                # 弹窗没出现时等待后重试，不要点登录（会触发图形验证失败）
                print("[CAPTCHA] Popup not shown yet, waiting before retry...")
                time.sleep(2)

    def _handle_captcha(self, captcha_cfg: Dict[str, Any]) -> None:
        captcha_type = captcha_cfg.get("type", "slider")
        manual_mode = bool(captcha_cfg.get("manual", False))
        try:
            if manual_mode:
                self._wait_manual_captcha(captcha_cfg)
            elif captcha_type == "slider":
                self._solve_slider_captcha(captcha_cfg)
            elif captcha_type == "checkbox":
                self._solve_checkbox_captcha(captcha_cfg)
            else:
                raise ValueError(f"Unsupported captcha type: {captcha_type}")
        finally:
            # Captcha can run in iframe; restore to main document for later steps.
            try:
                self.driver.switch_to.default_content()
            except Exception:
                pass

    def _wait_manual_captcha(self, cfg: Dict[str, Any]) -> None:
        """Pause for manual captcha solving and continue once popup disappears or success marker appears."""
        manual_timeout = int(cfg.get("manual_timeout", 180))
        popup_selector = cfg.get("popup_selector", "")
        wait_selector = cfg.get("wait_selector", "")
        fail_selector = cfg.get("fail_selector", "")
        fail_text = cfg.get("fail_text", "")

        popup_selectors = self._cfg_selectors(cfg, "popup_selector")
        self._switch_to_captcha_iframe(cfg, probe_selectors=popup_selectors)

        print(f"[CAPTCHA] Manual mode enabled. Please drag the slider manually within {manual_timeout}s...")
        deadline = time.time() + manual_timeout

        while time.time() < deadline:
            if wait_selector:
                try:
                    self._wait_visible("css", wait_selector, timeout=2)
                    print("[CAPTCHA] Manual captcha success marker detected.")
                    return
                except Exception:
                    pass

            if popup_selector:
                try:
                    popup_visible = False
                    for sel in popup_selectors or [popup_selector]:
                        for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                            if el.is_displayed():
                                popup_visible = True
                                break
                        if popup_visible:
                            break
                    if not popup_visible:
                        print("[CAPTCHA] Manual captcha popup disappeared, continue.")
                        return
                except Exception:
                    pass

            if self._is_captcha_failed(fail_selector, fail_text):
                print("[CAPTCHA] Manual captcha failed message detected, continue waiting...")

            time.sleep(1)

        print("[CAPTCHA] Manual captcha wait timeout reached, continue workflow.")

    def _solve_slider_captcha(self, cfg: Dict[str, Any], max_retries: int = 3) -> None:
        """自动识别滑动拼图验证码并完成拖拽。支持多个备选选择器。"""
        bg_selectors = self._cfg_selectors(cfg, "bg_selector")
        piece_selectors = self._cfg_selectors(cfg, "piece_selector")
        slider_selectors = self._cfg_selectors(cfg, "slider_selector")
        popup_selectors = self._cfg_selectors(cfg, "popup_selector")
        wait_selector = cfg.get("wait_selector", "")
        fail_selector = cfg.get("fail_selector", "")
        fail_text = cfg.get("fail_text", "")
        timeout = int(cfg.get("timeout", 10))

        fixed_drag_distance = cfg.get("fixed_drag_distance")
        track_width = cfg.get("track_width")
        slider_width = cfg.get("slider_width")
        if fixed_drag_distance is None and track_width is not None and slider_width is not None:
            try:
                fixed_drag_distance = int(track_width) - int(slider_width)
            except Exception:
                fixed_drag_distance = None

        probe_selectors = list(dict.fromkeys(slider_selectors + bg_selectors + piece_selectors + popup_selectors))

        print(f"[CAPTCHA] bg_selectors={bg_selectors}")
        print(f"[CAPTCHA] slider_selectors={slider_selectors}")

        for attempt in range(1, max_retries + 1):
            print(f"[CAPTCHA] Attempt {attempt}/{max_retries}")
            try:
                self._switch_to_captcha_iframe(cfg, probe_selectors=probe_selectors)

                # 查找滑块元素
                slider = None
                slider_selector = None
                for sel in slider_selectors:
                    try:
                        slider = self._wait_visible("css", sel, timeout=8)
                        slider_selector = sel
                        print(f"[CAPTCHA] Found slider with selector: {sel}")
                        break
                    except:
                        continue
                
                if not slider:
                    # 尝试查找任何按钮作为滑块
                    try:
                        slider = self._wait_visible("xpath", "//div[contains(@class, 'slider') or contains(@class, 'captcha')]//*[@draggable='true' or contains(@class, 'btn')]", timeout=3)
                        print(f"[CAPTCHA] Found slider via generic XPath")
                    except:
                        pass
                
                if not slider:
                    print(f"[CAPTCHA] Could not find slider element")
                    time.sleep(1)
                    continue

                # 获取缺口位置：优先 canvas JS 方法，回退截图法，最后固定距离
                gap_x = None

                canvas_selector = cfg.get("canvas_selector", "")
                piece_img_selector = cfg.get("piece_img_selector", "")
                if canvas_selector and piece_img_selector:
                    # 某些验证码拼图块 img 会稍后渲染，先短暂等待
                    try:
                        self._wait_visible("css", piece_img_selector.split(",")[0].strip(), timeout=2)
                    except Exception:
                        pass
                    gap_x = self._detect_gap_canvas_js(canvas_selector, piece_img_selector)

                if gap_x is None:
                    bg_selector = None
                    for sel in bg_selectors:
                        try:
                            self.driver.find_element(By.CSS_SELECTOR, sel)
                            bg_selector = sel
                            print(f"[CAPTCHA] Found background with selector: {sel}")
                            break
                        except:
                            continue
                    if bg_selector:
                        gap_x = self._detect_slider_gap(bg_selector, piece_selectors[0] if piece_selectors else None)

                if gap_x is None:
                    if fixed_drag_distance is not None:
                        drag_distance = int(fixed_drag_distance)
                        print(f"[CAPTCHA] Gap detection failed, use fixed drag distance={drag_distance}px")
                    else:
                        print("[CAPTCHA] All gap detection failed, refreshing captcha and retrying...")
                        self._recover_captcha(cfg)
                        time.sleep(1)
                        continue
                else:
                    print(f"[CAPTCHA] Target position x={gap_x}px, slider at x={slider.location['x']}")
                    drag_distance = gap_x - slider.location["x"]

                if abs(drag_distance) < 20:
                    print(f"[CAPTCHA] Computed drag too small ({drag_distance}px), refreshing captcha and retrying...")
                    self._recover_captcha(cfg)
                    time.sleep(1)
                    continue
                
                # 执行拖动
                self._drag_slider(slider, drag_distance)
                print(f"[CAPTCHA] Dragged {drag_distance}px")

                # 等待成功：优先检测滑块消失（说明页面已响应），其次检测成功元素
                time.sleep(1)
                slider_gone = False
                try:
                    WebDriverWait(self.driver, 3).until(
                        EC.invisibility_of_element_located((By.CSS_SELECTOR, slider_selector))
                    )
                    slider_gone = True
                    print("[CAPTCHA] ✓ Slider disappeared - captcha accepted!")
                except:
                    pass

                if slider_gone:
                    return

                if wait_selector:
                    try:
                        self._wait_visible("css", wait_selector, timeout=timeout)
                        print("[CAPTCHA] ✓ Slider captcha solved!")
                        return
                    except TimeoutException:
                        print(f"[CAPTCHA] Verification still waiting... retrying...")
                        self._recover_captcha(cfg)
                        time.sleep(1)
                else:
                    if self._is_captcha_failed(fail_selector, fail_text):
                        print("[CAPTCHA] Explicit failure detected, refreshing captcha and retrying...")
                        self._recover_captcha(cfg)
                        time.sleep(1)
                        continue
                    print("[CAPTCHA] Drag completed but no success marker, retrying...")
                    self._recover_captcha(cfg)
                    time.sleep(1)
                    continue
            except Exception as e:
                print(f"[CAPTCHA] Error on attempt {attempt}: {type(e).__name__}: {str(e)[:100]}")
                self._recover_captcha(cfg)
                time.sleep(1.5)

        print("[CAPTCHA] All retries exhausted. Assuming captcha may be auto-verified or skipped...")
        time.sleep(3)

    def _is_captcha_failed(self, fail_selector: str, fail_text: str) -> bool:
        """检测是否出现验证码失败提示。"""
        try:
            if not fail_selector:
                return False
            el = self.driver.find_element(By.CSS_SELECTOR, fail_selector)
            if not el.is_displayed():
                return False
            text = (el.text or "").strip()
            if fail_text:
                return fail_text in text
            return bool(text)
        except Exception:
            return False

    def _solve_checkbox_captcha(self, cfg: Dict[str, Any]) -> None:
        """Solve checkbox-style verification (e.g. Cloudflare Turnstile checkbox) when present."""
        checkbox_selectors = self._cfg_selectors(cfg, "checkbox_selector")
        popup_selectors = self._cfg_selectors(cfg, "popup_selector")
        success_selectors = self._cfg_selectors(cfg, "success_selector")
        max_retries = max(1, int(cfg.get("checkbox_retries", 2)))
        monitor_seconds = max(1.0, float(cfg.get("checkbox_monitor_seconds", 8.0) or 8.0))
        retry_interval = max(0.2, float(cfg.get("checkbox_retry_interval_seconds", 0.8) or 0.8))

        if not checkbox_selectors:
            checkbox_selectors = [
                "#content input[type='checkbox']",
                "#content .cb-lb",
                "#content .cb-i",
            ]

        probe_selectors = list(dict.fromkeys(checkbox_selectors + popup_selectors))

        for attempt in range(1, max_retries + 1):
            try:
                self._switch_to_captcha_iframe(cfg, probe_selectors=probe_selectors)
                end_time = time.time() + monitor_seconds
                self._log_challenge_event(
                    "checkbox_attempt_start",
                    attempt=attempt,
                    max_retries=max_retries,
                    monitor_seconds=monitor_seconds,
                )
                while time.time() < end_time:
                    # Success condition 1: success marker appears.
                    if success_selectors:
                        for success_sel in success_selectors:
                            try:
                                elements = self.driver.find_elements(By.CSS_SELECTOR, success_sel)
                                if any(el.is_displayed() for el in elements):
                                    print(f"[CAPTCHA] Checkbox verification success marker found: {success_sel}")
                                    self._log_challenge_event(
                                        "checkbox_success_marker",
                                        attempt=attempt,
                                        selector=success_sel,
                                    )
                                    return
                            except Exception:
                                continue

                    # Success condition 2: popup container is no longer visible.
                    if popup_selectors:
                        popup_visible = False
                        for popup_sel in popup_selectors:
                            try:
                                popup_elems = self.driver.find_elements(By.CSS_SELECTOR, popup_sel)
                                if any(el.is_displayed() for el in popup_elems):
                                    popup_visible = True
                                    break
                            except Exception:
                                continue
                        if not popup_visible:
                            print("[CAPTCHA] Checkbox popup disappeared, continue workflow.")
                            self._log_challenge_event(
                                "checkbox_popup_disappeared",
                                attempt=attempt,
                            )
                            return

                    clicked = False
                    for sel in checkbox_selectors:
                        elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                        for el in elements:
                            try:
                                if not el.is_displayed():
                                    continue
                                try:
                                    self.driver.execute_script(
                                        "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                                        el,
                                    )
                                except Exception:
                                    pass

                                try:
                                    ActionChains(self.driver).move_to_element(el).pause(0.05).click(el).perform()
                                except Exception:
                                    self.driver.execute_script("arguments[0].click();", el)

                                # For Turnstile, clicking the parent label often works better than input itself.
                                try:
                                    self.driver.execute_script(
                                        "const n=arguments[0]; const lb=n.closest('label'); if (lb) lb.click();",
                                        el,
                                    )
                                except Exception:
                                    pass

                                print(f"[CAPTCHA] Checkbox clicked via selector: {sel}")
                                self._log_challenge_event(
                                    "checkbox_clicked",
                                    attempt=attempt,
                                    selector=sel,
                                )
                                clicked = True
                                break
                            except Exception:
                                continue
                        if clicked:
                            break

                    # Checked state is a useful intermediate signal; continue monitoring because
                    # Cloudflare may require another click/challenge pass.
                    if clicked:
                        try:
                            checked_inputs = self.driver.find_elements(By.CSS_SELECTOR, "#content input[type='checkbox']:checked")
                            if checked_inputs:
                                print("[CAPTCHA] Checkbox checked state detected, keep monitoring...")
                                self._log_challenge_event(
                                    "checkbox_checked_state",
                                    attempt=attempt,
                                )
                        except Exception:
                            pass
                    else:
                        print(f"[CAPTCHA] Checkbox not found/clickable yet (attempt {attempt}/{max_retries})")
                        self._log_challenge_event(
                            "checkbox_not_found",
                            attempt=attempt,
                        )

                    time.sleep(retry_interval)
            except Exception as e:
                print(f"[CAPTCHA] Checkbox solve attempt {attempt} error: {type(e).__name__}: {e}")
                self._log_challenge_event(
                    "checkbox_attempt_error",
                    attempt=attempt,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                time.sleep(0.8)

        print("[CAPTCHA] Checkbox captcha not solved after retries; continue workflow.")

    def _handle_managed_challenge(self, cfg: Dict[str, Any], context: str = "") -> bool:
        """Best-effort handling for Cloudflare managed challenge pages."""
        wait_seconds = max(3.0, float(cfg.get("managed_wait_seconds", 12.0) or 12.0))
        poll_interval = max(0.3, float(cfg.get("managed_poll_interval_seconds", 0.8) or 0.8))
        refresh_retries = max(0, int(cfg.get("managed_refresh_retries", 1) or 1))

        marker = f" ({context})" if context else ""
        self._log_challenge_event(
            "managed_challenge_detected",
            context=context,
            wait_seconds=wait_seconds,
            refresh_retries=refresh_retries,
        )

        for attempt in range(1, refresh_retries + 2):
            print(
                f"[CAPTCHA] Managed challenge detected{marker}. "
                f"Waiting for auto-pass ({attempt}/{refresh_retries + 1})..."
            )
            end_time = time.time() + wait_seconds
            while time.time() < end_time:
                managed = self._is_managed_challenge_page()
                token_len = self._challenge_token_len()
                current_url = (self._safe_current_url() or "").lower()
                if not managed and "__cf_chl" not in current_url:
                    print(f"[CAPTCHA] Managed challenge cleared{marker}, continue workflow.")
                    self._log_challenge_event(
                        "managed_challenge_cleared",
                        context=context,
                        attempt=attempt,
                        token_len=token_len,
                    )
                    return True

                clicked = self._attempt_managed_challenge_click(cfg)
                if clicked:
                    self._log_challenge_event(
                        "managed_challenge_click",
                        context=context,
                        attempt=attempt,
                    )
                time.sleep(poll_interval)

            if attempt <= refresh_retries:
                print(f"[CAPTCHA] Managed challenge still active{marker}, refreshing page and retrying...")
                self._log_challenge_event(
                    "managed_challenge_refresh",
                    context=context,
                    attempt=attempt,
                )
                try:
                    self.driver.refresh()
                except Exception as e:
                    self._log_challenge_event(
                        "managed_challenge_refresh_error",
                        context=context,
                        attempt=attempt,
                        error_type=type(e).__name__,
                        error=str(e),
                    )
                time.sleep(1.0)

        print(f"[CAPTCHA] Managed challenge unresolved{marker}; fail current task.")
        self._log_challenge_event(
            "managed_challenge_unresolved",
            context=context,
        )
        raise ManagedChallengeUnresolved(
            f"Managed challenge unresolved{marker}; aborting to avoid false allow_empty result"
        )

    def _attempt_managed_challenge_click(self, cfg: Dict[str, Any]) -> bool:
        """Try clicking visible verify/checkbox controls in default content and challenge iframe."""
        clicked = False
        verify_xpaths = cfg.get("managed_verify_xpaths", [])
        if not isinstance(verify_xpaths, list) or not verify_xpaths:
            verify_xpaths = [
                "//span[contains(normalize-space(), 'Verify you are human')]/ancestor::label",
                "//label[.//span[contains(normalize-space(), 'Verify you are human')]]",
                "//div[@id='content']//label[contains(@class,'cb-lb')]",
                "//div[@id='content']//input[@type='checkbox']",
                "//input[@type='checkbox' or @role='checkbox']",
            ]

        checkbox_selectors = self._cfg_selectors(cfg, "checkbox_selector")
        popup_selectors = self._cfg_selectors(cfg, "popup_selector")
        probe_selectors = list(dict.fromkeys(checkbox_selectors + popup_selectors))

        def _click_targets(in_iframe: bool = False) -> bool:
            local_clicked = False
            for xp in verify_xpaths:
                try:
                    elements = self.driver.find_elements(By.XPATH, xp)
                except Exception:
                    continue
                for el in elements:
                    try:
                        if not el.is_displayed():
                            continue
                        try:
                            self.driver.execute_script(
                                "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                                el,
                            )
                        except Exception:
                            pass
                        try:
                            ActionChains(self.driver).move_to_element(el).pause(0.05).click(el).perform()
                        except Exception:
                            self.driver.execute_script("arguments[0].click();", el)
                        print(f"[CAPTCHA] Managed verify clicked via xpath: {xp}")
                        local_clicked = True
                        break
                    except Exception:
                        continue
                if local_clicked:
                    break

            if local_clicked:
                return True

            for sel in checkbox_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                except Exception:
                    continue
                for el in elements:
                    try:
                        if not el.is_displayed():
                            continue
                        try:
                            self.driver.execute_script(
                                "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                                el,
                            )
                        except Exception:
                            pass
                        try:
                            ActionChains(self.driver).move_to_element(el).pause(0.05).click(el).perform()
                        except Exception:
                            self.driver.execute_script("arguments[0].click();", el)
                        print(
                            f"[CAPTCHA] Managed verify clicked via css: {sel}"
                            f"{' (iframe)' if in_iframe else ''}"
                        )
                        return True
                    except Exception:
                        continue

                        # Some challenge widgets render controls under shadow DOM.
                        try:
                                clicked_shadow = bool(
                                        self.driver.execute_script(
                                                """
                                                const selectors = arguments[0] || [];
                                                const isVisible = (el) => {
                                                    if (!el) return false;
                                                    const st = window.getComputedStyle(el);
                                                    if (!st) return false;
                                                    const r = el.getBoundingClientRect();
                                                    return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 0 && r.height > 0;
                                                };
                                                const clickInRoot = (root) => {
                                                    for (const sel of selectors) {
                                                        const el = root.querySelector(sel);
                                                        if (el && isVisible(el)) {
                                                            try { el.scrollIntoView({block:'center', inline:'center'}); } catch(e) {}
                                                            try { el.click(); return true; } catch(e) {}
                                                        }
                                                    }
                                                    const nodes = root.querySelectorAll('*');
                                                    for (const n of nodes) {
                                                        if (n && n.shadowRoot && clickInRoot(n.shadowRoot)) return true;
                                                    }
                                                    return false;
                                                };
                                                return clickInRoot(document);
                                                """,
                                                [
                                                        "label.cb-lb",
                                                        "input[type='checkbox']",
                                                        "span.cb-lb-t",
                                                        "[role='checkbox']",
                                                ],
                                        )
                                )
                                if clicked_shadow:
                                        print(
                                                "[CAPTCHA] Managed verify clicked via shadow DOM"
                                                f"{' (iframe)' if in_iframe else ''}"
                                        )
                                        return True
                        except Exception:
                                pass
            return False

        try:
            try:
                self.driver.switch_to.default_content()
            except Exception:
                pass
            clicked = _click_targets(in_iframe=False)

            if clicked:
                return True

            in_iframe = False
            try:
                in_iframe = self._switch_to_captcha_iframe(cfg, probe_selectors=probe_selectors)
                if in_iframe:
                    clicked = _click_targets(in_iframe=True)
            finally:
                if in_iframe:
                    try:
                        self.driver.switch_to.default_content()
                    except Exception:
                        pass

            if clicked:
                return True

            # Last resort: scan all iframes because challenge widget frame may vary by build.
            try:
                frames = self.driver.find_elements(By.TAG_NAME, "iframe")
            except Exception:
                frames = []

            for idx in range(len(frames)):
                try:
                    self.driver.switch_to.default_content()
                except Exception:
                    pass
                try:
                    frames = self.driver.find_elements(By.TAG_NAME, "iframe")
                    if idx >= len(frames):
                        continue
                    self.driver.switch_to.frame(frames[idx])
                    if _click_targets(in_iframe=True):
                        print(f"[CAPTCHA] Managed verify clicked in iframe index={idx}")
                        clicked = True
                        break
                except Exception:
                    continue
                finally:
                    try:
                        self.driver.switch_to.default_content()
                    except Exception:
                        pass
        except Exception:
            return False

        return clicked

    def _handle_captcha_if_exists(self, cfg: Dict[str, Any], context: str = "") -> bool:
        """Check captcha quickly and solve only when slider/popup is present."""
        if not isinstance(cfg, dict) or not cfg:
            return False

        captcha_type = str(cfg.get("type", "slider")).strip().lower()
        popup_selectors = self._cfg_selectors(cfg, "popup_selector")
        slider_selectors = self._cfg_selectors(cfg, "slider_selector")
        checkbox_selectors = self._cfg_selectors(cfg, "checkbox_selector")

        if captcha_type == "checkbox":
            probe_selectors = list(dict.fromkeys(checkbox_selectors + popup_selectors))
        else:
            probe_selectors = list(dict.fromkeys(popup_selectors + slider_selectors))

        if not probe_selectors:
            probe_selectors = [
                ".slider .sliderIcon",
                ".sliderIcon",
                ".sliderTarget .sliderTargetIcon",
            ]

        detected = False
        in_iframe = False
        try:
            self._log_challenge_event(
                "captcha_probe",
                context=context,
                captcha_type=captcha_type,
                probe_selector_count=len(probe_selectors),
            )

            if self._is_managed_challenge_page():
                return self._handle_managed_challenge(cfg, context=context)

            in_iframe = self._switch_to_captcha_iframe(cfg, probe_selectors=probe_selectors)

            for sel in probe_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    if any(el.is_displayed() for el in elements):
                        detected = True
                        break
                except Exception:
                    continue

            if not detected:
                try:
                    fallback = self.driver.find_elements(
                        By.XPATH,
                        "//div[contains(@class,'slider') or contains(@class,'captcha')]//*[@draggable='true' or contains(@class,'sliderIcon') or contains(@class,'sliderTargetIcon')]",
                    )
                    detected = any(el.is_displayed() for el in fallback)
                except Exception:
                    detected = False

            if detected:
                marker = f" ({context})" if context else ""
                detected_kind = "checkbox" if captcha_type == "checkbox" else "slider"
                print(f"[CAPTCHA] Detected {detected_kind} captcha{marker}, handling now...")
                self._log_challenge_event(
                    "captcha_detected",
                    context=context,
                    captcha_type=detected_kind,
                )
                self._handle_captcha(cfg)
                self._log_challenge_event(
                    "captcha_handle_done",
                    context=context,
                    captcha_type=detected_kind,
                )
                return True
            return False
        finally:
            if in_iframe:
                try:
                    self.driver.switch_to.default_content()
                except Exception:
                    pass

    def _get_auto_captcha_cfg(self) -> Optional[Dict[str, Any]]:
        """Return captcha config when auto watch during steps is enabled."""
        captcha_cfg = self.config.get("login", {}).get("captcha", {})
        if not isinstance(captcha_cfg, dict) or not captcha_cfg:
            return None
        if not bool(captcha_cfg.get("auto_check_during_steps", False)):
            return None
        return captcha_cfg

    def _auto_check_captcha(self, context: str = "") -> bool:
        """Best-effort captcha check used by long-running actions."""
        captcha_cfg = self._get_auto_captcha_cfg()
        if not captcha_cfg:
            return False
        try:
            return self._handle_captcha_if_exists(captcha_cfg, context=context)
        except ManagedChallengeUnresolved:
            raise
        except Exception as e:
            marker = f" ({context})" if context else ""
            print(f"[CAPTCHA] Auto check skipped due to error{marker}: {type(e).__name__}: {e}")
            return False

    def _sleep_with_captcha_watch(self, seconds: float, context: str = "") -> None:
        """Sleep in short chunks so intermittent captcha can be handled promptly."""
        total = max(0.0, float(seconds or 0))
        if total <= 0:
            return

        captcha_cfg = self._get_auto_captcha_cfg()
        if not captcha_cfg:
            time.sleep(total)
            return

        interval = float(captcha_cfg.get("monitor_interval_seconds", 0.6) or 0.6)
        if interval <= 0:
            interval = 0.6

        end_time = time.time() + total
        while True:
            remaining = end_time - time.time()
            if remaining <= 0:
                return
            self._auto_check_captcha(context=context or "during sleep")
            time.sleep(min(interval, remaining))

    def _is_abort_page_detected(self, selector: str, text_contains: str) -> bool:
        if not self.driver:
            return False
        sel = str(selector or "").strip()
        txt = str(text_contains or "").strip().lower()
        if not sel and not txt:
            return False

        try:
            if sel:
                elems = self.driver.find_elements(By.CSS_SELECTOR, sel)
                if not elems:
                    return False
                for el in elems:
                    try:
                        if not el.is_displayed():
                            continue
                    except Exception:
                        continue
                    if not txt:
                        return True
                    try:
                        content = (el.text or "").strip().lower()
                    except Exception:
                        content = ""
                    if txt and txt in content:
                        return True
                return False

            if txt:
                page_text = ""
                try:
                    page_text = (self.driver.find_element(By.TAG_NAME, "body").text or "").lower()
                except Exception:
                    page_text = ""
                return bool(page_text and txt in page_text)
        except Exception:
            return False
        return False

    def _is_fallback_no_data_matched(self) -> bool:
        """Check fallback.no_data rule from site config and return True once matched."""
        if not self.driver:
            return False

        fallback_cfg = self.config.get("fallback", {})
        if not isinstance(fallback_cfg, dict):
            return False
        no_data_cfg = fallback_cfg.get("no_data", {})
        if not isinstance(no_data_cfg, dict):
            return False

        # Check page text against fallback.no_data.text_contains only.
        # This avoids treating "page not opened/target not reached" as no-data.
        raw_text_contains = no_data_cfg.get("text_contains", [])
        text_needles: List[str] = []
        if isinstance(raw_text_contains, str):
            text_needles = [s.strip().lower() for s in raw_text_contains.split(",") if s.strip()]
        elif isinstance(raw_text_contains, list):
            text_needles = [str(s).strip().lower() for s in raw_text_contains if str(s).strip()]

        message_selector = str(no_data_cfg.get("message_selector", "")).strip()
        no_data_message = ""
        if message_selector:
            try:
                msg_elements = self.driver.find_elements(By.CSS_SELECTOR, message_selector)
                for el in msg_elements:
                    try:
                        if not el.is_displayed():
                            continue
                    except Exception:
                        continue
                    text = (el.text or "").strip()
                    if text:
                        no_data_message = text
                        break
            except Exception:
                no_data_message = ""

        if not text_needles and not no_data_message:
            return False

        page_text = ""
        try:
            page_text = (self.driver.find_element(By.TAG_NAME, "body").text or "").lower()
        except Exception:
            page_text = ""

        for needle in text_needles:
            if needle and needle in page_text:
                if no_data_message:
                    print(f"[FALLBACK] no_data message: {no_data_message}")
                print(f"[FALLBACK] no_data matched by text: {needle}")
                return True

        if no_data_message:
            no_data_message_lower = no_data_message.lower()
            for needle in text_needles:
                if needle and needle in no_data_message_lower:
                    print(f"[FALLBACK] no_data message: {no_data_message}")
                    print(f"[FALLBACK] no_data matched by message text: {needle}")
                    return True

        return False

    def _wait_visible_with_captcha_watch(
        self,
        by: str,
        selector: str,
        timeout: Optional[int] = None,
        context: str = "",
    ) -> Any:
        """Wait for visible element while checking captcha in the background loop."""
        total_timeout = int(timeout or self.default_timeout)
        by_value = self._to_by(by)
        end_time = time.time() + total_timeout
        poll = 0.25

        while time.time() < end_time:
            self._auto_check_captcha(context=context or f"wait visible: {selector}")
            if self._is_fallback_no_data_matched():
                raise NoDataMatchedStop("fallback.no_data matched during wait visible")
            try:
                elements = self.driver.find_elements(by_value, selector)
                for el in elements:
                    if el.is_displayed():
                        return el
            except Exception:
                pass
            time.sleep(poll)

        print(f"[WAIT] visible timeout: by={by}, selector={selector}, timeout={total_timeout}s")
        print(f"[WAIT] URL={self._safe_current_url()}")
        raise TimeoutException(f"Element not visible within timeout: by={by}, selector={selector}")

    def _wait_present_with_captcha_watch(
        self,
        by: str,
        selector: str,
        timeout: Optional[int] = None,
        context: str = "",
    ) -> Any:
        """Wait for element presence (exists in DOM) while checking captcha in background."""
        total_timeout = int(timeout or self.default_timeout)
        by_value = self._to_by(by)
        end_time = time.time() + total_timeout
        poll = 0.25

        while time.time() < end_time:
            self._auto_check_captcha(context=context or f"wait present: {selector}")
            if self._is_fallback_no_data_matched():
                raise NoDataMatchedStop("fallback.no_data matched during wait present")
            try:
                elements = self.driver.find_elements(by_value, selector)
                if elements:
                    return elements[0]
            except Exception:
                pass
            time.sleep(poll)

        print(f"[WAIT] present timeout: by={by}, selector={selector}, timeout={total_timeout}s")
        print(f"[WAIT] URL={self._safe_current_url()}")
        raise TimeoutException(f"Element not present within timeout: by={by}, selector={selector}")

    def _wait_invisible_with_captcha_watch(
        self,
        by: str,
        selector: str,
        timeout: Optional[int] = None,
        context: str = "",
    ) -> bool:
        """Wait for element invisible while checking captcha in the background loop."""
        total_timeout = int(timeout or self.default_timeout)
        by_value = self._to_by(by)
        end_time = time.time() + total_timeout
        poll = 0.25

        while time.time() < end_time:
            self._auto_check_captcha(context=context or f"wait invisible: {selector}")
            if self._is_fallback_no_data_matched():
                raise NoDataMatchedStop("fallback.no_data matched during wait invisible")
            try:
                elements = self.driver.find_elements(by_value, selector)
                if not elements:
                    return True
                if all(not el.is_displayed() for el in elements):
                    return True
            except Exception:
                return True
            time.sleep(poll)

        print(f"[WAIT] invisible timeout: by={by}, selector={selector}, timeout={total_timeout}s")
        print(f"[WAIT] URL={self._safe_current_url()}")
        raise TimeoutException(f"Element still visible after timeout: by={by}, selector={selector}")

    def _wait_url_contains_with_captcha_watch(
        self,
        keyword: str,
        timeout: Optional[int] = None,
        context: str = "",
    ) -> None:
        """Wait for URL contains while checking captcha in the background loop."""
        total_timeout = int(timeout or self.default_timeout)
        end_time = time.time() + total_timeout
        poll = 0.25
        marker = str(keyword or "")

        while time.time() < end_time:
            self._auto_check_captcha(context=context or f"wait url contains: {marker}")
            if self._is_fallback_no_data_matched():
                raise NoDataMatchedStop("fallback.no_data matched during wait_url_contains")
            try:
                current_url = self._safe_current_url()
                if marker in current_url:
                    return
            except Exception:
                pass
            time.sleep(poll)

        raise TimeoutException(f"URL did not contain '{marker}' within {total_timeout}s")

    def _recover_captcha(self, cfg: Dict[str, Any]) -> None:
        """验证码失败后尝试刷新/重开验证码。"""
        refresh_selectors = cfg.get("refresh_selector", "")
        reopen_selector = cfg.get("reopen_selector", "")
        reopen_by = cfg.get("reopen_by", "css")

        # 优先点击验证码刷新按钮
        candidates = []
        if isinstance(refresh_selectors, str):
            candidates.extend([s.strip() for s in refresh_selectors.split(",") if s.strip()])
        elif isinstance(refresh_selectors, list):
            candidates.extend(refresh_selectors)

        candidates.extend([
            ".slider-refresh",
            ".captcha-refresh",
            ".icon-refresh",
            ".iconfont.icon-shuaxin",
        ])

        for sel in candidates:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed() and el.is_enabled():
                    self.driver.execute_script("arguments[0].click();", el)
                    print(f"[CAPTCHA] Clicked refresh selector: {sel}")
                    time.sleep(0.6)
                    return
            except Exception:
                continue

        # 没有刷新按钮则重开验证码（重新点登录）
        # 但若验证码弹窗当前仍可见，不能点登录，否则触发「图形验证失败」
        popup_selector_check = cfg.get("popup_selector", "")
        if popup_selector_check:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, popup_selector_check)
                if el.is_displayed():
                    print("[CAPTCHA] Popup still visible, skip reopen to avoid captcha failure")
                    time.sleep(1.0)
                    return
            except Exception:
                pass

        if reopen_selector:
            try:
                self._click(reopen_by, reopen_selector, timeout=5)
                print(f"[CAPTCHA] Reopened captcha via: {reopen_selector}")
                # 等弹窗重新出现，而非固定等 0.8s
                popup_selector = cfg.get("popup_selector")
                popup_timeout = int(cfg.get("popup_timeout", 15))
                if popup_selector:
                    try:
                        self._wait_visible("css", popup_selector, timeout=popup_timeout)
                        time.sleep(0.5)  # 弹窗出现后给动画完成时间
                        print("[CAPTCHA] Captcha popup ready after reopen.")
                    except TimeoutException:
                        time.sleep(1.0)
                else:
                    time.sleep(2.0)
            except Exception:
                pass

    def _detect_gap_canvas_js(self, canvas_selector: str, piece_img_selector: str) -> Optional[int]:
        """通过 JS 读取 canvas 背景图和拼图块图像，OpenCV 模板匹配找缺口位置，返回页面绝对 X 坐标（CSS 像素）。"""
        try:
            # 读取 canvas 图像和位置信息
            canvas_info = self.driver.execute_script(f"""
                var c = document.querySelector('{canvas_selector}');
                if (!c) return null;
                var r = c.getBoundingClientRect();
                return {{
                    b64: c.toDataURL('image/png').split(',')[1],
                    left: r.left + window.scrollX,
                    domWidth: r.width,
                    logicalWidth: c.width
                }};
            """)
            if not canvas_info or not canvas_info.get('b64'):
                print(f"[CAPTCHA] Canvas not found: {canvas_selector}")
                return None

            # 解码图像
            bg_img = cv2.imdecode(np.frombuffer(base64.b64decode(canvas_info['b64']), np.uint8), cv2.IMREAD_COLOR)
            if bg_img is None:
                print("[CAPTCHA] Image decode failed")
                return None

            # 读取拼图块 img src（某些验证码无该元素，需要回退边缘检测）
            piece_b64 = self.driver.execute_script(f"""
                var img = document.querySelector('{piece_img_selector}') || document.querySelector('.canvasArea .block img') || document.querySelector('.block img');
                if (!img) return null;
                var src = img.src || '';
                return src.startsWith('data:') ? src.split(',')[1] : null;
            """)
            piece_img = None
            if piece_b64:
                piece_img = cv2.imdecode(np.frombuffer(base64.b64decode(piece_b64), np.uint8), cv2.IMREAD_UNCHANGED)

            # 缩放比（canvas 逻辑像素 → CSS 像素）
            dom_width = float(canvas_info['domWidth'])
            logical_width = float(canvas_info['logicalWidth']) or bg_img.shape[1]
            scale = dom_width / logical_width if logical_width > 0 else 1.0
            canvas_left = float(canvas_info['left'])

            gap_x_logical = None
            if piece_img is not None:
                # 模板匹配（带 alpha 掩码）
                bg_gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)
                if piece_img.ndim == 3 and piece_img.shape[2] == 4:
                    piece_gray = cv2.cvtColor(piece_img[:, :, :3], cv2.COLOR_BGR2GRAY)
                    alpha = piece_img[:, :, 3]
                    result = cv2.matchTemplate(bg_gray, piece_gray, cv2.TM_CCOEFF_NORMED, mask=alpha)
                else:
                    piece_gray = cv2.cvtColor(piece_img, cv2.COLOR_BGR2GRAY) if piece_img.ndim == 3 else piece_img
                    result = cv2.matchTemplate(bg_gray, piece_gray, cv2.TM_CCOEFF_NORMED)

                _, max_val, _, max_loc = cv2.minMaxLoc(result)
                gap_x_logical = max_loc[0]
                print(f"[CAPTCHA] Canvas match score={max_val:.3f}, gap logical x={gap_x_logical}")

                if max_val < 0.3:
                    print("[CAPTCHA] Match score too low, falling back to edge detection on canvas")
                    gap_x_logical = None
            else:
                print(f"[CAPTCHA] Piece image not found: {piece_img_selector}, using edge detection")

            if gap_x_logical is None:
                alt = self._detect_gap_by_edge_canvas(bg_img)
                if alt is not None:
                    gap_x_logical = alt
                else:
                    return None

            gap_x_page = int(canvas_left + gap_x_logical * scale)
            print(f"[CAPTCHA] Gap page X={gap_x_page}px (canvas_left={canvas_left:.0f}, scale={scale:.3f})")
            return gap_x_page

        except Exception as e:
            print(f"[CAPTCHA] Canvas gap detection error: {e}")
            return None

    def _detect_gap_by_edge_canvas(self, bg_img: np.ndarray) -> Optional[int]:
        """边缘检测：在背景图中找缺口列位置（排除最左侧初始拼图区域）。"""
        try:
            gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)
            blurred = cv2.GaussianBlur(gray, (3, 3), 0)
            edges = cv2.Canny(blurred, 50, 150)
            edges[:, :40] = 0  # 排除初始拼图区域
            edges[:, -20:] = 0  # 排除最右侧边框
            col_sums = np.sum(edges, axis=0)
            max_edge = int(np.max(col_sums))
            if max_edge < 200:
                print(f"[CAPTCHA] Edge signal too weak (max={max_edge})")
                return None
            gap_col = int(np.argmax(col_sums))
            print(f"[CAPTCHA] Edge detection gap at x={gap_col}")
            return gap_col
        except Exception as e:
            print(f"[CAPTCHA] Edge detection error: {e}")
            return None

    def _detect_slider_gap(self, bg_selector: Optional[str], piece_selector: Optional[str]) -> Optional[int]:
        """截图背景和滑块拼图，用模板匹配找到缺口的 X 坐标（页面绝对坐标）。"""
        try:
            # 截取整页截图转为 numpy
            screenshot = self.driver.get_screenshot_as_png()
            full_img = cv2.imdecode(np.frombuffer(screenshot, np.uint8), cv2.IMREAD_COLOR)

            # 截取背景图区域
            bg_el = self.driver.find_element(By.CSS_SELECTOR, bg_selector)
            bg_rect = self._get_element_rect(bg_el)
            bg_img = full_img[
                bg_rect["top"]:bg_rect["top"] + bg_rect["height"],
                bg_rect["left"]:bg_rect["left"] + bg_rect["width"]
            ]

            # 如果有拼图片元素，截取它作为模板
            if piece_selector:
                piece_el = self.driver.find_element(By.CSS_SELECTOR, piece_selector)
                piece_rect = self._get_element_rect(piece_el)
                piece_img = full_img[
                    piece_rect["top"]:piece_rect["top"] + piece_rect["height"],
                    piece_rect["left"]:piece_rect["left"] + piece_rect["width"]
                ]
                template = piece_img
            else:
                # 无拼图片元素：用边缘检测直接找缺口
                return self._detect_gap_by_edge(bg_img, bg_rect)

            # 模板匹配找缺口
            bg_gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)
            tpl_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
            tpl_gray = cv2.resize(tpl_gray, (template.shape[1], template.shape[0]))

            result = cv2.matchTemplate(bg_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)
            _, _, _, max_loc = cv2.minMaxLoc(result)
            gap_x_in_bg = max_loc[0]
            return bg_rect["left"] + gap_x_in_bg
        except Exception as e:
            print(f"[CAPTCHA] detect_slider_gap error: {e}")
            return None

    def _detect_gap_by_edge(self, bg_img: np.ndarray, bg_rect: Dict) -> Optional[int]:
        """用边缘检测在背景图上找缺口列位置。"""
        try:
            gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)
            blurred = cv2.GaussianBlur(gray, (3, 3), 0)
            edges = cv2.Canny(blurred, 50, 150)
            # 对每一列求边缘像素数，取最大值所在列
            col_sums = np.sum(edges, axis=0)
            gap_col = int(np.argmax(col_sums))
            return bg_rect["left"] + gap_col
        except Exception as e:
            print(f"[CAPTCHA] detect_gap_by_edge error: {e}")
            return None

    def _get_element_rect(self, element: Any) -> Dict[str, int]:
        """获取元素截图坐标（跨域 iframe 场景下避免读取 window 滚动属性）。"""
        rect = element.rect
        # DataDome 等跨域 iframe 可能禁止访问 pageXOffset/scrollX，
        # 这里仅使用 Selenium 提供的元素坐标并可选乘以 DPR。
        try:
            dpr = self.driver.execute_script("return window.devicePixelRatio;") or 1
        except Exception:
            dpr = 1
        return {
            "left":   int(rect["x"] * dpr),
            "top":    int(rect["y"] * dpr),
            "width":  int(rect["width"] * dpr),
            "height": int(rect["height"] * dpr),
        }

    def _drag_slider(self, slider: Any, distance: int) -> None:
                """模拟人工拖拽：优先用 JS 事件轨迹，失败时回退 ActionChains。"""
                import math

                n_steps = max(40, distance // 3)
                jitter_values = [-2, -1, -1, 0, 0, 0, 1, 1, 2]
                x_points: list[int] = []
                y_points: list[int] = []
                prev_x = 0
                for i in range(1, n_steps + 1):
                        t = i / n_steps
                        eased = (1 - math.cos(math.pi * t)) / 2
                        cur_x = int(distance * eased)
                        if cur_x > prev_x:
                                x_points.append(cur_x)
                                y_points.append(int(np.random.choice(jitter_values)))
                                prev_x = cur_x

                if prev_x < distance:
                        x_points.append(int(distance))
                        y_points.append(0)

                overshoot = int(np.random.uniform(4, 9))
                x_points.append(int(distance + overshoot))
                y_points.append(0)
                x_points.append(int(distance))
                y_points.append(0)

                # 优先使用脚本派发鼠标事件，减少 WebDriver move_by_offset 对特定页面的影响。
                try:
                        self.driver.execute_script(
                                """
                                const slider = arguments[0];
                                const xPoints = arguments[1];
                                const yPoints = arguments[2];
                                const holdMs = arguments[3];

                                if (!slider || !xPoints || !xPoints.length) {
                                    return false;
                                }

                                const center = (el) => {
                                    const r = el.getBoundingClientRect();
                                    return {
                                        x: Math.round(r.left + r.width / 2),
                                        y: Math.round(r.top + r.height / 2),
                                    };
                                };

                                const fire = (type, x, y) => {
                                    const evt = new MouseEvent(type, {
                                        bubbles: true,
                                        cancelable: true,
                                        view: window,
                                        clientX: x,
                                        clientY: y,
                                        screenX: x,
                                        screenY: y,
                                        button: 0,
                                        buttons: type === 'mouseup' ? 0 : 1,
                                    });
                                    slider.dispatchEvent(evt);
                                    document.dispatchEvent(evt);
                                };

                                const start = center(slider);
                                fire('mousemove', start.x, start.y);
                                fire('mousedown', start.x, start.y);

                                const blockUntil = performance.now() + Math.max(0, holdMs);
                                while (performance.now() < blockUntil) {
                                    // busy wait for short human-like hold
                                }

                                for (let i = 0; i < xPoints.length; i += 1) {
                                    fire('mousemove', start.x + xPoints[i], start.y + yPoints[i]);
                                }

                                fire('mouseup', start.x + xPoints[xPoints.length - 1], start.y + yPoints[yPoints.length - 1]);
                                return true;
                                """,
                                slider,
                                x_points,
                                y_points,
                                int(np.random.uniform(350, 650)),
                        )
                        time.sleep(round(np.random.uniform(0.18, 0.35), 3))
                        return
                except Exception:
                        pass

                actions = ActionChains(self.driver)
                actions.click_and_hold(slider)
                actions.pause(round(np.random.uniform(0.35, 0.65), 3))

                current_x = 0
                for idx, target_x in enumerate(x_points):
                        dx = int(target_x) - int(current_x)
                        if dx <= 0:
                                continue
                        jitter_y = int(y_points[idx])
                        actions.move_by_offset(dx, jitter_y)
                        t = min(1.0, max(0.0, float(target_x) / max(1.0, float(distance))))
                        speed = ((1 - math.cos(math.pi * t)) / 2) * (1 - (1 - math.cos(math.pi * t)) / 2) * 4
                        delay = round(np.random.uniform(0.012, 0.025) / (speed + 0.1), 4)
                        actions.pause(min(delay, 0.10))
                        current_x = int(target_x)

                actions.pause(round(np.random.uniform(0.18, 0.35), 3))
                actions.release()
                actions.perform()

    def _wait_login_success(self, login_cfg: Dict[str, Any]) -> None:
        wait_success = login_cfg.get("wait_success", {})
        wait_type = wait_success.get("type")
        timeout = int(wait_success.get("timeout", self.default_timeout))

        try:
            if wait_type == "url_contains":
                keyword = wait_success["value"]
                WebDriverWait(self.driver, timeout).until(EC.url_contains(keyword))
                print("[INFO] Login success by URL condition.")
            elif wait_type == "url_not_contains":
                keyword = wait_success["value"]
                WebDriverWait(self.driver, timeout).until(lambda d: keyword not in d.current_url)
                print("[INFO] Login success by URL not-contains condition.")
            elif wait_type == "element_visible":
                by, selector = wait_success["by"], wait_success["selector"]
                WebDriverWait(self.driver, timeout).until(
                    EC.visibility_of_element_located((self._to_by(by), selector))
                )
                print("[INFO] Login success by element visibility.")
            else:
                sleep_secs = int(login_cfg.get("post_submit_sleep", 3))
                time.sleep(sleep_secs)
                print("[INFO] Login wait fallback sleep done.")
        except TimeoutException as e:
            raise RuntimeError("Login success condition timed out. Please verify selectors/conditions.") from e
        except InvalidSessionIdException as e:
            raise RuntimeError("Browser session ended unexpectedly during login wait.") from e

    def _perform_steps(self, steps: List[Dict[str, Any]]) -> None:
        print(f"[STEPS] Total steps to perform: {len(steps)}")
        captcha_cfg = self._get_auto_captcha_cfg() or {}
        auto_check_captcha = bool(captcha_cfg)
        for i, step in enumerate(steps, start=1):
            next_step = steps[i] if i < len(steps) else None
            runtime_step = dict(step)
            # 动态替换常用字符串字段中的 {{param}} 变量
            for key in ("value", "text", "url"):
                if key in runtime_step and isinstance(runtime_step[key], str):
                    for k, v in self.params.items():
                        runtime_step[key] = runtime_step[key].replace(f"{{{{{k}}}}}", v)
            action = runtime_step.get("action")
            by = runtime_step.get("by", "")
            selector = runtime_step.get("selector", "")
            print(f"[STEP {i}] action={action}, by={by}, selector={selector}")
            try:
                if self._is_fallback_no_data_matched():
                    raise NoDataMatchedStop("fallback.no_data matched before step execution")
                self._ensure_browser_context(context=f"before step {i} ({action})")
                if auto_check_captcha:
                    self._handle_captcha_if_exists(captcha_cfg, context=f"before step {i} ({action})")
                if isinstance(next_step, dict):
                    runtime_step["_next_step"] = next_step
                self._do_action(runtime_step)
                if self._is_fallback_no_data_matched():
                    raise NoDataMatchedStop("fallback.no_data matched after step execution")
                if auto_check_captcha:
                    self._handle_captcha_if_exists(captcha_cfg, context=f"after step {i} ({action})")
                print(f"[STEP {i}] action={action} done")
            except NoDataMatchedStop:
                raise
            except Exception as e:
                print(f"[ERROR] Step {i} failed: action={action}, err={type(e).__name__}: {e}")
                print(f"[ERROR] Step context URL: {self._safe_current_url()}")
                raise

    def _do_action(self, step: Dict[str, Any]) -> None:
        action = step.get("action")
        try:
            if action == "switch_to_new_tab":
                timeout = int(step.get("timeout", self.default_timeout))
                current_handles = set(self._get_window_handles_with_retry())
                start_url = self._safe_current_url()
                target_url_contains = str(step.get("url_contains", "")).strip().lower()
                end_time = time.time() + timeout
                while time.time() < end_time:
                    handles = self._get_window_handles_with_retry(retries=2, delay=0.2)

                    # Prefer a handle whose current URL matches the expected destination.
                    if target_url_contains:
                        for h in handles:
                            try:
                                self.driver.switch_to.window(h)
                                cur = (self._safe_current_url() or "").lower()
                                if target_url_contains in cur:
                                    print(f"[STEP] Switched to tab by url_contains: {self.driver.current_url}")
                                    return
                            except Exception:
                                continue

                    new_handles = [h for h in handles if h not in current_handles]
                    if new_handles:
                        self.driver.switch_to.window(new_handles[-1])
                        print(f"[STEP] Switched to new tab: {self.driver.current_url}")
                        break
                    # Some sites navigate in the same tab instead of opening a new handle.
                    if step.get("allow_same_tab", True):
                        current_url = self._safe_current_url()
                        if current_url and current_url != start_url:
                            print(f"[STEP] No new tab handle; continue on current tab URL: {current_url}")
                            break
                    time.sleep(0.2)
                else:
                    # Fallback: if no strictly new handle appears, switch to last handle.
                    handles = self._get_window_handles_with_retry(retries=2, delay=0.2)
                    if len(handles) > 1:
                        self.driver.switch_to.window(handles[-1])
                        if target_url_contains:
                            cur = (self._safe_current_url() or "").lower()
                            if target_url_contains not in cur:
                                raise TimeoutException(
                                    f"No tab URL matched '{target_url_contains}' within {timeout}s"
                                )
                        print(f"[STEP] Switched to latest tab (fallback): {self.driver.current_url}")
                    elif step.get("allow_same_tab", True):
                        current_url = self._safe_current_url()
                        if current_url and current_url != start_url:
                            print(f"[STEP] Fallback same-tab URL accepted: {current_url}")
                            return
                        raise TimeoutException(f"No new tab opened within {timeout}s and URL unchanged")
                    else:
                        raise TimeoutException(f"No new tab opened within {timeout}s")
            elif action == "switch_to_frame":
                by = step.get("by", "name")
                selector = step.get("selector")
                if by == "name":
                    self.driver.switch_to.frame(selector)
                    print(f"[STEP] Switched to frame by name: {selector}")
                elif by == "id":
                    self.driver.switch_to.frame(self.driver.find_element(By.ID, selector))
                    print(f"[STEP] Switched to frame by id: {selector}")
                elif by == "css":
                    self.driver.switch_to.frame(self.driver.find_element(By.CSS_SELECTOR, selector))
                    print(f"[STEP] Switched to frame by css: {selector}")
                elif by == "xpath":
                    self.driver.switch_to.frame(self.driver.find_element(By.XPATH, selector))
                    print(f"[STEP] Switched to frame by xpath: {selector}")
                else:
                    raise ValueError(f"Unsupported frame switch method: {by}")
            elif action == "click":
                self._click_with_post_verify(step)
            elif action == "click_shadow":
                host_selector = str(step.get("host_selector", "")).strip()
                target_selector = str(step.get("selector", "")).strip()
                if not host_selector or not target_selector:
                    raise ValueError("click_shadow requires host_selector and selector")
                self._click_shadow(host_selector, target_selector, timeout=step.get("timeout"))
            elif action == "click_if_exists":
                by = self._to_by(step["by"])
                selector = step["selector"]
                timeout = float(step.get("timeout", 0))
                max_rounds = max(1, int(step.get("max_rounds", 1)))
                check_next_on_timeout = bool(step.get("check_next_on_timeout", False))
                next_check_timeout = float(step.get("next_check_timeout", 1.5))
                post_click_invisible_by = str(step.get("post_click_invisible_by", step.get("by", ""))).strip()
                post_click_invisible_selector = str(step.get("post_click_invisible_selector", "")).strip()
                post_click_invisible_timeout = float(step.get("post_click_invisible_timeout", 0))
                clicked = False
                total_matches_seen = 0

                for round_idx in range(1, max_rounds + 1):
                    end_time = time.time() + max(0.0, timeout)
                    round_seen = 0
                    while True:
                        self._auto_check_captcha(context=f"during click_if_exists ({selector})")
                        elements = self.driver.find_elements(by, selector)
                        round_seen += len(elements)
                        total_matches_seen += len(elements)
                        for el in elements:
                            try:
                                # Some transient overlays/buttons are visible but not strictly "enabled".
                                # Prefer normal click, then fall back to JS click to improve robustness.
                                if el.is_displayed():
                                    try:
                                        self.driver.execute_script(
                                            "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
                                            el,
                                        )
                                    except Exception:
                                        pass

                                    try:
                                        ActionChains(self.driver).move_to_element(el).pause(0.05).click(el).perform()
                                    except Exception:
                                        self.driver.execute_script("arguments[0].click();", el)

                                    if post_click_invisible_selector:
                                        try:
                                            self._wait_invisible_with_captcha_watch(
                                                post_click_invisible_by or step.get("by", "css"),
                                                post_click_invisible_selector,
                                                timeout=max(0.2, post_click_invisible_timeout),
                                                context=f"click_if_exists post-click invisible ({selector})",
                                            )
                                            print(
                                                f"[STEP] click_if_exists clicked and overlay hidden: {selector}"
                                            )
                                            clicked = True
                                        except Exception:
                                            print(
                                                f"[STEP] click_if_exists clicked but overlay still visible, will retry: {selector}"
                                            )
                                            clicked = False
                                    else:
                                        print(f"[STEP] click_if_exists clicked: {selector}")
                                        clicked = True
                                    break
                            except Exception:
                                continue

                        if clicked:
                            break
                        if time.time() >= end_time:
                            break
                        self._sleep_with_captcha_watch(0.25, context=f"during click_if_exists ({selector})")

                    print(
                        f"[STEP] click_if_exists round {round_idx}/{max_rounds}: "
                        f"matched_elements={round_seen}, clicked={clicked}"
                    )

                    if clicked:
                        break

                    if not check_next_on_timeout:
                        break

                    next_by = step.get("next_by")
                    next_selector = step.get("next_selector")
                    if not next_by or not next_selector:
                        next_step = step.get("_next_step")
                        if isinstance(next_step, dict):
                            next_by = next_step.get("by")
                            next_selector = next_step.get("selector")

                    if next_by and next_selector:
                        try:
                            self._wait_visible_with_captcha_watch(
                                str(next_by),
                                str(next_selector),
                                timeout=next_check_timeout,
                                context=f"click_if_exists next-step check ({selector})",
                            )
                            print(
                                f"[STEP] click_if_exists timeout round {round_idx}/{max_rounds}; "
                                "next step element is already visible, continue"
                            )
                            break
                        except Exception:
                            if round_idx < max_rounds:
                                print(
                                    f"[STEP] click_if_exists timeout round {round_idx}/{max_rounds}; "
                                    "next step not ready, retrying"
                                )
                            continue

                    if round_idx < max_rounds:
                        print(
                            f"[STEP] click_if_exists timeout round {round_idx}/{max_rounds}; "
                            "no next-step check target, retrying"
                        )

                if not clicked:
                    print(f"[STEP] click_if_exists no clickable element for: {selector}")
                print(
                    f"[STEP] click_if_exists result: clicked={clicked}, "
                    f"total_matches_seen={total_matches_seen}, selector={selector}"
                )
            elif action == "type":
                text = step.get("text", "")
                if step.get("env"):
                    text = os.getenv(step["env"], "")
                # 兼容 config 里用 value 字段传递输入内容
                if not text and step.get("value"):
                    text = step["value"]
                print(f"[STEP] type input value: {text}")
                self._type_text(
                    step["by"],
                    step["selector"],
                    text,
                    clear_first=step.get("clear_first", True),
                    timeout=step.get("timeout"),
                )
            elif action == "type_shadow":
                text = step.get("text", "")
                if step.get("env"):
                    text = os.getenv(step["env"], "")
                if not text and step.get("value"):
                    text = step["value"]
                host_selector = str(step.get("host_selector", "")).strip()
                target_selector = str(step.get("selector", "")).strip()
                if not host_selector or not target_selector:
                    raise ValueError("type_shadow requires host_selector and selector")
                print(f"[STEP] type_shadow input value: {text}")
                self._type_text_shadow(
                    host_selector=host_selector,
                    target_selector=target_selector,
                    text=text,
                    clear_first=step.get("clear_first", True),
                    timeout=step.get("timeout"),
                )
            elif action == "press_enter":
                el = self._wait_clickable(step["by"], step["selector"], step.get("timeout"))
                el.send_keys(Keys.ENTER)
            elif action == "press_enter_if_exists":
                by = self._to_by(step["by"])
                selector = step["selector"]
                timeout = float(step.get("timeout", 0))
                end_time = time.time() + max(0.0, timeout)
                sent = False
                while True:
                    elements = self.driver.find_elements(by, selector)
                    for el in elements:
                        try:
                            if el.is_displayed() and el.is_enabled():
                                el.send_keys(Keys.ENTER)
                                print(f"[STEP] press_enter_if_exists sent ENTER: {selector}")
                                sent = True
                                break
                        except Exception:
                            continue
                    if sent:
                        break
                    if time.time() >= end_time:
                        break
                    self._sleep_with_captcha_watch(0.2, context=f"during press_enter_if_exists ({selector})")
                if not sent:
                    print(f"[STEP] press_enter_if_exists no target found: {selector}")
            elif action == "select":
                self._select_option(step)
            elif action == "wait":
                self._wait_visible_with_captcha_watch(
                    step["by"],
                    step["selector"],
                    timeout=step.get("timeout"),
                    context=f"step wait ({step.get('selector', '')})",
                )
            elif action == "wait_present":
                self._wait_present_with_captcha_watch(
                    step["by"],
                    step["selector"],
                    timeout=step.get("timeout"),
                    context=f"step wait_present ({step.get('selector', '')})",
                )
            elif action == "exec_js":
                script = str(step.get("script", ""))
                try:
                    result = self.driver.execute_script(script)
                    print(f"[STEP] exec_js result: {result}")
                except Exception as e:
                    print(f"[STEP] exec_js error: {type(e).__name__}: {e}")
            elif action == "wait_invisible":
                self._wait_invisible_with_captcha_watch(
                    step["by"],
                    step["selector"],
                    timeout=step.get("timeout"),
                    context=f"step wait_invisible ({step.get('selector', '')})",
                )
            elif action == "wait_url_contains":
                timeout = int(step.get("timeout", self.default_timeout))
                self._wait_url_contains_with_captcha_watch(
                    step["value"],
                    timeout=timeout,
                    context=f"step wait_url_contains ({step.get('value', '')})",
                )
                print(f"[STEP] URL now contains: {step['value']}")
            elif action == "log_title":
                try:
                    print(f"[PAGE] title: {self.driver.title}")
                except Exception as e:
                    print(f"[PAGE] title read failed: {type(e).__name__}: {e}")
            elif action == "goto":
                url = step["url"]
                retries = max(1, int(step.get("retries", 1)))
                verify_by = step.get("verify_by")
                verify_selector = step.get("verify_selector")
                verify_timeout = int(step.get("timeout", self.default_timeout))
                after_goto_sleep = float(step.get("after_goto_sleep", 0))
                abort_selector = str(step.get("abort_if_visible_selector", "")).strip()
                abort_text = str(step.get("abort_if_text_contains", "")).strip()
                last_err: Optional[Exception] = None

                for attempt in range(1, retries + 1):
                    try:
                        self._auto_check_captcha(context=f"before goto {url}")
                        self.driver.get(url)
                        self._override_navigator_webdriver()
                        if after_goto_sleep > 0:
                            self._sleep_with_captcha_watch(after_goto_sleep, context=f"after goto {url}")

                        if self._is_abort_page_detected(abort_selector, abort_text):
                            raise RuntimeError(
                                f"FATAL_PAGE_BLOCKED: selector={abort_selector}, text={abort_text}"
                            )

                        if verify_by and verify_selector:
                            self._wait_visible_with_captcha_watch(
                                verify_by,
                                verify_selector,
                                timeout=verify_timeout,
                                context=f"verify goto {url}",
                            )

                        if attempt > 1:
                            print(f"[STEP] Navigated to: {url} (attempt {attempt}/{retries})")
                        else:
                            print(f"[STEP] Navigated to: {url}")
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        err_text = str(e).lower()
                        is_fatal_blocked = "fatal_page_blocked" in err_text
                        is_session_lost = isinstance(e, (NoSuchWindowException, InvalidSessionIdException)) or any(
                            marker in err_text
                            for marker in [
                                "invalid session id",
                                "target window already closed",
                                "web view not found",
                                "no such window",
                            ]
                        )

                        if is_session_lost and attempt < retries:
                            print(
                                f"[STEP] goto session lost on attempt {attempt}/{retries}: "
                                f"{type(e).__name__}. Recovering browser context..."
                            )
                            try:
                                self._ensure_browser_context(context=f"goto retry {attempt}/{retries}")
                            except Exception:
                                self._restart_browser()
                            self._sleep_with_captcha_watch(1, context=f"retry goto {url}")
                            continue

                        if is_fatal_blocked:
                            print(
                                f"[STEP] goto detected blocked page on attempt {attempt}/{retries}: "
                                f"{type(e).__name__}: {e}. Abort retries."
                            )
                            raise

                        if attempt < retries:
                            print(
                                f"[STEP] goto verify failed on attempt {attempt}/{retries}: "
                                f"{type(e).__name__}: {e}. Retrying..."
                            )
                            self._sleep_with_captcha_watch(1, context=f"retry goto {url}")
                            continue
                        raise

                if last_err:
                    raise last_err
            elif action == "scroll":
                y = int(step.get("y", 1000))
                self.driver.execute_script("window.scrollBy(0, arguments[0]);", y)
                print(f"[STEP] Scrolled by y={y}")
            elif action == "sleep":
                secs = float(step.get("seconds", 1))
                print(f"[STEP] Sleeping {secs}s")
                self._sleep_with_captcha_watch(secs, context=f"step sleep {secs}s")
            elif action == "wait_download":
                pattern = step.get("pattern", "*.xlsx")
                timeout = int(step.get("timeout", 30))
                dest = step.get("dest")
                self._wait_for_download(pattern, timeout, dest)
            elif action == "scrape_popup":
                self._scrape_popup_data()
            else:
                raise ValueError(f"Unsupported action: {action}")
        except NoDataMatchedStop:
            raise
        except Exception:
            print(
                f"[ERROR] Action failed: action={action}, by={step.get('by')}, "
                f"selector={step.get('selector')}, url={self._safe_current_url()}"
            )
            raise

    def _get_window_handles_with_retry(self, retries: int = 4, delay: float = 0.25) -> List[str]:
        last_err: Optional[Exception] = None
        total = max(1, retries)
        for idx in range(total):
            try:
                return self.driver.window_handles
            except WebDriverException as e:
                last_err = e
                if idx < total - 1:
                    print(f"[WARN] window_handles read failed (attempt {idx + 1}/{total}), retrying...")
                    time.sleep(delay)
        if last_err:
            raise last_err
        return []

    def _click_with_post_verify(self, step: Dict[str, Any]) -> None:
        by = step["by"]
        selector = step["selector"]
        timeout = step.get("timeout")

        expect_new_tab = bool(step.get("post_click_expect_new_tab", False))
        target_url_contains = str(step.get("post_click_url_contains", "")).strip().lower()
        allow_same_tab = bool(step.get("post_click_allow_same_tab", True))
        verify_timeout = int(step.get("verify_timeout", timeout or self.default_timeout))
        retry_on_verify_fail = int(step.get("retry_on_verify_fail", 0))
        retry_frame_by = str(step.get("retry_frame_by", "")).strip().lower()
        retry_frame_selector = str(step.get("retry_frame_selector", "")).strip()
        retry_close_extra_tabs = bool(step.get("retry_close_extra_tabs", True))

        def _restore_retry_context(base_handle: Optional[str]) -> None:
            if not self.driver:
                return

            if retry_close_extra_tabs:
                try:
                    handles = list(self.driver.window_handles)
                    if base_handle and base_handle in handles:
                        for h in handles:
                            if h == base_handle:
                                continue
                            try:
                                self.driver.switch_to.window(h)
                                self.driver.close()
                            except Exception:
                                continue
                except Exception:
                    pass

            try:
                if base_handle:
                    self.driver.switch_to.window(base_handle)
            except Exception:
                pass

            if retry_frame_by and retry_frame_selector:
                try:
                    self.driver.switch_to.default_content()
                except Exception:
                    pass
                try:
                    if retry_frame_by == "name":
                        self.driver.switch_to.frame(retry_frame_selector)
                    elif retry_frame_by == "id":
                        self.driver.switch_to.frame(self.driver.find_element(By.ID, retry_frame_selector))
                    elif retry_frame_by == "css":
                        self.driver.switch_to.frame(self.driver.find_element(By.CSS_SELECTOR, retry_frame_selector))
                    elif retry_frame_by == "xpath":
                        self.driver.switch_to.frame(self.driver.find_element(By.XPATH, retry_frame_selector))
                    print(
                        f"[STEP] click retry context restored to frame: "
                        f"by={retry_frame_by}, selector={retry_frame_selector}"
                    )
                except Exception as e:
                    print(
                        f"[WARN] click retry frame restore failed: "
                        f"by={retry_frame_by}, selector={retry_frame_selector}, err={type(e).__name__}: {e}"
                    )

        # If no post-click checks are configured, keep the original click behavior.
        if not expect_new_tab and not target_url_contains:
            self._click(by, selector, timeout=timeout)
            return

        attempts = max(1, retry_on_verify_fail + 1)
        for attempt in range(1, attempts + 1):
            base_handles = set(self._get_window_handles_with_retry(retries=2, delay=0.2))
            base_url = self._safe_current_url()
            try:
                base_handle = self.driver.current_window_handle
            except Exception:
                base_handle = None

            self._click(by, selector, timeout=timeout)
            verified = self._wait_click_effect(
                base_handles=base_handles,
                base_url=base_url,
                timeout=verify_timeout,
                expect_new_tab=expect_new_tab,
                target_url_contains=target_url_contains,
                allow_same_tab=allow_same_tab,
            )
            if verified:
                print(f"[STEP] click post-verify passed: by={by}, selector={selector}")
                return

            if attempt < attempts:
                _restore_retry_context(base_handle)
                print(f"[WARN] click post-verify failed (attempt {attempt}/{attempts}), retrying click...")

        raise TimeoutException(
            f"Click post-verify failed after {attempts} attempt(s): by={by}, selector={selector}, "
            f"expect_new_tab={expect_new_tab}, url_contains={target_url_contains or '(none)'}"
        )

    def _wait_click_effect(
        self,
        base_handles: set[str],
        base_url: str,
        timeout: int,
        expect_new_tab: bool,
        target_url_contains: str,
        allow_same_tab: bool,
    ) -> bool:
        end_time = time.time() + timeout
        base_url_lower = (base_url or "").lower()

        while time.time() < end_time:
            handles = self._get_window_handles_with_retry(retries=2, delay=0.15)
            new_handles = [h for h in handles if h not in base_handles]

            if expect_new_tab and new_handles:
                self.driver.switch_to.window(new_handles[-1])
                if target_url_contains:
                    current = (self._safe_current_url() or "").lower()
                    if target_url_contains in current:
                        return True
                else:
                    return True

            if target_url_contains:
                for h in handles:
                    try:
                        self.driver.switch_to.window(h)
                        current = (self._safe_current_url() or "").lower()
                        if target_url_contains not in current:
                            continue
                        if expect_new_tab and not allow_same_tab and h in base_handles:
                            continue
                        return True
                    except Exception:
                        continue

            if allow_same_tab:
                current_url = (self._safe_current_url() or "").lower()
                if target_url_contains and target_url_contains in current_url:
                    return True
                if not target_url_contains and current_url and current_url != base_url_lower:
                    return True

            time.sleep(0.2)
        return False

    def _wait_for_download(self, pattern: str, timeout: int, dest: Optional[str] = None) -> str:
        """Block until a matching file appears in the download dir, then copy to dest."""
        end_time = time.time() + timeout
        download_path = Path(self._download_dir)
        print(
            f"[DOWNLOAD] Waiting for pattern='{pattern}', timeout={timeout}s, "
            f"dir={download_path}, dest={dest or '(none)'}"
        )
        while time.time() < end_time:
            matches = [
                f for f in download_path.glob(pattern)
                if not f.name.endswith(".crdownload") and not f.name.endswith(".tmp")
            ]
            if matches:
                downloaded = max(matches, key=lambda p: p.stat().st_mtime)
                if dest:
                    dest_path = Path(dest)
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(downloaded), str(dest_path))
                    print(f"[INFO] Downloaded file saved to: {dest_path}")
                    return str(dest_path)
                print(f"[INFO] Downloaded file: {downloaded}")
                return str(downloaded)
            time.sleep(0.5)
        raise TimeoutError(
            f"Download timed out after {timeout}s. "
            f"Pattern '{pattern}' not found in {self._download_dir}"
        )

    def _scrape_popup_data(self) -> None:
        """从弹出框爬取指定字段数据，支持单值字段和表格多行字段。"""
        scrape_cfg = self.config.get("scrape", {})
        popup_table = scrape_cfg.get("popup_table")
        popup_fields = scrape_cfg.get("popup_fields", [])
        print(
            f"[SCRAPE] popup mode: table={bool(popup_table)}, "
            f"popup_fields={len(popup_fields)}"
        )

        if popup_table:
            row_by = self._to_by(popup_table["row_by"])
            row_selector = popup_table["row_selector"]
            columns = popup_table.get("columns", [])
            table_selector = popup_table.get("table_selector", ".ant-modal-content table")
            required_fields = popup_table.get("required_fields", [])
            filldown_fields = popup_table.get("filldown_fields", [])
            last_values: Dict[str, str] = {}

            header_index: Dict[str, int] = {}
            try:
                table = self.driver.find_element(By.CSS_SELECTOR, table_selector)
                headers = table.find_elements(By.CSS_SELECTOR, "thead th")
                for idx, header in enumerate(headers):
                    text = header.text.strip()
                    if text:
                        header_index[text] = idx
            except NoSuchElementException:
                header_index = {}

            timeout = int(popup_table.get("timeout", self.default_timeout))
            stable_checks = int(popup_table.get("stable_checks", 3))
            stable_poll = float(popup_table.get("stable_poll", 0.4))
            try:
                WebDriverWait(self.driver, timeout).until(
                    lambda d: len(d.find_elements(row_by, row_selector)) > 0
                )
            except TimeoutException:
                print("[SCRAPE] Popup table rows not ready before timeout")
                return

            if not self._wait_rows_stable(row_by, row_selector, timeout, stable_checks, stable_poll):
                print("[SCRAPE] Popup rows did not stabilize before timeout")
                return

            rows = self.driver.find_elements(row_by, row_selector)
            print(f"[SCRAPE] Popup table rows found: {len(rows)}")

            skipped_required = 0
            for row_element in rows:
                row_data: Dict[str, Any] = {}
                row_cells = row_element.find_elements(By.CSS_SELECTOR, "td")
                for col in columns:
                    name = col["name"]
                    header_name = col.get("header")

                    if header_name and header_name in header_index and row_cells:
                        cell_index = header_index[header_name]
                        row_data[name] = row_cells[cell_index].text.strip() if cell_index < len(row_cells) else ""
                        pattern = col.get("pattern")
                        if pattern and row_data[name] and re.fullmatch(pattern, str(row_data[name])) is None:
                            row_data[name] = ""
                        continue

                    by = self._to_by(col["by"])
                    selector = col["selector"]
                    attr = col.get("attr", "text")
                    try:
                        target = row_element.find_element(by, selector)
                        row_data[name] = target.text.strip() if attr == "text" else target.get_attribute(attr)
                        pattern = col.get("pattern")
                        if pattern and row_data[name] and re.fullmatch(pattern, str(row_data[name])) is None:
                            row_data[name] = ""
                    except NoSuchElementException:
                        row_data[name] = ""

                for field in filldown_fields:
                    current = str(row_data.get(field, "")).strip()
                    if current:
                        last_values[field] = current
                    elif field in last_values:
                        row_data[field] = last_values[field]

                if required_fields and any(not str(row_data.get(f, "")).strip() for f in required_fields):
                    skipped_required += 1
                    continue

                if any(str(v).strip() for v in row_data.values()):
                    self.popup_data.append(row_data)
            print(
                f"[SCRAPE] Popup rows parsed: kept={len(self.popup_data)}, "
                f"skipped_required={skipped_required}"
            )
            return

        if not popup_fields:
            print("[INFO] No popup fields configured for scraping")
            return

        row: Dict[str, Any] = {}
        for field in popup_fields:
            name = field["name"]
            by = self._to_by(field["by"])
            selector = field["selector"]
            attr = field.get("attr", "text")
            try:
                target = self.driver.find_element(by, selector)
                row[name] = target.text.strip() if attr == "text" else target.get_attribute(attr)
                print(f"[SCRAPE] {name}: {row[name]}")
            except NoSuchElementException:
                row[name] = ""
                print(f"[SCRAPE] {name}: (not found)")

        self.popup_data.append(row)
        print(f"[SCRAPE] Popup single-row collected fields={len(row)}")

    def _wait_rows_stable(
        self,
        row_by: str,
        row_selector: str,
        timeout: int,
        stable_checks: int = 2,
        poll: float = 0.4,
    ) -> bool:
        """等待表格行数连续稳定若干次，作为数据渲染完成信号。"""
        start_time = time.time()
        end_time = start_time + timeout
        last_count = -1
        stable_count = 0
        print(
            f"[SCRAPE] Waiting rows stable: selector={row_selector}, timeout={timeout}s, "
            f"stable_checks={stable_checks}, poll={poll}s"
        )

        while time.time() < end_time:
            count = len(self.driver.find_elements(row_by, row_selector))
            if count > 0 and count == last_count:
                stable_count += 1
            else:
                stable_count = 0
                last_count = count

            if count > 0 and stable_count >= stable_checks:
                elapsed = time.time() - start_time
                print(f"[SCRAPE] Rows stabilized at {count} rows (stable_checks={stable_checks}) in {elapsed:.2f}s")
                return True

            time.sleep(poll)

        elapsed = time.time() - start_time
        print(f"[SCRAPE] Rows stability timeout after {elapsed:.2f}s, last_count={last_count}")
        return False

    def _scrape_records(self) -> List[Dict[str, Any]]:
        scrape_cfg = self.config.get("scrape", {})
        if scrape_cfg.get("skip"):
            print("[SCRAPE] Skip enabled by config")
            return []
        if scrape_cfg.get("popup_table"):
            print("[SCRAPE] Popup table mode handled by scrape_popup step, skip list scraping")
            return []
        list_selector = scrape_cfg.get("list_selector")
        fields = scrape_cfg.get("fields", [])
        global_fields = scrape_cfg.get("global_fields", [])
        print(
            f"[SCRAPE] List scraping config: list_selector={list_selector}, "
            f"fields={len(fields)}, global_fields={len(global_fields)}"
        )

        if not list_selector:
            print("[SCRAPE] No list selector, extracting single record from page")
            row = self._extract_fields_from_element(self.driver, fields)
            global_row = self._extract_fields_from_element(self.driver, global_fields) if global_fields else {}
            if global_row:
                row.update(global_row)
            return [row]

        all_records: List[Dict[str, Any]] = []
        max_pages = int(scrape_cfg.get("max_pages", 1))
        next_page = scrape_cfg.get("next_page")

        current_url_before = (self._safe_current_url() or "").lower()
        if self._is_managed_challenge_page() or "__cf_chl" in current_url_before:
            raise ManagedChallengeUnresolved(
                "Managed challenge detected before scraping; cannot determine real no-data state"
            )

        for page in range(1, max_pages + 1):
            page_start_time = time.time()
            try:
                self._wait_visible(scrape_cfg["list_by"], list_selector, timeout=scrape_cfg.get("timeout"))
            except TimeoutException:
                current_url_timeout = (self._safe_current_url() or "").lower()
                if self._is_managed_challenge_page() or "__cf_chl" in current_url_timeout:
                    raise ManagedChallengeUnresolved(
                        "Managed challenge still active at scrape timeout; refuse allow_empty"
                    )
                if scrape_cfg.get("allow_empty", False):
                    raw_no_data_selectors = scrape_cfg.get("no_data_selectors", [])
                    no_data_selectors: List[str] = []
                    if isinstance(raw_no_data_selectors, str):
                        no_data_selectors = [s.strip() for s in raw_no_data_selectors.split(",") if s.strip()]
                    elif isinstance(raw_no_data_selectors, list):
                        no_data_selectors = [str(s).strip() for s in raw_no_data_selectors if str(s).strip()]

                    matched_selector = ""
                    for sel in no_data_selectors:
                        try:
                            elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                            for el in elements:
                                if not el.is_displayed():
                                    continue
                                element_text = (el.text or "").strip()
                                if not element_text:
                                    continue
                                matched_selector = sel
                                break
                            if matched_selector:
                                break
                        except Exception:
                            continue

                    if matched_selector:
                        print(f"[SCRAPE] No-data selector matched: {matched_selector}")
                    print("[SCRAPE] No list rows found within timeout; allow_empty=true, returning empty records.")
                    return all_records
                raise
            items = self.driver.find_elements(self._to_by(scrape_cfg["list_by"]), list_selector)
            extract_time_total = 0.0
            global_row = self._extract_fields_from_element(self.driver, global_fields) if global_fields else {}
            print(f"[INFO] Page {page}, found {len(items)} items")

            # Fast JS batch extract to avoid per-cell WebDriver calls when configured.
            use_fast = bool(scrape_cfg.get("fast_extract_enabled", True))
            threshold = int(scrape_cfg.get("fast_extract_threshold", 8) or 8)
            list_by = str(scrape_cfg.get("list_by", "css")).lower()

            if use_fast and list_by == "css" and len(items) >= threshold:
                t0 = time.time()
                rows = self._fast_extract(list_selector, fields)
                extract_time_total = time.time() - t0
                if rows:
                    for row in rows:
                        if global_row:
                            row.update(global_row)
                        all_records.append(row)
                else:
                    print("[FAST_EXTRACT] empty result, falling back to per-item extraction")
                    for item in items:
                        t0 = time.time()
                        row = self._extract_fields_from_element(item, fields)
                        extract_time_total += (time.time() - t0)
                        if global_row:
                            row.update(global_row)
                        all_records.append(row)
            else:
                for item in items:
                    t0 = time.time()
                    row = self._extract_fields_from_element(item, fields)
                    extract_time_total += (time.time() - t0)
                    if global_row:
                        row.update(global_row)
                    all_records.append(row)

            page_elapsed = time.time() - page_start_time
            avg_extract = (extract_time_total / len(items)) if items else 0.0
            print(f"[INFO] Page {page} timing: page_elapsed={page_elapsed:.2f}s, extract_total={extract_time_total:.2f}s, avg_per_item={avg_extract:.3f}s")

            if not next_page:
                break
            if page == max_pages:
                break

            if not self._click_next_page(next_page):
                break

        print(f"[INFO] Scrape done, total records: {len(all_records)}")
        return all_records

    def _extract_fields_from_element(self, element: Any, fields: List[Dict[str, str]]) -> Dict[str, Any]:
        row: Dict[str, Any] = {}
        for field in fields:
            name = field["name"]
            by = self._to_by(field["by"])
            selector = field["selector"]
            attr = field.get("attr", "text")
            try:
                target = element.find_element(by, selector)
                value = target.text.strip() if attr == "text" else target.get_attribute(attr)
                value = "" if value is None else str(value).strip()

                # Optional regex extraction for site-specific cleanup (e.g., strip status labels before datetime text).
                extract_regex = str(field.get("extract_regex", "")).strip()
                if extract_regex and value:
                    try:
                        match = re.search(extract_regex, value)
                    except re.error:
                        match = None
                    if match:
                        if match.lastindex:
                            group_index_raw = field.get("extract_group", 1)
                            try:
                                group_index = int(group_index_raw)
                            except Exception:
                                group_index = 1
                            if group_index < 1 or group_index > match.lastindex:
                                group_index = 1
                            value = match.group(group_index).strip()
                        else:
                            value = match.group(0).strip()
                    else:
                        value = ""

                row[name] = value
            except NoSuchElementException:
                row[name] = ""
        return row

    def _fast_extract(self, list_selector: str, fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Use a single execute_script call to extract all rows/fields in-page.

        This avoids repeated WebDriver round-trips for each cell.
        Only supports CSS `list_selector` and CSS-relative `field['selector']`.
        """
        if not self.driver:
            return []
        try:
            # Use execute_async_script to wait briefly for row rendering/content.
            script = (
                "var sel=arguments[0], fields=arguments[1]||[], timeout=arguments[2]||1500, poll=arguments[3]||100;"
                "var cb=arguments[arguments.length-1];"
                "var end=Date.now()+timeout;"
                "function extract(){"
                "  try{"
                "    var rows = Array.prototype.slice.call(document.querySelectorAll(sel)||[]);"
                "    var res = rows.map(function(tr){"
                "      var obj={};"
                "      fields.forEach(function(field){"
                "        var name = field && field.name ? field.name : '';"
                "        var selector = field && field.selector ? field.selector : '';"
                "        var attr = field && field.attr ? field.attr : 'text';"
                "        var val = '';"
                "        try{"
                "          if(selector){ var el = tr.querySelector(selector); if(el){ val = (attr==='text') ? (el.innerText||'') : (el.getAttribute(attr)||''); } } else { val = tr.innerText||''; }"
                "        }catch(e){ val=''; }"
                "        obj[name] = (val===null||val===undefined)?'':String(val).trim();"
                "      });"
                "      return obj;"
                "    });"
                "    var any=false; for(var i=0;i<res.length;i++){ for(var k in res[i]){ if(res[i][k]){ any=true; break;} } if(any) break; }"
                "    if(res.length>0 && any){ cb(res); return; }"
                "  }catch(e){}"
                "  if(Date.now()<end){ setTimeout(extract,poll); return; } cb([]);"
                "}; extract();"
            )

            # Default timeout 1500ms, poll every 100ms
            result = self.driver.execute_async_script(script, list_selector, fields or [], 1500, 100)
            if not isinstance(result, list):
                return []
            cleaned: List[Dict[str, Any]] = []
            for r in result:
                if not isinstance(r, dict):
                    continue
                cleaned.append({k: ('' if v is None else str(v).strip()) for k, v in r.items()})
            return cleaned
        except Exception as e:
            print(f"[FAST_EXTRACT] JS extract failed: {type(e).__name__}: {e}")
            return []

    def _click_next_page(self, next_page: Dict[str, Any]) -> bool:
        try:
            btn = self._wait_clickable(next_page["by"], next_page["selector"], next_page.get("timeout"))
            self.driver.execute_script("arguments[0].click();", btn)
            delay = float(next_page.get("after_click_sleep", 1.5))
            time.sleep(delay)
            print(f"[PAGE] Clicked next page, slept {delay}s")
            return True
        except TimeoutException:
            print("[INFO] Next page button not found/clickable. Stop paging.")
            return False
        except (InvalidSessionIdException, NoSuchWindowException) as e:
            print(f"[INFO] Pagination stopped due to browser/session disconnect: {type(e).__name__}")
            return False
        except Exception as e:
            print(f"[ERROR] Next page failed: {type(e).__name__}: {e}")
            print(f"[ERROR] URL when paging failed: {self._safe_current_url()}")
            raise

    def _save_records(self, records: List[Dict[str, Any]]) -> None:
        output_cfg = self.config.get("output", {})
        fmt = output_cfg.get("format", "json").lower()
        print(f"[OUTPUT] Saving records: format={fmt}")

        # "file" mode means the file was already saved by wait_download action
        if fmt == "file":
            print(f"[INFO] Output file: {output_cfg.get('path', '(see download dir)')}")
            return

        output_path = Path(output_cfg.get("path", "output/data.json"))
        if fmt == "json":
            output_path = self._append_timestamp_to_path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._cleanup_output_dir(output_path.parent)

        # 优先使用 popup_data（从弹出框爬取的数据）
        data_to_save = self.popup_data if self.popup_data else records
        data_to_save = self._attach_request_identifiers(data_to_save)
        source = "popup_data" if self.popup_data else "records"

        if bool(self.config.get("__defer_output_save", False)):
            deferred = self.config.setdefault("__deferred_output_items", [])
            if isinstance(deferred, list):
                deferred.append({
                    "output_cfg": dict(output_cfg) if isinstance(output_cfg, dict) else {},
                    "records": data_to_save,
                })
            print(f"[OUTPUT] Deferred save enabled, queued records only: source={source}, count={len(data_to_save)}")
            return

        print(f"[OUTPUT] Data source={source}, count={len(data_to_save)}, path={output_path}")

        if fmt == "txt":
            self._save_txt(output_path, data_to_save)
        elif fmt == "csv":
            self._save_csv(output_path, data_to_save)
        else:
            json_structure = str(output_cfg.get("json_structure", "")).strip().lower()
            payload: Any = data_to_save
            if json_structure == "globals_plus_records":
                payload = self._build_globals_plus_records_payload(data_to_save)
            with output_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

        print(f"[INFO] Saved {len(data_to_save)} records to {output_path}")

    def _attach_request_identifiers(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Attach request identifiers (MAWB/ContainerNo/HAWNO) to each saved record when available."""
        if not records:
            return records

        tracked_keys = ("MAWB", "ContainerNo", "HAWNO")
        enriched: List[Dict[str, Any]] = []
        for item in records:
            row = dict(item) if isinstance(item, dict) else {"value": item}
            for key in tracked_keys:
                value = self.params.get(key)
                if value and key not in row:
                    row[key] = value
            enriched.append(row)
        return enriched

    def _append_timestamp_to_path(self, path: Path) -> Path:
        """Append a timestamp so each JSON run writes to a unique file."""
        # Extract base name before the date pattern to replace timestamp instead of appending
        stem_prefix = path.stem
        match = re.match(r"^(.*)_\d{8}(?:_\d{6}(?:_\d{6})?)$", path.stem)
        if match:
            stem_prefix = match.group(1)
        ts = time.strftime("%Y%m%d_%H%M%S")
        return path.with_name(f"{stem_prefix}_{ts}{path.suffix}")

    def _build_globals_plus_records_payload(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Promote global values to top-level and keep detail rows under records."""
        output_cfg = self.config.get("output", {})
        configured_keys = output_cfg.get("global_identifier_keys", ["ContainerNo", "MAWB", "HAWNO"])
        if isinstance(configured_keys, list):
            tracked_keys = tuple(str(k).strip() for k in configured_keys if str(k).strip())
        else:
            tracked_keys = ("ContainerNo", "MAWB", "HAWNO")
        scrape_cfg = self.config.get("scrape", {}) or {}
        global_fields = scrape_cfg.get("global_fields", [])
        global_field_names = [
            str(field.get("name", "")).strip()
            for field in global_fields
            if isinstance(field, dict) and str(field.get("name", "")).strip()
        ]

        globals_payload: Dict[str, Any] = {}

        # Prefer runtime params; for ENX HAWNO input, mirror it to ContainerNo when missing.
        for key in tracked_keys:
            value = self.params.get(key)
            if not value and key == "ContainerNo":
                value = self.params.get("HAWNO")
            if value:
                globals_payload[key] = value

        # Pull page-level global fields from records (first non-empty wins).
        for name in global_field_names:
            selected = ""
            for row in records:
                if not isinstance(row, dict):
                    continue
                raw = row.get(name)
                if raw is not None and str(raw).strip():
                    selected = str(raw).strip()
                    break
            if not selected and records and isinstance(records[0], dict) and name in records[0]:
                raw = records[0].get(name)
                selected = "" if raw is None else str(raw).strip()
            globals_payload[name] = selected

        remove_keys = set(tracked_keys) | set(global_field_names)
        remove_keys.add("HAWNO")
        details: List[Dict[str, Any]] = []
        for row in records:
            if not isinstance(row, dict):
                details.append({"value": row})
                continue
            details.append({k: v for k, v in row.items() if k not in remove_keys})

        payload: Dict[str, Any] = dict(globals_payload)
        payload["records"] = details
        return payload

    def _save_txt(self, output_path: Path, records: List[Dict[str, Any]]) -> None:
        """保存为文本格式，每条记录一行"""
        lines = []
        for record in records:
            line_parts = [f"{k}: {v}" for k, v in record.items()]
            lines.append(" | ".join(line_parts))

        content = "\n".join(lines)
        output_path.write_text(content, encoding="utf-8-sig")

    def _save_csv(self, output_path: Path, records: List[Dict[str, Any]]) -> None:
        if not records:
            output_path.write_text("", encoding="utf-8")
            return

        fieldnames = list(records[0].keys())
        with output_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(records)

    def _to_by(self, by_name: str) -> str:
        key = by_name.lower()
        if key not in BY_MAP:
            raise ValueError(f"Unsupported by type: {by_name}")
        return BY_MAP[key]

    def _wait_visible(self, by: str, selector: str, timeout: Optional[int] = None) -> Any:
        timeout = int(timeout or self.default_timeout)
        try:
            return WebDriverWait(self.driver, timeout).until(
                EC.visibility_of_element_located((self._to_by(by), selector))
            )
        except TimeoutException as e:
            print(f"[WAIT] visible timeout: by={by}, selector={selector}, timeout={timeout}s")
            print(f"[WAIT] URL={self._safe_current_url()}")
            raise e

    def _wait_clickable(self, by: str, selector: str, timeout: Optional[int] = None) -> Any:
        timeout = int(timeout or self.default_timeout)
        try:
            return WebDriverWait(self.driver, timeout).until(
                EC.element_to_be_clickable((self._to_by(by), selector))
            )
        except TimeoutException as e:
            print(f"[WAIT] clickable timeout: by={by}, selector={selector}, timeout={timeout}s")
            print(f"[WAIT] URL={self._safe_current_url()}")
            raise e

    def _wait_invisible(self, by: str, selector: str, timeout: Optional[int] = None) -> bool:
        timeout = int(timeout or self.default_timeout)
        try:
            return WebDriverWait(self.driver, timeout).until(
                EC.invisibility_of_element_located((self._to_by(by), selector))
            )
        except TimeoutException as e:
            print(f"[WAIT] invisible timeout: by={by}, selector={selector}, timeout={timeout}s")
            print(f"[WAIT] URL={self._safe_current_url()}")
            raise e

    def _click(self, by: str, selector: str, timeout: Optional[int] = None) -> None:
        element = self._wait_clickable(by, selector, timeout)
        try:
            # 优先用 ActionChains 点击，产生 isTrusted=true 的原生鼠标事件
            ActionChains(self.driver).move_to_element(element).click().perform()
            print(f"[ACTION] click success via ActionChains: by={by}, selector={selector}")
        except Exception:
            # 元素不在视口等情况回退到 JS click
            self.driver.execute_script("arguments[0].click();", element)
            print(f"[ACTION] click fallback via JS: by={by}, selector={selector}")

    def _select_option(self, step: Dict[str, Any]) -> None:
        element = self._wait_visible(step["by"], step["selector"], timeout=step.get("timeout"))
        select = Select(element)

        if "text" in step:
            select.select_by_visible_text(step["text"])
            return
        if "value" in step:
            select.select_by_value(step["value"])
            return
        if "index" in step:
            select.select_by_index(int(step["index"]))
            return

        raise ValueError("Select action requires one of: text, value, index")

    def _type_text(
        self,
        by: str,
        selector: str,
        text: str,
        clear_first: bool = True,
        timeout: Optional[int] = None,
    ) -> None:
        element = self._wait_visible(by, selector, timeout)
        # 用 ActionChains 点击聚焦，再 send_keys 输入，产生 isTrusted=true 的原生事件
        # JS dispatchEvent 产生 isTrusted=false，会被风控检测到
        actions = ActionChains(self.driver)
        actions.move_to_element(element).click().perform()
        if clear_first:
            try:
                # For token/chip inputs, clear() alone may not remove existing chips.
                element.send_keys(Keys.CONTROL, "a")
                element.send_keys(Keys.BACKSPACE)
                element.send_keys(Keys.DELETE)
            except Exception:
                pass
            try:
                element.clear()
            except Exception:
                pass
        element.send_keys(text)

        actual = (element.get_attribute("value") or "")
        expected = "" if text is None else str(text)
        if actual != expected:
            # Some sites bind key events twice under automation and duplicate characters.
            self.driver.execute_script(
                """
                const el = arguments[0];
                const val = arguments[1];
                el.focus();
                el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                """,
                element,
                expected,
            )
            corrected = (element.get_attribute("value") or "")
            print(
                f"[ACTION] type value corrected: by={by}, selector={selector}, "
                f"before='{actual}', after='{corrected}'"
            )

    def _wait_shadow_element(
        self,
        host_selector: str,
        target_selector: str,
        timeout: Optional[int] = None,
        require_clickable: bool = False,
    ) -> Any:
        timeout = int(timeout or self.default_timeout)
        end_time = time.time() + timeout
        poll = 0.25
        last_state = ""

        while time.time() < end_time:
            try:
                host = self.driver.find_element(By.CSS_SELECTOR, host_selector)
            except Exception:
                host = None

            if host is not None:
                try:
                    element = self.driver.execute_script(
                        """
                        const host = arguments[0];
                        const selector = arguments[1];
                        if (!host || !host.shadowRoot) return null;
                        return host.shadowRoot.querySelector(selector);
                        """,
                        host,
                        target_selector,
                    )
                except Exception:
                    element = None

                if element is not None:
                    try:
                        visible = bool(element.is_displayed())
                    except Exception:
                        visible = False
                    if require_clickable:
                        try:
                            clickable = visible and bool(element.is_enabled())
                        except Exception:
                            clickable = False
                        if clickable:
                            return element
                    elif visible:
                        return element
                    last_state = "found_not_interactable"
                else:
                    last_state = "target_not_found"
            else:
                last_state = "host_not_found"

            time.sleep(poll)

        print(
            f"[WAIT] shadow timeout: host={host_selector}, selector={target_selector}, "
            f"timeout={timeout}s, state={last_state}"
        )
        print(f"[WAIT] URL={self._safe_current_url()}")
        raise TimeoutException(
            f"Shadow element timeout: host={host_selector}, selector={target_selector}"
        )

    def _click_shadow(self, host_selector: str, target_selector: str, timeout: Optional[int] = None) -> None:
        element = self._wait_shadow_element(
            host_selector,
            target_selector,
            timeout=timeout,
            require_clickable=True,
        )
        try:
            ActionChains(self.driver).move_to_element(element).click().perform()
            print(
                f"[ACTION] click_shadow success via ActionChains: "
                f"host={host_selector}, selector={target_selector}"
            )
        except Exception:
            self.driver.execute_script("arguments[0].click();", element)
            print(
                f"[ACTION] click_shadow fallback via JS: "
                f"host={host_selector}, selector={target_selector}"
            )

    def _type_text_shadow(
        self,
        host_selector: str,
        target_selector: str,
        text: str,
        clear_first: bool = True,
        timeout: Optional[int] = None,
    ) -> None:
        element = self._wait_shadow_element(
            host_selector,
            target_selector,
            timeout=timeout,
            require_clickable=False,
        )
        actions = ActionChains(self.driver)
        actions.move_to_element(element).click().perform()
        if clear_first:
            try:
                element.send_keys(Keys.CONTROL, "a")
                element.send_keys(Keys.BACKSPACE)
                element.send_keys(Keys.DELETE)
            except Exception:
                pass
            try:
                element.clear()
            except Exception:
                pass
        element.send_keys(text)

        actual = (element.get_attribute("value") or "")
        expected = "" if text is None else str(text)
        if actual != expected:
            self.driver.execute_script(
                """
                const el = arguments[0];
                const val = arguments[1];
                el.focus();
                el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                """,
                element,
                expected,
            )
            corrected = (element.get_attribute("value") or "")
            print(
                f"[ACTION] type_shadow value corrected: host={host_selector}, selector={target_selector}, "
                f"before='{actual}', after='{corrected}'"
            )
