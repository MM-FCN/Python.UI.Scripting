import requests
import json
import time

flaresolverr_url = "http://localhost:8191/v1"

# ============ 第一步：创建会话 ============
def create_session(session_name):
    payload = {
        "cmd": "sessions.create",
        "session": session_name
    }
    response = requests.post(flaresolverr_url, json=payload)
    result = response.json()
    if result.get('status') == 'ok':
        print(f"✅ 会话 '{session_name}' 创建成功")
        return True
    else:
        print(f"❌ 会话创建失败: {result.get('message')}")
        return False

# ============ 第二步：用会话访问页面 ============
def get_page(session_name, url):
    payload = {
        "cmd": "request.get",
        "url": url,
        "session": session_name,  # 关键：指定会话
        "maxTimeout": 60000
    }
    response = requests.post(flaresolverr_url, json=payload)
    return response.json()

# ============ 第三步：销毁会话 ============
def destroy_session(session_name):
    payload = {
        "cmd": "sessions.destroy",
        "session": session_name
    }
    requests.post(flaresolverr_url, json=payload)
    print(f"会话 '{session_name}' 已销毁")

# ============ 实际使用流程 ============
session_name = "my_scraper_session"

# 1. 创建会话
create_session(session_name)

try:
    # 2. 访问初始页面（会触发并完成 5s 挑战）
    print("\n访问初始页面...")
    result1 = get_page(session_name, "https://www.hapag-lloyd.com/en/home.html")
    
    if result1.get('status') == 'ok':
        html1 = result1['solution']['response']
        print(f"✅ 初始页面获取成功，HTML长度: {len(html1)}")
        
        # 3. 点击查询后的跳转页面（复用同一个会话，不会再次挑战）
        print("\n访问查询结果页面...")
        result2 = get_page(session_name, "https://www.hapag-lloyd.com/en/online-business/track/track-by-container-solution.html")
        
        if result2.get('status') == 'ok':
            html2 = result2['solution']['response']
            print(f"✅ 查询页面获取成功，HTML长度: {len(html2)}")
            
            # 现在可以正常解析数据了
            # from bs4 import BeautifulSoup
            # soup = BeautifulSoup(html2, 'html.parser')
            # ...
        else:
            print(f"❌ 查询页面失败: {result2.get('message')}")
    else:
        print(f"❌ 初始页面失败: {result1.get('message')}")

finally:
    # 4. 用完销毁会话，释放资源
    destroy_session(session_name)