# server.py
# Distributed Pong server with Bully-style leader election
# + Reliable ordered multicast (ACK / NACK / retransmission)

import argparse
import threading
import time
from typing import Dict, Tuple, Set
from collections import defaultdict

from lamport_clock import LamportClock
from common import (
    make_udp_socket, send_message, recv_message,
    SERVER_CONTROL_PORT, CLIENT_PORT,
    MSG_GAME_INPUT, MSG_GAME_UPDATE, MSG_HEARTBEAT,
    MSG_ELECTION, MSG_ELECTION_OK, MSG_COORDINATOR,
    MSG_ACK, MSG_NACK,                    # <<< NEW
)
from game_state import GameState
from discovery import server_discovery_listener

HEARTBEAT_INTERVAL = 1.0
HEARTBEAT_TIMEOUT = 3.0
ELECTION_TIMEOUT = 3.0
TICK_INTERVAL = 0.05
RETRANSMIT_TIMEOUT = 0.5                 # <<< NEW


def id_rank(sid: str) -> int:
    digits = "".join(ch for ch in sid if ch.isdigit())
    return int(digits) if digits else 0


class PongServer:
    def __init__(self, server_id: str, ip: str, peer_ids: Dict[str, Tuple[str, int]]):
        self.server_id = server_id
        self.ip = ip
        self.peers = peer_ids
        self.clock = LamportClock()

        self.control_sock = make_udp_socket(bind_ip=ip, bind_port=SERVER_CONTROL_PORT)
        self.client_sock = make_udp_socket(bind_ip=ip, bind_port=CLIENT_PORT)

        # leader selection
        all_ids = list(self.peers.keys()) + [self.server_id]
        self.sorted_ids = sorted(all_ids, key=id_rank)
        self.leader_id = self.sorted_ids[-1]
        print(f"[{self.server_id}] initial leader is {self.leader_id}")

        self.game_state = GameState()
        self.seq = 0
        self.expected_seq = 1                 # <<< NEW (follower)
        self.pending_updates = {}             # <<< NEW (follower)

        self.inputs = {1: 0, 2: 0}
        self.client_addrs: Set[Tuple[str, int]] = set()

        # ---------- NEW: reliable multicast state ----------
        self.retransmit_buffer = {}           # seq -> update msg
        self.acks = defaultdict(set)          # seq -> set(server_ids)
        self.last_send_time = {}               # seq -> timestamp
        # ---------------------------------------------------

        self.last_heartbeat_from_leader = time.time()
        self.running = True
        self.election_in_progress = False
        self.election_higher_responded = False
        self.election_deadline = None

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

    # -----------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------
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

    # -----------------------------------------------------
    # Election helpers (UNCHANGED)
    # -----------------------------------------------------
    def higher_peers(self):
        my_rank = id_rank(self.server_id)
        return {sid: addr for sid, addr in self.peers.items() if id_rank(sid) > my_rank}

    def start_election(self):
        if self.election_in_progress:
            return
        print(f"[{self.server_id}] starting election")
        self.election_in_progress = True
        self.election_higher_responded = False
        self.election_deadline = time.time() + ELECTION_TIMEOUT

        higher = self.higher_peers()
        if not higher:
            self.become_leader()
            return

        msg = {"type": MSG_ELECTION, "candidate": self.server_id}
        for ip, port in higher.values():
            send_message(self.control_sock, (ip, port), msg, self.clock)

    def become_leader(self):
        self.leader_id = self.server_id
        self.election_in_progress = False
        self.election_deadline = None
        self.last_heartbeat_from_leader = time.time()
        print(f"[{self.server_id}] I am the new leader")

        msg = {"type": MSG_COORDINATOR, "leader_id": self.server_id}
        for ip, port in self.peers.values():
            send_message(self.control_sock, (ip, port), msg, self.clock)

    # -----------------------------------------------------
    # CONTROL LOOP (ACK / NACK HANDLING ADDED)
    # -----------------------------------------------------
    def control_loop(self):
        while self.running:
            try:
                msg, addr = recv_message(self.control_sock, self.clock)
            except Exception:
                continue

            if not isinstance(msg, dict):
                continue

            mtype = msg.get("type")

            if mtype == MSG_GAME_UPDATE and not self.is_leader():
                self.handle_game_update(msg)

            elif mtype == MSG_ACK and self.is_leader():               # <<< NEW
                self.handle_ack(msg)

            elif mtype == MSG_NACK and self.is_leader():              # <<< NEW
                self.handle_nack(msg)

            elif mtype == MSG_HEARTBEAT:
                self.last_heartbeat_from_leader = time.time()

            elif mtype == MSG_ELECTION:
                candidate = msg.get("candidate")
                if candidate and id_rank(self.server_id) > id_rank(candidate):
                    send_message(self.control_sock, addr,
                                 {"type": MSG_ELECTION_OK, "from": self.server_id},
                                 self.clock)
                    if not self.election_in_progress:
                        self.start_election()

            elif mtype == MSG_ELECTION_OK:
                self.election_higher_responded = True

            elif mtype == MSG_COORDINATOR:
                self.leader_id = msg.get("leader_id")
                self.election_in_progress = False
                self.last_heartbeat_from_leader = time.time()

    # -----------------------------------------------------
    # FOLLOWER: ordered delivery + ACK/NACK
    # -----------------------------------------------------
    def handle_game_update(self, msg):
        seq = msg.get("seq")

        if seq == self.expected_seq:
            self.apply_game_update(msg)
            self.send_ack(seq)
            self.expected_seq += 1

            # drain buffer
            while self.expected_seq in self.pending_updates:
                buffered = self.pending_updates.pop(self.expected_seq)
                self.apply_game_update(buffered)
                self.send_ack(self.expected_seq)
                self.expected_seq += 1

        elif seq > self.expected_seq:
            self.pending_updates[seq] = msg
            self.send_nack(self.expected_seq)

        else:
            self.send_ack(seq)

    def send_ack(self, seq):
        send_message(
            self.control_sock,
            self.get_leader_addr(),
            {"type": MSG_ACK, "seq": seq, "from": self.server_id},
            self.clock
        )

    def send_nack(self, missing_seq):
        send_message(
            self.control_sock,
            self.get_leader_addr(),
            {"type": MSG_NACK, "seq": missing_seq, "from": self.server_id},
            self.clock
        )

    # -----------------------------------------------------
    # LEADER: ACK tracking + retransmission
    # -----------------------------------------------------
    def handle_ack(self, msg):
        seq = msg["seq"]
        sender = msg["from"]
        self.acks[seq].add(sender)

        if self.acks[seq] == set(self.peers.keys()):
            self.retransmit_buffer.pop(seq, None)
            self.last_send_time.pop(seq, None)

    def handle_nack(self, msg):
        seq = msg["seq"]
        sender = msg["from"]
        if seq in self.retransmit_buffer:
            send_message(
                self.control_sock,
                self.peers[sender],
                self.retransmit_buffer[seq],
                self.clock
            )

    def retransmission_check(self):
        now = time.time()
        for seq, msg in list(self.retransmit_buffer.items()):
            if now - self.last_send_time[seq] > RETRANSMIT_TIMEOUT:
                for ip, port in self.peers.values():
                    send_message(self.control_sock, (ip, port), msg, self.clock)
                self.last_send_time[seq] = now

    # -----------------------------------------------------
    # GAME LOOP (leader sends ordered reliable updates)
    # -----------------------------------------------------
    def game_loop(self):
        while self.running:
            time.sleep(TICK_INTERVAL)
            if not self.is_leader():
                continue

            self.game_state.step(self.inputs[1], self.inputs[2])
            self.seq += 1

            update = {
                "type": MSG_GAME_UPDATE,
                "seq": self.seq,
                "state": self.game_state.to_dict()
            }

            self.retransmit_buffer[self.seq] = update
            self.acks[self.seq] = set()
            self.last_send_time[self.seq] = time.time()

            for ip, port in self.peers.values():
                send_message(self.control_sock, (ip, port), update, self.clock)

            self.apply_game_update(update)

            for caddr in list(self.client_addrs):
                send_message(self.client_sock, caddr, update, self.clock)

            self.retransmission_check()

    # -----------------------------------------------------
    # CLIENT + HEARTBEAT (UNCHANGED)
    # -----------------------------------------------------
    def apply_game_update(self, update_msg):
        state = update_msg.get("state")
        if state:
            self.game_state = GameState.from_dict(state)

    def client_loop(self):
        while self.running:
            try:
                msg, addr = recv_message(self.client_sock, self.clock)
            except Exception:
                continue

            self.client_addrs.add(addr)
            if msg.get("type") == MSG_GAME_INPUT:
                if self.is_leader():
                    self.inputs[int(msg["player"])] = int(msg["dir"])
                else:
                    send_message(self.control_sock, self.get_leader_addr(), msg, self.clock)

    def heartbeat_loop(self):
        while self.running:
            time.sleep(HEARTBEAT_INTERVAL)
            if self.is_leader():
                hb = {"type": MSG_HEARTBEAT}
                for ip, port in self.peers.values():
                    send_message(self.control_sock, (ip, port), hb, self.clock)
            else:
                if time.time() - self.last_heartbeat_from_leader > HEARTBEAT_TIMEOUT:
                    self.start_election()

    # -----------------------------------------------------
    def is_leader(self):
        return self.leader_id == self.server_id

    def get_leader_addr(self):
        if self.leader_id == self.server_id:
            return (self.ip, SERVER_CONTROL_PORT)
        return self.peers.get(self.leader_id)

# ---------------------------------------------------------
# CLI
# ---------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--ip", default="127.0.0.1")
    ap.add_argument("--peers", nargs="*", default=[])
    args = ap.parse_args()

    peers = {}
    for p in args.peers:
        sid, ip = p.split(":")
        peers[sid] = (ip, SERVER_CONTROL_PORT)

    srv = PongServer(args.id, args.ip, peers)
    srv.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
