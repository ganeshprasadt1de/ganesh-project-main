# discovery.py
# Robust discovery helpers for the distributed Pong system.
# - server_discovery_listener(server_id, bind_ip, stop_event)
# - client_discover_servers(timeout=1.0, clock=None)
#
# This implementation:
#  - replies to broadcast requests (existing behavior)
#  - also joins a multicast group and replies to multicast discovery
#  - client side will broadcast+multicast, and if nothing found will probe common loopback IPs
#    (127.0.0.2 / 127.0.0.3) as a fallback for Windows loopback testing.

import socket
import struct
import time
from typing import List, Tuple, Optional
from common import (
    make_udp_socket,
    recv_message,
    send_message,
    DISCOVERY_BROADCAST_PORT,
    MSG_DISCOVER_REQUEST,
    MSG_DISCOVER_RESPONSE,
    MSG_DISCOVER_REPLY,
)

MULTICAST_GROUP = "239.255.255.250"   # SSDP-style address (private/local multicast)
MULTICAST_PORT = DISCOVERY_BROADCAST_PORT

# Accept both names
_DISCOVER_RESPONSE_KEY = MSG_DISCOVER_RESPONSE if "MSG_DISCOVER_RESPONSE" in globals() else MSG_DISCOVER_REPLY


def _join_multicast(sock: socket.socket, mcast_group: str, bind_ip: str = ""):
    """
    Join multicast group on the provided socket.
    bind_ip should be the local interface IP to receive multicasts on ('' works as default).
    """
    try:
        # pack group + interface (IPv4)
        mreq = struct.pack("4s4s", socket.inet_aton(mcast_group), socket.inet_aton(bind_ip or "0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except Exception:
        # best-effort: ignore if not supported
        pass


def server_discovery_listener(server_id: str, bind_ip: str = "", stop_event=None):
    """
    Run inside each server in a daemon thread.
    - Listens for DISCOVER requests via broadcast AND multicast on DISCOVERY_BROADCAST_PORT.
    - Replies directly to requestor with {"type": MSG_DISCOVER_RESPONSE, "id": server_id, "ip": bind_ip}
    """
    bind_ip = bind_ip or ""
    # socket for broadcast/unicast replies
    try:
        sock = make_udp_socket(bind_ip=bind_ip, bind_port=DISCOVERY_BROADCAST_PORT, broadcast=True)
    except OSError:
        # fallback ephemeral bind if bind fails
        sock = make_udp_socket(bind_ip=bind_ip, bind_port=0, broadcast=True)

    # also create a multicast-listening socket bound to the same port
    try:
        mcast_sock = make_udp_socket(bind_ip=bind_ip, bind_port=DISCOVERY_BROADCAST_PORT, broadcast=False)
        # allow reuse for multicast
        _join_multicast(mcast_sock, MULTICAST_GROUP, bind_ip)
        mcast_sock.settimeout(0.5)
    except Exception:
        mcast_sock = None

    sock.settimeout(0.5)
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            # listen on both sockets (sock then mcast_sock), small timeout to check stop_event frequently
            handled = False
            try:
                msg, addr = recv_message(sock, None)
                handled = True
            except socket.timeout:
                pass
            except OSError:
                break
            except Exception:
                pass

            if not handled and mcast_sock:
                try:
                    msg, addr = recv_message(mcast_sock, None)
                    handled = True
                except socket.timeout:
                    pass
                except OSError:
                    break
                except Exception:
                    pass

            if not handled:
                continue

            if not isinstance(msg, dict):
                continue

            mtype = msg.get("type")
            if mtype == MSG_DISCOVER_REQUEST:
                reply = {"type": _DISCOVER_RESPONSE_KEY, "id": server_id, "ip": bind_ip or sock.getsockname()[0]}
                try:
                    send_message(sock, addr, reply, None)
                except Exception:
                    pass
    finally:
        try:
            sock.close()
        except Exception:
            pass
        if mcast_sock:
            try:
                mcast_sock.close()
            except Exception:
                pass


def client_discover_servers(timeout: float = 1.0, clock = None) -> List[Tuple[str, str]]:
    """
    Broadcast and multicast a discovery request, collect responses for up to `timeout` seconds.
    If no responses and common local loopback addresses are present, probe them (Windows-friendly).
    Returns a list of (server_id, server_ip) tuples.
    """
    results: List[Tuple[str, str]] = []
    start = time.time()

    # create a socket that can broadcast and receive replies
    try:
        sock = make_udp_socket(bind_ip="", bind_port=0, broadcast=True)
    except OSError:
        sock = make_udp_socket(bind_ip="", bind_port=0, broadcast=False)

    sock.settimeout(0.25)

    # form request
    request = {"type": MSG_DISCOVER_REQUEST}

    # 1) Send broadcast (best-effort)
    try:
        # raw broadcast
        sock.sendto(b"DISCOVER", ("<broadcast>", DISCOVERY_BROADCAST_PORT))
    except Exception:
        pass

    try:
        # structured broadcast
        try:
            send_message(sock, ("<broadcast>", DISCOVERY_BROADCAST_PORT), request, clock)
        except Exception:
            pass
    except Exception:
        pass

    # 2) Send multicast discovery
    try:
        mcast_sock = make_udp_socket(bind_ip="", bind_port=0, broadcast=False)
        mcast_sock.settimeout(0.25)
        try:
            send_message(mcast_sock, (MULTICAST_GROUP, MULTICAST_PORT), request, clock)
        except Exception:
            pass
    except Exception:
        mcast_sock = None

    # 3) Collect replies until timeout
    try:
        while time.time() - start < timeout:
            try:
                msg, addr = recv_message(sock, clock)
            except socket.timeout:
                # try to drain multicast socket too
                if mcast_sock:
                    try:
                        msg, addr = recv_message(mcast_sock, clock)
                    except Exception:
                        continue
                else:
                    continue
            except OSError:
                break
            except Exception:
                continue

            if not isinstance(msg, dict):
                continue

            mtype = msg.get("type")
            if mtype in (MSG_DISCOVER_RESPONSE, MSG_DISCOVER_REPLY, _DISCOVER_RESPONSE_KEY):
                sid = msg.get("id")
                sip = msg.get("ip") or (addr[0] if addr else None)
                if sid and sip:
                    tup = (sid, sip)
                    if tup not in results:
                        results.append(tup)
    finally:
        try:
            sock.close()
        except Exception:
            pass
        if mcast_sock:
            try:
                mcast_sock.close()
            except Exception:
                pass

    # 4) If no results, attempt a small loopback probe (Windows dev fallback)
    if not results:
        probe_ips = ("127.0.0.2", "127.0.0.3", "127.0.0.1")
        probe_sock = make_udp_socket(bind_ip="", bind_port=0, broadcast=False)
        probe_sock.settimeout(0.2)
        for ip in probe_ips:
            try:
                send_message(probe_sock, (ip, DISCOVERY_BROADCAST_PORT), request, clock)
            except Exception:
                continue

        probe_start = time.time()
        while time.time() - probe_start < 0.3:
            try:
                msg, addr = recv_message(probe_sock, clock)
            except Exception:
                break
            if not isinstance(msg, dict):
                continue
            mtype = msg.get("type")
            if mtype in (MSG_DISCOVER_RESPONSE, MSG_DISCOVER_REPLY, _DISCOVER_RESPONSE_KEY):
                sid = msg.get("id")
                sip = msg.get("ip") or (addr[0] if addr else None)
                if sid and sip:
                    tup = (sid, sip)
                    if tup not in results:
                        results.append(tup)
        try:
            probe_sock.close()
        except Exception:
            pass

    return results
