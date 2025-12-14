# client.py
# FINAL VERIFIED CLIENT - DYNAMIC DISCOVERY
# - No hardcoded IPs
# - Auto-discovers server on startup
# - Auto-scans for new server on failure
# - Windows-safe

import argparse
import pygame
import time

from lamport_clock import LamportClock
from discovery import client_discover_servers  # <--- NEW IMPORT
from common import (
    make_udp_socket, send_message, recv_message,
    CLIENT_PORT, MSG_GAME_INPUT, MSG_GAME_UPDATE
)

CLIENT_TIMEOUT = 2.0  # seconds without updates before rescanning

# =====================================================
# Rendering constants
# =====================================================
SCREEN_WIDTH = 800
SCREEN_HEIGHT = 400
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
GRAY  = (100, 100, 100)

class PongClient:
    def __init__(self, player: int):
        self.player = player
        self.clock = LamportClock()
        self.sock = make_udp_socket(bind_ip="", bind_port=0)

        # --- DYNAMIC DISCOVERY ---
        self.server_addr = self._find_server()
        
        self.state = None
        self.last_update_time = time.time()
        self.running = True
        self.font = None 

    def _find_server(self):
        """Helper to scan for servers until one is found."""
        print(f"[CLIENT {self.player}] Scanning for servers...")
        while True:
            # Scan for 2 seconds
            found = client_discover_servers(timeout=2.0)
            if found:
                # Pick the first one found
                sid, sip = found[0]
                print(f"[CLIENT] Found server {sid} at {sip}")
                return (sip, CLIENT_PORT)
            
            print("[CLIENT] No servers found. Retrying...")
            time.sleep(1)

    # -------------------------------------------------
    def start(self):
        print(f"[CLIENT {self.player}] Connected to {self.server_addr}")
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
        if self.server_addr:
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
        # If we haven't heard from the leader in a while, scan for a new one
        if time.time() - self.last_update_time > CLIENT_TIMEOUT:
            print("[CLIENT] Connection lost. Rescanning...")
            
            # Try to find a new server (this blocks for ~2 seconds)
            new_addr = self._find_server()
            
            if new_addr:
                self.server_addr = new_addr
                self._announce()
                self.last_update_time = time.time()

    # -------------------------------------------------
    def draw(self, screen):
        if not self.state:
            screen.fill(BLACK)
            if self.font:
                txt = self.font.render("Connecting...", True, WHITE)
                screen.blit(txt, (SCREEN_WIDTH//2 - 60, SCREEN_HEIGHT//2))
            pygame.display.flip()
            return

        screen.fill(BLACK)

        # Draw Scores
        if self.font:
            s1 = self.state.get("score1", 0)
            s2 = self.state.get("score2", 0)
            t1 = self.font.render(str(s1), True, WHITE)
            t2 = self.font.render(str(s2), True, WHITE)
            screen.blit(t1, (SCREEN_WIDTH//4, 20))
            screen.blit(t2, (SCREEN_WIDTH*3//4, 20))
            pygame.draw.line(screen, GRAY, (SCREEN_WIDTH//2, 0), (SCREEN_WIDTH//2, SCREEN_HEIGHT), 1)

        # Draw Game Objects
        width = self.state["width"]
        paddle_h = self.state["paddle_height"]
        paddle_w = self.state["paddle_width"]
        ball_size = self.state["ball_size"]
        offset = self.state["paddle_x_offset"]

        p1_y = self.state["p1_y"] - paddle_h // 2
        p2_y = self.state["p2_y"] - paddle_h // 2
        ball_x = self.state["ball_x"] - ball_size // 2
        ball_y = self.state["ball_y"] - ball_size // 2

        pygame.draw.rect(screen, WHITE, (offset, p1_y, paddle_w, paddle_h))
        pygame.draw.rect(screen, WHITE, (width - offset - paddle_w, p2_y, paddle_w, paddle_h))
        pygame.draw.rect(screen, WHITE, (ball_x, ball_y, ball_size, ball_size))

        pygame.display.flip()

    # -------------------------------------------------
    def game_loop(self):
        pygame.init()
        pygame.font.init()
        screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
        pygame.display.set_caption(f"Pong Player {self.player}")
        clock = pygame.time.Clock()
        self.font = pygame.font.Font(None, 50)

        while self.running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False

            keys = pygame.key.get_pressed()
            d = 0
            if keys[pygame.K_UP]: d = -1
            elif keys[pygame.K_DOWN]: d = 1

            self.maybe_failover()
            self.send_input(d)
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