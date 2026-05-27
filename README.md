# Python 登录后流程化爬虫模板

这个项目用于以下场景：

1. 需要先登录网站。
2. 需要按固定操作步骤进入目标页面。
3. 进入页面后再抓取数据。

## 1. 安装

说明：项目使用 Firefox + geckodriver（由 `webdriver-manager` 自动管理）。

如果公司网络无法访问 GitHub，可手动放置 `geckodriver.exe`：

- 放到项目根目录（`./geckodriver.exe`）或 `./drivers/geckodriver.exe`
- 或配置环境变量 `GECKODRIVER_PATH` 指向驱动完整路径

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
```

## 2. 配置

1. 在 `config/sites/<site>/config.json` 中配置：

- 登录页 URL
- 登录输入框和提交按钮选择器
- 登录后的成功判定条件
- 进入目标页面的步骤（`navigation_steps`）
- 数据列表选择器和字段选择器（`scrape`）
- 多任务模式可用 `tasks`，每个任务配置独立的 `navigation_steps/scrape/output`

2. 创建 `.env`（用于账号密码）：

```env
SITE_USERNAME=你的用户名
SITE_PASSWORD=你的密码
```

## 3. 运行

有界面模式（推荐先调试）：

```bash
python -m src.main --site site1
```

无界面模式：

```bash
python -m src.main --site site1 --headless
```

运行所有站点：

```bash
python -m src.main --all-sites
```

## 4. 配置结构说明

### navigation_steps 支持的 action

- `click`：点击元素
- `type`：输入文本（支持 `env` 从环境变量读取）
- `press_enter`：对元素发送回车
- `wait`：等待元素可见
- `wait_url_contains`：等待 URL 包含指定关键词
- `goto`：跳转 URL
- `scroll`：向下滚动
- `sleep`：固定等待秒数
- `select`：下拉框选择（按文本/value/index）

### scrape 说明

- `list_by` + `list_selector`：列表每条记录容器
- `fields`：每个字段的提取规则
- `next_page`：分页按钮配置（可选）
- `max_pages`：最多翻页数

## 5. 常见问题

1. 登录后跳不过去：
- 检查 `wait_success` 的条件是否正确。
- 如果有验证码/二次验证，建议先手动完成再继续自动流程。

2. 抓不到数据：
- 打开浏览器开发者工具，重新确认 CSS/XPath。
- 页面可能有 iframe，需要先切换 frame（可在代码中补充）。

3. 反爬限制：
- 控制请求频率，避免高并发。
- 遵守目标网站的服务条款与法律要求。
