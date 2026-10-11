"""Serve the unchanged static app locally, with history-route fallback."""

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class Handler(SimpleHTTPRequestHandler):
    def send_head(self):
        requested = Path(self.translate_path(self.path))
        # Vue history routes need index.html; missing assets must remain 404s.
        if not requested.exists() and not Path(urlsplit(self.path).path).suffix:
            self.path = "/index.html"
        return super().send_head()

    def list_directory(self, path):
        self.send_error(404)
        return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parent / "dist")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6159)
    args = parser.parse_args()
    if not (args.directory / "index.html").is_file():
        parser.error("ORBAT Mapper is not installed. Run python orbat-mapper/download.py first.")
    server = ThreadingHTTPServer(
        (args.host, args.port), partial(Handler, directory=str(args.directory.resolve()))
    )
    print(f"ORBAT Mapper: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
