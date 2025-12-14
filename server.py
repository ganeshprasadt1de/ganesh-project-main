# server.py
# Distributed Pong server with Bully-style leader election (fixed)

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
ELECTION_TIMEOUT = 3.0
TICK_INTERVAL = 0.05


def id_rank(sid: str) -> int:
    """Numeric rank used for Bully election: extracts digits from id, fallback 0."""
    digits = "".join(ch for ch in sid if ch.isdigit())
    return int(digits) if digits else 0


class PongServer:
    def __init__(self, server_id: str, ip: str, peer_ids: Dict[str, Tuple[str, int]]):
        self.server_id = server_id
        self.ip = ip
        self.peers = peer_ids  # dict: id -> (ip, port)
        self.clock = LamportClock()

        # Sockets (bind to the server's ip on the control and client ports)
        try:
            self.control_sock = make_udp_socket(
                bind_ip=ip, bind_port=SERVER_CONTROL_PORT)
            self.client_sock = make_udp_socket(
                bind_ip=ip, bind_port=CLIENT_PORT)
        except OSError as e:
            print(
                f"[{self.server_id}] CRITICAL ERROR: Could not bind ports on {ip}. {e}")
            raise

        # Determine initial leader (highest-ranked id)
        all_ids = list(self.peers.keys()) + [self.server_id]
        self.sorted_ids = sorted(all_ids, key=id_rank)
        self.leader_id = self.sorted_ids[-1] if self.sorted_ids else self.server_id
        print(f"[{self.server_id}] initial leader is {self.leader_id}")

        self.game_state = GameState()
        self.seq = 0
        self.inputs = {1: 0, 2: 0}
        self.client_addrs: Set[Tuple[str, int]] = set()

        self.last_heartbeat_from_leader = time.time()
        self.running = True
        self.election_in_progress = False
        self.election_higher_responded = False
        self.election_deadline = None

        self.discovery_stop = threading.Event()
        # ---- FIX: use instance attributes (self.server_id, self.ip) not an undefined server_id var
        self.discovery_thread = threading.Thread(
            target=server_discovery_listener, args=(
                self.server_id, self.ip, self.discovery_stop), daemon=True
        )

        self.control_thread = threading.Thread(
            target=self.control_loop, daemon=True)
        self.client_thread = threading.Thread(
            target=self.client_loop, daemon=True)
        self.heartbeat_thread = threading.Thread(
            target=self.heartbeat_loop, daemon=True)
        self.game_thread = threading.Thread(target=self.game_loop, daemon=True)

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
        try:
            self.control_sock.close()
        except Exception:
            pass
        try:
            self.client_sock.close()
        except Exception:
            pass

    def higher_peers(self):
        """Return dict of peers whose id rank is higher than self."""
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
            # No higher peers: become leader immediately
            self.become_leader()
            return

        msg = {"type": MSG_ELECTION, "candidate": self.server_id}
        for sid, (ip, port) in higher.items():
            send_message(self.control_sock, (ip, port), msg, self.clock)

    def become_leader(self):
        # Avoid redundant work if already leader and no election in progress
        if self.leader_id == self.server_id and not self.election_in_progress:
            return

        self.leader_id = self.server_id
        self.election_in_progress = False
        self.election_higher_responded = False
        self.election_deadline = None
        self.last_heartbeat_from_leader = time.time()
        # ---- FIX: stray 'p' + 'rint' bug removed; proper print here
        print(f"[{self.server_id}] I am the new leader")

        # Announce coordinator to all peers
        msg = {"type": MSG_COORDINATOR, "leader_id": self.server_id}
        for sid, (ip, port) in self.peers.items():
            send_message(self.control_sock, (ip, port), msg, self.clock)

    def control_loop(self):
        while self.running:
            try:
                msg, addr = recv_message(self.control_sock, self.clock)
            except OSError:
                break
            except Exception:
                # keep running on transient errors
                continue

            if not isinstance(msg, dict):
                continue

            mtype = msg.get("type")

            # If non-leader received a state update from leader, apply it
            if mtype == MSG_GAME_UPDATE and not self.is_leader():
                self.apply_game_update(msg)

            elif mtype == MSG_HEARTBEAT:
                # leader heartbeat received
                self.last_heartbeat_from_leader = time.time()

            elif mtype == MSG_ELECTION:
                candidate = msg.get("candidate")
                # If I have higher rank than candidate, reply OK and (if not already) start my election
                if candidate and id_rank(self.server_id) > id_rank(candidate):
                    reply = {"type": MSG_ELECTION_OK, "from": self.server_id}
                    send_message(self.control_sock, addr, reply, self.clock)

                    # If I'm already leader inform them; else ensure I start an election if appropriate
                    if self.is_leader():
                        coord_msg = {"type": MSG_COORDINATOR,
                                     "leader_id": self.server_id}
                        send_message(self.control_sock, addr,
                                     coord_msg, self.clock)
                    elif not self.election_in_progress:
                        self.start_election()

            elif mtype == MSG_ELECTION_OK:
                if self.election_in_progress:
                    self.election_higher_responded = True

            elif mtype == MSG_COORDINATOR:
                new_leader = msg.get("leader_id")
                if new_leader:
                    print(
                        f"[{self.server_id}] coordinator: new leader is {new_leader}")
                    self.leader_id = new_leader
                    self.election_in_progress = False
                    self.election_higher_responded = False
                    self.election_deadline = None
                    self.last_heartbeat_from_leader = time.time()

            # other control messages (extensions) can be handled here

    def apply_game_update(self, update_msg: dict):
        # Replace local state with authoritative state from leader (safe simple approach)
        state = update_msg.get("state")
        if state:
            try:
                self.game_state = GameState.from_dict(state)
            except Exception:
                # fall back to ignoring malformed state
                pass

    def client_loop(self):
        print(f"[{self.server_id}] listening for clients on {CLIENT_PORT}")
        while self.running:
            try:
                msg, addr = recv_message(self.client_sock, self.clock)
            except OSError:
                break
            except Exception:
                continue

            # remember client for updates
            if addr:
                self.client_addrs.add(addr)

            if not isinstance(msg, dict):
                continue

            if msg.get("type") == MSG_GAME_INPUT:
                if self.is_leader():
                    try:
                        p = int(msg.get("player", 1))
                        d = int(msg.get("dir", 0))
                        self.inputs[p] = d
                    except Exception:
                        pass
                else:
                    # forward inputs to leader control port
                    lid = self.get_leader_addr()
                    if lid:
                        send_message(self.control_sock, lid, msg, self.clock)

    def heartbeat_loop(self):
        while self.running:
            time.sleep(HEARTBEAT_INTERVAL)
            if self.is_leader():
                hb = {"type": MSG_HEARTBEAT}
                for sid, (ip, port) in self.peers.items():
                    send_message(self.control_sock, (ip, port), hb, self.clock)
            else:
                # if follower and leader heartbeat timed out, start election
                if time.time() - self.last_heartbeat_from_leader > HEARTBEAT_TIMEOUT:
                    print(f"[{self.server_id}] Leader timed out!")
                    self.start_election()

                # election timeout handling
                if self.election_in_progress and self.election_deadline:
                    if time.time() > self.election_deadline:
                        if not self.election_higher_responded:
                            self.become_leader()
                        else:
                            # higher one responded; reset and try again (they should coordinate)
                            self.election_in_progress = False
                            self.start_election()

    def game_loop(self):
        while self.running:
            time.sleep(TICK_INTERVAL)
            if not self.is_leader():
                continue

            # leader advances game and broadcasts authoritative updates
            self.game_state.step(self.inputs.get(1, 0), self.inputs.get(2, 0))
            self.seq += 1
            update = {"type": MSG_GAME_UPDATE, "seq": self.seq,
                      "state": self.game_state.to_dict()}

            # send to other servers (control plane)
            for sid, (ip, port) in self.peers.items():
                send_message(self.control_sock, (ip, port), update, self.clock)

            # apply locally and inform clients
            self.apply_game_update(update)
            for caddr in list(self.client_addrs):
                send_message(self.client_sock, caddr, update, self.clock)

    def is_leader(self) -> bool:
        return self.leader_id == self.server_id

    def get_leader_addr(self):
        if self.leader_id == self.server_id:
            return (self.ip, SERVER_CONTROL_PORT)
        return self.peers.get(self.leader_id)

# ---------- CLI / entrypoint -------------


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--ip", default="127.0.0.1")
    ap.add_argument("--peers", nargs="*", default=[])
    return ap.parse_args()


def main():
    args = parse_args()
    peers = {}
    for p in args.peers:
        # expect entries like "s1:127.0.0.1"
        sid, ip = p.split(":")
        peers[sid] = (ip, SERVER_CONTROL_PORT)
    srv = PongServer(args.id, args.ip, peers)
    try:
        srv.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
