import sys
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

SERVER_TIMEOUT = 3.0 

class PongClient:
    def __init__(self, player: int):
        self.player = player
        self.sock = make_udp_socket(bind_ip="0.0.0.0", bind_port=0)
        self.server_addr = None
        self.state = None
        self.running = True
        self.font = None 
        
        self.last_update_time = 0
        self.last_seq = -1
        
        self.both_players_ready = False
        self.waiting_for_players = True

    def _find_server(self):
        print(f"[CLIENT {self.player}] Scanning for servers...")
        while True:
            found = client_discover_servers(timeout=1.0)
            if found:
                sid, sip = found[0]
                print(f"[CLIENT {self.player}] Found server {sid} at {sip}")
                return (sip, CLIENT_PORT)
            
            time.sleep(0.5)

    def start(self):
        while self.running:
            print(f"[CLIENT {self.player}] Connecting to server...")
            self.server_addr = self._find_server()
            if not self.server_addr:
                continue
                
            self.last_update_time = time.time()
            self._announce()
            
            print(f"[CLIENT {self.player}] Waiting for both players to join...")
            self.waiting_for_players = True
            self.both_players_ready = False
            
            while self.waiting_for_players and self.running:
                self.sock.settimeout(0.1)
                try:
                    msg, _ = recv_message(self.sock)
                    if msg.get("type") == MSG_GAME_UPDATE:
                        seq = msg.get("seq", -1)
                        if seq > self.last_seq:
                            self.last_seq = seq
                            self.state = msg.get("state")
                            self.last_update_time = time.time()
                            if seq > 0:
                                self.waiting_for_players = False
                                self.both_players_ready = True
                                print(f"[CLIENT {self.player}] Both players ready! Starting game...")
                                break
                except Exception:
                    pass
                
                if time.time() - self.last_update_time > SERVER_TIMEOUT:
                    print(f"[CLIENT {self.player}] Server timeout during waiting, reconnecting...")
                    break
                
                self.send_input(0)
                time.sleep(0.5)
            
            if self.both_players_ready:
                self.game_loop()
                if self.running:
                    continue

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
                
                if msg.get("type") == MSG_GAME_UPDATE:
                    seq = msg.get("seq", -1)

                    if seq > self.last_seq:
                        self.last_seq = seq
                        self.state = msg.get("state")
                        self.last_update_time = time.time()

                elif msg.get("type") == "GAME_OVER":
                    print("Game Over. Closing client.")
                    pygame.quit()
                    sys.exit(0)
            
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

    def game_loop(self):
        pygame.init()
        pygame.font.init()
        screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
        pygame.display.set_caption(f"Pong Player {self.player}")
        clock = pygame.time.Clock()
        self.font = pygame.font.Font(None, 50)
        
        while self.running:
            if time.time() - self.last_update_time > SERVER_TIMEOUT:
                print(f"[CLIENT {self.player}] Server timeout detected")
                pygame.quit()
                return

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

        pygame.quit()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--player", type=int, required=True)
    args = parser.parse_args()
    PongClient(args.player).start()