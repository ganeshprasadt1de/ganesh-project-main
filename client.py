# client.py
# FINAL VERIFIED CLIENT
# - Windows-safe
# - Correct GameState schema
# - Client-side leader failover

import argparse
import pygame
import time

from lamport_clock import LamportClock
from common import (
    make_udp_socket, send_message, recv_message,
    CLIENT_PORT, MSG_GAME_INPUT, MSG_GAME_UPDATE
)

# =====================================================
# SERVER ADDRESSES (UPDATE ONLY IF IPs CHANGE)
# =====================================================
PRIMARY_SERVER = ("192.168.0.61", CLIENT_PORT)   # s2 (Laptop M)
BACKUP_SERVER  = ("192.168.0.97", CLIENT_PORT)   # s1 (Laptop A)

CLIENT_TIMEOUT = 1.5  # seconds without updates before failover

# =====================================================
# Rendering constants (match game_state.py)
# =====================================================
SCREEN_WIDTH = 800
SCREEN_HEIGHT = 400
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


class PongClient:
    def __init__(self, player: int):
        self.player = player
        self.clock = LamportClock()

        self.server_addr = PRIMARY_SERVER
        self.backup_server = BACKUP_SERVER

        self.sock = make_udp_socket(bind_ip="", bind_port=0)

        self.state = None
        self.last_update_time = time.time()
        self.running = True

    # -------------------------------------------------
    def start(self):
        print(f"[CLIENT {self.player}] connecting to server {self.server_addr}")

        # HELLO packet so server learns client address
        self._announce()

        self.game_loop()

    def _announce(self):
        send_message(
            self.sock,
            self.server_addr,
            {"type": MSG_GAME_INPUT, "player": self.player, "dir": 0},
            self.clock
        )

    # -------------------------------------------------
    def send_input(self, direction: int):
        send_message(
            self.sock,
            self.server_addr,
            {"type": MSG_GAME_INPUT, "player": self.player, "dir": direction},
            self.clock
        )

    def receive_updates(self):
        self.sock.settimeout(0.001)
        try:
            msg, _ = recv_message(self.sock, self.clock)
        except Exception:
            return

        if isinstance(msg, dict) and msg.get("type") == MSG_GAME_UPDATE:
            self.state = msg.get("state")
            self.last_update_time = time.time()

    # -------------------------------------------------
    def maybe_failover(self):
        if time.time() - self.last_update_time > CLIENT_TIMEOUT:
            if self.server_addr != self.backup_server:
                print("[CLIENT] leader timeout → switching server")
                self.server_addr = self.backup_server
                self._announce()

    # -------------------------------------------------
    def draw(self, screen):
        if not self.state:
            return

        screen.fill(BLACK)

        # authoritative state (from server)
        width = self.state["width"]
        paddle_h = self.state["paddle_height"]
        paddle_w = self.state["paddle_width"]
        ball_size = self.state["ball_size"]
        offset = self.state["paddle_x_offset"]

        # convert center-coordinates to pygame coords
        p1_y = self.state["p1_y"] - paddle_h // 2
        p2_y = self.state["p2_y"] - paddle_h // 2
        ball_x = self.state["ball_x"] - ball_size // 2
        ball_y = self.state["ball_y"] - ball_size // 2

        # paddles
        pygame.draw.rect(screen, WHITE, (offset, p1_y, paddle_w, paddle_h))
        pygame.draw.rect(
            screen, WHITE,
            (width - offset - paddle_w, p2_y, paddle_w, paddle_h)
        )

        # ball
        pygame.draw.rect(
            screen, WHITE,
            (ball_x, ball_y, ball_size, ball_size)
        )

        pygame.display.flip()

    # -------------------------------------------------
    def game_loop(self):
        pygame.init()
        screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
        pygame.display.set_caption(f"Pong Player {self.player}")
        clock = pygame.time.Clock()

        while self.running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False

            keys = pygame.key.get_pressed()
            direction = 0
            if keys[pygame.K_UP]:
                direction = -1
            elif keys[pygame.K_DOWN]:
                direction = 1

            self.maybe_failover()
            self.send_input(direction)
            self.receive_updates()
            self.draw(screen)

            clock.tick(60)

        pygame.quit()


# =====================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--player", type=int, required=True)
    args = parser.parse_args()

    PongClient(args.player).start()


if __name__ == "__main__":
    main()
