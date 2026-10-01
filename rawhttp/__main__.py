"""Serve a directory: python -m rawhttp [--dir DIR] [--port PORT] [--workers N]"""

import argparse
import logging

from . import HTTPServer, Router


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m rawhttp", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--dir", default=".", help="directory to serve (default: cwd)")
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    router = Router()
    router.mount_static("/", args.dir)
    server = HTTPServer(router, args.host, args.port, workers=args.workers)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
