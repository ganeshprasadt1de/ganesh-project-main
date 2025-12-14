# server.py
# Distributed Pong Server
# FINAL VERIFIED VERSION (PATCHED)
# - Bully election with guard (no spam)
# - Immediate leader takeover
# - Leader never times out itself
# - ACK / NACK logging
# - Late-join sequence initialization
# - [FIX] Added election timeout to prevent deadlocks when higher peers are offline

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
)
from game_state import GameState
from discovery import server_discovery_listener

HEARTBEAT_INTERVAL = 1.0
HEARTBEAT_TIMEOUT = 3.0
TICK_INTERVAL = 0.05


# --------------------------------------------------
def id_rank(sid: str) -> int:
    digits = "".join(ch for ch in sid if ch.isdigit())
    return int(digits) if digits else 0


# --------------------------------------------------
class PongServer:
    def __init__(self, server_id: str, ip: str, peers: Dict[str, Tuple[str, int]]):
        self.server_id = server_id
        self.ip = ip
        self.peers = peers
        self.clock = LamportClock()

        self.control_sock = make_udp_socket(bind_ip=ip, bind_port=SERVER_CONTROL_PORT)
        self.client_sock = make_udp_socket(bind_ip=ip, bind_port=CLIENT_PORT)

        # Initial leader = highest ID
        all_ids = list(peers.keys()) + [server_id]
        self.leader_id = max(all_ids, key=id_rank)
        print(f"[{self.server_id}] initial leader is {self.leader_id}")

        self.game_state = GameState()
        self.seq = 0
        self.last_seq_received = 0

        self.inputs = {1: 0, 2: 0}
        self.client_addrs: Set[Tuple[str, int]] = set()

        self.last_heartbeat_from_leader = time.time()
        self.running = True

        # 🔒 Election guard
        self.election_active = False

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
        print(f"[{self.server_id}] starting on {self.ip}")
        self.discovery_thread.start()
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
    # [FIX] Helper to forcibly take leadership if election times out
    def _finalize_election(self):
        """
        Called by a timer. If the election is still active (meaning no higher
        peer took over), we assume they are dead and take leadership.
        """
        if self.election_active and not self.is_leader():
            print(f"[{self.server_id}] Election timeout: higher peers dead. I am taking over.")
            self.become_leader()

    # [FIX] Updated start_election with timeout timer
    def start_election(self):
        if self.election_active:
            return

        self.election_active = True
        print(f"[{self.server_id}] starting election")

        higher = self.higher_peers()

        # No higher peers → immediate leader
        if not higher:
            self.become_leader()
            return

        # Send election requests to higher peers
        msg = {"type": MSG_ELECTION, "candidate": self.server_id}
        for _, addr in higher.items():
            send_message(self.control_sock, addr, msg, self.clock)
        
        # Start a timer to take over if they don't reply in 2.0 seconds
        election_timeout = threading.Timer(2.0, self._finalize_election)
        election_timeout.daemon = True
        election_timeout.start()

    # --------------------------------------------------
    def become_leader(self):
        self.leader_id = self.server_id
        self.last_heartbeat_from_leader = time.time()
        self.election_active = False
        print(f"[{self.server_id}] I am the new leader")

        msg = {"type": MSG_COORDINATOR, "leader_id": self.server_id}
        for _, addr in self.peers.items():
            send_message(self.control_sock, addr, msg, self.clock)

    # --------------------------------------------------
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

            if t == MSG_HEARTBEAT:
                self.last_heartbeat_from_leader = time.time()

            elif t == MSG_ELECTION:
                candidate = msg.get("candidate")
                # Bully logic: If my rank is higher, I bully them back
                if id_rank(self.server_id) > id_rank(candidate):
                    send_message(
                        self.control_sock,
                        addr,
                        {"type": MSG_ELECTION_OK},
                        self.clock
                    )
                    self.start_election()

            elif t == MSG_COORDINATOR:
                self.leader_id = msg.get("leader_id")
                self.election_active = False
                print(f"[{self.server_id}] new leader is {self.leader_id}")
                self.last_heartbeat_from_leader = time.time()

            elif t == MSG_GAME_UPDATE and not self.is_leader():
                seq = msg.get("seq", 0)

                # Late-join initialization (prevents NACK spam)
                if self.last_seq_received == 0:
                    self.last_seq_received = seq - 1

                if seq == self.last_seq_received + 1:
                    # print(f"[{self.server_id}] ACK seq={seq}")
                    self.last_seq_received = seq

                    send_message(
                        self.control_sock,
                        addr,
                        {"type": "ACK", "seq": seq},
                        self.clock
                    )

                    state = msg.get("state")
                    if state:
                        self.game_state = GameState.from_dict(state)

                elif seq > self.last_seq_received + 1:
                    print(
                        f"[{self.server_id}] NACK expected={self.last_seq_received + 1} got={seq}"
                    )

                    send_message(
                        self.control_sock,
                        addr,
                        {
                            "type": "NACK",
                            "expected": self.last_seq_received + 1,
                            "got": seq
                        },
                        self.clock
                    )
            
            # (Optional) Log ACKs/NACKs if debugging leader logic
            elif t == "ACK":
                pass 
            elif t == "NACK":
                print(
                    f"[{self.server_id}] received NACK from {addr} "
                    f"(expected={msg.get('expected')}, got={msg.get('got')})"
                )

    # --------------------------------------------------
    def client_loop(self):
        while self.running:
            try:
                msg, addr = recv_message(self.client_sock, self.clock)
            except Exception:
                continue

            if addr:
                self.client_addrs.add(addr)

            if not isinstance(msg, dict):
                continue

            if msg.get("type") == MSG_GAME_INPUT:
                if self.is_leader():
                    p = int(msg.get("player", 1))
                    d = int(msg.get("dir", 0))
                    self.inputs[p] = d
                else:
                    # Forward to leader if we know who it is
                    leader = self.peers.get(self.leader_id)
                    if leader:
                        send_message(self.control_sock, leader, msg, self.clock)

    # --------------------------------------------------
    def heartbeat_loop(self):
        while self.running:
            time.sleep(HEARTBEAT_INTERVAL)

            if self.is_leader():
                # Leader sends heartbeats only
                hb = {"type": MSG_HEARTBEAT}
                for _, addr in self.peers.items():
                    send_message(self.control_sock, addr, hb, self.clock)
                continue  # leader never times out itself

            # Followers only
            if time.time() - self.last_heartbeat_from_leader > HEARTBEAT_TIMEOUT:
                print(f"[{self.server_id}] leader timed out")
                self.start_election()

    # --------------------------------------------------
    def game_loop(self):
        while self.running:
            time.sleep(TICK_INTERVAL)
            if not self.is_leader():
                continue

            self.game_state.step(
                self.inputs.get(1, 0),
                self.inputs.get(2, 0)
            )
            self.seq += 1

            update = {
                "type": MSG_GAME_UPDATE,
                "seq": self.seq,
                "state": self.game_state.to_dict()
            }

            for _, addr in self.peers.items():
                send_message(self.control_sock, addr, update, self.clock)

            for c in list(self.client_addrs):
                send_message(self.client_sock, c, update, self.clock)


# --------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True)
    parser.add_argument("--ip", required=True)
    parser.add_argument("--peers", nargs="*", default=[])
    args = parser.parse_args()

    peers = {}
    for p in args.peers:
        try:
            sid, ip = p.split(":")
            peers[sid] = (ip, SERVER_CONTROL_PORT)
        except ValueError:
            print(f"Skipping invalid peer format: {p} (use id:ip)")

    srv = PongServer(args.id, args.ip, peers)
    try:
        srv.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n[{args.id}] Stopping server...")
        srv.stop()


if __name__ == "__main__":
    main()