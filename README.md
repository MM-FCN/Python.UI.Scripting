# Python 登录后流程化爬虫模板

这个项目用于以下场景：

1. 需要先登录网站。
2. 需要按固定操作步骤进入目标页面。
3. 进入页面后再抓取数据。

## 1. 安装

说明：项目支持通过本地浏览器驱动或 Selenium Remote / Edge attach 模式运行。

注：为避免在受限网络中自动下载安装驱动，`webdriver-manager` 已从项目依赖中移除，代码不再自动下载 geckodriver/msedgedriver。

如果公司网络无法访问 GitHub，请手动准备驱动：

- 将 `geckodriver` / `msedgedriver` 放到项目根目录（`./geckodriver` 或 `./msedgedriver`）或 `./drivers/` 下；
- 或将驱动路径加入系统 `PATH`；
- 或设置环境变量 `GECKODRIVER_PATH` / `MSEDGEDRIVER_PATH` 指向驱动完整路径；
- 另外可使用 Edge attach 模式（见下文）或 Selenium Remote（设置 `selenium_remote_url`）。

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
- 可选 `crawl_strategy`：`default` 或 `anonymous`（未配置或非法值自动回退 `default`）

2. 创建 `.env`（用于账号密码）：

```env
SITE_USERNAME=你的用户名
SITE_PASSWORD=你的密码
```

3. 可选：在 `config/global.json` 里配置运行日志与轮询默认项：

```json
{
	"selenium_remote_url": "http://szh2vm0372.apac.bosch.com:4444/wd/hub",
	"selenium_remote_anonymous_url": "http://szh2vm0372.apac.bosch.com:4445/wd/hub",
	"watch": {
		"enabled_by_default": true,
		"interval_seconds": 10,
		"input_root": "input",
		"push_timeout_seconds": 30,
		"push_retries": 1,
		"sites": ["cargo", "cargonavi"],
		"log": {
			"max_mb": 80,
			"retention_days": 7,
			"cleanup_interval_seconds": 3600
		},
		"parallel": {
			"enabled": true,
			"max_workers": 6,
			"chunk_size": 0
		},
		"db_config": {
			"skip_successful_items": true,
			"force_output_on_skipped_success": true,
			"recrawl_skipped_without_history": true,
			"state_db_path": "state/crawl_item_state.db",
			"max_size_mb": 200
		}
	}
}
```

配置项说明：

- `selenium_remote_url`：默认 Selenium Remote 地址。
- `selenium_remote_anonymous_url`：匿名策略 Selenium Remote 地址。
- `watch`：轮询相关总配置对象。
- `watch.log`：运行日志总配置对象。
- `watch.log.max_mb`：单个运行日志文件大小上限（MB）。达到上限后不会生成 `.1/.2` 轮转文件，而是按环形覆盖保留最新日志窗口。
- `watch.log.retention_days`：运行日志保留天数。超过天数的日志文件会被自动删除。
- `watch.log.cleanup_interval_seconds`：常驻进程执行旧日志清理的周期（秒）。
- `watch.enabled_by_default`：是否在未传 `--site/--config/--all-sites` 时自动进入轮询模式。`true` 表示默认启动即轮询。
- `watch.interval_seconds`：轮询间隔（秒）。每次扫描 `input_root` 后等待该秒数再进行下一次扫描。
- `watch.input_root`：轮询输入根目录。程序会扫描 `input_root/<site>/*.json`。
- `watch.push_timeout_seconds`：向 input JSON 里的 `Uri` 推送结果时的 HTTP 超时（秒）。
- `watch.push_retries`：推送失败后的重试次数。`0` 表示只请求 1 次，`1` 表示最多 2 次。
- `watch.sites`：允许轮询处理的 site 名单（数组）。例如 `cargo`、`cargonavi`。不在名单中的 site 输入文件会被跳过。
- `watch.parallel`：批量输入并行抓取配置。
- `watch.parallel.enabled`：是否启用批量并行模式。
- `watch.parallel.max_workers`：允许启动的最大 worker 数量。
- `watch.parallel.chunk_size`：每个 worker 处理的单号数量；`<=0` 时按总数自动平均分配。
- `watch.db_config`：单号状态与历史输出复用配置。
- `watch.db_config.skip_successful_items`：是否跳过历史成功单号，避免重复爬取。
- `watch.db_config.force_output_on_skipped_success`：当单号因历史成功被跳过时，是否仍基于历史数据生成新的 output 文件。
- `watch.db_config.recrawl_skipped_without_history`：当单号被判定历史成功但找不到历史 output 时，是否自动回爬该单号。
- `watch.db_config.state_db_path`：单号状态库路径（SQLite）。
- `watch.db_config.max_size_mb`：单号状态库文件大小上限（MB）。超过上限时，会在处理单个 input 文件开始前清空并重建状态库。
- input JSON 支持批量字段：`ContainerNo` / `MAWB` 可传字符串或数组；传数组时会逐条执行抓取并逐条推送。

站点远程策略说明（在启用 `--selenium-remote-url` 时生效）：

- `crawl_strategy=default`：使用 `selenium_remote_url`。
- `crawl_strategy=anonymous`：优先使用 `selenium_remote_anonymous_url`，为空时回退 `selenium_remote_url`。
- 未配置 `crawl_strategy` 或配置非法值：回退 `default`。

说明：

- 运行日志会按 `log/<site>/<YYYY-MM>/run_<YYYY-MM-DD>.txt` 写入。
- 当单个日志文件达到 `watch.log.max_mb` 后，只保留最近日志窗口，不再保留编号轮转历史文件。
- `output` 清理策略保持原有站点配置逻辑，不会因为 `config/global.json` 的运行日志策略变化而被联动修改。

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

说明：`edge_attach` 站点会连接到 `127.0.0.1:9222`，打印当前页面标题，并默认不关闭你手动打开的 Edge。使用 attach/remote 模式可以保留 CDP 注入能力（例如在需要在文档加载前注入脚本的场景），但如果你运行的是直接由 Selenium 启动的 msedgedriver，项目默认已禁用 CDP 注入以避免在不支持 goog/cdp 的驱动上报错。

当你希望在每个新文档加载前注入 stealth 脚本，请优先使用 Edge attach（远程调试）或 Selenium Remote 实例；在直接由 Selenium 启动的场景，本项目改为在页面加载后通过 `execute_script` 注入（仅作用于当前文档）。

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

## 3.1 Linux Docker 一键切换

适用场景：Linux 服务器 + Docker 部署（建议使用 headless）。

已提供的最小部署文件：

- `Dockerfile.linux`：Python + Firefox + geckodriver 运行镜像。
- `docker-compose.linux.yml`：容器编排（挂载 `config/input/output/log`）。
- `config/global.linux-docker.template.json`：Linux 默认轮询模板（`cargo/cargonavi/enx`）。
- `scripts/switch_to_linux_docker.sh`：备份并切换 `config/global.json`，然后一键拉起容器。

一键切换并启动：

```bash
chmod +x scripts/switch_to_linux_docker.sh
./scripts/switch_to_linux_docker.sh
```

查看运行日志：

```bash
docker compose -f docker-compose.linux.yml logs -f
```

停止容器：

```bash
docker compose -f docker-compose.linux.yml down
```

说明：

- Linux Docker 默认建议跑 `cargo/cargonavi/enx` 的 headless 轮询。
- `cma/hapag` 若存在风控识别，建议保留非 headless 或接管模式在桌面环境执行。

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
