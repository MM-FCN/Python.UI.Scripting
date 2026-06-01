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

3. 可选：在 `config/global.json` 里配置轮询默认项（轮询间隔、输入目录、处理站点）：

```json
{
	"watch": {
		"enabled_by_default": true,
		"interval_seconds": 10,
		"input_root": "input",
		"push_timeout_seconds": 30,
		"push_retries": 1,
		"sites": ["cargo", "cargonavi"]
	}
}
```

配置项说明：

- `watch`：轮询相关总配置对象。
- `watch.enabled_by_default`：是否在未传 `--site/--config/--all-sites` 时自动进入轮询模式。`true` 表示默认启动即轮询。
- `watch.interval_seconds`：轮询间隔（秒）。每次扫描 `input_root` 后等待该秒数再进行下一次扫描。
- `watch.input_root`：轮询输入根目录。程序会扫描 `input_root/<site>/*.json`。
- `watch.push_timeout_seconds`：向 input JSON 里的 `Uri` 推送结果时的 HTTP 超时（秒）。
- `watch.push_retries`：推送失败后的重试次数。`0` 表示只请求 1 次，`1` 表示最多 2 次。
- `watch.sites`：允许轮询处理的 site 名单（数组）。例如 `cargo`、`cargonavi`。不在名单中的 site 输入文件会被跳过。
- input JSON 支持批量字段：`ContainerNo` / `MAWB` 可传字符串或数组；传数组时会逐条执行抓取并逐条推送。

优先级说明：

- 命令行参数优先级高于 `config/global.json`。
- 例如传了 `--watch-sites cargo`，会覆盖 `watch.sites` 的配置。

## 3. 运行

有界面模式（推荐先调试）：

```bash
python -m src.main --site cargonavi
```

传入查询参数：

```bash
python -m src.main --site cargonavi --mawb 217-08282315
python -m src.main --site cargo --container-no ONEU6961505
```

接管已手动打开的 Edge（9222 调试端口）：

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222 --user-data-dir="C:\edge-debug-profile"
python -m src.main --site edge_attach
```

说明：`edge_attach` 站点会连接到 `127.0.0.1:9222`，打印当前页面标题，并默认不关闭你手动打开的 Edge。

接管浏览器后直接执行 hapag 配置抓取：

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222 --user-data-dir="C:\edge-debug-profile"
python -m src.main --site hapag --container-no ONEU6961505
```

说明：`config/sites/hapag/config.json` 已支持接管参数（`edge.attach_existing`、`edge.debugger_address`、`startup_navigate`、`keep_browser_open`），可由客户自行修改。

轮询模式下按配置决定“系统启动”或“批处理启动后接管”：

```json
{
	"browser_startup": {
		"mode": "system",
		"batch_file": "run_edge_hapag_9222.bat",
		"args": [],
		"wait_seconds": 3,
		"run_once": true
	}
}
```

说明：
- `mode=system`：不执行批处理，由 Selenium 按站点配置直接启动浏览器。
- `mode=batch_attach`：watcher 会先执行 `batch_file` 启动浏览器，再按 `edge.attach_existing=true` 去接管。
- `run_once=true`：同一站点在该次 watcher 生命周期中只执行一次批处理，避免重复起浏览器。

无界面模式：

```bash
python -m src.main --site cargonavi --headless
```

运行所有站点：

```bash
python -m src.main --all-sites
```

定时轮询模式（仅处理指定 site）：

```bash
python -m src.main --watch-input --watch-sites cargo,cargonavi --watch-interval 10
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
