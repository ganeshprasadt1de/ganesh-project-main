# election.py
# Robust leader election helper (LCR-style ring) with safe socket usage and flexible integration.
#
# Usage notes:
# - Pass `peers` as a dict mapping id -> (ip, port). It MAY omit your own id.
# - Prefer passing an existing `control_sock` (e.g., the server's control socket) to avoid binding the same port twice.
# - If `control_sock` is not provided, this class creates an ephemeral UDP socket (bind_port=0) to avoid collisions.
# - The class will include `my_id` in the logical ring even when `peers` omits it.
#
# The implementation uses the MSG_ELECTION and MSG_ELECTION_RESULT message types from common.py.
# It is defensive against malformed messages and transient socket errors.

import threading
import time
from typing import Dict, Tuple, Any, Optional
from lamport_clock import LamportClock
from common import (
    MSG_ELECTION,
    MSG_ELECTION_RESULT,
    send_message,
    recv_message,
    make_udp_socket,
    SERVER_CONTROL_PORT,
)

class LeaderElection:
    def __init__(
        self,
        my_id: str,
        peers: Dict[str, Tuple[str, int]],
        *,
        my_addr: Optional[Tuple[str, int]] = None,
        control_sock: Optional[object] = None,
        start_immediately: bool = True,
        startup_delay: float = 1.0,
    ):
        """
        my_id: identifier for this node (string).
        peers: dict mapping peer_id -> (ip, port). May exclude my_id.
        my_addr: optional tuple (ip, port) describing this node's control address.
                 If omitted, and control_sock is provided, my_addr is inferred not required.
                 If omitted and control_sock not provided, my_addr is ("" , SERVER_CONTROL_PORT) for message routing
                 but the local ephemeral socket will be bound to an ephemeral port.
        control_sock: optional already-bound UDP socket to use (recommended when integrating with server.py).
        start_immediately: if True, creates and starts the background thread.
        startup_delay: seconds to wait before initiating first election.
        """
        self.my_id = my_id
        # copy peers (do not mutate the caller's dict)
        self.peers = dict(peers)
        self.my_addr = my_addr
        self.clock = LamportClock()

        # election state
        self.leader_id: Optional[str] = None
        self._election_in_progress = False
        self.running = False
        self.startup_delay = float(startup_delay)

        # socket handling:
        # - if control_sock is provided, use it (no bind done here)
        # - otherwise create an ephemeral UDP socket (no SERVER_CONTROL_PORT bind) to avoid collisions
        self._external_sock = control_sock is not None
        if control_sock is not None:
            self.sock = control_sock
        else:
            # ephemeral socket avoids binding SERVER_CONTROL_PORT which the server's control plane uses
            self.sock = make_udp_socket(bind_ip="", bind_port=0)

        # build an ordered ring of ids (ensure my_id is present)
        self._ids = sorted(set(list(self.peers.keys()) + [self.my_id]), key=str)
        # map id -> addr for convenience; if my_id missing in peers, use my_addr if provided
        self._id_to_addr: Dict[str, Tuple[str, int]] = dict(self.peers)
        if self.my_id not in self._id_to_addr and self.my_addr:
            self._id_to_addr[self.my_id] = self.my_addr

        # background thread
        self.thread = threading.Thread(target=self._loop, daemon=True)

        if start_immediately:
            self.start()

    def start(self):
        if self.running:
            return
        self.running = True
        # refresh id->addr mapping in case peers changed before start
        self._ids = sorted(set(list(self.peers.keys()) + [self.my_id]), key=str)
        if self.my_id not in self._id_to_addr and self.my_addr:
            self._id_to_addr[self.my_id] = self.my_addr
        self.thread.start()

    def stop(self):
        self.running = False
        # close only if we created the socket (don't close an externally provided one)
        if not self._external_sock:
            try:
                self.sock.close()
            except Exception:
                pass

    def update_peers(self, peers: Dict[str, Tuple[str, int]], my_addr: Optional[Tuple[str, int]] = None):
        """Update peer list (thread-safe enough for typical use)."""
        self.peers = dict(peers)
        if my_addr:
            self.my_addr = my_addr
            self._id_to_addr[self.my_id] = my_addr
        # rebuild ring
        self._ids = sorted(set(list(self.peers.keys()) + [self.my_id]), key=str)
        # merge addresses
        for k, v in self.peers.items():
            self._id_to_addr[k] = v

    def _next_id(self, sid: str) -> str:
        """Return the id of the node after `sid` in the ring (wrap-around)."""
        if sid not in self._ids:
            # defensive: rebuild ids then try again
            self._ids = sorted(set(list(self.peers.keys()) + [self.my_id]), key=str)
        if not self._ids:
            return self.my_id
        idx = self._ids.index(sid)
        return self._ids[(idx + 1) % len(self._ids)]

    def get_next_neighbour(self) -> Tuple[str, Tuple[str, int]]:
        """
        Returns (next_id, (ip, port)).
        If next_id == my_id and we don't have an explicit address stored, we return a best-effort address:
        either my_addr (if provided) or ("", SERVER_CONTROL_PORT) as a hint.
        """
        next_id = self._next_id(self.my_id)
        addr = self._id_to_addr.get(next_id)
        if addr is None:
            # if the next id is ourselves, try my_addr, else try to lookup in peers mapping
            if next_id == self.my_id and self.my_addr:
                addr = self.my_addr
            else:
                # best-effort fallback: use SERVER_CONTROL_PORT on the stored ip if peers had something
                cand = self.peers.get(next_id)
                if cand:
                    addr = cand
                else:
                    addr = ("", SERVER_CONTROL_PORT)
        return next_id, addr

    def start_election(self):
        """Start an LCR-style ring election: send candidate = my_id to next neighbour."""
        if self._election_in_progress:
            return
        self._election_in_progress = True
        try:
            my_msg = {"type": MSG_ELECTION, "candidate": self.my_id}
            _, addr = self.get_next_neighbour()
            send_message(self.sock, addr, my_msg, self.clock)
        except Exception:
            # on send failure, mark election not in progress so we can try again later
            self._election_in_progress = False

    def _handle_election(self, msg: Dict[str, Any], src_addr):
        incoming_candidate = msg.get("candidate")
        if incoming_candidate is None:
            return
        # choose lexicographically max id as the winner
        best = max(str(incoming_candidate), str(self.my_id))
        if best == self.my_id and incoming_candidate == self.my_id:
            # my own ID returned -> I'm leader
            self.leader_id = self.my_id
            self._election_in_progress = False
            # announce leader around ring by sending MSG_ELECTION_RESULT to next neighbour
            next_id, addr = self.get_next_neighbour()
            res = {"type": MSG_ELECTION_RESULT, "leader": self.my_id}
            try:
                send_message(self.sock, addr, res, self.clock)
            except Exception:
                pass
        else:
            # forward election with best id to next neighbour
            fwd = {"type": MSG_ELECTION, "candidate": best}
            next_id, addr = self.get_next_neighbour()
            try:
                send_message(self.sock, addr, fwd, self.clock)
            except Exception:
                # on failure, we stop election to avoid infinite state; caller can restart later
                self._election_in_progress = False

    def _handle_election_result(self, msg: Dict[str, Any], src_addr):
        leader = msg.get("leader")
        if leader is None:
            return
        if self.leader_id != leader:
            self.leader_id = leader
        # forward until everyone knows (stop forwarding when it comes back to the origin)
        if leader != self.my_id:
            next_id, addr = self.get_next_neighbour()
            try:
                send_message(self.sock, addr, msg, self.clock)
            except Exception:
                pass
        self._election_in_progress = False

    def _loop(self):
        # small delay to let all peers start
        time.sleep(self.startup_delay)
        # initiate first election
        self.start_election()
        while self.running:
            try:
                msg, addr = recv_message(self.sock, self.clock)
            except OSError:
                break
            except Exception:
                # ignore transient parse/recv errors
                continue

            if not isinstance(msg, dict):
                continue
            mtype = msg.get("type")
            if mtype == MSG_ELECTION:
                self._handle_election(msg, addr)
            elif mtype == MSG_ELECTION_RESULT:
                self._handle_election_result(msg, addr)
            # otherwise ignore unknown message types
