import uuid
import threading
import time
from typing import Dict, Tuple, Set, Optional

from game_message import make_udp_socket, send_message, recv_message
from game_room_state import GameState
from room import PongRoom
from discovery_protocol import server_discovery_listener, client_discover_servers
from bully_election import BullyElection
from settings import (
    SERVER_CONTROL_PORT,
    CLIENT_PORT,
    PONG_SERVER_LOGGER,
    HEARTBEAT_INTERVAL,
    HEARTBEAT_TIMEOUT,
    TICK_INTERVAL,
    WIN_SCORE,
    PEER_STALE_TIMEOUT,
    CLIENT_SERVER_TIMEOUT,
    MSG_GAME_INPUT,
    MSG_GAME_UPDATE,
    MSG_ROOM_READY,
    MSG_HEARTBEAT,
    MSG_ELECTION,
    MSG_ELECTION_OK,
    MSG_COORDINATOR,
    MSG_JOIN,
    MSG_STATE_SNAPSHOT_REQUEST,
    MSG_STATE_SNAPSHOT_RESPONSE,
    MSG_ROOMS_UPDATE,
    MSG_ACK,
    MSG_NACK,
    MSG_CLOSE_ROOM,
    MSG_GAME_OVER,
)


class PongServer:
    def __init__(self):
        self.server_id = str(uuid.uuid4())
        self.server_rank = BullyElection.rank_for_id(self.server_id)
        self.peers: Dict[str, Tuple[str, int]] = {}
        self.log = PONG_SERVER_LOGGER

        self.control_sock = make_udp_socket(bind_ip="0.0.0.0", bind_port=SERVER_CONTROL_PORT)
        self.client_sock = make_udp_socket(bind_ip="0.0.0.0", bind_port=CLIENT_PORT)

        self.rooms: Dict[int, PongRoom] = {}

        self.game_state = GameState()

        self.inputs = {1: 0, 2: 0}
        self.connected_players: Set[int] = set()
        self.client_addrs: Set[Tuple[str, int]] = set()

        self.last_heartbeat_from_leader = time.time()
        self._last_seen: Dict[str, float] = {}

        self.election = BullyElection(
            server_id=self.server_id,
            server_rank=self.server_rank,
            peers=self.peers,
            control_sock=self.control_sock,
            on_become_leader=self._on_became_leader,
        )

        self._snapshot_lock = threading.Lock()
        self._snapshot_received = None
        self._rooms_snapshot_received = None

        self.closed_rooms: Dict[int, float] = {}
        self._room_close_grace = max(CLIENT_SERVER_TIMEOUT * 2, 2.0)

        self.running = True
        self._last_logged_leader: Optional[str] = None

        self.discovery_stop = threading.Event()
        self.discovery_thread = threading.Thread(
            target=server_discovery_listener,
            args=(self.server_id, self.discovery_stop),
            daemon=True,
        )

        self.control_thread = threading.Thread(target=self._control_loop, daemon=True)
        self.client_thread = threading.Thread(target=self._client_loop, daemon=True)
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.game_thread = threading.Thread(target=self._game_loop, daemon=True)


    @staticmethod
    def _room_id_for_player(pid: int) -> int:
        return (pid - 1) // 2

    def _get_room(self, room_id: int) -> PongRoom:
        room = self.rooms.get(room_id)
        if room is None:
            room = PongRoom(room_id)
            self.rooms[room_id] = room
            self.log.info(f"Created room {room_id}")
        return room

    def _mark_room_closed(self, room_id: int):
        self.closed_rooms[room_id] = time.time()

    def _is_room_closed(self, room_id: int) -> bool:
        closed_at = self.closed_rooms.get(room_id)
        if closed_at is None:
            return False
        if time.time() - closed_at > self._room_close_grace:
            self.closed_rooms.pop(room_id, None)
            return False
        return True

    def _send_room_ready(self, room: PongRoom, targets=None):
        if len(room.connected_players) < 2:
            return
        msg = {"type": MSG_ROOM_READY, "room_id": room.room_id}
        if targets is None:
            targets = list(room.client_addrs)
        for c in targets:
            send_message(self.client_sock, c, msg)
        room.ready_sent = True
        self.log.info(f"Room {room.room_id} is READY — both players connected")


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
                    self.log.info("Recovered single-state snapshot from peer")
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
                    self.log.info("Recovered rooms snapshot from peer")
                    break
            time.sleep(0.01)

    def _rooms_snapshot(self) -> dict:
        snap = {}
        for rid, room in self.rooms.items():
            snap[rid] = {"seq": room.seq, "state": room.snapshot()}
        return snap


    def is_leader(self) -> bool:
        return self.election.is_leader()

    def _on_became_leader(self):
        if self.peers:
            self._request_state_snapshot()
            self._request_rooms_snapshot()
        self._log_leader_change()

    def _log_leader_change(self):
        lid = self.election.leader_id
        if lid == self._last_logged_leader:
            return
        self._last_logged_leader = lid
        self.log.info(f"Leader confirmed: {lid[:8] if lid else 'unknown'}")


    def start(self):
        self.log.info(f"Starting server {self.server_id[:8]} ...")
        self.discovery_thread.start()

        self.log.info("Scanning for peers...")
        found = client_discover_servers(timeout=2.0)

        if found:
            peers_found = [(sid, sip) for sid, sip in found if sid != self.server_id]
            self.log.info(f"Found peers: {[(s[:8], ip) for s, ip in peers_found]}")
            for sid, sip in peers_found:
                self.peers[sid] = (sip, SERVER_CONTROL_PORT)
                join_msg = {"type": MSG_JOIN, "id": self.server_id, "rank": self.server_rank}
                send_message(self.control_sock, (sip, SERVER_CONTROL_PORT), join_msg)
        else:
            self.log.info("No peers found. I am the first server.")

        if self.peers:
            self._request_state_snapshot()
            self._request_rooms_snapshot()

        if self.peers:
            self.election.leader_id = None
            self.election.leader_rank = 0
            self.election.leader_addr = None
            self.log.info("Waiting for leader announcement...")
        else:
            self.election.leader_id = self.server_id
            self.election.leader_rank = self.server_rank
            self.log.info(f"I am the initial leader ({self.server_id[:8]})")

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
        self.log.info("Shutting down server...")
        self.running = False
        self.discovery_stop.set()
        self.control_sock.close()
        self.client_sock.close()


    def _control_loop(self):
        while self.running:
            try:
                msg, addr = recv_message(self.control_sock)
            except Exception:
                continue

            t = msg.get("type")

            if t == MSG_HEARTBEAT:
                self._handle_heartbeat(msg, addr)
                continue

            if t == MSG_ACK:
                self.election.handle_ack(msg.get("server_id", ""))
                continue

            if t == MSG_NACK and self.is_leader():
                self._handle_nack(msg, addr)
                continue

            if t == MSG_CLOSE_ROOM:
                rid = msg.get("room_id")
                if rid in self.rooms:
                    self.log.info(f"Closing room {rid} (leader request)")
                    self.rooms.pop(rid, None)
                if rid is not None:
                    self._mark_room_closed(int(rid))
                continue

            if t == MSG_STATE_SNAPSHOT_REQUEST:
                reply = {
                    "type": MSG_STATE_SNAPSHOT_RESPONSE,
                    "state": self.game_state.to_dict(),
                    "rooms": self._rooms_snapshot(),
                }
                send_message(self.control_sock, addr, reply)
                continue

            if t == MSG_STATE_SNAPSHOT_RESPONSE:
                with self._snapshot_lock:
                    if msg.get("state"):
                        self._snapshot_received = msg["state"]
                    if msg.get("rooms"):
                        self._rooms_snapshot_received = msg["rooms"]
                continue

            if t == MSG_JOIN:
                self._handle_join(msg, addr)

            elif t == MSG_ELECTION:
                candidate = msg.get("candidate", "")
                candidate_rank = msg.get("candidate_rank")
                if candidate_rank is None:
                    candidate_rank = BullyElection.rank_for_id(candidate)
                self.election.handle_election(candidate, candidate_rank, addr)

            elif t == MSG_ELECTION_OK:
                self.election.handle_election_ok()

            elif t == MSG_COORDINATOR:
                self._handle_coordinator(msg, addr)

            elif t == MSG_GAME_UPDATE:
                self._handle_game_update(msg, addr)

            elif t == MSG_ROOMS_UPDATE:
                self._handle_rooms_update(msg, addr)

            elif t == MSG_GAME_INPUT and self.is_leader():
                self._handle_forwarded_input(msg)


    def _handle_heartbeat(self, msg, addr):
        sid = msg.get("server_id")
        sid_rank = msg.get("server_rank")
        if sid:
            self._last_seen[sid] = time.time()
            if sid not in self.peers and sid != self.server_id:
                self.peers[sid] = (addr[0], SERVER_CONTROL_PORT)
                self.log.info(f"Discovered peer {sid[:8]} via heartbeat")
        if msg.get("is_leader") and sid:
            if sid == self.election.leader_id:
                self.last_heartbeat_from_leader = time.time()
            changed = self.election.accept_leader_from_heartbeat(sid, sid_rank, addr)
            if changed:
                self.last_heartbeat_from_leader = time.time()
                self._log_leader_change()

    def _handle_nack(self, msg, addr):
        rid = msg.get("room_id")
        sid = msg.get("server_id")
        peer_addr = self.peers.get(sid)
        if peer_addr and rid is not None:
            room = self._get_room(int(rid))
            resend = {
                "type": MSG_ROOMS_UPDATE,
                "rooms": {rid: {"seq": room.seq, "state": room.snapshot()}},
            }
            send_message(self.control_sock, peer_addr, resend)
            self.log.debug(f"Resent room {rid} state to {sid[:8]} on NACK")

    def _handle_join(self, msg, addr):
        new_id = msg.get("id")
        new_rank = msg.get("rank")
        if not new_id or new_id == self.server_id:
            return
        if new_rank is None:
            new_rank = BullyElection.rank_for_id(new_id)

        if new_id not in self.peers:
            self.log.info(f"Peer joined: {new_id[:8]} from {addr[0]}")
            self.peers[new_id] = (addr[0], SERVER_CONTROL_PORT)

            for pid, paddr in self.peers.items():
                if pid != new_id:
                    send_message(
                        self.control_sock, addr,
                        {"type": MSG_JOIN, "id": pid, "rank": BullyElection.rank_for_id(pid)},
                    )
                    send_message(
                        self.control_sock, (paddr[0], SERVER_CONTROL_PORT),
                        {"type": MSG_JOIN, "id": new_id, "rank": new_rank},
                    )

        if self.is_leader():
            self.log.debug(f"Informing {new_id[:8]} about leader {self.election.leader_id[:8]}")
            send_message(
                self.control_sock, addr,
                {"type": MSG_COORDINATOR, "leader_id": self.election.leader_id, "leader_rank": self.election.leader_rank},
            )
        else:
            leader_addr = self.election.leader_addr or self.peers.get(self.election.leader_id)
            if leader_addr and self.election.leader_id:
                send_message(
                    self.control_sock, leader_addr,
                    {"type": MSG_JOIN, "id": new_id, "rank": new_rank},
                )

    def _handle_coordinator(self, msg, addr):
        leader_id = msg.get("leader_id")
        leader_rank = msg.get("leader_rank")
        if not leader_id:
            return

        prev = self.election.leader_id
        self.election.handle_coordinator(leader_id, leader_rank, addr)
        self.last_heartbeat_from_leader = time.time()

        if self.game_state.score1 > 0 or self.game_state.score2 > 0:
            la = self.peers.get(self.election.leader_id)
            if la:
                send_message(
                    self.control_sock, la,
                    {"type": MSG_GAME_UPDATE, "state": self.game_state.to_dict()},
                )

        la = self.peers.get(self.election.leader_id)
        if la and self.rooms:
            send_message(
                self.control_sock, la,
                {"type": MSG_ROOMS_UPDATE, "rooms": self._rooms_snapshot()},
            )

        if prev != self.election.leader_id:
            self._log_leader_change()

    def _handle_game_update(self, msg, addr):
        if not self.is_leader():
            if msg.get("state"):
                self.game_state = GameState.from_dict(msg["state"])
                update = {"type": MSG_GAME_UPDATE, "state": self.game_state.to_dict()}
                for c in list(self.client_addrs):
                    send_message(self.client_sock, c, update)
        else:
            remote = msg.get("state", {})
            if (remote.get("score1", 0) > 0 or remote.get("score2", 0) > 0):
                if self.game_state.score1 == 0 and self.game_state.score2 == 0:
                    self.log.info("Leader: Recovering game state from survivor!")
                    self.game_state = GameState.from_dict(remote)

    def _handle_rooms_update(self, msg, addr):
        rooms = msg.get("rooms", {})
        if not self.is_leader():
            for rid, state in rooms.items():
                if self._is_room_closed(int(rid)):
                    continue
                room = self._get_room(int(rid))
                incoming_seq = state.get("seq", -1)

                expected = room.last_seen_seq + 1
                if incoming_seq > expected:
                    nack = {
                        "type": MSG_NACK,
                        "server_id": self.server_id,
                        "room_id": rid,
                        "expected_seq": expected,
                    }
                    send_message(self.control_sock, addr, nack)

                if incoming_seq > room.seq:
                    room.seq = incoming_seq
                    room.restore(state.get("state"))
                    room.last_seen_seq = incoming_seq

                update = {"type": MSG_GAME_UPDATE, "seq": room.seq, "state": room.game_state.to_dict()}
                for c in list(room.client_addrs):
                    send_message(self.client_sock, c, update)
        else:
            for rid, state in rooms.items():
                room = self._get_room(int(rid))
                if isinstance(state, dict):
                    incoming_seq = state.get("seq", room.seq)
                    room.seq = max(room.seq, incoming_seq)
                    room.last_seen_seq = room.seq
                    room.restore(state.get("state", {}))

    def _handle_forwarded_input(self, msg):
        pid = int(msg.get("player", 1))
        direction = int(msg.get("dir", 0))
        client_addr = msg.get("client_addr")

        room_id = self._room_id_for_player(pid)
        if self._is_room_closed(room_id):
            if client_addr and isinstance(client_addr, list) and len(client_addr) == 2:
                try:
                    send_message(self.client_sock, (client_addr[0], int(client_addr[1])), {"type": MSG_GAME_OVER})
                except Exception:
                    pass
            return
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
            self.log.info(f"Leader: Player {pid} detected via forwarding (room {room_id})!")

        room.apply_input(pid, direction)
        if len(room.connected_players) >= 2 and not room.ready_sent:
            self._send_room_ready(room)


    def _heartbeat_loop(self):
        while self.running:
            time.sleep(HEARTBEAT_INTERVAL)

            if self.is_leader():
                hb = {
                    "type": MSG_HEARTBEAT,
                    "server_id": self.server_id,
                    "server_rank": self.server_rank,
                    "is_leader": True,
                }
                for _, addr in self.peers.items():
                    send_message(self.control_sock, addr, hb)

                if self.election.pending_acks and self.election._last_control_msg:
                    for sid in list(self.election.pending_acks):
                        peer_addr = self.peers.get(sid)
                        if peer_addr:
                            send_message(self.control_sock, peer_addr, self.election._last_control_msg)

            now = time.time()
            stale = [
                sid for sid, last in self._last_seen.items()
                if now - last > PEER_STALE_TIMEOUT
            ]
            for sid in stale:
                if sid in self.peers:
                    self.log.warning(f"Pruning dead peer: {sid[:8]}")
                    self.peers.pop(sid, None)
                self._last_seen.pop(sid, None)

            if not self.is_leader():
                if (
                    self.election.leader_id is None
                    or time.time() - self.last_heartbeat_from_leader > HEARTBEAT_TIMEOUT
                ):
                    if not self.election.election_active:
                        if self.election.leader_id is None:
                            self.log.warning("Leader unknown. Scheduling election (jittered).")
                        else:
                            self.log.warning("Leader timeout! Scheduling election (jittered).")
                        self.election.schedule_election()


    def _client_loop(self):
        while self.running:
            try:
                msg, addr = recv_message(self.client_sock)
            except Exception:
                continue

            if msg.get("type") != MSG_GAME_INPUT:
                continue

            pid = int(msg.get("player", 1))
            room_id = self._room_id_for_player(pid)
            if self._is_room_closed(room_id):
                send_message(self.client_sock, addr, {"type": MSG_GAME_OVER})
                continue
            room = self._get_room(room_id)
            room.add_client(addr)

            if room.ready_sent:
                self._send_room_ready(room, targets=[addr])

            if self.is_leader():
                direction = int(msg.get("dir", 0))
                if pid not in room.connected_players:
                    self.log.info(f"Leader: Player {pid} detected locally (room {room_id})!")
                room.apply_input(pid, direction)
                if len(room.connected_players) >= 2 and not room.ready_sent:
                    self._send_room_ready(room)
            else:
                leader_addr = self.election.leader_addr or self.peers.get(self.election.leader_id)
                if leader_addr:
                    fwd = dict(msg)
                    fwd["client_addr"] = [addr[0], addr[1]]
                    send_message(self.control_sock, leader_addr, fwd)


    def _game_loop(self):
        last_status_print = time.time()

        while self.running:
            time.sleep(TICK_INTERVAL)

            if not self.is_leader():
                continue

            if time.time() - last_status_print > 5.0:
                for rid, room in self.rooms.items():
                    self.log.info(
                        f"Room {rid}: players={room.connected_players} "
                        f"score={room.game_state.score1}-{room.game_state.score2}"
                    )
                last_status_print = time.time()

            rooms_to_close = []
            for rid, room in list(self.rooms.items()):
                room.step()

                if room.game_state.score1 >= WIN_SCORE or room.game_state.score2 >= WIN_SCORE:
                    winner = 1 if room.game_state.score1 >= WIN_SCORE else 2
                    self.log.info(
                        f"Room {rid} finished — Player {winner} wins "
                        f"({room.game_state.score1}-{room.game_state.score2})"
                    )
                    for c in list(room.client_addrs):
                        send_message(self.client_sock, c, {"type": MSG_GAME_OVER})
                    for _, paddr in self.peers.items():
                        send_message(self.control_sock, paddr, {"type": MSG_CLOSE_ROOM, "room_id": rid})
                    self._mark_room_closed(rid)
                    rooms_to_close.append(rid)
                    continue

                update = {"type": MSG_GAME_UPDATE, "seq": room.seq, "state": room.game_state.to_dict()}
                for c in list(room.client_addrs):
                    send_message(self.client_sock, c, update)

            for rid in rooms_to_close:
                self.rooms.pop(rid, None)

            if self.peers and self.rooms:
                rooms_update = {"type": MSG_ROOMS_UPDATE, "rooms": self._rooms_snapshot()}
                for _, paddr in self.peers.items():
                    send_message(self.control_sock, paddr, rooms_update)
