import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from settings import MAIN_SERVER_LOGGER, setup_server_file_logging
from pong_server import PongServer


def parse_args():
    p = argparse.ArgumentParser(description="Distributed Pong Game Server")
    p.add_argument(
        "--logging_level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Console and file logging level",
    )
    return p.parse_args()


def main():
    args = parse_args()

    server = PongServer()

    log_path = setup_server_file_logging(server.server_id, args.logging_level)

    MAIN_SERVER_LOGGER.info(f"Starting Pong server {server.server_id[:8]}")
    MAIN_SERVER_LOGGER.info(f"Logs saved to: {log_path}")

    server.start()


if __name__ == "__main__":
    main()
