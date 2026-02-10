import json
import socket
from typing import Any, Dict, Optional, Tuple

from settings import ALL_MESSAGE_TYPES, NEEDED_PAYLOADS


class GameMessage:
    def __init__(self, msg_type: str, payload: Dict[str, Any]):
        if msg_type not in ALL_MESSAGE_TYPES:
            raise ValueError(f"Invalid message type: {msg_type}")
        self.msg_type = msg_type

        required = NEEDED_PAYLOADS.get(msg_type, [])
        for field in required:
            if field not in payload:
                raise ValueError(
                    f"Missing required payload field '{field}' for message type '{msg_type}'"
                )
        self.payload = payload

    def serialize(self) -> bytes:
        envelope = {"type": self.msg_type, **self.payload}
        return json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    @classmethod
    def deserialize(cls, data: bytes) -> Optional["GameMessage"]:
        try:
            obj = json.loads(data.decode("utf-8"))
            msg_type = obj.pop("type", None)
            if msg_type is None:
                return None
            return cls(msg_type, obj)
        except (json.JSONDecodeError, ValueError):
            return None

    def get(self, key: str, default=None):
        return self.payload.get(key, default)

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.msg_type, **self.payload}

    def __repr__(self):
        return f"GameMessage(type={self.msg_type}, payload={self.payload})"


def make_udp_socket(
    bind_ip: str = "0.0.0.0",
    bind_port: Optional[int] = 0,
    broadcast: bool = False,
) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except Exception:
        pass
    try:
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except Exception:
        pass
    if broadcast:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:
            pass
    if bind_port is not None:
        s.bind((bind_ip, bind_port))
    return s


def send_message(
    sock: socket.socket,
    addr: Tuple[str, int],
    msg: "GameMessage | Dict[str, Any]",
) -> None:
    if isinstance(msg, GameMessage):
        data = msg.serialize()
    else:
        data = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sock.sendto(data, addr)


def recv_message(
    sock: socket.socket,
    bufsize: int = 65535,
) -> Tuple[Dict[str, Any], Tuple[str, int]]:
    data, addr = sock.recvfrom(bufsize)
    try:
        obj = json.loads(data.decode("utf-8"))
        return obj, addr
    except Exception:
        return {}, addr
