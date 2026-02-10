import uuid
import threading
import time
import random
import argparse
import sys
from typing import Dict, Tuple, Set

import common
from common import (
    make_udp_socket, send_message, recv_message,
    get_server_control_port, get_client_port, get_discovery_port,
    MSG_GAME_INPUT, MSG_GAME_UPDATE, MSG_HEARTBEAT,
    MSG_ELECTION, MSG_ELECTION_OK, MSG_COORDINATOR, MSG_JOIN
)
from game_state import GameState
from discovery import server_discovery_listener, client_discover_servers


MSG_STATE_SNAPSHOT_REQUEST = "STATE_SNAPSHOT_REQUEST"
MSG_STATE_SNAPSHOT_RESPONSE = "STATE_SNAPSHOT_RESPONSE"


MSG_ROOMS_UPDATE = "ROOMS_UPDATE"
MSG_ACK = "ACK"
MSG_NACK = "NACK"

MSG_CLOSE_ROOM = "CLOSE_ROOM"
MSG_GAME_OVER = "GAME_OVER"
WIN_SCORE = 3

HEARTBEAT_INTERVAL = 1.0
HEARTBEAT_TIMEOUT = 5.0
TICK_INTERVAL = 0.016
ELECTION_COOLDOWN = 3.0


class PongRoom:
    def __init__(self, room_id: int):
        self.room_id = room_id
        self.game_state = GameState()
        self.seq = 0
        self.last_seen_seq = -1

        self.inputs = {}
        self.connected_players = set()
        self.client_addrs: Set[Tuple[str, int]] = set()

    def step(self):
        if len(self.connected_players) >= 2:
            self.game_state.step(
                self.inputs.get(1, 0),
                self.inputs.get(2, 0)
            )
            self.seq += 1

    def apply_input(self, pid: int, direction: int):
        local_pid = ((pid - 1) % 2) + 1
        self.inputs[local_pid] = direction
        self.connected_players.add(local_pid)

    def add_client(self, addr: Tuple[str, int]):
        if addr:
            self.client_addrs.add(addr)

    def snapshot(self):
        return self.game_state.to_dict()

    def restore(self, state_dict):
        self.game_state = GameState.from_dict(state_dict)


class PongServer:
    def __init__(self):
        self.server_id = str(uuid.uuid4())
        self.peers: Dict[str, Tuple[str, int]] = {}

        self.control_port = get_server_control_port()
        self.client_port = get_client_port()
        
        self.control_sock = make_udp_socket(bind_ip="0.0.0.0", bind_port=self.control_port)
        self.client_sock = make_udp_socket(bind_ip="0.0.0.0", bind_port=self.client_port)

        self.leader_id = self.server_id

        self.game_state = GameState()

        self.rooms: Dict[int, PongRoom] = {}

        self.inputs = {1: 0, 2: 0}
        self.connected_players = set()
        self.client_addrs: Set[Tuple[str, int]] = set()

        self.last_heartbeat_from_leader = time.monotonic()
        self.running = True
        self.election_active = False
        self.last_election_time = 0.0
        self.finalize_timer = None
        self.timeout_with_jitter = HEARTBEAT_TIMEOUT

        self._snapshot_lock = threading.Lock()
        self._snapshot_received = None

        self._rooms_snapshot_received = None

        self._last_seen = {}
        
        self.pending_acks: Set[str] = set()
        self._last_control_msg = None
        
        self._recent_election_messages = {}
        self._election_msg_timeout = 2.0
        
        self._forwarding_servers: Set[Tuple[str, int]] = set()

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

    def _room_id_for_player(self, pid: int) -> int:
        return (pid - 1) // 2

    def _get_room(self, room_id: int) -> PongRoom:
        room = self.rooms.get(room_id)
        if room is None:
            room = PongRoom(room_id)
            self.rooms[room_id] = room
        return room
    
    def _request_state_snapshot(self):
        with self._snapshot_lock:
            self._snapshot_received = None

        req = {"type": MSG_STATE_SNAPSHOT_REQUEST}

        for _, addr in list(self.peers.items()):
            send_message(self.control_sock, addr, req)

        start = time.time()
        while time.time() - start < 0.5:
            with self._snapshot_lock:
                if self._snapshot_received:
                    self.game_state = GameState.from_dict(self._snapshot_received)
                    break
            time.sleep(0.01)

    def _request_rooms_snapshot(self):
        with self._snapshot_lock:
            self._rooms_snapshot_received = None

        req = {"type": MSG_STATE_SNAPSHOT_REQUEST, "rooms": True}

        for _, addr in list(self.peers.items()):
            send_message(self.control_sock, addr, req)

        start = time.time()
        while time.time() - start < 0.5:
            with self._snapshot_lock:
                if self._rooms_snapshot_received:
                    for rid, state in self._rooms_snapshot_received.items():
                        room = self._get_room(int(rid))
                        room.restore(state.get("state"))
                        room.seq = state.get("seq", 0)
                        room.last_seen_seq = room.seq
                    break
            time.sleep(0.01)

    def _rooms_snapshot(self):
        snap = {}
        for rid, room in self.rooms.items():
            snap[rid] = {
                "seq": room.seq,
                "state": room.snapshot()
            }
        return snap

    def _add_unknown_peer(self, sid: str, addr: Tuple[str, int]):
        if sid and sid not in self.peers and sid != self.server_id:
            self.peers[sid] = addr

    def _print_membership(self):
        print("\n===== MEMBERSHIP TABLE =====")
        print(f"Self   : {self.server_id}")
        print(f"Leader : {self.leader_id}")
        peers_snapshot = list(self.peers.items())
        if not peers_snapshot:
            print("Peers  : None")
        else:
            for sid, (ip, _) in peers_snapshot:
                role = "LEADER" if sid == self.leader_id else "FOLLOWER"

                now = time.time()
                last = self._last_seen.get(sid, 0)
                if now - last > HEARTBEAT_TIMEOUT * 2:
                    continue

                print(f"{sid} -> {ip} ({role})")
        print("============================\n")

    def start(self):
        print(f"[{self.server_id}] Starting server...")
        self.discovery_thread.start()

        self.control_thread.start()
        self.client_thread.start()
        self.heartbeat_thread.start()
        self.game_thread.start()

        print("Scanning for peers...")
        found = client_discover_servers(timeout=2.0)

        if found:
            print(f"Found peers: {found}")
            for sid, sip, sport in found:
                if sid != self.server_id:
                    self.peers[sid] = (sip, sport)
                    join_msg = {"type": MSG_JOIN, "id": self.server_id, "control_port": self.control_port}
                    send_message(self.control_sock, (sip, sport), join_msg)
        else:
            print("No peers found. I am the first server.")

        if self.peers:
            self._request_state_snapshot()

        if self.peers:
            self._request_rooms_snapshot()

        all_ids = list(self.peers.keys()) + [self.server_id]
        self.leader_id = max(all_ids)
        print(f"[{self.server_id}] Initial leader: {self.leader_id}")

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
        return {sid: addr for sid, addr in list(self.peers.items()) if sid > self.server_id}

    def _finalize_election(self):
        if self.election_active and not self.is_leader():
            self.become_leader()
    
    def _cancel_finalize_timer(self):
        if self.finalize_timer and self.finalize_timer.is_alive():
            self.finalize_timer.cancel()
            self.finalize_timer = None

    def start_election(self):
        self._cancel_finalize_timer()
        
        now = time.monotonic()
        if now - self.last_election_time < ELECTION_COOLDOWN:
            print(f"Election cooldown active. Skipping election.")
            return
        
        if len(self.peers) == 1:
            peer_id = list(self.peers.keys())[0]
            if peer_id > self.server_id:
                print(f"2-server mode: Auto-electing higher peer {peer_id} as leader")
                self.leader_id = peer_id
                self.election_active = False
                self.last_heartbeat_from_leader = time.monotonic()
                self.timeout_with_jitter = HEARTBEAT_TIMEOUT + random.uniform(0, 1.0)
                self._print_membership()
                return
            else:
                print(f"2-server mode: I have higher UUID, becoming leader")
                self.become_leader()
                return
        
        if self.election_active:
            print(f"Election already in progress. Skipping.")
            return

        self.election_active = True
        self.last_election_time = now
        higher = self.higher_peers()

        if not higher:
            self.become_leader()
            return

        msg = {"type": MSG_ELECTION, "candidate": self.server_id}
        for _, addr in higher.items():
            send_message(self.control_sock, addr, msg)

        jitter = random.uniform(0, 0.5)
        self.finalize_timer = threading.Timer(2.5 + jitter, self._finalize_election)
        self.finalize_timer.daemon = True
        self.finalize_timer.start()

    def become_leader(self):
        self._cancel_finalize_timer()
        
        self.leader_id = self.server_id
        self.last_heartbeat_from_leader = time.monotonic()
        self.election_active = False

        print(f"*** I AM LEADER NOW ({self.server_id}) ***")

        if self.peers:
            self._request_state_snapshot()

        if self.peers:
            self._request_rooms_snapshot()

        msg = {"type": MSG_COORDINATOR, "leader_id": self.server_id}

        self.pending_acks = set(self.peers.keys())
        self._last_control_msg = msg
        for _, addr in list(self.peers.items()):
            send_message(self.control_sock, addr, msg)

        hb = {"type": MSG_HEARTBEAT, "server_id": self.server_id}
        for _, addr in list(self.peers.items()):
            send_message(self.control_sock, addr, hb)

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

            if t == MSG_HEARTBEAT:
                sid = msg.get("server_id")
                if sid and sid == self.leader_id:
                    self._last_seen[sid] = time.time()
                    self.last_heartbeat_from_leader = time.monotonic()
                    self.timeout_with_jitter = HEARTBEAT_TIMEOUT + random.uniform(0, 1.0)
                elif sid and self.leader_id is not None and sid > self.leader_id:
                    self._last_seen[sid] = time.time()
                    self.leader_id = sid
                    self.last_heartbeat_from_leader = time.monotonic()
                    self.timeout_with_jitter = HEARTBEAT_TIMEOUT + random.uniform(0, 1.0)
                    print(f"Higher ID peer {sid} is now leader")
                    self._add_unknown_peer(sid, addr)
                elif sid:
                    self._last_seen[sid] = time.time()
                    self._add_unknown_peer(sid, addr)

            elif t == MSG_ACK:
                sid = msg.get("server_id")
                if sid in self.pending_acks:
                    self.pending_acks.discard(sid)
                continue


            elif t == MSG_NACK and self.is_leader():
                rid = msg.get("room_id")
                sid = msg.get("server_id")

                addr = self.peers.get(sid)
                if addr and rid is not None:
                    room = self._get_room(int(rid))

                    resend = {
                        "type": MSG_ROOMS_UPDATE,
                        "rooms": {
                            rid: {
                                "seq": room.seq,
                                "state": room.snapshot()
                            }
                        }
                    }

                    send_message(self.control_sock, addr, resend)

                continue

            elif t == MSG_CLOSE_ROOM:
                rid = msg.get("room_id")
                if rid in self.rooms:
                    print(f"Closing room {rid} (leader request)")
                    self.rooms.pop(rid, None)
                continue



            if t == MSG_STATE_SNAPSHOT_REQUEST:
                reply = {
                    "type": MSG_STATE_SNAPSHOT_RESPONSE,
                    "state": self.game_state.to_dict(),
                    "rooms": self._rooms_snapshot()
                }
                send_message(self.control_sock, addr, reply)
                continue

            elif t == MSG_STATE_SNAPSHOT_RESPONSE:
                with self._snapshot_lock:
                    state = msg.get("state")
                    rooms = msg.get("rooms")

                    if state:
                        self._snapshot_received = state

                    if rooms:
                        self._rooms_snapshot_received = rooms
                continue

            if t == MSG_JOIN:
                new_id = msg.get("id")
                peer_control_port = msg.get("control_port", addr[1])

                if new_id and new_id != self.server_id:
                    if new_id not in self.peers:
                        print(f"Peer Joined: {new_id} from {addr[0]}:{peer_control_port}")
                        self.peers[new_id] = (addr[0], peer_control_port)

                        peers_snapshot = list(self.peers.items())
                        for pid, paddr in peers_snapshot:
                            if pid != new_id:
                                send_message(self.control_sock, (addr[0], peer_control_port), {"type": MSG_JOIN, "id": pid, "control_port": paddr[1]})
                                send_message(
                                    self.control_sock,
                                    paddr,
                                    {"type": MSG_JOIN, "id": new_id, "control_port": peer_control_port}
                                )

                        if self.is_leader() and new_id > self.server_id:
                            print(f"Higher peer {new_id} joined. I am stepping down. Starting Election.")
                            self.start_election()
                        elif not self.is_leader() and new_id > self.leader_id:
                            print(
                                f"Higher peer {new_id} joined (bigger than known leader {self.leader_id}). Starting Election."
                            )
                            self.start_election()

            elif t == MSG_ELECTION:
                candidate = msg.get("candidate")
                
                now = time.time()
                if candidate in self._recent_election_messages:
                    last_seen = self._recent_election_messages[candidate]
                    if now - last_seen < self._election_msg_timeout:
                        continue
                
                self._recent_election_messages[candidate] = now
                
                self._recent_election_messages = {
                    cid: ts for cid, ts in self._recent_election_messages.items()
                    if now - ts < self._election_msg_timeout
                }

                if candidate and self.server_id > candidate:
                    send_message(self.control_sock, addr, {"type": MSG_ELECTION_OK})
                    now_monotonic = time.monotonic()
                    if not self.election_active and (now_monotonic - self.last_election_time >= ELECTION_COOLDOWN):
                        print(f"Received election from {candidate}, starting my own election")
                        self.start_election()

            elif t == MSG_ELECTION_OK:
                self._cancel_finalize_timer()
                self.election_active = False
                print(f"Received ELECTION_OK. A higher-ID peer will become leader.")

            elif t == MSG_COORDINATOR:
                self._cancel_finalize_timer()
                
                leader_id_from_msg = msg.get("leader_id")
                
                if leader_id_from_msg == self.server_id:
                    continue
                
                self.leader_id = leader_id_from_msg
                self.election_active = False
                self.last_heartbeat_from_leader = time.monotonic()
                self.timeout_with_jitter = HEARTBEAT_TIMEOUT + random.uniform(0, 1.0)

                ack = {"type": MSG_ACK, "server_id": self.server_id}
                send_message(self.control_sock, addr, ack)

                print(f"New Leader Elected: {self.leader_id}")
                self._print_membership()

                if self.game_state.score1 > 0 or self.game_state.score2 > 0:
                    leader_addr = self.peers.get(self.leader_id)
                    if leader_addr:
                        send_message(
                            self.control_sock,
                            leader_addr,
                            {"type": MSG_GAME_UPDATE, "state": self.game_state.to_dict()}
                        )

                leader_addr = self.peers.get(self.leader_id)
                if leader_addr and self.rooms:
                    send_message(
                        self.control_sock,
                        leader_addr,
                        {"type": MSG_ROOMS_UPDATE, "rooms": self._rooms_snapshot()}
                    )

            elif t == MSG_GAME_UPDATE:
                if not self.is_leader():
                    if msg.get("state"):
                        self.game_state = GameState.from_dict(msg.get("state"))

                        update_msg = {
                            "type": MSG_GAME_UPDATE,
                            "state": self.game_state.to_dict()
                        }

                        for c in list(self.client_addrs):
                            send_message(self.client_sock, c, update_msg)

                elif self.is_leader():
                    remote_state = msg.get("state", {})

                    if (remote_state.get("score1", 0) > 0 or remote_state.get("score2", 0) > 0):
                        if self.game_state.score1 == 0 and self.game_state.score2 == 0:
                            print("Leader: Recovering game state from survivor!")
                            self.game_state = GameState.from_dict(remote_state)

            elif t == MSG_ROOMS_UPDATE:
                rooms = msg.get("rooms", {})

                if not self.is_leader():
                    for rid, state in rooms.items():
                        room = self._get_room(int(rid))
                        incoming_seq = state.get("seq", -1)


                        expected = room.last_seen_seq + 1
                        if incoming_seq > expected:
                            nack = {
                                "type": MSG_NACK,
                                "server_id": self.server_id,
                                "room_id": rid,
                                "expected_seq": expected
                            }
                            send_message(self.control_sock, addr, nack)


                        if incoming_seq > room.seq:
                            room.seq = incoming_seq
                            room.restore(state.get("state"))

                            room.last_seen_seq = incoming_seq

                        update_msg = {
                            "type": MSG_GAME_UPDATE,
                            "seq": room.seq,
                            "state": room.game_state.to_dict()
                        }

                        for c in list(room.client_addrs):
                            send_message(self.client_sock, c, update_msg)

                elif self.is_leader():
                    for rid, state in rooms.items():
                        room = self._get_room(int(rid))
                        incoming_seq = state.get("seq", -1)
                        if incoming_seq > room.seq:
                            room.restore(state.get("state"))
                            room.seq = incoming_seq
                            print(f"Leader: Recovered room {rid} state from follower (seq={incoming_seq})")

            elif t == MSG_GAME_INPUT and self.is_leader():
                pid = int(msg.get("player", 1))
                direction = int(msg.get("dir", 0))

                room_id = self._room_id_for_player(pid)
                room = self._get_room(room_id)

                local_pid = ((pid - 1) % 2) + 1
                if local_pid not in room.connected_players:
                    print(f"Leader: Player {pid} detected via forwarding (room {room_id})!")

                room.apply_input(pid, direction)
                
                if addr[1] == self.control_port:
                    sender_found = False
                    for sid, (sip, sport) in self.peers.items():
                        if sip == addr[0] and sport == addr[1]:
                            sender_found = True
                            break
                    
                    if not sender_found:
                        print(f"Leader: Auto-discovered forwarding server from {addr[0]}")
                        self._forwarding_servers.add(addr)

    def heartbeat_loop(self):
        while self.running:
            time.sleep(HEARTBEAT_INTERVAL)

            if self.is_leader():
                hb = {"type": MSG_HEARTBEAT, "server_id": self.server_id}
                peers_snapshot = list(self.peers.items())
                if peers_snapshot:
                    for _, addr in peers_snapshot:
                        send_message(self.control_sock, addr, hb)

                if self.pending_acks and self._last_control_msg:
                    for sid in list(self.pending_acks):
                        addr = self.peers.get(sid)
                        if addr:
                            send_message(self.control_sock, addr, self._last_control_msg)

            now = time.time()
            stale = [
                sid for sid, last in list(self._last_seen.items())
                if now - last > HEARTBEAT_TIMEOUT * 2
            ]

            for sid in stale:
                if sid in self.peers:
                    print(f"Pruning dead peer: {sid}")
                    self.peers.pop(sid, None)
                self._last_seen.pop(sid, None)

            if not self.is_leader():
                time_since_last_hb = time.monotonic() - self.last_heartbeat_from_leader
                
                leader_addr = self.peers.get(self.leader_id)
                if leader_addr and time_since_last_hb > HEARTBEAT_INTERVAL * 2:
                    if time_since_last_hb < self.timeout_with_jitter:
                        join_msg = {"type": MSG_JOIN, "id": self.server_id}
                        send_message(self.control_sock, leader_addr, join_msg)
                
                if time_since_last_hb > self.timeout_with_jitter:
                    print("Leader timeout! Starting election.")
                    self.start_election()

    def client_loop(self):
        while self.running:
            try:
                msg, addr = recv_message(self.client_sock)

                if msg.get("type") == MSG_GAME_INPUT:
                    pid = int(msg.get("player", 1))
                    room_id = self._room_id_for_player(pid)
                    room = self._get_room(room_id)

                    room.add_client(addr)

                    if self.is_leader():
                        direction = int(msg.get("dir", 0))

                        local_pid = ((pid - 1) % 2) + 1
                        if local_pid not in room.connected_players:
                            print(f"Leader: Player {pid} detected locally (room {room_id})!")

                        room.apply_input(pid, direction)

                    else:
                        leader_addr = self.peers.get(self.leader_id)
                        if leader_addr:
                            send_message(self.control_sock, leader_addr, msg)
                        else:
                            print(f"Follower: Cannot forward player {pid} input - no leader address!")

            except Exception as e:
                import traceback
                print(f"Error in client_loop: {e}")
                traceback.print_exc()
                continue

    def game_loop(self):
        last_print = time.time()

        while self.running:
            time.sleep(TICK_INTERVAL)

            if not self.is_leader():
                continue

            if time.time() - last_print > 5.0:
                for rid, room in self.rooms.items():
                    print(
                        f"Room {rid} Status: Players Connected: {room.connected_players} (Need 2 to start)"
                    )
                last_print = time.time()

            rooms_to_close = []
            for rid, room in self.rooms.items():

                room.step()

                if (room.game_state.score1 >= WIN_SCORE or
                    room.game_state.score2 >= WIN_SCORE):

                    print(f"Room {rid} finished. Closing room.")

                    game_over_msg = {"type": MSG_GAME_OVER}
                    for c in list(room.client_addrs):
                        send_message(self.client_sock, c, game_over_msg)

                    close_msg = {"type": MSG_CLOSE_ROOM, "room_id": rid}
                    for _, addr in list(self.peers.items()):
                        send_message(self.control_sock, addr, close_msg)

                    rooms_to_close.append(rid)
                    continue

                update = {
                    "type": MSG_GAME_UPDATE,
                    "seq": room.seq,
                    "state": room.game_state.to_dict()
                    
                }

                for c in list(room.client_addrs):
                    send_message(self.client_sock, c, update)

            for rid in rooms_to_close:
                self.rooms.pop(rid, None)

            if (self.peers or self._forwarding_servers) and self.rooms:
                rooms_update = {
                    "type": MSG_ROOMS_UPDATE,
                    "rooms": self._rooms_snapshot()
                }

                for _, addr in list(self.peers.items()):
                    send_message(self.control_sock, addr, rooms_update)
                
                for addr in list(self._forwarding_servers):
                    send_message(self.control_sock, addr, rooms_update)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Distributed Pong Server')
    parser.add_argument('--port-offset', type=int, default=0,
                        help='Port offset for running multiple servers on same machine (default: 0)')
    args = parser.parse_args()
    
    common.PORT_OFFSET = args.port_offset
    
    if args.port_offset > 0:
        print(f"Starting server with port offset {args.port_offset}")
        print(f"  Discovery port: {common.get_discovery_port()}")
        print(f"  Control port: {common.get_server_control_port()}")
        print(f"  Client port: {common.get_client_port()}")
    
    PongServer().start()
