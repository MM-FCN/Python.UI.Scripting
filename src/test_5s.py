import requests
import json

# 本地 FlareSolverr 地址
flaresolverr_url = "http://localhost:8191/v1"

# 测试目标：一个有 Cloudflare 保护的网站
# 你可以换成你实际要爬的网站
test_urls = [
    "https://www.hapag-lloyd.com/en/online-business/track/track-by-container-solution.html"
]

for url in test_urls:
    print(f"\n{'='*60}")
    print(f"测试目标: {url}")
    print(f"{'='*60}")

    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": 60000
    }

    try:
        response = requests.post(flaresolverr_url, json=payload, timeout=70)
        result = response.json()

        if result.get('status') == 'ok':
            html = result['solution']['response']
            cookies = result['solution']['cookies']

            print(f"✅ 成功！耗时: {result['solution'].get('startTimestamp')}ms")
            print(f"HTML 长度: {len(html)} 字符")
            print(f"获取到的 Cookies: {len(cookies)} 个")

            # 检查是否真的绕过了 Cloudflare
            if "cf-challenge" in html or "Checking your browser" in html:
                print("⚠️ 警告：页面仍包含 Cloudflare 挑战内容！")
            else:
                print("✅ 确认已绕过 Cloudflare 保护")

            # 打印前300字符预览
            print(f"\nHTML 预览:\n{html[:300]}...")

        else:
            print(f"❌ 失败: {result.get('message')}")

    except Exception as e:
        print(f"❌ 请求异常: {e}")