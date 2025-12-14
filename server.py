# server.py
# Distributed Pong Server - Dynamic Discovery & Membership
# - No --peers argument needed
# - Auto-discovers other servers on LAN
# - Dynamically updates peer lists

import argparse
import threading
import time
from typing import Dict, Tuple, Set

from lamport_clock import LamportClock
from common import (
    make_udp_socket, send_message, recv_message,
    SERVER_CONTROL_PORT, CLIENT_PORT,
    MSG_GAME_INPUT, MSG_GAME_UPDATE, MSG_HEARTBEAT,
    MSG_ELECTION, MSG_ELECTION_OK, MSG_COORDINATOR,
    MSG_JOIN  # <--- Make sure you added this to common.py
)
from game_state import GameState
from discovery import server_discovery_listener, client_discover_servers

HEARTBEAT_INTERVAL = 1.0
HEARTBEAT_TIMEOUT = 3.0
TICK_INTERVAL = 0.05

# --------------------------------------------------
def id_rank(sid: str) -> int:
    digits = "".join(ch for ch in sid if ch.isdigit())
    return int(digits) if digits else 0

# --------------------------------------------------
class PongServer:
    def __init__(self, server_id: str, ip: str):
        self.server_id = server_id
        self.ip = ip
        self.peers: Dict[str, Tuple[str, int]] = {}  # Start empty, fill dynamically
        self.clock = LamportClock()

        self.control_sock = make_udp_socket(bind_ip=ip, bind_port=SERVER_CONTROL_PORT)
        self.client_sock = make_udp_socket(bind_ip=ip, bind_port=CLIENT_PORT)

        self.leader_id = self.server_id # Assume I am leader until I find someone higher
        self.game_state = GameState()
        self.seq = 0
        self.last_seq_received = 0

        self.inputs = {1: 0, 2: 0}
        self.client_addrs: Set[Tuple[str, int]] = set()

        self.last_heartbeat_from_leader = time.time()
        self.running = True
        self.election_active = False

        # Discovery Listener (So others can find ME)
        self.discovery_stop = threading.Event()
        self.discovery_thread = threading.Thread(
            target=server_discovery_listener,
            args=(self.server_id, self.ip, self.discovery_stop),
            daemon=True
        )

        self.control_thread = threading.Thread(target=self.control_loop, daemon=True)
        self.client_thread = threading.Thread(target=self.client_loop, daemon=True)
        self.heartbeat_thread = threading.Thread(target=self.heartbeat_loop, daemon=True)
        self.game_thread = threading.Thread(target=self.game_loop, daemon=True)

    # --------------------------------------------------
    def start(self):
        print(f"[{self.server_id}] Starting on {self.ip}...")
        
        # 1. Start listening for discovery probes (so I am visible)
        self.discovery_thread.start()
        
        # 2. Actively look for existing peers
        print(f"[{self.server_id}] Scanning for peers...")
        found = client_discover_servers(timeout=2.0, clock=self.clock)
        
        for sid, sip in found:
            if sid != self.server_id:
                # Add to my list
                self.peers[sid] = (sip, SERVER_CONTROL_PORT)
                print(f"[{self.server_id}] Found peer: {sid} at {sip}")
                
                # 3. Introduce myself (Dynamic Membership)
                # Send a JOIN message so they add ME to THEIR list
                join_msg = {"type": MSG_JOIN, "id": self.server_id, "ip": self.ip}
                send_message(self.control_sock, (sip, SERVER_CONTROL_PORT), join_msg, self.clock)

        # 4. Determine initial leader based on who I found
        all_ids = list(self.peers.keys()) + [self.server_id]
        self.leader_id = max(all_ids, key=id_rank)
        print(f"[{self.server_id}] Initial leader is {self.leader_id}")

        # 5. Start normal operations
        self.control_thread.start()
        self.client_thread.start()
        self.heartbeat_thread.start()
        self.game_thread.start()

    def stop(self):
        self.running = False
        self.discovery_stop.set()
        self.control_sock.close()
        self.client_sock.close()

    # --------------------------------------------------
    def higher_peers(self):
        my_rank = id_rank(self.server_id)
        return {sid: addr for sid, addr in self.peers.items() if id_rank(sid) > my_rank}

    # --------------------------------------------------
    def _finalize_election(self):
        if self.election_active and not self.is_leader():
            print(f"[{self.server_id}] Election timeout. Taking leadership.")
            self.become_leader()

    def start_election(self):
        if self.election_active:
            return

        self.election_active = True
        print(f"[{self.server_id}] Starting election")

        higher = self.higher_peers()
        if not higher:
            self.become_leader()
            return

        msg = {"type": MSG_ELECTION, "candidate": self.server_id}
        for _, addr in higher.items():
            send_message(self.control_sock, addr, msg, self.clock)
        
        # Timeout: if higher peers are dead/unresponsive, take over
        t = threading.Timer(2.5, self._finalize_election)
        t.daemon = True
        t.start()

    def become_leader(self):
        self.leader_id = self.server_id
        self.last_heartbeat_from_leader = time.time()
        self.election_active = False
        print(f"[{self.server_id}] I AM LEADER")

        msg = {"type": MSG_COORDINATOR, "leader_id": self.server_id}
        for _, addr in self.peers.items():
            send_message(self.control_sock, addr, msg, self.clock)

    def is_leader(self):
        return self.leader_id == self.server_id

    # --------------------------------------------------
    def control_loop(self):
        while self.running:
            try:
                msg, addr = recv_message(self.control_sock, self.clock)
            except Exception:
                continue

            if not isinstance(msg, dict):
                continue
            
            t = msg.get("type")

            # --- DYNAMIC MEMBERSHIP: Handle New Joiners ---
            if t == MSG_JOIN:
                new_id = msg.get("id")
                new_ip = msg.get("ip")
                if new_id and new_ip and new_id != self.server_id:
                    if new_id not in self.peers:
                        print(f"[{self.server_id}] New peer joined: {new_id} ({new_ip})")
                        self.peers[new_id] = (new_ip, SERVER_CONTROL_PORT)
                        # If I am leader, I should probably send them a heartbeat or state soon
                        # But the heartbeat loop will catch them automatically next tick
            
            # --- Standard Logic ---
            elif t == MSG_HEARTBEAT:
                # Update heartbeat timestamp
                self.last_heartbeat_from_leader = time.time()
                
                # Robustness: If I receive a heartbeat from someone I don't know, add them!
                sender_id = msg.get("server_id")
                if sender_id and sender_id not in self.peers and sender_id != self.server_id:
                     # (Inferred IP from socket addr)
                     print(f"[{self.server_id}] Discovered peer via heartbeat: {sender_id}")
                     self.peers[sender_id] = (addr[0], SERVER_CONTROL_PORT)

            elif t == MSG_ELECTION:
                candidate = msg.get("candidate")
                if id_rank(self.server_id) > id_rank(candidate):
                    send_message(self.control_sock, addr, {"type": MSG_ELECTION_OK}, self.clock)
                    self.start_election()

            elif t == MSG_COORDINATOR:
                self.leader_id = msg.get("leader_id")
                self.election_active = False
                print(f"[{self.server_id}] New leader: {self.leader_id}")
                self.last_heartbeat_from_leader = time.time()

            elif t == MSG_GAME_UPDATE and not self.is_leader():
                # (Same ACK/NACK logic as before)
                seq = msg.get("seq", 0)
                if self.last_seq_received == 0: self.last_seq_received = seq - 1
                
                if seq == self.last_seq_received + 1:
                    self.last_seq_received = seq
                    send_message(self.control_sock, addr, {"type": "ACK", "seq": seq}, self.clock)
                    if msg.get("state"):
                        self.game_state = GameState.from_dict(msg.get("state"))
                elif seq > self.last_seq_received + 1:
                    send_message(self.control_sock, addr, {"type": "NACK", "expected": self.last_seq_received+1, "got": seq}, self.clock)

    # --------------------------------------------------
    def heartbeat_loop(self):
        while self.running:
            time.sleep(HEARTBEAT_INTERVAL)

            if self.is_leader():
                # Include ID so receivers can learn about me if they just joined
                hb = {"type": MSG_HEARTBEAT, "server_id": self.server_id}
                for _, addr in self.peers.items():
                    send_message(self.control_sock, addr, hb, self.clock)
                continue

            # Follower timeout logic
            if time.time() - self.last_heartbeat_from_leader > HEARTBEAT_TIMEOUT:
                print(f"[{self.server_id}] Leader timed out!")
                self.start_election()

    # --------------------------------------------------
    def client_loop(self):
        # (Same as before)
        while self.running:
            try:
                msg, addr = recv_message(self.client_sock, self.clock)
                if addr: self.client_addrs.add(addr)
                
                if isinstance(msg, dict) and msg.get("type") == MSG_GAME_INPUT:
                    if self.is_leader():
                        self.inputs[int(msg.get("player", 1))] = int(msg.get("dir", 0))
                    else:
                        leader = self.peers.get(self.leader_id)
                        if leader:
                            send_message(self.control_sock, leader, msg, self.clock)
            except Exception:
                continue
    
    # --------------------------------------------------
    def game_loop(self):
        # (Same as before)
        while self.running:
            time.sleep(TICK_INTERVAL)
            if not self.is_leader(): continue

            self.game_state.step(self.inputs.get(1, 0), self.inputs.get(2, 0))
            self.seq += 1
            
            update = {"type": MSG_GAME_UPDATE, "seq": self.seq, "state": self.game_state.to_dict()}
            
            for _, addr in self.peers.items():
                send_message(self.control_sock, addr, update, self.clock)
            for c in list(self.client_addrs):
                send_message(self.client_sock, c, update, self.clock)

# --------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True)
    parser.add_argument("--ip", required=True) 
    # Notice: --peers is gone!
    args = parser.parse_args()

    srv = PongServer(args.id, args.ip)
    try:
        srv.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n[{args.id}] Stopping...")
        srv.stop()

if __name__ == "__main__":
    main()