import uuid
import random
import threading
import time
from typing import Dict, Tuple, Set

from common import (
    make_udp_socket, send_message, recv_message,
    SERVER_CONTROL_PORT, CLIENT_PORT,
    MSG_GAME_INPUT, MSG_GAME_UPDATE, MSG_ROOM_READY, MSG_HEARTBEAT,
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
HEARTBEAT_TIMEOUT = 3.0
TICK_INTERVAL = 0.016


class PongRoom:
    def __init__(self, room_id: int):
        self.room_id = room_id
        self.game_state = GameState()
        self.seq = 0
        self.last_seen_seq = -1
        self.ready_sent = False

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
        self.server_rank = self._rank_for_id(self.server_id)
        self.peers: Dict[str, Tuple[str, int]] = {}

        self.control_sock = make_udp_socket(bind_ip="0.0.0.0", bind_port=SERVER_CONTROL_PORT)
        self.client_sock = make_udp_socket(bind_ip="0.0.0.0", bind_port=CLIENT_PORT)

        self.leader_id = self.server_id
        self.leader_rank = self.server_rank

        self.game_state = GameState()

        self.rooms: Dict[int, PongRoom] = {}

        self.inputs = {1: 0, 2: 0}
        self.connected_players = set()
        self.client_addrs: Set[Tuple[str, int]] = set()

        self.last_heartbeat_from_leader = time.time()
        self.running = True
        self.election_active = False
        self.election_ok_received = False
        self.leader_addr = None
        self._last_logged_leader = None

        self._snapshot_lock = threading.Lock()
        self._snapshot_received = None

        self._rooms_snapshot_received = None

        self._last_seen = {}
        self.pending_acks: Set[str] = set()
        self._last_control_msg = None
        self._election_jitter_timer: threading.Timer = None

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

    def _rank_for_id(self, sid: str) -> int:
        try:
            return uuid.UUID(sid).int
        except Exception:
            return 0

    def _is_higher(self, a_id: str, a_rank: int, b_id: str, b_rank: int) -> bool:
        if a_rank != b_rank:
            return a_rank > b_rank
        return a_id > b_id

    def _get_room(self, room_id: int) -> PongRoom:
        room = self.rooms.get(room_id)
        if room is None:
            room = PongRoom(room_id)
            self.rooms[room_id] = room
        return room

    def _send_room_ready(self, room: PongRoom, targets=None):
        if len(room.connected_players) < 2:
            return

        msg = {"type": MSG_ROOM_READY, "room_id": room.room_id}
        if targets is None:
            targets = list(room.client_addrs)

        for c in targets:
            send_message(self.client_sock, c, msg)

        room.ready_sent = True
    
    def _request_state_snapshot(self):
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

    def _request_rooms_snapshot(self):
        with self._snapshot_lock:
            self._rooms_snapshot_received = None

        req = {"type": MSG_STATE_SNAPSHOT_REQUEST, "rooms": True}

        for _, addr in self.peers.items():
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

    def _print_membership(self):
        if self.leader_id == self._last_logged_leader:
            return
        self._last_logged_leader = self.leader_id
        print(f"[{self.server_id}] Current leader: {self.leader_id}")
        return
        print("\n===== MEMBERSHIP TABLE =====")
        print(f"Self   : {self.server_id}")
        print(f"Leader : {self.leader_id}")
        if not self.peers:
            print("Peers  : None")
        else:
            for sid, (ip, _) in self.peers.items():
                role = "LEADER" if sid == self.leader_id else "FOLLOWER"

                now = time.time()
                last = self._last_seen.get(sid, 0)
                if now - last > HEARTBEAT_TIMEOUT * 2:
                    continue

                print(f"{sid} -> {ip} ({role})")
        print("============================\n")

    def _log_leader_set(self):
        if self.leader_id == self._last_logged_leader:
            return
        self._last_logged_leader = self.leader_id
        print(f"[{self.server_id}] Leader confirmed: {self.leader_id}")

    def start(self):
        print(f"[{self.server_id}] Starting server...")
        self.discovery_thread.start()

        print("Scanning for peers...")
        found = client_discover_servers(timeout=2.0)

        if found:
            peers_found = [(sid, sip) for sid, sip in found if sid != self.server_id]
            if peers_found:
                print(f"Found peers: {peers_found}")
            else:
                print("Found peers: []")
            for sid, sip in peers_found:
                self.peers[sid] = (sip, SERVER_CONTROL_PORT)
                join_msg = {"type": MSG_JOIN, "id": self.server_id, "rank": self.server_rank}
                send_message(self.control_sock, (sip, SERVER_CONTROL_PORT), join_msg)
        else:
            print("No peers found. I am the first server.")

        if self.peers:
            self._request_state_snapshot()

        if self.peers:
            self._request_rooms_snapshot()

        if self.peers:
            self.leader_id = None
            self.leader_rank = 0
            self.leader_addr = None
            print(f"[{self.server_id}] Initial leader: unknown (waiting for announcement)")
        else:
            self.leader_id = self.server_id
            self.leader_rank = self.server_rank
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
        higher = {}
        for sid, addr in self.peers.items():
            if self._is_higher(sid, self._rank_for_id(sid), self.server_id, self.server_rank):
                higher[sid] = addr
        return higher

    def _finalize_election(self):
        if not self.election_active:
            return

        if not self.election_ok_received and not self.is_leader():
            self.become_leader()
            return

        if self.election_ok_received and not self.is_leader():
            self.election_active = False
            self.start_election()

    def _jittered_start_election(self):
        self._election_jitter_timer = None
        if self.is_leader() or self.election_active:
            return
        if (self.leader_id is not None
                and time.time() - self.last_heartbeat_from_leader <= HEARTBEAT_TIMEOUT):
            return
        print("Starting election after jitter delay.")
        self.start_election()

    def _cancel_election_jitter(self):
        if self._election_jitter_timer is not None:
            self._election_jitter_timer.cancel()
            self._election_jitter_timer = None

    def start_election(self):
        if self.election_active:
            return

        self._cancel_election_jitter()
        self.election_active = True
        self.election_ok_received = False
        higher = self.higher_peers()

        if not higher:
            self.become_leader()
            return

        msg = {"type": MSG_ELECTION, "candidate": self.server_id, "candidate_rank": self.server_rank}
        for _, addr in higher.items():
            send_message(self.control_sock, addr, msg)

        t = threading.Timer(2.5, self._finalize_election)
        t.daemon = True
        t.start()

    def become_leader(self):
        for sid in list(self.peers):
            sid_rank = self._rank_for_id(sid)
            if self._is_higher(sid, sid_rank, self.server_id, self.server_rank):
                last = self._last_seen.get(sid, 0)
                if time.time() - last < HEARTBEAT_TIMEOUT * 2:
                    print(f"Yielding leadership to higher-ranked peer {sid[:8]}")
                    self.election_active = False
                    return

        prev_leader = self.leader_id
        self.leader_id = self.server_id
        self.leader_rank = self.server_rank
        self.last_heartbeat_from_leader = time.time()
        self.election_active = False
        self.election_ok_received = False
        self.leader_addr = None

        if prev_leader != self.server_id:
            print(f"*** I AM LEADER NOW ({self.server_id}) ***")
            self._log_leader_set()

        if self.peers:
            self._request_state_snapshot()

        if self.peers:
            self._request_rooms_snapshot()

        msg = {"type": MSG_COORDINATOR, "leader_id": self.server_id, "leader_rank": self.server_rank}

        self.pending_acks = set(self.peers.keys())
        self._last_control_msg = msg
        for _, addr in self.peers.items():
            send_message(self.control_sock, addr, msg)

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
                sid_rank = msg.get("server_rank")
                if sid:
                    self._last_seen[sid] = time.time()
                    if sid not in self.peers and sid != self.server_id:
                        self.peers[sid] = (addr[0], SERVER_CONTROL_PORT)
                if msg.get("is_leader") and sid:
                    if sid_rank is None:
                        sid_rank = self._rank_for_id(sid)
                    if sid == self.leader_id:
                        self.last_heartbeat_from_leader = time.time()
                    accept = False
                    if self.leader_id is None:
                        accept = True
                    elif self._is_higher(sid, sid_rank, self.leader_id, self.leader_rank):
                        accept = True
                    if accept:
                        prev_leader = self.leader_id
                        if self.is_leader() and sid != self.server_id:
                            print(f"Stepping down: higher leader {sid[:8]} detected via heartbeat")
                        self.leader_id = sid
                        self.leader_rank = sid_rank
                        self.leader_addr = (addr[0], SERVER_CONTROL_PORT)
                        self.election_active = False
                        self.election_ok_received = False
                        self.last_heartbeat_from_leader = time.time()
                        self._cancel_election_jitter()
                        if prev_leader != self.leader_id:
                            self._print_membership()
                continue

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
                new_rank = msg.get("rank")

                if new_id and new_id != self.server_id:
                    if new_rank is None:
                        new_rank = self._rank_for_id(new_id)

                    join_ip = addr[0]
                    stale = [
                        sid for sid, (ip, _) in list(self.peers.items())
                        if ip == join_ip and sid != new_id
                    ]
                    for sid in stale:
                        print(f"Evicting ghost peer {sid[:8]} (same IP as {new_id[:8]})")
                        self.peers.pop(sid, None)
                        self._last_seen.pop(sid, None)

                    if new_id not in self.peers:
                        print(f"Peer Joined: {new_id} from {join_ip}")
                        self.peers[new_id] = (join_ip, SERVER_CONTROL_PORT)

                        for pid, paddr in self.peers.items():
                            if pid != new_id:
                                send_message(
                                    self.control_sock,
                                    addr,
                                    {"type": MSG_JOIN, "id": pid, "rank": self._rank_for_id(pid)}
                                )
                                send_message(
                                    self.control_sock,
                                    (paddr[0], SERVER_CONTROL_PORT),
                                    {"type": MSG_JOIN, "id": new_id, "rank": new_rank}
                                )

                    if self.is_leader():
                        print(f"Informing {new_id} about leader {self.leader_id}.")
                        send_message(
                            self.control_sock,
                            addr,
                            {"type": MSG_COORDINATOR, "leader_id": self.leader_id, "leader_rank": self.leader_rank}
                        )
                    else:
                        leader_addr = self.leader_addr or self.peers.get(self.leader_id)
                        if leader_addr and self.leader_id:
                            send_message(
                                self.control_sock,
                                leader_addr,
                                {"type": MSG_JOIN, "id": new_id, "rank": new_rank}
                            )

            elif t == MSG_ELECTION:
                candidate = msg.get("candidate")
                candidate_rank = msg.get("candidate_rank")

                if candidate:
                    if candidate_rank is None:
                        candidate_rank = self._rank_for_id(candidate)
                    if self._is_higher(self.server_id, self.server_rank, candidate, candidate_rank):
                        send_message(self.control_sock, addr, {"type": MSG_ELECTION_OK})
                        if self.is_leader():
                            coord = {"type": MSG_COORDINATOR,
                                     "leader_id": self.server_id,
                                     "leader_rank": self.server_rank}
                            send_message(self.control_sock, addr, coord)
                        elif (self.leader_id is None) and not self.election_active:
                            self.start_election()

            elif t == MSG_ELECTION_OK:
                self.election_ok_received = True

            elif t == MSG_COORDINATOR:
                prev_leader = self.leader_id
                incoming_leader = msg.get("leader_id")
                incoming_rank = msg.get("leader_rank")
                if incoming_leader:
                    if incoming_rank is None:
                        incoming_rank = self._rank_for_id(incoming_leader)
                    if self._is_higher(incoming_leader, incoming_rank, self.server_id, self.server_rank):
                        pass
                    elif incoming_leader == self.server_id:
                        continue
                    else:
                        continue
                self.leader_id = incoming_leader
                self.leader_rank = incoming_rank if incoming_rank is not None else self._rank_for_id(self.leader_id)
                self.election_active = False
                self.election_ok_received = False
                self.leader_addr = (addr[0], SERVER_CONTROL_PORT)
                self.last_heartbeat_from_leader = time.time()
                self._cancel_election_jitter()

                ack = {"type": MSG_ACK, "server_id": self.server_id}
                send_message(self.control_sock, addr, ack)

                if prev_leader != self.leader_id:
                    print(f"New Leader Elected: {self.leader_id}")
                    self._log_leader_set()

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
                        if isinstance(state, dict):
                            incoming_seq = state.get("seq", room.seq)
                            room.seq = max(room.seq, incoming_seq)
                            room.last_seen_seq = room.seq
                            room.restore(state.get("state", {}))

            elif t == MSG_GAME_INPUT and self.is_leader():
                pid = int(msg.get("player", 1))
                direction = int(msg.get("dir", 0))
                client_addr = msg.get("client_addr")

                room_id = self._room_id_for_player(pid)
                room = self._get_room(room_id)

                if client_addr and isinstance(client_addr, list) and len(client_addr) == 2:
                    try:
                        fwd_addr = (client_addr[0], int(client_addr[1]))
                        room.add_client(fwd_addr)
                        if room.ready_sent:
                            self._send_room_ready(room, targets=[fwd_addr])
                    except Exception:
                        pass

                if pid not in room.connected_players:
                    print(f"Leader: Player {pid} detected via forwarding (room {room_id})!")

                room.apply_input(pid, direction)
                if len(room.connected_players) >= 2 and not room.ready_sent:
                    self._send_room_ready(room)

    def heartbeat_loop(self):
        while self.running:
            time.sleep(HEARTBEAT_INTERVAL)

            if self.is_leader():
                hb = {"type": MSG_HEARTBEAT, "server_id": self.server_id, "server_rank": self.server_rank, "is_leader": True}
                for _, addr in self.peers.items():
                    send_message(self.control_sock, addr, hb)

            if self.is_leader() and self.pending_acks and self._last_control_msg:
                for sid in list(self.pending_acks):
                    addr = self.peers.get(sid)
                    if addr:
                        send_message(self.control_sock, addr, self._last_control_msg)

            now = time.time()
            stale = [
                sid for sid, last in self._last_seen.items()
                if now - last > HEARTBEAT_TIMEOUT * 2
            ]

            for sid in stale:
                if sid in self.peers:
                    print(f"Pruning dead peer: {sid}")
                    self.peers.pop(sid, None)
                self._last_seen.pop(sid, None)

            if not self.is_leader():
                if (self.leader_id is None) or (time.time() - self.last_heartbeat_from_leader > HEARTBEAT_TIMEOUT):
                    if not self.election_active and self._election_jitter_timer is None:
                        jitter = random.uniform(0.05, HEARTBEAT_INTERVAL)
                        if self.leader_id is None:
                            print(f"Leader unknown. Scheduling election in {jitter:.2f}s (jitter)")
                        else:
                            print(f"Leader timeout! Scheduling election in {jitter:.2f}s (jitter)")
                        self._election_jitter_timer = threading.Timer(jitter, self._jittered_start_election)
                        self._election_jitter_timer.daemon = True
                        self._election_jitter_timer.start()

    def client_loop(self):
        while self.running:
            try:
                msg, addr = recv_message(self.client_sock)

                if msg.get("type") == MSG_GAME_INPUT:
                    pid = int(msg.get("player", 1))
                    room_id = self._room_id_for_player(pid)
                    room = self._get_room(room_id)

                    room.add_client(addr)

                    if room.ready_sent:
                        self._send_room_ready(room, targets=[addr])

                    if self.is_leader():
                        direction = int(msg.get("dir", 0))

                        if pid not in room.connected_players:
                            print(f"Leader: Player {pid} detected locally (room {room_id})!")

                        room.apply_input(pid, direction)
                        if len(room.connected_players) >= 2 and not room.ready_sent:
                            self._send_room_ready(room)

                    else:
                        leader_addr = self.leader_addr or self.peers.get(self.leader_id)
                        if leader_addr:
                            fwd_msg = dict(msg)
                            fwd_msg["client_addr"] = [addr[0], addr[1]]
                            send_message(self.control_sock, leader_addr, fwd_msg)

            except Exception:
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
                    for _, addr in self.peers.items():
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

            if self.peers and self.rooms:
                rooms_update = {
                    "type": MSG_ROOMS_UPDATE,
                    "rooms": self._rooms_snapshot()
                }

                for _, addr in self.peers.items():
                    send_message(self.control_sock, addr, rooms_update)


if __name__ == "__main__":
    PongServer().start()
