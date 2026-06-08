from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from pathlib import Path

options = Options()
# 基础反检测参数
options.add_experimental_option('excludeSwitches', ['enable-automation'])
options.add_experimental_option('useAutomationExtension', False)
options.add_argument('--disable-blink-features=AutomationControlled')

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
    input('页面已加载完成，按 Enter 关闭浏览器...')
finally:
    driver.quit()