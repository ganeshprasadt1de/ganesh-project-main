import random
import threading
import time
import uuid
from typing import Dict, Tuple, Callable, Optional

from game_message import send_message
from settings import (
    BULLY_ELECTION_LOGGER,
    ELECTION_TIMEOUT,
    HEARTBEAT_TIMEOUT,
    MSG_ELECTION,
    MSG_ELECTION_OK,
    MSG_COORDINATOR,
    MSG_ACK,
    SERVER_CONTROL_PORT,
)


class BullyElection:
    def __init__(
        self,
        server_id: str,
        server_rank: int,
        peers: Dict[str, Tuple[str, int]],
        control_sock,
        on_become_leader: Optional[Callable] = None,
    ):
        self.server_id = server_id
        self.server_rank = server_rank
        self.peers = peers
        self.control_sock = control_sock
        self.on_become_leader = on_become_leader

        self.log = BULLY_ELECTION_LOGGER
        self.leader_id: Optional[str] = None
        self.leader_rank: int = 0
        self.leader_addr: Optional[Tuple[str, int]] = None
        self.election_active = False
        self.election_ok_received = False
        self.pending_acks: set = set()
        self._last_control_msg = None
        self._last_seen: Dict[str, float] = {}
        self._election_jitter_timer: threading.Timer = None

    @staticmethod
    def rank_for_id(sid: str) -> int:
        try:
            return uuid.UUID(sid).int
        except Exception:
            return 0

    def _is_higher(self, a_id, a_rank, b_id, b_rank) -> bool:
        if a_rank != b_rank:
            return a_rank > b_rank
        return a_id > b_id

    def higher_peers(self) -> Dict[str, Tuple[str, int]]:
        return {
            sid: addr
            for sid, addr in self.peers.items()
            if self._is_higher(sid, self.rank_for_id(sid), self.server_id, self.server_rank)
        }

    def is_leader(self) -> bool:
        return self.leader_id == self.server_id

    def schedule_election(self, jitter_max: float = None):
        if self.election_active or self._election_jitter_timer is not None:
            return
        if jitter_max is None:
            from settings import HEARTBEAT_INTERVAL
            jitter_max = HEARTBEAT_INTERVAL
        jitter = random.uniform(0.05, jitter_max)
        self.log.info(f"Scheduling election in {jitter:.2f}s (jitter)")
        self._election_jitter_timer = threading.Timer(jitter, self._jittered_start_election)
        self._election_jitter_timer.daemon = True
        self._election_jitter_timer.start()

    def _jittered_start_election(self):
        self._election_jitter_timer = None
        if self.is_leader() or self.election_active:
            return
        self.log.info("Starting election after jitter delay.")
        self._start_election_internal()

    def _cancel_election_jitter(self):
        if self._election_jitter_timer is not None:
            self._election_jitter_timer.cancel()
            self._election_jitter_timer = None

    def start_election(self):
        self.schedule_election()

    def _start_election_internal(self):
        if self.election_active:
            return

        self._cancel_election_jitter()
        self.election_active = True
        self.election_ok_received = False
        self.log.info(f"Starting election (server={self.server_id[:8]})")

        higher = self.higher_peers()
        if not higher:
            self.become_leader()
            return

        msg = {
            "type": MSG_ELECTION,
            "candidate": self.server_id,
            "candidate_rank": self.server_rank,
        }
        for _, addr in higher.items():
            send_message(self.control_sock, addr, msg)
        self.log.debug(f"Sent ELECTION to {len(higher)} higher peers")

        t = threading.Timer(ELECTION_TIMEOUT, self._finalize_election)
        t.daemon = True
        t.start()

    def _finalize_election(self):
        if not self.election_active:
            return

        if not self.election_ok_received and not self.is_leader():
            self.become_leader()
            return

        if self.election_ok_received and not self.is_leader():
            self.election_active = False
            self._start_election_internal()

    def become_leader(self):
        for sid in list(self.peers):
            sid_rank = self.rank_for_id(sid)
            if self._is_higher(sid, sid_rank, self.server_id, self.server_rank):
                last = self._last_seen.get(sid, 0)
                if time.time() - last < HEARTBEAT_TIMEOUT * 2:
                    self.log.info(f"Yielding to higher-ranked peer {sid[:8]}")
                    self.election_active = False
                    return

        prev = self.leader_id
        self.leader_id = self.server_id
        self.leader_rank = self.server_rank
        self.election_active = False
        self.election_ok_received = False
        self.leader_addr = None

        if prev != self.server_id:
            self.log.warning(f"*** I AM LEADER NOW ({self.server_id[:8]}) ***")

        msg = {
            "type": MSG_COORDINATOR,
            "leader_id": self.server_id,
            "leader_rank": self.server_rank,
        }
        self.pending_acks = set(self.peers.keys())
        self._last_control_msg = msg
        for _, addr in self.peers.items():
            send_message(self.control_sock, addr, msg)

        if self.on_become_leader:
            self.on_become_leader()

    def handle_election(self, candidate_id: str, candidate_rank: int, addr: Tuple[str, int]):
        if self._is_higher(self.server_id, self.server_rank, candidate_id, candidate_rank):
            send_message(self.control_sock, addr, {"type": MSG_ELECTION_OK})
            self.log.debug(f"Sent ELECTION_OK to {candidate_id[:8]}")
            if self.is_leader():
                coord = {"type": MSG_COORDINATOR,
                         "leader_id": self.server_id,
                         "leader_rank": self.server_rank}
                send_message(self.control_sock, addr, coord)
            elif self.leader_id is None and not self.election_active:
                self._start_election_internal()

    def handle_election_ok(self):
        self.election_ok_received = True
        self.log.debug("Received ELECTION_OK from higher peer")

    def handle_coordinator(self, leader_id: str, leader_rank: Optional[int], addr: Tuple[str, int]):
        if leader_rank is None:
            leader_rank = self.rank_for_id(leader_id)

        if self._is_higher(leader_id, leader_rank, self.server_id, self.server_rank):
            pass
        elif leader_id == self.server_id:
            return
        else:
            return

        prev = self.leader_id
        self.leader_id = leader_id
        self.leader_rank = leader_rank
        self.election_active = False
        self.election_ok_received = False
        self.leader_addr = (addr[0], SERVER_CONTROL_PORT)
        self._cancel_election_jitter()

        send_message(self.control_sock, addr, {"type": MSG_ACK, "server_id": self.server_id})

        if prev != self.leader_id:
            self.log.info(f"New leader elected: {leader_id[:8]}")

    def handle_ack(self, server_id: str):
        self.pending_acks.discard(server_id)


    def accept_leader_from_heartbeat(self, sid: str, sid_rank: int, addr: Tuple[str, int]) -> bool:
        if sid_rank is None:
            sid_rank = self.rank_for_id(sid)

        self._last_seen[sid] = time.time()

        accept = False
        if self.leader_id is None:
            accept = True
        elif self._is_higher(sid, sid_rank, self.leader_id, self.leader_rank):
            accept = True

        if accept:
            self.leader_id = sid
            self.leader_rank = sid_rank
            self.leader_addr = (addr[0], SERVER_CONTROL_PORT)
            self.election_active = False
            self.election_ok_received = False
            self._cancel_election_jitter()
            return True
        return False
