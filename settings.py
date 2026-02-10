import logging
import os
import socket
from pathlib import Path

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


SERVER_CONTROL_PORT = 50010
CLIENT_PORT = 50020
DISCOVERY_PORT = 50000
DISCOVERY_BROADCAST_PORT = DISCOVERY_PORT

MSG_GAME_INPUT = "GAME_INPUT"
MSG_GAME_UPDATE = "GAME_UPDATE"
MSG_ROOM_READY = "ROOM_READY"
MSG_HEARTBEAT = "HEARTBEAT"
MSG_ELECTION = "ELECTION"
MSG_ELECTION_OK = "ELECTION_OK"
MSG_COORDINATOR = "COORDINATOR"
MSG_JOIN = "JOIN"
MSG_DISCOVER_REQUEST = "DISCOVER_REQUEST"
MSG_DISCOVER_RESPONSE = "DISCOVER_RESPONSE"
MSG_STATE_SNAPSHOT_REQUEST = "STATE_SNAPSHOT_REQUEST"
MSG_STATE_SNAPSHOT_RESPONSE = "STATE_SNAPSHOT_RESPONSE"
MSG_ROOMS_UPDATE = "ROOMS_UPDATE"
MSG_ACK = "ACK"
MSG_NACK = "NACK"
MSG_CLOSE_ROOM = "CLOSE_ROOM"
MSG_GAME_OVER = "GAME_OVER"
ALL_MESSAGE_TYPES = [
    MSG_GAME_INPUT, MSG_GAME_UPDATE, MSG_ROOM_READY, MSG_HEARTBEAT,
    MSG_ELECTION, MSG_ELECTION_OK, MSG_COORDINATOR, MSG_JOIN,
    MSG_DISCOVER_REQUEST, MSG_DISCOVER_RESPONSE,
    MSG_STATE_SNAPSHOT_REQUEST, MSG_STATE_SNAPSHOT_RESPONSE,
    MSG_ROOMS_UPDATE, MSG_ACK, MSG_NACK, MSG_CLOSE_ROOM, MSG_GAME_OVER,
]

NEEDED_PAYLOADS = {
    MSG_GAME_INPUT: ["player", "dir"],
    MSG_GAME_UPDATE: ["state"],
    MSG_ROOM_READY: ["room_id"],
    MSG_HEARTBEAT: ["server_id"],
    MSG_ELECTION: ["candidate"],
    MSG_COORDINATOR: ["leader_id"],
    MSG_JOIN: ["id"],
    MSG_DISCOVER_REQUEST: [],
    MSG_DISCOVER_RESPONSE: ["id"],
    MSG_STATE_SNAPSHOT_REQUEST: [],
    MSG_STATE_SNAPSHOT_RESPONSE: ["state"],
    MSG_ROOMS_UPDATE: ["rooms"],
    MSG_ACK: ["server_id"],
    MSG_NACK: ["server_id", "room_id"],
    MSG_CLOSE_ROOM: ["room_id"],
    MSG_GAME_OVER: [],
}

WIN_SCORE = 3
HEARTBEAT_INTERVAL = 1.0
HEARTBEAT_TIMEOUT = 3.0
TICK_INTERVAL = 0.016
CLIENT_SERVER_TIMEOUT = 2.0
DISCOVERY_TIMEOUT = 2.0
PEER_STALE_TIMEOUT = HEARTBEAT_TIMEOUT * 2
ELECTION_TIMEOUT = 2.5

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s')

MAIN_SERVER_LOGGER = logging.getLogger("MainServer")
MAIN_CLIENT_LOGGER = logging.getLogger("MainClient")
PONG_SERVER_LOGGER = logging.getLogger("PongServer")
PONG_CLIENT_LOGGER = logging.getLogger("PongClient")
DISCOVERY_LOGGER = logging.getLogger("DiscoveryProtocol")
BULLY_ELECTION_LOGGER = logging.getLogger("BullyElection")


def setup_server_file_logging(server_id, logging_level="INFO"):
    log_filename = f"pong_server_{server_id[:8]}.log"
    log_path = LOG_DIR / log_filename

    file_handler = logging.FileHandler(log_path, mode='w')
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))

    for logger_name in ["MainServer", "PongServer", "DiscoveryProtocol", "BullyElection"]:
        logger = logging.getLogger(logger_name)
        logger.addHandler(file_handler)
        logger.setLevel(getattr(logging, logging_level))

    return str(log_path)


def setup_client_file_logging(player_id, logging_level="INFO"):
    log_filename = f"pong_client_player_{player_id}.log"
    log_path = LOG_DIR / log_filename

    file_handler = logging.FileHandler(log_path, mode='w')
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))

    for logger_name in ["MainClient", "PongClient"]:
        logger = logging.getLogger(logger_name)
        logger.addHandler(file_handler)
        logger.setLevel(getattr(logging, logging_level))

    return str(log_path)
