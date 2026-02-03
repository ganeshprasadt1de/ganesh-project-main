import socket
import time
import json
from typing import List, Tuple
from common import (
    make_udp_socket, recv_message, send_message,
    DISCOVERY_BROADCAST_PORT, MSG_DISCOVER_REQUEST, MSG_DISCOVER_RESPONSE
)

def get_smart_broadcast_ip():
    """
    Automatically calculates the subnet broadcast address (e.g., 10.154.170.255)
    based on the current machine's IP. This fixes the Windows Hotspot issue.
    """
    try:
        # We connect to a public DNS to find out which interface has Internet/Network.
        # We don't actually send data, just checking the route.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        my_ip = s.getsockname()[0]
        s.close()
        
        # Assume standard Home/Hotspot /24 subnet (255.255.255.0)
        # We take "10.154.170.213" -> "10.154.170.255"
        base_ip = my_ip.rsplit('.', 1)[0]
        return f"{base_ip}.255"
    except Exception:
        # Fallback if something goes wrong
        return "255.255.255.255"

def server_discovery_listener(server_id: str, stop_event=None):
    # Bind to 0.0.0.0 to listen on ALL interfaces
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
                # Reply directly to the sender
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
    
    # --- SMART BROADCAST STRATEGY ---
    # 1. Send to the calculated Subnet Broadcast (Fixes Hotspot/Windows)
    subnet_bcast = get_smart_broadcast_ip()
    try:
        sock.sendto(encoded_req, (subnet_bcast, DISCOVERY_BROADCAST_PORT))
    except Exception:
        pass

    # 2. Send to Global Broadcast (Backup for standard routers)
    try:
        sock.sendto(encoded_req, ("<broadcast>", DISCOVERY_BROADCAST_PORT))
    except Exception:
        pass
    # --------------------------------

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