import argparse
import webbrowser

import uvicorn

from app.main import app


class DesktopServer(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.started:
            url = f"http://{self.config.host}:{self.config.port}/"
            print(f"Local Torrent is ready at {url}\nPress Ctrl+C to shut down safely.", flush=True)
            webbrowser.open(url)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Local Torrent and open its web interface.")
    parser.add_argument("--port", type=int, default=8000, help="Local web interface port (default: 8000)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, proxy_headers=False,
                            loop="asyncio", http="h11", ws="websockets", lifespan="on")
    DesktopServer(config).run()


if __name__ == "__main__":
    main()
