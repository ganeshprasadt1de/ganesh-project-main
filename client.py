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

# Timeout in seconds before client decides server is dead
SERVER_TIMEOUT = 6.0  # Increased to match server timeout 

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
        self.last_seq = -1
        
        # Track if both players in our room have joined
        self.both_players_ready = False
        self.waiting_for_players = True

    def _find_server(self):
        """Blocking search for a server."""
        print(f"[CLIENT {self.player}] Scanning for servers...")
        while True:
            found = client_discover_servers(timeout=1.0)
            if found:
                sid, sip = found[0]
                print(f"[CLIENT {self.player}] Found server {sid} at {sip}")
                return (sip, CLIENT_PORT)
            
            # Brief pause before retry
            time.sleep(0.5)

    def start(self):
        # Initial connection (no pygame window yet)
        while self.running:
            print(f"[CLIENT {self.player}] Connecting to server...")
            self.server_addr = self._find_server()
            if not self.server_addr:
                continue
                
            self.last_update_time = time.time()
            self._announce()
            
            # Wait for both players before opening pygame window
            print(f"[CLIENT {self.player}] Waiting for both players to join...")
            self.waiting_for_players = True
            self.both_players_ready = False
            
            while self.waiting_for_players and self.running:
                # Check for state updates
                self.sock.settimeout(0.1)
                try:
                    msg, _ = recv_message(self.sock)
                    if msg.get("type") == MSG_GAME_UPDATE:
                        seq = msg.get("seq", -1)
                        if seq > self.last_seq:
                            self.last_seq = seq
                            self.state = msg.get("state")
                            self.last_update_time = time.time()
                            # Check if game has started (both players present)
                            # Game starts when seq > 0, meaning both players are connected
                            if seq > 0:
                                self.waiting_for_players = False
                                self.both_players_ready = True
                                print(f"[CLIENT {self.player}] Both players ready! Starting game...")
                                break
                except:
                    pass
                
                # Check for server timeout during waiting
                if time.time() - self.last_update_time > SERVER_TIMEOUT:
                    print(f"[CLIENT {self.player}] Server timeout during waiting, reconnecting...")
                    break  # Break inner loop to reconnect
                
                # Send periodic inputs to keep connection alive
                self.send_input(0)
                time.sleep(0.5)
            
            # If both players ready, start the game loop
            if self.both_players_ready:
                self.game_loop()
                # If game loop exits due to timeout, we'll reconnect
                if self.running:
                    continue  # Reconnect and wait again

    def _announce(self):
        if self.server_addr:
            # Send initial input 0 to register the player with the new server/leader
            send_message(self.sock, self.server_addr, {"type": MSG_GAME_INPUT, "player": self.player, "dir": 0})

    def send_input(self, direction: int):
        if self.server_addr:
            send_message(self.sock, self.server_addr, {"type": MSG_GAME_INPUT, "player": self.player, "dir": direction})

    def receive_updates(self):
        self.sock.settimeout(0.001)     
        while True:
            try:
                # Keep reading until the socket raises an exception (is empty)
                msg, _ = recv_message(self.sock)
                
                # Update state if valid
                if msg.get("type") == MSG_GAME_UPDATE:
                    seq = msg.get("seq", -1)

                    # Only process if this packet is newer than what we have
                    if seq > self.last_seq:
                        self.last_seq = seq
                        self.state = msg.get("state")
                        # Update watchdog
                        self.last_update_time = time.time()

                # =====================================================
                # POISON PILL → terminate client immediately
                # =====================================================
                elif msg.get("type") == "GAME_OVER":
                    print("Game Over. Closing client.")
                    pygame.quit()
                    sys.exit(0)
                # =====================================================
            
            except Exception:
                # No more data in the socket buffer, break the loop
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
            # 1. Check Watchdog (Fault Tolerance)
            if time.time() - self.last_update_time > SERVER_TIMEOUT:
                print(f"[CLIENT {self.player}] Server timeout detected")
                pygame.quit()
                return  # Exit game_loop to reconnect in start()

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