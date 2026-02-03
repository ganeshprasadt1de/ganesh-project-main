import argparse
import pygame
import time
from discovery import client_discover_servers
from common import (
    make_udp_socket, send_message, recv_message,
    CLIENT_PORT, MSG_GAME_INPUT, MSG_GAME_UPDATE
)

SCREEN_WIDTH = 800
SCREEN_HEIGHT = 400
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
RED = (255, 0, 0)

# Timeout in seconds before client decides server is dead
SERVER_TIMEOUT = 2.0 

class PongClient:
    def __init__(self, player: int):
        self.player = player
        self.sock = make_udp_socket(bind_ip="0.0.0.0", bind_port=0)
        self.server_addr = None # Will be set in _reconnect or _find_server
        self.state = None
        self.running = True
        self.font = None 
        
        # FAULT TOLERANCE: Track when we last heard from server
        self.last_update_time = 0 

    def _find_server(self):
        """Blocking search for a server."""
        print(f"[CLIENT {self.player}] Scanning for servers...")
        while True:
            # Check for quit events so we don't hang if user closes window during search
            if pygame.get_init():
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        self.running = False
                        return None

            found = client_discover_servers(timeout=1.0) # Short timeout for responsiveness
            if found:
                sid, sip = found[0]
                print(f"[CLIENT] Found server {sid} at {sip}")
                return (sip, CLIENT_PORT)
            
            # Optional: Visual feedback in console
            print(".", end="", flush=True)

    def _reconnect(self, screen=None):
        """Called when connection is lost. Loops until a new server is found."""
        print("\n[CLIENT] Connection lost! Reconnecting...")
        self.server_addr = None
        
        while self.server_addr is None and self.running:
            # Draw "Reconnecting" screen
            if screen and self.font:
                screen.fill(BLACK)
                text = self.font.render("Connection Lost...", True, RED)
                text2 = self.font.render("Searching for new Server...", True, WHITE)
                screen.blit(text, (SCREEN_WIDTH//2 - 150, SCREEN_HEIGHT//2 - 30))
                screen.blit(text2, (SCREEN_WIDTH//2 - 200, SCREEN_HEIGHT//2 + 20))
                pygame.display.flip()

            # Attempt discovery
            self.server_addr = self._find_server()
        
        # Once found, announce presence immediately
        if self.server_addr:
            self.last_update_time = time.time()
            self._announce()

    def start(self):
        # Initial connection
        self.server_addr = self._find_server()
        if self.server_addr:
            self.last_update_time = time.time()
            self._announce()
            self.game_loop()

    def _announce(self):
        if self.server_addr:
            # Send initial input 0 to register the player with the new server/leader
            send_message(self.sock, self.server_addr, {"type": MSG_GAME_INPUT, "player": self.player, "dir": 0})

    def send_input(self, direction: int):
        if self.server_addr:
            send_message(self.sock, self.server_addr, {"type": MSG_GAME_INPUT, "player": self.player, "dir": direction})

    def receive_updates(self):
        self.sock.settimeout(0.001)
        try:
            msg, _ = recv_message(self.sock)
        except Exception:
            return

        if msg.get("type") == MSG_GAME_UPDATE:
            self.state = msg.get("state")
            # FAULT TOLERANCE: Update watchdog timestamp
            self.last_update_time = time.time()

    def draw(self, screen):
        if not self.state:
            screen.fill(BLACK)
            if self.font:
                text = self.font.render("Waiting for State...", True, WHITE)
                screen.blit(text, (SCREEN_WIDTH//2 - 150, SCREEN_HEIGHT//2))
            pygame.display.flip()
            return

        screen.fill(BLACK)
        if self.font:
            s1 = self.state.get("score1", 0)
            s2 = self.state.get("score2", 0)
            screen.blit(self.font.render(str(s1), True, WHITE), (SCREEN_WIDTH//4, 20))
            screen.blit(self.font.render(str(s2), True, WHITE), (SCREEN_WIDTH*3//4, 20))

        paddle_h = self.state["paddle_height"]
        paddle_w = self.state["paddle_width"]
        ball_size = self.state["ball_size"]
        offset = self.state["paddle_x_offset"]

        pygame.draw.rect(screen, WHITE, (offset, self.state["p1_y"] - paddle_h // 2, paddle_w, paddle_h))
        pygame.draw.rect(screen, WHITE, (self.state["width"] - offset - paddle_w, self.state["p2_y"] - paddle_h // 2, paddle_w, paddle_h))
        pygame.draw.rect(screen, WHITE, (self.state["ball_x"] - ball_size // 2, self.state["ball_y"] - ball_size // 2, ball_size, ball_size))

        pygame.display.flip()

    def game_loop(self):
        pygame.init()
        pygame.font.init()
        screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
        pygame.display.set_caption(f"Pong Player {self.player}")
        clock = pygame.time.Clock()
        self.font = pygame.font.Font(None, 50)
        
        while self.running:
            # 1. Check Watchdog (Fault Tolerance)
            if time.time() - self.last_update_time > SERVER_TIMEOUT:
                self._reconnect(screen)
                clock.tick(60)
                continue

            # 2. Event Handling
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False

            keys = pygame.key.get_pressed()
            d = 0
            if keys[pygame.K_UP]: d = -1
            elif keys[pygame.K_DOWN]: d = 1

            # 3. Network & Draw
            self.send_input(d)
            self.receive_updates()
            self.draw(screen)
            clock.tick(60)

        pygame.quit()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--player", type=int, required=True)
    args = parser.parse_args()
    PongClient(args.player).start()