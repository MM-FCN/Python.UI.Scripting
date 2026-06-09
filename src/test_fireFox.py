from playwright.sync_api import sync_playwright
import time
import random
from bs4 import BeautifulSoup
import subprocess
import os

# ⭐ 2.0+ 版本的导入方式
from playwright_stealth.stealth import Stealth
stealth_instance = Stealth()

def get_firefox_user_agent():
    """自动获取本机 Firefox User-Agent"""
    try:
        result = subprocess.run(
            ["firefox", "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        version = result.stdout.strip().split()[-1]
        major_version = version.split('.')[0]
        ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{major_version}.0) Gecko/20100101 Firefox/{version}"
        return ua
    except Exception:
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"

def get_firefox_path():
    """获取本机 Firefox 安装路径"""
    paths = [
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ]
    for path in paths:
        if os.path.exists(path):
            return path
    return None

def crawl_with_firefox():
    user_agent = get_firefox_user_agent()
    print(f"使用的 User-Agent: {user_agent}")
    
    firefox_path = get_firefox_path()
    if firefox_path:
        print(f"找到 Firefox: {firefox_path}")
    
    with sync_playwright() as p:
        browser = p.firefox.launch(
            headless=False,
            executable_path=firefox_path,
            args=["--width=1920", "--height=1080"]
        )
        
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=user_agent
        )
        page = context.new_page()
        
        # ⭐ 应用 stealth（2.0+ 版本写法）
        stealth_instance.apply_stealth_sync(page)
        
        # 额外隐藏 webdriver 特征
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => false,
            });
        """)
        
        try:
            # ================= 测试访问 =================
            print("正在访问测试页面...")
            page.goto("https://www.hapag-lloyd.com/en/online-business/track/track-by-container-solution.html", wait_until="networkidle", timeout=60000)
            time.sleep(2)
            
            content = page.content()
            print(content)
            print("✅ 访问成功！")
            
        finally:
            browser.close()
            print("🧹 浏览器已关闭")

if __name__ == "__main__":
    crawl_with_firefox()