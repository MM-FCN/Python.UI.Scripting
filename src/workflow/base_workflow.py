# -*- coding: utf-8 -*-
import base64
import csv
import io
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# import cv2
cv2 = None

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


class WorkflowCrawler:
    def __init__(self, config: Dict[str, Any], headless: bool = False, params: Dict[str, str] = None) -> None:
        self.config = config
        self.headless = headless
        self.params = params or {}
        self.driver: Optional[webdriver.Remote] = None
        self._browser_name: str = str(config.get("browser", "firefox")).strip().lower()
        self._attached_existing_browser: bool = False
        self.default_timeout = int(config.get("default_timeout", 15))
        self._download_dir: str = str(
            Path(config.get("download_dir", "output/downloads")).resolve()
        )
        self.popup_data: List[Dict[str, Any]] = []  # 存储从弹出框爬取的数据
        self._current_task_name: str = ""

    def _opencv_available(self) -> bool:
        if cv2 is not None:
            return True
        print("[CAPTCHA] OpenCV is not installed; image-based captcha detection is disabled.")
        return False

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
            print("[STEP 3] Executing login...")
            self._login()

            tasks = self.config.get("tasks")
            if tasks:
                print(f"[STEP 4] Running {len(tasks)} task(s) after login...")
                records = self._run_tasks(tasks)
            else:
                print("[STEP 4] Performing navigation steps...")
                self._perform_steps(self.config.get("navigation_steps", []))
                print("[STEP 5] Scraping records...")
                records = self._scrape_records()
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

    def _run_tasks(self, tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """在同一登录会话中顺序执行多个页面任务。"""
        all_records: List[Dict[str, Any]] = []
        base_scrape = self.config.get("scrape", {})
        base_output = self.config.get("output", {})

        for idx, task in enumerate(tasks, start=1):
            task_name = task.get("name", f"task_{idx}")
            self._current_task_name = task_name
            print(f"[TASK {idx}] {task_name} started")

            self.popup_data = []

            self.config["scrape"] = task.get("scrape", base_scrape)
            self.config["output"] = task.get("output", base_output)

            nav_steps = task.get("navigation_steps", self.config.get("navigation_steps", []))
            print(f"[TASK {idx}] navigation steps: {len(nav_steps)}")
            try:
                self._perform_steps(nav_steps)

                records = self._scrape_records()
                self._save_records(records)

                task_records = self.popup_data if self.popup_data else records
                all_records.extend(task_records)
                print(
                    f"[TASK {idx}] {task_name} completed, records={len(task_records)} "
                    f"(popup_records={len(self.popup_data)}, list_records={len(records)})"
                )
            except Exception as e:
                print(f"[ERROR] Task '{task_name}' failed: {type(e).__name__}: {e}")
                print(f"[ERROR] URL when task failed: {self._safe_current_url()}")
                raise

        self._current_task_name = ""

        self.config["scrape"] = base_scrape
        self.config["output"] = base_output
        print(f"[TASK] All tasks finished, total aggregated records={len(all_records)}")
        return all_records

    def _start_browser(self) -> None:
        browser = str(self.config.get("browser", "firefox")).strip().lower()
        self._browser_name = browser
        page_load_strategy = str(self.config.get("page_load_strategy", "eager")).strip().lower()
        if page_load_strategy not in {"normal", "eager", "none"}:
            page_load_strategy = "eager"

        print(f"[BROWSER] Creating {browser} options...")
        print(f"[BROWSER] Setting download directory: {self._download_dir}")
        Path(self._download_dir).mkdir(parents=True, exist_ok=True)

        try:
            if browser == "edge":
                options = EdgeOptions()
                options.use_chromium = True
                options.page_load_strategy = page_load_strategy
                edge_cfg = self.config.get("edge", {})
                if not isinstance(edge_cfg, dict):
                    edge_cfg = {}
                edge_stealth_mode = bool(edge_cfg.get("stealth_mode", True))
                attach_existing = bool(
                    edge_cfg.get("attach_existing", self.config.get("edge_attach_existing", False))
                )
                debugger_address = str(
                    edge_cfg.get("debugger_address", self.config.get("edge_debugger_address", ""))
                ).strip()
                edge_user_data_dir = str(
                    edge_cfg.get("user_data_dir", self.config.get("edge_user_data_dir", ""))
                ).strip()
                if edge_user_data_dir:
                    edge_user_data_dir = os.path.expanduser(os.path.expandvars(edge_user_data_dir))
                edge_extra_args = edge_cfg.get("extra_args", [])
                if not isinstance(edge_extra_args, list):
                    edge_extra_args = []
                if self.headless:
                    options.add_argument("--headless=new")
                    options.add_argument("--window-size=1600,1000")
                else:
                    options.add_argument("--start-maximized")

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

                if edge_user_data_dir and not attach_existing:
                    try:
                        Path(edge_user_data_dir).mkdir(parents=True, exist_ok=True)
                    except Exception:
                        pass
                    options.add_argument(f"--user-data-dir={edge_user_data_dir}")
                    print(f"[BROWSER] Edge direct mode using user data dir: {edge_user_data_dir}")

                for arg in edge_extra_args:
                    arg_str = str(arg).strip()
                    if arg_str:
                        options.add_argument(arg_str)

                options.add_experimental_option("prefs", {
                    "download.default_directory": self._download_dir,
                    "download.prompt_for_download": False,
                    "download.directory_upgrade": True,
                    "safebrowsing.enabled": True,
                    "credentials_enable_service": False,
                    "profile.password_manager_enabled": False,
                })
                if attach_existing:
                    if not debugger_address:
                        raise ValueError(
                            "Edge attach mode requires debugger address via edge.debugger_address or edge_debugger_address"
                        )
                    options.add_experimental_option("debuggerAddress", debugger_address)
                    print(f"[BROWSER] Edge attach mode enabled: debuggerAddress={debugger_address}")
                self._attached_existing_browser = attach_existing

                print("[BROWSER] Resolving msedgedriver...")
                edge_exe = self._resolve_edgedriver_path()
                service = EdgeService(edge_exe)
                self.driver = webdriver.Edge(service=service, options=options)
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
                options.set_preference("browser.download.dir", self._download_dir)
                options.set_preference("browser.download.useDownloadDir", True)
                options.set_preference("browser.download.manager.showWhenStarting", False)
                options.set_preference(
                    "browser.helperApps.neverAsk.saveToDisk",
                    "application/octet-stream,application/pdf,text/csv,application/zip",
                )
                options.set_preference("pdfjs.disabled", True)
                options.set_preference("dom.webdriver.enabled", False)

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
        try:
            # Use blank page as bootstrap context so we can inject before target-site navigation.
            self.driver.get("about:blank")
        except Exception as e:
            print(f"[BROWSER] about:blank bootstrap skipped: {type(e).__name__}: {e}")
        self._override_navigator_webdriver()

    def _install_cdp_stealth_script(self) -> None:
        """Install CDP script so stealth overrides run before every new document."""
        if not self.driver:
            return
        script = """
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
                configurable: true
            });

            window.chrome = window.chrome || { runtime: {} };

            Object.defineProperty(navigator, 'plugins', {
                get: () => [
                    { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                    { name: 'Chromium PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                    { name: 'Microsoft Edge PDF Viewer', filename: 'internal-edge-pdf-viewer', description: '' }
                ],
                configurable: true
            });

            Object.defineProperty(navigator, 'languages', {
                get: () => ['zh-CN', 'en-US', 'ja-JP'],
                configurable: true
            });
        """
        try:
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": script},
            )
            print("[BROWSER] CDP stealth script installed.")
        except Exception as e:
            print(f"[BROWSER] CDP stealth install skipped: {type(e).__name__}: {e}")

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
                    get: () => ['zh-CN', 'zh', 'en-US', 'en'],
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

    def _navigate_to_base_url(self) -> None:
        """Navigate to base URL with retry for transient Firefox/geckodriver session loss."""
        url = self.config["base_url"]
        retry_count = int(self.config.get("startup_nav_retry", 1))
        max_attempts = max(1, retry_count + 1)
        page_load_timeout = int(self.config.get("page_load_timeout", 60))

        last_error: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                if not self.driver:
                    self._start_browser()
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

                target_prefix = url.split("#", 1)[0]
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
                    self.driver.switch_to.frame(frame)
                    if not probe_selectors:
                        print(f"[CAPTCHA] Switched to iframe via selector: {sel}")
                        return True
                    for p in probe_selectors:
                        if self.driver.find_elements(By.CSS_SELECTOR, p):
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

                clicked = False
                for sel in checkbox_selectors:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    for el in elements:
                        try:
                            if el.is_displayed() and el.is_enabled():
                                self.driver.execute_script("arguments[0].click();", el)
                                print(f"[CAPTCHA] Checkbox clicked via selector: {sel}")
                                clicked = True
                                break
                        except Exception:
                            continue
                    if clicked:
                        break

                if not clicked:
                    print(f"[CAPTCHA] Checkbox not found/clickable (attempt {attempt}/{max_retries})")
                    time.sleep(0.8)
                    continue

                time.sleep(1.2)

                # If a success marker is configured, wait briefly for confirmation.
                if success_selectors:
                    for success_sel in success_selectors:
                        try:
                            self._wait_visible("css", success_sel, timeout=3)
                            print(f"[CAPTCHA] Checkbox verification success marker found: {success_sel}")
                            return
                        except Exception:
                            continue

                # No explicit success marker configured/found; click was issued, continue workflow.
                return
            except Exception as e:
                print(f"[CAPTCHA] Checkbox solve attempt {attempt} error: {type(e).__name__}: {e}")
                time.sleep(0.8)

        print("[CAPTCHA] Checkbox captcha not solved after retries; continue workflow.")

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
                print(f"[CAPTCHA] Detected slider captcha{marker}, handling now...")
                self._handle_captcha(cfg)
                return True
            return False
        finally:
            if in_iframe:
                try:
                    self.driver.switch_to.default_content()
                except Exception:
                    pass

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
        if not self._opencv_available():
            return None
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
        if not self._opencv_available():
            return None
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
        if not self._opencv_available():
            return None
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
        if not self._opencv_available():
            return None
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
        """模拟人工拖拽：正弦 ease-in-out 加速曲线 + 随机抖动，更接近真人行为。"""
        import math
        actions = ActionChains(self.driver)
        actions.click_and_hold(slider)
        # 按下后先停顿更长时间，模拟真人「握住」
        actions.pause(round(np.random.uniform(0.35, 0.65), 3))

        # 生成正弦 ease-in-out 轨迹，步数更多使轨迹更细腻
        n_steps = max(40, distance // 3)
        prev_x = 0
        for i in range(1, n_steps + 1):
            t = i / n_steps
            # ease-in-out sine: 慢 → 快 → 慢
            eased = (1 - math.cos(math.pi * t)) / 2
            cur_x = int(distance * eased)
            dx = cur_x - prev_x
            if dx > 0:
                # Y 轴抖动范围扩大，更像真人手颤
                jitter_y = int(np.random.choice([-2, -1, -1, 0, 0, 0, 1, 1, 2]))
                actions.move_by_offset(dx, jitter_y)
                # 整体速度降低：delay 基数从 0.006~0.015 提高到 0.012~0.025
                speed = eased * (1 - eased) * 4
                delay = round(np.random.uniform(0.012, 0.025) / (speed + 0.1), 4)
                actions.pause(min(delay, 0.10))
                prev_x = cur_x

        # 确保到达目标（补足误差）
        if prev_x < distance:
            actions.move_by_offset(distance - prev_x, 0)
            actions.pause(0.04)

        # 轻微超冲再回正，模拟真人校准
        overshoot = int(np.random.uniform(4, 9))
        actions.move_by_offset(overshoot, 0)
        actions.pause(round(np.random.uniform(0.15, 0.28), 3))
        actions.move_by_offset(-overshoot, 0)
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
        captcha_cfg = self.config.get("login", {}).get("captcha", {})
        auto_check_captcha = bool(captcha_cfg.get("auto_check_during_steps", False)) if isinstance(captcha_cfg, dict) else False
        for i, step in enumerate(steps, start=1):
            # 动态替换所有 {{param}} 变量
            if "value" in step and isinstance(step["value"], str):
                for k, v in self.params.items():
                    step["value"] = step["value"].replace(f"{{{{{k}}}}}", v)
            action = step.get("action")
            by = step.get("by", "")
            selector = step.get("selector", "")
            print(f"[STEP {i}] action={action}, by={by}, selector={selector}")
            try:
                self._ensure_browser_context(context=f"before step {i} ({action})")
                if auto_check_captcha:
                    self._handle_captcha_if_exists(captcha_cfg, context=f"before step {i} ({action})")
                self._do_action(step)
                print(f"[STEP {i}] action={action} done")
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
            elif action == "click_if_exists":
                by = self._to_by(step["by"])
                selector = step["selector"]
                timeout = float(step.get("timeout", 0))
                end_time = time.time() + max(0.0, timeout)
                clicked = False

                while True:
                    elements = self.driver.find_elements(by, selector)
                    for el in elements:
                        try:
                            if el.is_displayed() and el.is_enabled():
                                self.driver.execute_script("arguments[0].click();", el)
                                print(f"[STEP] click_if_exists clicked: {selector}")
                                clicked = True
                                break
                        except Exception:
                            continue

                    if clicked:
                        break
                    if time.time() >= end_time:
                        break
                    time.sleep(0.25)

                if not clicked:
                    print(f"[STEP] click_if_exists no clickable element for: {selector}")
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
            elif action == "press_enter":
                el = self._wait_clickable(step["by"], step["selector"], step.get("timeout"))
                el.send_keys(Keys.ENTER)
            elif action == "select":
                self._select_option(step)
            elif action == "wait":
                self._wait_visible(step["by"], step["selector"], timeout=step.get("timeout"))
            elif action == "wait_invisible":
                self._wait_invisible(step["by"], step["selector"], timeout=step.get("timeout"))
            elif action == "wait_url_contains":
                timeout = int(step.get("timeout", self.default_timeout))
                WebDriverWait(self.driver, timeout).until(EC.url_contains(step["value"]))
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
                last_err: Optional[Exception] = None

                for attempt in range(1, retries + 1):
                    try:
                        self.driver.get(url)
                        self._override_navigator_webdriver()
                        if after_goto_sleep > 0:
                            time.sleep(after_goto_sleep)

                        if verify_by and verify_selector:
                            self._wait_visible(verify_by, verify_selector, timeout=verify_timeout)

                        if attempt > 1:
                            print(f"[STEP] Navigated to: {url} (attempt {attempt}/{retries})")
                        else:
                            print(f"[STEP] Navigated to: {url}")
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        if attempt < retries:
                            print(
                                f"[STEP] goto verify failed on attempt {attempt}/{retries}: "
                                f"{type(e).__name__}: {e}. Retrying..."
                            )
                            time.sleep(1)
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
                time.sleep(secs)
            elif action == "wait_download":
                pattern = step.get("pattern", "*.xlsx")
                timeout = int(step.get("timeout", 30))
                dest = step.get("dest")
                self._wait_for_download(pattern, timeout, dest)
            elif action == "scrape_popup":
                self._scrape_popup_data()
            else:
                raise ValueError(f"Unsupported action: {action}")
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

        # If no post-click checks are configured, keep the original click behavior.
        if not expect_new_tab and not target_url_contains:
            self._click(by, selector, timeout=timeout)
            return

        attempts = max(1, retry_on_verify_fail + 1)
        for attempt in range(1, attempts + 1):
            base_handles = set(self._get_window_handles_with_retry(retries=2, delay=0.2))
            base_url = self._safe_current_url()

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
        stable_checks: int = 3,
        poll: float = 0.4,
    ) -> bool:
        """等待表格行数连续稳定若干次，作为数据渲染完成信号。"""
        end_time = time.time() + timeout
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
                print(f"[SCRAPE] Rows stabilized at {count} rows")
                return True

            time.sleep(poll)

        print(f"[SCRAPE] Rows stability timeout, last_count={last_count}")
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

        for page in range(1, max_pages + 1):
            try:
                self._wait_visible(scrape_cfg["list_by"], list_selector, timeout=scrape_cfg.get("timeout"))
            except TimeoutException:
                if scrape_cfg.get("allow_empty", False):
                    print("[SCRAPE] No list rows found within timeout; allow_empty=true, returning empty records.")
                    return all_records
                raise
            items = self.driver.find_elements(self._to_by(scrape_cfg["list_by"]), list_selector)
            global_row = self._extract_fields_from_element(self.driver, global_fields) if global_fields else {}
            print(f"[INFO] Page {page}, found {len(items)} items")

            for item in items:
                row = self._extract_fields_from_element(item, fields)
                if global_row:
                    row.update(global_row)
                all_records.append(row)

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
                row[name] = target.text.strip() if attr == "text" else target.get_attribute(attr)
            except NoSuchElementException:
                row[name] = ""
        return row

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

        # 优先使用 popup_data（从弹出框爬取的数据）
        data_to_save = self.popup_data if self.popup_data else records
        data_to_save = self._attach_request_identifiers(data_to_save)
        source = "popup_data" if self.popup_data else "records"
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
        ts = time.strftime("%Y%m%d_%H%M%S")
        return path.with_name(f"{path.stem}_{ts}{path.suffix}")

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
            element.clear()
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
