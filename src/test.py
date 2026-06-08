from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import StaleElementReferenceException
from pathlib import Path
import time

options = Options()
# 基础反检测参数
options.add_experimental_option('excludeSwitches', ['enable-automation'])
options.add_experimental_option('useAutomationExtension', False)
options.add_argument('--disable-blink-features=AutomationControlled')
options.add_argument('--lang=en-US')

driver = webdriver.Edge(options=options)

try:
    # 注入 stealth.min.js
    stealth_js_path = Path(__file__).with_name('stealth.min.js')
    with stealth_js_path.open('r', encoding='utf-8') as f:
        js = f.read()
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': js})

    #driver.get("https://bot.sannysoft.com")
    driver.get("https://www.hapag-lloyd.com/en/online-business/track/track-by-container-solution.html")
    WebDriverWait(driver, 30).until(
        lambda d: d.execute_script('return document.readyState') == 'complete'
    )

    max_wait_seconds = 150
    retry_interval_seconds = 0.5
    missing_rounds_to_stop = 30
    max_scan_depth = 4
    max_scan_frames = 120
    deadline = time.time() + max_wait_seconds
    round_idx = 0
    missing_rounds = 0
    cf_iframe_css = "iframe[src*='challenges.cloudflare.com'], iframe[title*='Cloudflare security challenge']"
    managed_challenge_markers = [
        ".hal-container-header",
        "script[src*='challenge-platform']",
        "input[name='cf-turnstile-response']",
        "input[id*='_response']",
    ]
    checkbox_selectors = [
        "#content input[type='checkbox']",
        "#content label.cb-lb",
        "#content .cb-i",
        "#content span.cb-lb-t",
        "label.cb-lb",
        "input[type='checkbox']",
    ]

    def switch_to_frame_path(path):
        try:
            driver.switch_to.default_content()
            for idx in path:
                frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
                if idx >= len(frames):
                    return False
                driver.switch_to.frame(frames[idx])
            return True
        except Exception:
            return False

    def collect_frame_paths():
        paths = [()]
        queue = [()]

        while queue and len(paths) < max_scan_frames:
            cur = queue.pop(0)
            if len(cur) >= max_scan_depth:
                continue
            if not switch_to_frame_path(cur):
                continue
            child_count = len(driver.find_elements(By.CSS_SELECTOR, "iframe, frame"))
            for idx in range(child_count):
                child = cur + (idx,)
                paths.append(child)
                queue.append(child)
                if len(paths) >= max_scan_frames:
                    break
        return paths

    def path_name(path):
        if not path:
            return "main"
        return "frame/" + "/".join(str(i) for i in path)

    def click_checkbox_candidate(el):
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                el,
            )
        except Exception:
            pass

        try:
            ActionChains(driver).move_to_element(el).pause(0.05).click(el).perform()
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", el)
            except Exception:
                pass

        # Cloudflare styled checkbox often needs clicking the label container.
        try:
            driver.execute_script(
                "const n=arguments[0]; const lb=n.closest('label'); if (lb) lb.click();",
                el,
            )
        except Exception:
            pass

    def find_checked_state():
        try:
            checked = driver.find_elements(By.CSS_SELECTOR, "#content input[type='checkbox']:checked, input[type='checkbox']:checked")
            return bool(checked)
        except Exception:
            return False

    def is_managed_challenge_page():
        try:
            for marker in managed_challenge_markers:
                if driver.find_elements(By.CSS_SELECTOR, marker):
                    return True
            page_html = (driver.page_source or "").lower()
            return "managed challenge" in page_html or "_cf_chl_opt" in page_html
        except Exception:
            return False

    def get_turnstile_token_len():
        try:
            token = driver.execute_script(
                """
                const el = document.querySelector("input[name='cf-turnstile-response'], input[id*='_response']");
                return el ? (el.value || '') : '';
                """
            )
            return len(token or "")
        except Exception:
            return 0

    while time.time() < deadline:
        round_idx += 1
        found_in_round = 0

        current_url = (driver.current_url or "").lower()
        challenge_mode = is_managed_challenge_page() or ("__cf_chl" in current_url)
        if challenge_mode:
            token_len = get_turnstile_token_len()
            print(
                f"challenge mode round {round_idx}: url_contains_cf={('__cf_chl' in current_url)}, token_len={token_len}"
            )
            if token_len > 20 and "__cf_chl" not in current_url:
                print("challenge token received and URL recovered, continue.")
                break

        frame_paths = collect_frame_paths()
        for path in frame_paths:
            context_name = path_name(path)
            if not switch_to_frame_path(path):
                continue

            seen_in_context = 0
            for selector in checkbox_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                except Exception:
                    continue

                visible_elements = []
                for element in elements:
                    try:
                        if element.is_displayed():
                            visible_elements.append(element)
                    except StaleElementReferenceException:
                        continue

                if not visible_elements:
                    continue

                seen_in_context += len(visible_elements)
                print(
                    f"checkbox candidates in {context_name}: selector={selector}, count={len(visible_elements)} (round {round_idx})"
                )

                for candidate in visible_elements:
                    try:
                        click_checkbox_candidate(candidate)
                        time.sleep(0.2)
                        print(f"checkbox checked state in {context_name}: {find_checked_state()}")
                    except Exception as e:
                        print(f"checkbox click failed in {context_name}: {type(e).__name__}: {e}")

            found_in_round += seen_in_context

        driver.switch_to.default_content()

        if challenge_mode:
            # On managed challenge page, checkbox may appear later after orchestration script runs.
            # Keep monitoring even if this round found nothing.
            missing_rounds = 0
        

        if round_idx % 6 == 0:
            try:
                main_cf_iframes = driver.find_elements(By.CSS_SELECTOR, cf_iframe_css)
                print(
                    f"diag round {round_idx}: cloudflare iframes in main={len(main_cf_iframes)}, "
                    f"scanned_frames={len(frame_paths)}, challenge_mode={challenge_mode}, token_len={get_turnstile_token_len()}"
                )
            except Exception:
                pass

        if found_in_round == 0:
            missing_rounds += 1
            print(f"verify checkbox not found in any context (round {round_idx}, missing={missing_rounds})")
            if missing_rounds >= missing_rounds_to_stop:
                print("verify checkbox no longer present")
                break
        else:
            missing_rounds = 0

        time.sleep(retry_interval_seconds)
    else:
        print("verify checkbox still present after timeout")

    input('页面已加载完成，按 Enter 关闭浏览器...')
finally:
    driver.quit()