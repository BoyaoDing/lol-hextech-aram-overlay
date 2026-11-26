"""CLI entrypoint for launching the Hextech ARAM live overlay service."""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

import uvicorn

from .engine import OverlayEngine
from .server import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description="英雄联盟海克斯大乱斗直播实时显示插件")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0, help="监听端口 (默认读取配置 18777)")
    parser.add_argument("--data-dir", default="data", help="配置与会话存储目录")
    parser.add_argument("--open-browser", action="store_true", help="启动后自动打开控制台页面")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    cache_dir = data_dir / "cache"

    engine = OverlayEngine(data_dir=data_dir, cache_dir=cache_dir)
    _, cfg = engine.snapshot()
    port = args.port or cfg.port
    app = create_app(engine)

    if args.open_browser:
        url = f"http://{args.host}:{port}/control"
        t = threading.Timer(1.0, lambda: webbrowser.open(url))
        t.daemon = True
        t.start()

    print(f"==================================================")
    print(f" 海克斯大乱斗直播插件已启动")
    print(f" 主播控制台: http://{args.host}:{port}/control")
    print(f" OBS 浏览器源: http://{args.host}:{port}/overlay")
    print(f"==================================================")

    uvicorn.run(app, host=args.host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
