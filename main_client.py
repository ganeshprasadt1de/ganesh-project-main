import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from settings import MAIN_CLIENT_LOGGER, setup_client_file_logging
from pong_client import PongClient


def parse_args():
    p = argparse.ArgumentParser(description="Distributed Pong Game Client")
    p.add_argument(
        "--player",
        type=int,
        required=True,
        help="Player number (1-based). Players 1 & 2 share room 0, 3 & 4 share room 1, etc.",
    )
    p.add_argument(
        "--logging_level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Console and file logging level",
    )
    return p.parse_args()


def main():
    args = parse_args()

    log_path = setup_client_file_logging(args.player, args.logging_level)

    MAIN_CLIENT_LOGGER.info(f"Starting Pong client — Player {args.player}")
    MAIN_CLIENT_LOGGER.info(f"Logs saved to: {log_path}")

    client = PongClient(args.player)
    client.start()


if __name__ == "__main__":
    main()
