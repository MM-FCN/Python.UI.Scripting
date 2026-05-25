import argparse
import json
import traceback
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from src.config_loader import ConfigError, load_config
from src.logging_utils import setup_file_logging
from src.workflow import create_workflow_crawler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Workflow-based website crawler")

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--config",
        help="Path to a site config JSON under config/sites/<name>/config.json",
    )
    group.add_argument(
        "--site",
        help="Site name under config/sites/<name>/config.json",
    )
    group.add_argument(
        "--all-sites",
        action="store_true",
        help="Run all sites found under config/sites/*/config.json",
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode",
    )
    return parser.parse_args()


def run_config(config_path: str, headless: bool) -> None:
    print(f"\n{'='*60}")
    print(f"[TASK] Config: {config_path}")
    print(f"{'='*60}")
    try:
        config = load_config(config_path)
        # 示例：传递多个参数
        params = {"MAWB": "217-08282315", "ANOTHER_PARAM": "test_value"}
        crawler = create_workflow_crawler(config=config, headless=headless, params=params)
        records = crawler.run()
        print(f"[DONE] Crawl finished. Records: {len(records)}")
    except ConfigError as e:
        print(f"[CONFIG ERROR] {e}")
    except Exception as e:
        print(f"[ERROR] {e}")
        traceback.print_exc()


def _read_log_retention_days(config_path: Optional[Path], default_days: int = 30) -> int:
    if not config_path or not config_path.exists():
        return default_days
    try:
        with config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("log", {}).get("retention_days", default_days)
        days = int(raw)
        if days < 1:
            print(f"[LOG] Invalid retention_days={raw}, fallback to {default_days}")
            return default_days
        return days
    except Exception as e:
        print(f"[LOG] Failed to read log.retention_days from {config_path}: {e}")
        return default_days


def _resolve_log_site_name(args: argparse.Namespace) -> str:
    if args.site:
        return args.site
    if args.config:
        return Path(args.config).resolve().parent.name
    if args.all_sites:
        return "all-sites"
    return "general"


def main() -> None:
    load_dotenv()
    args = parse_args()
    site_root = Path("config/sites").resolve()

    log_cfg_path: Optional[Path] = None
    if args.config:
        log_cfg_path = Path(args.config)
    elif args.site:
        log_cfg_path = Path("config/sites") / args.site / "config.json"
    elif args.all_sites:
        first_site_cfg = sorted(Path("config/sites").glob("*/config.json"))
        if first_site_cfg:
            log_cfg_path = first_site_cfg[0]

    retention_days = _read_log_retention_days(log_cfg_path, default_days=30)
    log_site_name = _resolve_log_site_name(args)

    log_manager = setup_file_logging(
        project_root=Path.cwd(),
        keep_days=retention_days,
        site_name=log_site_name,
    )
    try:
        print(f"[LOG] Writing runtime logs to: {log_manager.log_path}")
        print(f"[LOG] Retention days: {retention_days}")
        print(f"[LOG] Site scope: {log_site_name}")

        if args.all_sites:
            site_configs = sorted(Path("config/sites").glob("*/config.json"))
            if not site_configs:
                print("[ERROR] No site configs found under config/sites/*/config.json")
                return
            print(f"[INFO] Found {len(site_configs)} site(s): {[p.parent.name for p in site_configs]}")
            for cfg_path in site_configs:
                run_config(str(cfg_path), args.headless)
        elif args.config:
            cfg_path = Path(args.config)
            if not cfg_path.exists():
                print(f"[ERROR] Config file not found: {cfg_path}")
                return
            try:
                relative = cfg_path.resolve().relative_to(site_root)
            except ValueError:
                print("[ERROR] --config only supports files under config/sites/*/config.json")
                return
            if relative.name != "config.json":
                print("[ERROR] --config only supports files named config.json under config/sites/<site>/")
                return
            run_config(str(cfg_path), args.headless)
        elif args.site:
            cfg_path = Path("config/sites") / args.site / "config.json"
            if not cfg_path.exists():
                print(f"[ERROR] Site config not found: {cfg_path}")
                return
            run_config(str(cfg_path), args.headless)
        else:
            print("[ERROR] Please provide --site <name>, --all-sites, or --config config/sites/<name>/config.json")
    finally:
        log_manager.close()


if __name__ == "__main__":
    main()
