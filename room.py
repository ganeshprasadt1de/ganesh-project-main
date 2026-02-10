from typing import Dict, Set, Tuple

from game_room_state import GameState
from settings import PONG_SERVER_LOGGER


class PongRoom:

    def __init__(self, room_id: int):
        self.room_id = room_id
        self.game_state = GameState()
        self.seq = 0
        self.last_seen_seq = -1
        self.ready_sent = False

        self.inputs: Dict[int, int] = {}
        self.connected_players: Set[int] = set()
        self.client_addrs: Set[Tuple[str, int]] = set()
        self.log = PONG_SERVER_LOGGER

    def step(self):
        if len(self.connected_players) >= 2:
            self.game_state.step(
                self.inputs.get(1, 0),
                self.inputs.get(2, 0),
            )
            self.seq += 1

    def apply_input(self, global_pid: int, direction: int):
        local_pid = ((global_pid - 1) % 2) + 1
        self.inputs[local_pid] = direction
        self.connected_players.add(local_pid)

    def add_client(self, addr: Tuple[str, int]):
        if addr:
            self.client_addrs.add(addr)

    def snapshot(self) -> dict:
        return self.game_state.to_dict()

    def restore(self, state_dict: dict):
        self.game_state = GameState.from_dict(state_dict)

    def __repr__(self):
        return (
            f"PongRoom(id={self.room_id}, players={self.connected_players}, "
            f"seq={self.seq}, score={self.game_state.score1}-{self.game_state.score2})"
        )
