import socket
import time
import json
from typing import List, Tuple
from common import (
    make_udp_socket, recv_message, send_message,
    DISCOVERY_BROADCAST_PORT, MSG_DISCOVER_REQUEST, MSG_DISCOVER_RESPONSE
)

def get_smart_broadcast_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        my_ip = s.getsockname()[0]
        s.close()

        base_ip = my_ip.rsplit('.', 1)[0]
        return f"{base_ip}.255"
    except Exception:
        return "255.255.255.255"

def server_discovery_listener(server_id: str, stop_event=None):
    sock = make_udp_socket(bind_ip="0.0.0.0", bind_port=DISCOVERY_BROADCAST_PORT, broadcast=True)
    sock.settimeout(0.5)
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                msg, addr = recv_message(sock)
            except socket.timeout:
                continue
            except Exception:
                continue

            if msg.get("type") == MSG_DISCOVER_REQUEST:
                reply = {"type": MSG_DISCOVER_RESPONSE, "id": server_id}
                try:
                    send_message(sock, addr, reply)
                except Exception:
                    pass
    finally:
        sock.close()

def client_discover_servers(timeout: float = 2.0) -> List[Tuple[str, str]]:
    results = []
    start = time.time()
    sock = make_udp_socket(bind_ip="0.0.0.0", bind_port=0, broadcast=True)
    sock.settimeout(0.3)
    
    request = {"type": MSG_DISCOVER_REQUEST}
    encoded_req = json.dumps(request).encode()

    subnet_bcast = get_smart_broadcast_ip()
    try:
        sock.sendto(encoded_req, (subnet_bcast, DISCOVERY_BROADCAST_PORT))
    except Exception:
        pass

    try:
        sock.sendto(encoded_req, ("<broadcast>", DISCOVERY_BROADCAST_PORT))
    except Exception:
        pass

    try:
        while time.time() - start < timeout:
            try:
                msg, addr = recv_message(sock)
            except socket.timeout:
                continue
            except Exception:
                continue

            if msg.get("type") == MSG_DISCOVER_RESPONSE:
                sid = msg.get("id")
                sip = addr[0] 
                if sid and sip:
                    tup = (sid, sip)
                    if tup not in results:
                        results.append(tup)
    finally:
        sock.close()
    return results