# discovery.py
# Robust discovery helpers for the distributed Pong system.
# - server_discovery_listener(server_id, bind_ip, stop_event)
# - client_discover_servers(timeout=1.0, clock=None)

import socket
import time
from typing import List, Tuple, Optional
from common import (
    make_udp_socket,
    recv_message,
    send_message,
    DISCOVERY_BROADCAST_PORT,
    MSG_DISCOVER_REQUEST,
    MSG_DISCOVER_RESPONSE,
    MSG_DISCOVER_REPLY,  # alias in some code-bases; import if present
)

# A small helper to pick whichever discovery response constant exists
_DISCOVER_RESPONSE_KEY = MSG_DISCOVER_RESPONSE if "MSG_DISCOVER_RESPONSE" in globals() else MSG_DISCOVER_REPLY


def server_discovery_listener(server_id: str, bind_ip: str = "", stop_event=None):
    """
    Run in a daemon thread inside each server.
    - server_id: identifier string to advertise
    - bind_ip: the IP address this server is using (e.g. "127.0.0.2"). If empty, binds to all interfaces.
    - stop_event: threading.Event() instance used to request shutdown (optional).
    Behavior:
      * listens on UDP DISCOVERY_BROADCAST_PORT for discovery requests
      * replies directly to the requestor with a small JSON-like dict:
          {"type": MSG_DISCOVER_RESPONSE, "id": server_id, "ip": bind_ip}
      * does not perform heavy work and returns when stop_event is set.
    """
    # Bind specifically to the server's IP so multiple local servers can each bind the same discovery port
    bind_ip = bind_ip or ""
    try:
        sock = make_udp_socket(bind_ip=bind_ip, bind_port=DISCOVERY_BROADCAST_PORT, broadcast=True)
    except OSError:
        # Fallback: ephemeral bind (some environments prevent binding the discovery port)
        sock = make_udp_socket(bind_ip=bind_ip, bind_port=0, broadcast=True)

    # Short recv timeout so we can check stop_event regularly
    sock.settimeout(0.5)

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                msg, addr = recv_message(sock, None)  # clock not required here
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception:
                # ignore malformed messages and continue listening
                continue

            if not isinstance(msg, dict):
                continue

            mtype = msg.get("type")
            if mtype == MSG_DISCOVER_REQUEST:
                # Send back a direct unicast reply to the requester with our id and bind IP.
                reply = {"type": _DISCOVER_RESPONSE_KEY, "id": server_id, "ip": bind_ip or sock.getsockname()[0]}
                try:
                    send_message(sock, addr, reply, None)
                except Exception:
                    # ignore send errors - discovery is best-effort
                    pass
            # ignore other message types
    finally:
        try:
            sock.close()
        except Exception:
            pass


def client_discover_servers(timeout: float = 1.0, clock = None) -> List[Tuple[str, str]]:
    """
    Broadcast a discovery request and collect replies for up to `timeout` seconds.
    Returns a list of (server_id, server_ip) tuples (may be empty).

    Behavior details:
      - Sends one UDP broadcast to (<broadcast>, DISCOVERY_BROADCAST_PORT).
      - Waits until `timeout` elapses, collecting all replies.
      - Uses small per-recv timeouts to keep collecting replies until the full timeout.
    """
    results: List[Tuple[str, str]] = []
    start = time.time()
    # Create a UDP socket that can broadcast; bind to ephemeral port so replies come back here
    try:
        sock = make_udp_socket(bind_ip="", bind_port=0, broadcast=True)
    except OSError:
        # fallback to non-broadcast socket (rare)
        sock = make_udp_socket(bind_ip="", bind_port=0, broadcast=False)

    # Make sure we can receive replies in a loop without stopping at first timeout
    sock.settimeout(0.3)
    # send discovery request
    request = {"type": MSG_DISCOVER_REQUEST}
    try:
        # Use standard broadcast address and port
        sock.sendto(b"DISCOVER", ("<broadcast>", DISCOVERY_BROADCAST_PORT))  # lightweight fallback for raw listeners
    except Exception:
        # best-effort; if broadcast fails, we'll still listen for direct replies from servers on same host
        pass

    try:
        # Also use send_message to send a structured payload (some listeners use structured recv)
        try:
            send_message(sock, ("<broadcast>", DISCOVERY_BROADCAST_PORT), request, clock)
        except Exception:
            # ignore if structured send fails; we already did a raw broadcast attempt
            pass

        # collect replies until timeout
        while time.time() - start < timeout:
            try:
                msg, addr = recv_message(sock, clock)
            except socket.timeout:
                # no data this slice — continue waiting until overall timeout
                continue
            except OSError:
                break
            except Exception:
                # ignore parse errors and keep listening
                continue

            if not isinstance(msg, dict):
                continue

            # Accept either MSG_DISCOVER_RESPONSE or MSG_DISCOVER_REPLY payloads (compatibility)
            mtype = msg.get("type")
            if mtype in (MSG_DISCOVER_RESPONSE, MSG_DISCOVER_REPLY, _DISCOVER_RESPONSE_KEY):
                sid = msg.get("id")
                sip = msg.get("ip") or (addr[0] if addr else None)
                if sid and sip:
                    tup = (sid, sip)
                    if tup not in results:
                        results.append(tup)
            # otherwise ignore

    finally:
        try:
            sock.close()
        except Exception:
            pass

    return results
