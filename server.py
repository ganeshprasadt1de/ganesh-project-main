import uuid
import threading
import time
from typing import Dict, Tuple, Set

from common import (
    make_udp_socket, send_message, recv_message,
    SERVER_CONTROL_PORT, CLIENT_PORT,
    MSG_GAME_INPUT, MSG_GAME_UPDATE, MSG_HEARTBEAT,
    MSG_ELECTION, MSG_ELECTION_OK, MSG_COORDINATOR, MSG_JOIN
)
from game_state import GameState
from discovery import server_discovery_listener, client_discover_servers


# ================================
# NEW: snapshot sync message types
# ================================
MSG_STATE_SNAPSHOT_REQUEST = "STATE_SNAPSHOT_REQUEST"
MSG_STATE_SNAPSHOT_RESPONSE = "STATE_SNAPSHOT_RESPONSE"
# ================================


HEARTBEAT_INTERVAL = 1.0
HEARTBEAT_TIMEOUT = 3.0
TICK_INTERVAL = 0.05


class PongServer:
    def __init__(self):
        self.server_id = str(uuid.uuid4())
        self.peers: Dict[str, Tuple[str, int]] = {}
        
        self.control_sock = make_udp_socket(bind_ip="0.0.0.0", bind_port=SERVER_CONTROL_PORT)
        self.client_sock = make_udp_socket(bind_ip="0.0.0.0", bind_port=CLIENT_PORT)

        self.leader_id = self.server_id
        self.game_state = GameState()
        
        self.inputs = {1: 0, 2: 0}
        self.connected_players = set()
        self.client_addrs: Set[Tuple[str, int]] = set()

        self.last_heartbeat_from_leader = time.time()
        self.running = True
        self.election_active = False

        # ================================
        # NEW: snapshot sync storage
        # ================================
        self._snapshot_lock = threading.Lock()
        self._snapshot_received = None
        # ================================

        self.discovery_stop = threading.Event()
        self.discovery_thread = threading.Thread(
            target=server_discovery_listener,
            args=(self.server_id, self.discovery_stop),
            daemon=True
        )

        self.control_thread = threading.Thread(target=self.control_loop, daemon=True)
        self.client_thread = threading.Thread(target=self.client_loop, daemon=True)
        self.heartbeat_thread = threading.Thread(target=self.heartbeat_loop, daemon=True)
        self.game_thread = threading.Thread(target=self.game_loop, daemon=True)

    # ================================
    # NEW: snapshot request helper
    # ================================
    def _request_state_snapshot(self):
        """
        Ask peers for their current game state.
        First response wins. Very small + non-invasive.
        """
        with self._snapshot_lock:
            self._snapshot_received = None

        req = {"type": MSG_STATE_SNAPSHOT_REQUEST}

        for _, addr in self.peers.items():
            send_message(self.control_sock, addr, req)

        start = time.time()
        while time.time() - start < 0.5:
            with self._snapshot_lock:
                if self._snapshot_received:
                    self.game_state = GameState.from_dict(self._snapshot_received)
                    break
            time.sleep(0.01)
    # ================================

    # ================================
    # NEW: MEMBERSHIP TABLE PRINTER
    # ================================
    def _print_membership(self):
        print("\n===== MEMBERSHIP TABLE =====")
        print(f"Self   : {self.server_id}")
        print(f"Leader : {self.leader_id}")
        if not self.peers:
            print("Peers  : None")
        else:
            for sid, (ip, _) in self.peers.items():
                role = "LEADER" if sid == self.leader_id else "FOLLOWER"
                print(f"{sid} -> {ip} ({role})")
        print("============================\n")
    # ================================

    def start(self):
        print(f"[{self.server_id}] Starting server...")
        self.discovery_thread.start()
        
        print("Scanning for peers...")
        found = client_discover_servers(timeout=2.0)
        
        if found:
            print(f"Found peers: {found}")
            for sid, sip in found:
                if sid != self.server_id:
                    self.peers[sid] = (sip, SERVER_CONTROL_PORT)
                    join_msg = {"type": MSG_JOIN, "id": self.server_id}
                    send_message(self.control_sock, (sip, SERVER_CONTROL_PORT), join_msg)
        else:
            print("No peers found. I am the first server.")

        if self.peers:
            self._request_state_snapshot()

        all_ids = list(self.peers.keys()) + [self.server_id]
        self.leader_id = max(all_ids)
        print(f"[{self.server_id}] Initial leader: {self.leader_id}")

        self.control_thread.start()
        self.client_thread.start()
        self.heartbeat_thread.start()
        self.game_thread.start()

        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        self.running = False
        self.discovery_stop.set()
        self.control_sock.close()
        self.client_sock.close()

    def higher_peers(self):
        return {sid: addr for sid, addr in self.peers.items() if sid > self.server_id}

    def _finalize_election(self):
        if self.election_active and not self.is_leader():
            self.become_leader()

    def start_election(self):
        if self.election_active: 
            return
        self.election_active = True
        higher = self.higher_peers()
        if not higher:
            self.become_leader()
            return
        msg = {"type": MSG_ELECTION, "candidate": self.server_id}
        for _, addr in higher.items():
            send_message(self.control_sock, addr, msg)
        t = threading.Timer(2.5, self._finalize_election)
        t.daemon = True
        t.start()

    def become_leader(self):
        self.leader_id = self.server_id
        self.last_heartbeat_from_leader = time.time()
        self.election_active = False

        print(f"*** I AM LEADER NOW ({self.server_id}) ***")

        # ================================
        # NEW: pull latest game snapshot BEFORE acting as leader
        # prevents score reset when higher UUID joins
        # ================================
        if self.peers:
            self._request_state_snapshot()
        # ================================

        msg = {"type": MSG_COORDINATOR, "leader_id": self.server_id}
        for _, addr in self.peers.items():
            send_message(self.control_sock, addr, msg)

        # NEW: print membership after election finishes
        self._print_membership()

    def is_leader(self):
        return self.leader_id == self.server_id

    def control_loop(self):
        while self.running:
            try:
                msg, addr = recv_message(self.control_sock)
            except Exception:
                continue

            t = msg.get("type")

            # ================================
            # NEW: snapshot request handler
            # follower replies with its state
            # ================================
            if t == MSG_STATE_SNAPSHOT_REQUEST:
                reply = {
                    "type": MSG_STATE_SNAPSHOT_RESPONSE,
                    "state": self.game_state.to_dict()
                }
                send_message(self.control_sock, addr, reply)
                continue

            elif t == MSG_STATE_SNAPSHOT_RESPONSE:
                with self._snapshot_lock:
                    state = msg.get("state")

                    if state:
                        s1 = state.get("score1", 0)
                        s2 = state.get("score2", 0)

                        # accept non-empty snapshot first (prevents reset from fresh server)
                        if (s1 != 0 or s2 != 0) or self._snapshot_received is None:
                            self._snapshot_received = state
                continue
            # ================================

            if t == MSG_JOIN:
                new_id = msg.get("id")
                if new_id and new_id != self.server_id:
                    if new_id not in self.peers:
                        print(f"Peer Joined: {new_id} from {addr[0]}")
                        self.peers[new_id] = (addr[0], SERVER_CONTROL_PORT)
                        for pid, paddr in self.peers.items():
                            if pid != new_id:
                                send_message(self.control_sock, addr, {"type": MSG_JOIN, "id": pid})
                                send_message(self.control_sock, (paddr[0], SERVER_CONTROL_PORT), {"type": MSG_JOIN, "id": new_id})

                    if new_id > self.server_id:
                        print(f"Higher peer {new_id} detected (higher than me). Starting Election.")
                        self.start_election()

                    if self.is_leader() and new_id > self.server_id:
                        print(f"Higher peer {new_id} joined. I am stepping down. Starting Election.")
                        self.start_election()

                    elif not self.is_leader() and new_id > self.leader_id:
                        print(f"Higher peer {new_id} joined (bigger than known leader {self.leader_id}). Starting Election.")
                        self.start_election()

            elif t == MSG_HEARTBEAT:
                self.last_heartbeat_from_leader = time.time()
                sender_id = msg.get("server_id")
                
                if sender_id and sender_id > self.leader_id:
                    self.leader_id = sender_id
                    
                if sender_id and sender_id not in self.peers and sender_id != self.server_id:
                    self.peers[sender_id] = (addr[0], SERVER_CONTROL_PORT)

            elif t == MSG_ELECTION:
                candidate = msg.get("candidate")
                if candidate and self.server_id > candidate:
                    send_message(self.control_sock, addr, {"type": MSG_ELECTION_OK})
                    self.start_election()

            elif t == MSG_COORDINATOR:
                self.leader_id = msg.get("leader_id")
                self.election_active = False
                self.last_heartbeat_from_leader = time.time()
                print(f"New Leader Elected: {self.leader_id}")

                # NEW: print membership after election finishes
                self._print_membership()

                # FIX 1: SURVIVOR PUSH - If I have a score, push it to the new leader
                if self.game_state.score1 > 0 or self.game_state.score2 > 0:
                    leader_addr = self.peers.get(self.leader_id)
                    if leader_addr:
                        send_message(self.control_sock, leader_addr, {"type": MSG_GAME_UPDATE, "state": self.game_state.to_dict()})

            elif t == MSG_GAME_UPDATE:
                # Case A: Follower updates normally
                if not self.is_leader():
                    if msg.get("state"):
                        self.game_state = GameState.from_dict(msg.get("state"))
                        update_msg = {"type": MSG_GAME_UPDATE, "state": self.game_state.to_dict()}
                        for c in list(self.client_addrs):
                            send_message(self.client_sock, c, update_msg)

                # FIX 2: Leader accepts recovery state if currently fresh (0-0)
                elif self.is_leader():
                    remote_state = msg.get("state", {})
                    if (remote_state.get("score1", 0) > 0 or remote_state.get("score2", 0) > 0):
                        if self.game_state.score1 == 0 and self.game_state.score2 == 0:
                            print(f"Leader: Recovering game state from survivor!")
                            self.game_state = GameState.from_dict(remote_state)

            # ================================
            # LEADER-ONLY membership (unchanged logic,
            # but now explicitly authoritative)
            # ================================
            elif t == MSG_GAME_INPUT and self.is_leader():
                pid = int(msg.get("player", 1))
                if pid not in self.connected_players:
                    print(f"Leader: Player {pid} detected via forwarding!")
                    self.connected_players.add(pid)
                self.inputs[pid] = int(msg.get("dir", 0))

    def heartbeat_loop(self):
        while self.running:
            time.sleep(HEARTBEAT_INTERVAL)
            if self.is_leader():
                hb = {"type": MSG_HEARTBEAT, "server_id": self.server_id}
                for _, addr in self.peers.items():
                    send_message(self.control_sock, addr, hb)
            else:
                if time.time() - self.last_heartbeat_from_leader > HEARTBEAT_TIMEOUT:
                    print("Leader timeout! Starting election.")
                    self.start_election()

    def client_loop(self):
        while self.running:
            try:
                msg, addr = recv_message(self.client_sock)

                # follower keeps addresses only for sending updates
                if addr and addr not in self.client_addrs:
                    self.client_addrs.add(addr)
                    print(f"New Client Connected: {addr}")

                if msg.get("type") == MSG_GAME_INPUT:
                    pid = int(msg.get("player", 1))

                    if self.is_leader():
                        # leader is single source of truth
                        if pid not in self.connected_players:
                            print(f"Leader: Player {pid} detected locally!")
                            self.connected_players.add(pid)
                        self.inputs[pid] = int(msg.get("dir", 0))
                    else:
                        # followers ONLY forward (no local membership tracking)
                        leader_addr = self.peers.get(self.leader_id)
                        if leader_addr:
                            send_message(self.control_sock, leader_addr, msg)

            except Exception:
                continue
    
    def game_loop(self):
        last_print = time.time()
        while self.running:
            time.sleep(TICK_INTERVAL)
            if not self.is_leader():
                continue

            if time.time() - last_print > 5.0:
                print(f"Game Status: Players Connected: {self.connected_players} (Need 2 to start)")
                last_print = time.time()

            if len(self.connected_players) >= 2:
                self.game_state.step(self.inputs.get(1, 0), self.inputs.get(2, 0))

            update = {"type": MSG_GAME_UPDATE, "state": self.game_state.to_dict()}

            for _, addr in self.peers.items():
                send_message(self.control_sock, addr, update)
            for c in list(self.client_addrs):
                send_message(self.client_sock, c, update)


if __name__ == "__main__":
    PongServer().start()

