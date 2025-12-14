# common.py
# Shared constants + helper functions for networking and messages.
# Hardened: tolerant JSON handling, multiple discovery constant aliases,
# safer socket creation (optional SO_REUSEPORT), and robust Lamport clock usage.

import json
import socket
from typing import Any, Dict, Tuple, Optional

# ----------------- ports / addresses -----------------
SERVER_CONTROL_PORT = 50010
CLIENT_PORT = 50020
DISCOVERY_PORT = 50000
DISCOVERY_BROADCAST_PORT = DISCOVERY_PORT
BROADCAST_ADDR = "255.255.255.255"

# ----------------- message types / aliases -----------------

# Client / game messages
MSG_GAME_INPUT    = "GAME_INPUT"
MSG_GAME_UPDATE   = "GAME_UPDATE"

# Reliable multicast control messages
MSG_ACK           = "ACK"
MSG_NACK          = "NACK"

# Heartbeat / failure detection
MSG_HEARTBEAT     = "HEARTBEAT"

# Bully-style election messages (used by server.py)
MSG_ELECTION      = "ELECTION"
MSG_ELECTION_OK   = "ELECTION_OK"
MSG_COORDINATOR   = "COORDINATOR"

# --- NEW: Dynamic Membership Message ---
MSG_JOIN          = "JOIN"

# Election helper (alternate style / LCR)
MSG_ELECTION_RESULT = "ELECTION_RESULT"

# Discovery messages: multiple names appear in the codebase; provide aliases
MSG_DISCOVER_SERVER   = "DISCOVER_SERVER"
MSG_DISCOVER_REQUEST  = "DISCOVER_REQUEST"
MSG_DISCOVER_RESPONSE = "DISCOVER_RESPONSE"
MSG_DISCOVER_REPLY    = MSG_DISCOVER_RESPONSE

# ----------------- socket helpers -----------------
def make_udp_socket(
    bind_ip: str = "",
    bind_port: Optional[int] = 0,
    broadcast: bool = False
) -> socket.socket:
    """
    Create and (optionally) bind a UDP socket.
    - bind_port: if None -> do not bind. If 0 -> bind ephemeral port.
    - broadcast: enable SO_BROADCAST when True.
    Notes:
      * Uses SO_REUSEADDR so multiple processes can bind on some platforms
        (use distinct bind IPs for multiple local servers to avoid conflicts).
      * Tries to set SO_REUSEPORT when available (useful for Linux).
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # allow quick reuse on many systems
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except Exception:
        pass

    # try reuseport where supported (Linux/macOS)
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

    # bind if requested (None means "don't bind"; 0 means ephemeral)
    if bind_port is not None:
        s.bind((bind_ip, bind_port))

    return s

# ----------------- Lamport-aware send / recv -----------------
def _json_dumps_bytes(obj: Any) -> bytes:
    """
    JSON-encode to bytes. Use safe fallback for non-serializable objects (str()).
    Keeps payloads small and avoids crashes for unexpected types.
    """
    try:
        return json.dumps(
            obj,
            separators=(",", ":"),
            ensure_ascii=False
        ).encode("utf-8")
    except TypeError:
        # best-effort: convert non-serializable values to strings
        def _fallback(o):
            return str(o)
        return json.dumps(
            obj,
            separators=(",", ":"),
            ensure_ascii=False,
            default=_fallback
        ).encode("utf-8")

def send_message(
    sock: socket.socket,
    addr: Tuple[str, int],
    payload: Dict[str, Any],
    clock: Optional[Any] = None
) -> None:
    """
    Wrap `payload` into an envelope { "payload": payload, "clock": <ts> }
    and send via UDP.

    - clock: optional logical clock object. If present, this function will try:
        1) clock.update_on_send()
        2) clock.tick()
      and include the returned integer as "clock".
    """
    envelope: Dict[str, Any] = {"payload": payload}

    if clock is not None:
        ts = None
        if hasattr(clock, "update_on_send"):
            try:
                ts = clock.update_on_send()
            except Exception:
                ts = None
        elif hasattr(clock, "tick"):
            try:
                ts = clock.tick()
            except Exception:
                ts = None

        if ts is not None:
            envelope["clock"] = ts

    data = _json_dumps_bytes(envelope)
    sock.sendto(data, addr)

def recv_message(
    sock: socket.socket,
    clock: Optional[Any] = None,
    bufsize: int = 65535
) -> Tuple[Dict[str, Any], Tuple[str, int]]:
    """
    Receive a UDP datagram and parse it.

    Returns:
        (payload_dict, (addr_ip, addr_port))

    - If the incoming datagram is the expected envelope, returns envelope['payload'].
    - If JSON parsing fails, returns {'raw': <decoded_text>} or {'raw_bytes': <bytes>}.
    - If clock is provided and message contains 'clock', calls clock.update_on_recv(ts)
      when available.
    """
    data, addr = sock.recvfrom(bufsize)

    text = None
    try:
        text = data.decode("utf-8")
    except Exception:
        try:
            text = data.decode("latin-1")
        except Exception:
            text = None

    payload: Any = {}
    ts = None

    if text is not None:
        try:
            obj = json.loads(text)
        except Exception:
            payload = {"raw": text}
        else:
            if isinstance(obj, dict) and "payload" in obj:
                payload = obj.get("payload")
                ts = obj.get("clock")
            else:
                payload = obj
    else:
        payload = {"raw_bytes": data}

    # update Lamport clock if available
    if clock is not None and ts is not None:
        try:
            if hasattr(clock, "update_on_recv"):
                clock.update_on_recv(ts)
            elif hasattr(clock, "tick"):
                clock.tick()
        except Exception:
            pass

    return payload, addr