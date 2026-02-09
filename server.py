import uuid
import threading
import time
import random
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


# =====================================================
# NEW: multi-room replication message (ADDITION ONLY)
# =====================================================
MSG_ROOMS_UPDATE = "ROOMS_UPDATE"
# =====================================================
MSG_ACK = "ACK"
MSG_NACK = "NACK"

MSG_CLOSE_ROOM = "CLOSE_ROOM"
MSG_GAME_OVER = "GAME_OVER"
WIN_SCORE = 3

HEARTBEAT_INTERVAL = 1.0
HEARTBEAT_TIMEOUT = 5.0  # Increased from 3.0 to reduce false positives
TICK_INTERVAL = 0.016
ELECTION_COOLDOWN = 3.0  # Prevent repeated elections


# =====================================================
# NEW: ROOM ABSTRACTION (does NOT replace old logic,
#      only encapsulates per-room state cleanly)
# =====================================================
class PongRoom:
    def __init__(self, room_id: int):
        self.room_id = room_id
        self.game_state = GameState()
        self.seq = 0
        self.last_seen_seq = -1

        # identical fields to original server but scoped
        self.inputs = {}
        self.connected_players = set()
        self.client_addrs: Set[Tuple[str, int]] = set()

    # identical stepping logic but per room
    def step(self):
        if len(self.connected_players) >= 2:
            self.game_state.step(
                self.inputs.get(1, 0),
                self.inputs.get(2, 0)
            )
            self.seq += 1

    def apply_input(self, pid: int, direction: int):
        # normalize GLOBAL pid -> LOCAL (1 or 2)
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


# =====================================================
# ORIGINAL SERVER (UNCHANGED STRUCTURE)
# Only ADDITIONS were inserted below
# =====================================================
class PongServer:
    def __init__(self):
        self.server_id = str(uuid.uuid4())
        self.peers: Dict[str, Tuple[str, int]] = {}

        self.control_sock = make_udp_socket(bind_ip="0.0.0.0", bind_port=SERVER_CONTROL_PORT)
        self.client_sock = make_udp_socket(bind_ip="0.0.0.0", bind_port=CLIENT_PORT)

        self.leader_id = self.server_id

        # =====================================================
        # ORIGINAL (kept for backward compatibility)
        # =====================================================
        self.game_state = GameState()

        # =====================================================
        # NEW: room dictionary (sharded architecture)
        # room_id -> PongRoom
        # =====================================================
        self.rooms: Dict[int, PongRoom] = {}

        self.inputs = {1: 0, 2: 0}
        self.connected_players = set()
        self.client_addrs: Set[Tuple[str, int]] = set()

        self.last_heartbeat_from_leader = time.monotonic()  # Use monotonic for stability
        self.running = True
        self.election_active = False
        self.last_election_time = 0  # Track last election to add cooldown
        self.finalize_timer = None  # Track election finalize timer for cancellation

        # ================================
        # NEW: snapshot sync storage
        # ================================
        self._snapshot_lock = threading.Lock()
        self._snapshot_received = None
        # ================================

        # =====================================================
        # NEW: multi-room snapshot storage (followers replicate)
        # =====================================================
        self._rooms_snapshot_received = None
        # =====================================================

        # ================================
        # NEW: peer liveness timestamps (display only)
        # ================================
        self._last_seen = {}
        # ================================
        # =====================================================
        # RELIABILITY ADDITIONS
        # pending_acks -> peers we are waiting ACKs from
        # _last_control_msg -> last critical control packet to retry
        # =====================================================
        self.pending_acks: Set[str] = set()
        self._last_control_msg = None
        # =====================================================

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

    # =====================================================
    # NEW: deterministic room routing helper
    # (Players 1&2 -> room0, 3&4 -> room1, ...)
    # =====================================================
    def _room_id_for_player(self, pid: int) -> int:
        return (pid - 1) // 2

    # =====================================================
    # NEW: fetch/create room lazily
    # =====================================================
    def _get_room(self, room_id: int) -> PongRoom:
        room = self.rooms.get(room_id)
        if room is None:
            room = PongRoom(room_id)
            self.rooms[room_id] = room
        return room
    
    # ================================
    # NEW: snapshot request helper
    # ================================
    def _request_state_snapshot(self):
        """
        Ask peers for their current game state.
        First response wins. Very small + non-invasive.
        (ORIGINAL behavior kept for backward compatibility)
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

    # =====================================================
    # NEW: multi-room snapshot request (authoritative)
    # used for leader recovery and follower sync
    # =====================================================
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

    # =====================================================
    # NEW: build full rooms snapshot for replication
    # =====================================================
    def _rooms_snapshot(self):
        snap = {}
        for rid, room in self.rooms.items():
            snap[rid] = {
                # =====================================================
                # SEQUENCER ADDITION
                # Attach ordering version to each room snapshot
                # =====================================================
                "seq": room.seq,
                "state": room.snapshot()
            }
        return snap

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

                now = time.time()
                last = self._last_seen.get(sid, 0)
                if now - last > HEARTBEAT_TIMEOUT * 2:
                    continue

                print(f"{sid} -> {ip} ({role})")
        print("============================\n")

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

        # =====================================================
        # ORIGINAL single-state snapshot (kept)
        # =====================================================
        if self.peers:
            self._request_state_snapshot()

        # =====================================================
        # NEW: also request multi-room snapshot for safety
        # =====================================================
        if self.peers:
            self._request_rooms_snapshot()

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
        """Finalize election after timeout. Guard against race conditions."""
        if self.election_active and not self.is_leader():
            self.become_leader()
    
    def _cancel_finalize_timer(self):
        """Cancel pending finalize timer to prevent double leadership."""
        if self.finalize_timer and self.finalize_timer.is_alive():
            self.finalize_timer.cancel()
            self.finalize_timer = None

    def start_election(self):
        """Start election with cooldown and 2-server special case."""
        # Check election cooldown
        now = time.time()
        if now - self.last_election_time < ELECTION_COOLDOWN:
            print(f"Election cooldown active. Skipping election.")
            return
        
        # Special case: If only 2 servers (self + 1 peer), auto-elect higher UUID
        if len(self.peers) == 1:
            peer_id = list(self.peers.keys())[0]
            if peer_id > self.server_id:
                print(f"2-server mode: Auto-electing higher peer {peer_id} as leader")
                self.leader_id = peer_id
                self.election_active = False
                self.last_heartbeat_from_leader = time.monotonic()
                self._print_membership()
                return
            else:
                print(f"2-server mode: I have higher UUID, becoming leader")
                self.become_leader()
                return
        
        if self.election_active:
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

        # Add random jitter to reduce simultaneous elections
        jitter = random.uniform(0, 0.5)
        self.finalize_timer = threading.Timer(2.5 + jitter, self._finalize_election)
        self.finalize_timer.daemon = True
        self.finalize_timer.start()

    def become_leader(self):
        # Cancel any pending finalize timer
        self._cancel_finalize_timer()
        
        self.leader_id = self.server_id
        self.last_heartbeat_from_leader = time.monotonic()
        self.election_active = False

        print(f"*** I AM LEADER NOW ({self.server_id}) ***")

        # =====================================================
        # ORIGINAL: pull single-state snapshot
        # =====================================================
        if self.peers:
            self._request_state_snapshot()

        # =====================================================
        # NEW: also pull multi-room snapshot (prevents resets)
        # =====================================================
        if self.peers:
            self._request_rooms_snapshot()

        msg = {"type": MSG_COORDINATOR, "leader_id": self.server_id}

        self.pending_acks = set(self.peers.keys())
        self._last_control_msg = msg
        for _, addr in self.peers.items():
            send_message(self.control_sock, addr, msg)

        # Send immediate heartbeat to prevent timeout
        hb = {"type": MSG_HEARTBEAT, "server_id": self.server_id}
        for _, addr in self.peers.items():
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

            # ================================
            # NEW: update liveness ONLY on heartbeat from LEADER
            # ================================
            if t == MSG_HEARTBEAT:
                sid = msg.get("server_id")
                if sid and sid == self.leader_id:
                    # Only update liveness for leader heartbeats
                    self._last_seen[sid] = time.time()
                    self.last_heartbeat_from_leader = time.monotonic()
                elif sid:
                    # Still track other peers for membership, but don't reset leader timeout
                    self._last_seen[sid] = time.time()

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

            # =====================================================
            # ROOM LIFECYCLE ADDITION
            # follower deletes finished room when leader commands
            # =====================================================
            elif t == MSG_CLOSE_ROOM:
                rid = msg.get("room_id")
                if rid in self.rooms:
                    print(f"Closing room {rid} (leader request)")
                    self.rooms.pop(rid, None)
                continue
            # =====================================================



            # =====================================================
            # NEW: snapshot request handler (ROOMS + legacy)
            # =====================================================
            if t == MSG_STATE_SNAPSHOT_REQUEST:
                # follower replies with BOTH legacy + rooms
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

                if new_id and new_id != self.server_id:
                    if new_id not in self.peers:
                        print(f"Peer Joined: {new_id} from {addr[0]}")
                        self.peers[new_id] = (addr[0], SERVER_CONTROL_PORT)

                        for pid, paddr in self.peers.items():
                            if pid != new_id:
                                send_message(self.control_sock, addr, {"type": MSG_JOIN, "id": pid})
                                send_message(
                                    self.control_sock,
                                    (paddr[0], SERVER_CONTROL_PORT),
                                    {"type": MSG_JOIN, "id": new_id}
                                )

                    if new_id > self.server_id:
                        print(f"Higher peer {new_id} detected (higher than me). Starting Election.")
                        self.start_election()

                    if self.is_leader() and new_id > self.server_id:
                        print(f"Higher peer {new_id} joined. I am stepping down. Starting Election.")
                        self.start_election()

                    elif not self.is_leader() and new_id > self.leader_id:
                        print(
                            f"Higher peer {new_id} joined (bigger than known leader {self.leader_id}). Starting Election."
                        )
                        self.start_election()

            elif t == MSG_HEARTBEAT:
                sender_id = msg.get("server_id")

                # Update leader if higher ID is sending heartbeats
                if sender_id and sender_id > self.leader_id:
                    self.leader_id = sender_id

                # Add unknown peers to membership
                if sender_id and sender_id not in self.peers and sender_id != self.server_id:
                    self.peers[sender_id] = (addr[0], SERVER_CONTROL_PORT)

            elif t == MSG_ELECTION:
                candidate = msg.get("candidate")

                if candidate and self.server_id > candidate:
                    send_message(self.control_sock, addr, {"type": MSG_ELECTION_OK})
                    self.start_election()

            elif t == MSG_COORDINATOR:
                # Cancel any pending finalize timer
                self._cancel_finalize_timer()
                
                self.leader_id = msg.get("leader_id")
                self.election_active = False
                self.last_heartbeat_from_leader = time.monotonic()

                # Immediately acknowledge leader announcement
                ack = {"type": MSG_ACK, "server_id": self.server_id}
                send_message(self.control_sock, addr, ack)

                print(f"New Leader Elected: {self.leader_id}")
                self._print_membership()

                # =====================================================
                # ORIGINAL survivor push (kept)
                # =====================================================
                if self.game_state.score1 > 0 or self.game_state.score2 > 0:
                    leader_addr = self.peers.get(self.leader_id)
                    if leader_addr:
                        send_message(
                            self.control_sock,
                            leader_addr,
                            {"type": MSG_GAME_UPDATE, "state": self.game_state.to_dict()}
                        )

                # =====================================================
                # NEW: push ALL room states to new leader (multi-room safe)
                # =====================================================
                leader_addr = self.peers.get(self.leader_id)
                if leader_addr and self.rooms:
                    send_message(
                        self.control_sock,
                        leader_addr,
                        {"type": MSG_ROOMS_UPDATE, "rooms": self._rooms_snapshot()}
                    )

            elif t == MSG_GAME_UPDATE:
                # =====================================================
                # ORIGINAL single-state behavior (unchanged)
                # =====================================================
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

            # =====================================================
            # NEW: MULTI-ROOM replication handling
            # =====================================================
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

                        # forward ONLY to that room's clients
                        update_msg = {
                            "type": MSG_GAME_UPDATE,
                            "seq": room.seq,   # propagate ordering to clients
                            "state": room.game_state.to_dict()
                        }

                        for c in list(room.client_addrs):
                            send_message(self.client_sock, c, update_msg)

                elif self.is_leader():
                    # ALWAYS accept authoritative snapshot (no condition)
                    for rid, state in rooms.items():
                        room = self._get_room(int(rid))
                        room.restore(state)

            # =====================================================
            # LEADER-ONLY input handling (NOW SHARDED)
            # =====================================================
            elif t == MSG_GAME_INPUT and self.is_leader():
                pid = int(msg.get("player", 1))
                direction = int(msg.get("dir", 0))

                room_id = self._room_id_for_player(pid)
                room = self._get_room(room_id)

                if pid not in room.connected_players:
                    print(f"Leader: Player {pid} detected via forwarding (room {room_id})!")

                room.apply_input(pid, direction)

    def heartbeat_loop(self):
        while self.running:
            time.sleep(HEARTBEAT_INTERVAL)

            # Only leader sends heartbeats
            if self.is_leader():
                hb = {"type": MSG_HEARTBEAT, "server_id": self.server_id}
                for _, addr in self.peers.items():
                    send_message(self.control_sock, addr, hb)

                # Retry coordinator message if peers haven't ACKed
                if self.pending_acks and self._last_control_msg:
                    for sid in list(self.pending_acks):
                        addr = self.peers.get(sid)
                        if addr:
                            send_message(self.control_sock, addr, self._last_control_msg)

            # =====================================================
            # NEW: prune stale peers based on heartbeat timeout
            # =====================================================
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

            # Check for leader timeout (followers only)
            if not self.is_leader():
                # Add jitter to prevent simultaneous election starts
                timeout_threshold = HEARTBEAT_TIMEOUT + random.uniform(0, 1.0)
                if time.monotonic() - self.last_heartbeat_from_leader > timeout_threshold:
                    print("Leader timeout! Starting election.")
                    self.start_election()

    def client_loop(self):
        while self.running:
            try:
                msg, addr = recv_message(self.client_sock)

                # =====================================================
                # NEW: register client inside its ROOM (not global)
                # =====================================================
                if msg.get("type") == MSG_GAME_INPUT:
                    pid = int(msg.get("player", 1))
                    room_id = self._room_id_for_player(pid)
                    room = self._get_room(room_id)

                    room.add_client(addr)

                    if self.is_leader():
                        # leader updates room directly
                        direction = int(msg.get("dir", 0))

                        if pid not in room.connected_players:
                            print(f"Leader: Player {pid} detected locally (room {room_id})!")

                        room.apply_input(pid, direction)

                    else:
                        # followers only forward to leader (unchanged behavior)
                        leader_addr = self.peers.get(self.leader_id)
                        if leader_addr:
                            send_message(self.control_sock, leader_addr, msg)

            except Exception:
                continue

    def game_loop(self):
        last_print = time.time()

        while self.running:
            time.sleep(TICK_INTERVAL)

            # followers don't simulate (unchanged)
            if not self.is_leader():
                continue

            # =====================================================
            # NEW: iterate ALL rooms instead of single game_state
            # =====================================================
            if time.time() - last_print > 5.0:
                for rid, room in self.rooms.items():
                    print(
                        f"Room {rid} Status: Players Connected: {room.connected_players} (Need 2 to start)"
                    )
                last_print = time.time()

            # =====================================================
            # NEW: per-room simulation + targeted client updates
            # =====================================================
            rooms_to_close = []
            for rid, room in self.rooms.items():

                # step only that room
                room.step()

                # WIN CONDITION CHECK (LEADER ONLY)
                # =====================================================
                if (room.game_state.score1 >= WIN_SCORE or
                    room.game_state.score2 >= WIN_SCORE):

                    print(f"Room {rid} finished. Closing room.")

                    # POISON PILL → immediately kill clients in this room
                    # =====================================================
                    game_over_msg = {"type": MSG_GAME_OVER}
                    for c in list(room.client_addrs):
                        send_message(self.client_sock, c, game_over_msg)
                    # =====================================================

                    # notify followers to delete room
                    close_msg = {"type": MSG_CLOSE_ROOM, "room_id": rid}
                    for _, addr in self.peers.items():
                        send_message(self.control_sock, addr, close_msg)

                    rooms_to_close.append(rid)
                    continue
                # =============

                update = {
                    "type": MSG_GAME_UPDATE,
                    "seq": room.seq,
                    "state": room.game_state.to_dict()
                    
                }

                # send only to clients in that room
                for c in list(room.client_addrs):
                    send_message(self.client_sock, c, update)

            # =====================================================
            # actually delete finished rooms locally
            # =====================================================
            for rid in rooms_to_close:
                self.rooms.pop(rid, None)
            # =====================================================

            # =====================================================
            # NEW: replicate FULL rooms snapshot to followers
            # (authoritative, replaces single-state broadcast)
            # =====================================================
            if self.peers and self.rooms:
                rooms_update = {
                    "type": MSG_ROOMS_UPDATE,
                    "rooms": self._rooms_snapshot()
                }

                for _, addr in self.peers.items():
                    send_message(self.control_sock, addr, rooms_update)


if __name__ == "__main__":
    PongServer().start()
