import json
import socket
from typing import Any, Dict, Tuple, Optional

# Base ports - can be offset for running multiple servers on same machine
SERVER_CONTROL_PORT_BASE = 50010
CLIENT_PORT_BASE = 50020
DISCOVERY_PORT_BASE = 50000

# Global port offset (set by server on startup)
PORT_OFFSET = 0

def get_server_control_port():
    """Get the actual server control port with offset applied."""
    return SERVER_CONTROL_PORT_BASE + PORT_OFFSET

def get_client_port():
    """Get the actual client port with offset applied."""
    return CLIENT_PORT_BASE + PORT_OFFSET

def get_discovery_port():
    """Get the actual discovery port with offset applied."""
    return DISCOVERY_PORT_BASE + PORT_OFFSET

# Backwards compatibility
SERVER_CONTROL_PORT = SERVER_CONTROL_PORT_BASE
CLIENT_PORT = CLIENT_PORT_BASE
DISCOVERY_PORT = DISCOVERY_PORT_BASE
DISCOVERY_BROADCAST_PORT = DISCOVERY_PORT

MSG_GAME_INPUT = "GAME_INPUT"
MSG_GAME_UPDATE = "GAME_UPDATE"
MSG_HEARTBEAT = "HEARTBEAT"
MSG_ELECTION = "ELECTION"
MSG_ELECTION_OK = "ELECTION_OK"
MSG_COORDINATOR = "COORDINATOR"
MSG_JOIN = "JOIN"
MSG_DISCOVER_REQUEST = "DISCOVER_REQUEST"
MSG_DISCOVER_RESPONSE = "DISCOVER_RESPONSE"

def make_udp_socket(bind_ip: str = "0.0.0.0", bind_port: Optional[int] = 0, broadcast: bool = False) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Set SO_REUSEADDR before binding to allow quick restarts
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except Exception:
        pass
    # Set SO_REUSEPORT on systems that support it (Linux, BSD, macOS)
    try:
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except Exception:
        pass
    # Enable broadcast if requested
    if broadcast:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:
            pass
    # Bind the socket if a port is specified
    if bind_port is not None:
        s.bind((bind_ip, bind_port))
    return s

def send_message(sock: socket.socket, addr: Tuple[str, int], payload: Dict[str, Any]) -> None:
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sock.sendto(data, addr)

def recv_message(sock: socket.socket, bufsize: int = 65535) -> Tuple[Dict[str, Any], Tuple[str, int]]:
    data, addr = sock.recvfrom(bufsize)
    try:
        obj = json.loads(data.decode("utf-8"))
        return obj, addr
    except Exception:
        return {}, addr