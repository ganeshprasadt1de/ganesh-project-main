# client.py
# FINAL VERIFIED CLIENT — matches GameState schema exactly

import argparse
import pygame

from lamport_clock import LamportClock
from common import (
    make_udp_socket, send_message, recv_message,
    CLIENT_PORT, MSG_GAME_INPUT, MSG_GAME_UPDATE
)
from game_state import GameState

# =====================================================
# CHANGE ONLY IF LEADER CHANGES
# =====================================================
LEADER_IP = "192.168.0.61"   # s2 (Laptop M)
SERVER_ADDR = (LEADER_IP, CLIENT_PORT)

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


class PongClient:
    def __init__(self, player: int):
        self.player = player
        self.clock = LamportClock()
        self.server_addr = SERVER_ADDR
        self.sock = make_udp_socket(bind_ip="", bind_port=0)

        self.state = None
        self.running = True

    def start(self):
        print(f"[CLIENT {self.player}] connecting to server {self.server_addr}")

        # HELLO so server learns client address
        send_message(
            self.sock,
            self.server_addr,
            {"type": MSG_GAME_INPUT, "player": self.player, "dir": 0},
            self.clock
        )

        self.game_loop()

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

    def draw(self, screen):
        if not self.state:
            return

        screen.fill(BLACK)

        # --- extract authoritative values ---
        width = self.state["width"]
        height = self.state["height"]
        paddle_h = self.state["paddle_height"]
        paddle_w = self.state["paddle_width"]
        ball_size = self.state["ball_size"]
        offset = self.state["paddle_x_offset"]

        # center → top-left conversion
        p1_y = self.state["p1_y"] - paddle_h // 2
        p2_y = self.state["p2_y"] - paddle_h // 2
        ball_x = self.state["ball_x"] - ball_size // 2
        ball_y = self.state["ball_y"] - ball_size // 2

        # paddles
        pygame.draw.rect(screen, WHITE, (offset, p1_y, paddle_w, paddle_h))
        pygame.draw.rect(screen, WHITE, (width - offset - paddle_w, p2_y, paddle_w, paddle_h))

        # ball
        pygame.draw.rect(screen, WHITE, (ball_x, ball_y, ball_size, ball_size))

        pygame.display.flip()

    def game_loop(self):
        pygame.init()
        screen = pygame.display.set_mode((800, 400))
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

            self.send_input(direction)
            self.receive_updates()
            self.draw(screen)

            clock.tick(60)

        pygame.quit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--player", type=int, required=True)
    args = parser.parse_args()

    PongClient(args.player).start()


if __name__ == "__main__":
    main()
