import argparse
import pygame
import time
from discovery import client_discover_servers
from common import (
    make_udp_socket, send_message, recv_message,
    CLIENT_PORT, MSG_GAME_INPUT, MSG_GAME_UPDATE, MSG_ROOM_READY
)

SCREEN_WIDTH = 800
SCREEN_HEIGHT = 400
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
RED = (255, 0, 0)

SERVER_TIMEOUT = 2.0 

class PongClient:
    def __init__(self, player: int):
        self.player = player
        self.sock = make_udp_socket(bind_ip="0.0.0.0", bind_port=0)
        self.server_addr = None
        self.state = None
        self.running = True
        self.font = None 
        self.room_ready = False
        self.game_over = False
        self.log_connections_only = True
        
        self.last_update_time = 0
        self.last_seq = -1

    def _find_server(self):
        if not self.log_connections_only:
            print(f"[CLIENT {self.player}] Scanning for servers...")
        while True:
            if pygame.get_init():
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        self.running = False
                        return None

            found = client_discover_servers(timeout=1.0)
            if found:
                sid, sip = found[0]
                print(f"[CLIENT] Found server {sid} at {sip}")
                return (sip, CLIENT_PORT)
            
            if not self.log_connections_only:
                print(".", end="", flush=True)

    def _reconnect(self, screen=None):
        if not self.log_connections_only:
            print("\n[CLIENT] Connection lost! Reconnecting...")
        self.server_addr = None
        
        while self.server_addr is None and self.running:
            if screen and self.font:
                screen.fill(BLACK)
                text = self.font.render("Waiting for opponent...", True, WHITE)
                screen.blit(text, (SCREEN_WIDTH//2 - 200, SCREEN_HEIGHT//2))
                pygame.display.flip()

            self.server_addr = self._find_server()
        
        if self.server_addr:
            self.last_update_time = time.time()
            self.last_seq = -1
            self.state = None
            self.room_ready = False
            self._announce()
            self._wait_for_room_ready(screen)

    def start(self):
        self.server_addr = self._find_server()
        if self.server_addr:
            self.last_update_time = time.time()
            self._announce()
            if self._wait_for_room_ready():
                self.game_loop()
        self._shutdown()

    def _announce(self):
        if self.server_addr:
            send_message(self.sock, self.server_addr, {"type": MSG_GAME_INPUT, "player": self.player, "dir": 0})

    def send_input(self, direction: int):
        if self.server_addr:
            send_message(self.sock, self.server_addr, {"type": MSG_GAME_INPUT, "player": self.player, "dir": direction})

    def receive_updates(self):
        self.sock.settimeout(0.001)     
        while True:
            try:
                msg, _ = recv_message(self.sock)
                msg_type = msg.get("type")
                
                if msg_type == MSG_GAME_UPDATE:
                    seq = msg.get("seq", -1)

                    if seq > self.last_seq:
                        self.last_seq = seq
                        self.state = msg.get("state")
                        self.last_update_time = time.time()

                elif msg_type == "GAME_OVER":
                    self._handle_game_over()
                    return
            
            except Exception:
                break

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

    def game_loop(self, screen=None, clock=None):
        if not pygame.get_init():
            pygame.init()
            pygame.font.init()
        if screen is None:
            screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
        pygame.display.set_caption(f"Pong Player {self.player}")
        if clock is None:
            clock = pygame.time.Clock()
        if self.font is None:
            self.font = pygame.font.Font(None, 50)
        
        while self.running:
            if time.time() - self.last_update_time > SERVER_TIMEOUT:
                self._reconnect(screen)
                clock.tick(60)
                continue

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False

            keys = pygame.key.get_pressed()
            d = 0
            if keys[pygame.K_UP]: d = -1
            elif keys[pygame.K_DOWN]: d = 1

            self.send_input(d)
            self.receive_updates()
            self.draw(screen)
            clock.tick(60)

    def _wait_for_room_ready(self, screen=None):
        self.room_ready = False
        last_announce = 0.0
        if not self.log_connections_only:
            print("[CLIENT] Waiting for opponent...")

        while self.running and not self.room_ready:
            if screen and self.font:
                screen.fill(BLACK)
                text = self.font.render("Waiting for opponent...", True, WHITE)
                screen.blit(text, (SCREEN_WIDTH//2 - 200, SCREEN_HEIGHT//2))
                pygame.display.flip()

            if self.server_addr and (time.time() - last_announce) > 1.0:
                self._announce()
                last_announce = time.time()

            self.sock.settimeout(0.5)
            try:
                msg, _ = recv_message(self.sock)
            except Exception:
                continue

            msg_type = msg.get("type")
            if msg_type == MSG_ROOM_READY:
                self.room_ready = True
                self.last_update_time = time.time()
                return True

            if msg_type == MSG_GAME_UPDATE:
                seq = msg.get("seq", -1)
                if seq > self.last_seq:
                    self.last_seq = seq
                    self.state = msg.get("state")
                    self.last_update_time = time.time()
                self.room_ready = True
                return True

            if msg_type == "GAME_OVER":
                self._handle_game_over()
                return False

        return False

    def _handle_game_over(self):
        print("Game Over. Closing client.")
        self.game_over = True
        self.running = False

    def _shutdown(self):
        try:
            self.sock.close()
        except Exception:
            pass
        if pygame.get_init():
            pygame.quit()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--player", type=int, required=True)
    args = parser.parse_args()
    PongClient(args.player).start()