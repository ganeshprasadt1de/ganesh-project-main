# client.py
# Pong client (pygame) — compatible with server.py and game_state.py
# - Expects GameState using center coordinates and fields like score1/score2, paddle_x_offset, paddle_width, ball_size

import threading
import time
import socket
import argparse
import json
import pygame
from typing import Optional, Tuple

from lamport_clock import LamportClock
from discovery import client_discover_servers
from common import (
    make_udp_socket, recv_message, send_message,
    CLIENT_PORT, SERVER_CONTROL_PORT,
    MSG_GAME_INPUT, MSG_GAME_UPDATE, MSG_HEARTBEAT,
)

# UI constants (fallbacks — replaced by values from server state when available)
FPS = 60
DEFAULT_WIDTH = 800
DEFAULT_HEIGHT = 400
DEFAULT_PADDLE_H = 80
DEFAULT_PADDLE_W = 10
DEFAULT_PADDLE_X = 30
DEFAULT_BALL = 10

class PongClient:
    def __init__(self, player: int = 1, discover_timeout: float = 1.0):
        self.player = player  # 1 or 2
        self.discover_timeout = discover_timeout

        self.clock = LamportClock()

        # network socket used for client <-> server messages (ephemeral bound port)
        # bind to all interfaces so server can reach us
        self.sock = make_udp_socket(bind_ip="", bind_port=0)
        self.sock.settimeout(0.5)

        self.server_addr: Optional[Tuple[str, int]] = None  # (ip, CLIENT_PORT)
        self.control_addr: Optional[Tuple[str, int]] = None  # (ip, SERVER_CONTROL_PORT) if needed

        # state mirrored from server (defaults until we receive update)
        self.state = {
            "width": DEFAULT_WIDTH,
            "height": DEFAULT_HEIGHT,
            "paddle_height": DEFAULT_PADDLE_H,
            "paddle_width": DEFAULT_PADDLE_W,
            "paddle_x_offset": DEFAULT_PADDLE_X,
            "ball_size": DEFAULT_BALL,
            "p1_y": DEFAULT_HEIGHT // 2,
            "p2_y": DEFAULT_HEIGHT // 2,
            "ball_x": DEFAULT_WIDTH // 2,
            "ball_y": DEFAULT_HEIGHT // 2,
            "score1": 0,
            "score2": 0,
        }
        self.running = True
        self.update_lock = threading.Lock()

        # thread for network receive
        self.net_thread = threading.Thread(target=self.network_loop, daemon=True)

    def discover_and_register(self):
        """Use discovery to find servers; pick the first result and register our client socket by sending a hello."""
        servers = client_discover_servers(timeout=self.discover_timeout, clock=self.clock)
        # client_discover_servers returns list of (sid, ip) tuples (or empty)
        if not servers:
            return False

        # pick the first server
        sid, ip = servers[0]
        self.server_addr = (ip, CLIENT_PORT)
        self.control_addr = (ip, SERVER_CONTROL_PORT)
        # send an initial hello so the server learns our client address (server.client_loop tracks addrs from incoming messages)
        hello = {"type": "CLIENT_HELLO", "player": self.player}
        try:
            send_message(self.sock, self.server_addr, hello, self.clock)
        except Exception:
            # best-effort — even if send fails, we'll still attempt to receive updates
            pass
        return True

    def start(self):
        found = self.discover_and_register()
        if not found:
            print("Warning: no server discovered (discovery returned nothing). Will still run and wait for manual server messages.")
        self.net_thread.start()
        self.run_ui()

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass

    def network_loop(self):
        """Receives network messages (authoritative GAME_UPDATE from leader or from servers)."""
        while self.running:
            try:
                msg, addr = recv_message(self.sock, self.clock)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception:
                # ignore malformed / transient network errors
                continue

            if not isinstance(msg, dict):
                continue

            mtype = msg.get("type")
            if mtype == MSG_GAME_UPDATE:
                # update contains 'state' key with GameState as dict
                st = msg.get("state")
                if isinstance(st, dict):
                    with self.update_lock:
                        # merge server state (only known keys)
                        self.state.update(st)
                        # ensure some keys exist (defensive)
                        self.state.setdefault("width", DEFAULT_WIDTH)
                        self.state.setdefault("height", DEFAULT_HEIGHT)
                        self.state.setdefault("paddle_height", DEFAULT_PADDLE_H)
                        self.state.setdefault("paddle_width", DEFAULT_PADDLE_W)
                        self.state.setdefault("paddle_x_offset", DEFAULT_PADDLE_X)
                        self.state.setdefault("ball_size", DEFAULT_BALL)
                        self.state.setdefault("score1", 0)
                        self.state.setdefault("score2", 0)
            elif mtype == MSG_HEARTBEAT:
                # could show leader alive; ignored for now
                pass
            # ignore other message types

    def send_input(self, direction: int):
        """
        Send a player input to server.
        direction: -1 up, 1 down, 0 none
        """
        if not self.server_addr:
            # try discovery once more
            ok = self.discover_and_register()
            if not ok:
                return
        msg = {"type": MSG_GAME_INPUT, "player": self.player, "dir": int(direction)}
        try:
            send_message(self.sock, self.server_addr, msg, self.clock)
        except Exception:
            # best effort — ignore send errors
            pass

    def run_ui(self):
        """Main pygame loop (runs in main thread)."""
        pygame.init()
        try:
            # window size from initial state (may be updated later)
            with self.update_lock:
                width = int(self.state.get("width", DEFAULT_WIDTH))
                height = int(self.state.get("height", DEFAULT_HEIGHT))
            screen = pygame.display.set_mode((width, height))
            pygame.display.set_caption(f"Pong Client (player {self.player})")
            clock = pygame.time.Clock()

            while self.running:
                for ev in pygame.event.get():
                    if ev.type == pygame.QUIT:
                        self.stop()
                        break
                    elif ev.type == pygame.KEYDOWN:
                        if ev.key in (pygame.K_w, pygame.K_UP):
                            self.send_input(-1)
                        elif ev.key in (pygame.K_s, pygame.K_DOWN):
                            self.send_input(1)
                    elif ev.type == pygame.KEYUP:
                        # stop movement on key up for both keys
                        if ev.key in (pygame.K_w, pygame.K_s, pygame.K_UP, pygame.K_DOWN):
                            self.send_input(0)

                # draw state (copy under lock)
                with self.update_lock:
                    st = dict(self.state)

                width = int(st.get("width", DEFAULT_WIDTH))
                height = int(st.get("height", DEFAULT_HEIGHT))
                paddle_h = int(st.get("paddle_height", DEFAULT_PADDLE_H))
                paddle_w = int(st.get("paddle_width", DEFAULT_PADDLE_W))
                paddle_x = int(st.get("paddle_x_offset", DEFAULT_PADDLE_X))
                bx = int(st.get("ball_x", width // 2))
                by = int(st.get("ball_y", height // 2))
                ball_size = int(st.get("ball_size", DEFAULT_BALL))
                p1_y = int(st.get("p1_y", height // 2))
                p2_y = int(st.get("p2_y", height // 2))
                score1 = int(st.get("score1", 0))
                score2 = int(st.get("score2", 0))

                # ensure screen size matches server size (resize if needed)
                if screen.get_width() != width or screen.get_height() != height:
                    screen = pygame.display.set_mode((width, height))

                screen.fill((0, 0, 0))

                # convert center coords to top-left rects for drawing
                half_p = paddle_h / 2
                p1_rect = pygame.Rect(paddle_x, int(p1_y - half_p), paddle_w, paddle_h)
                p2_rect = pygame.Rect(width - paddle_x - paddle_w, int(p2_y - half_p), paddle_w, paddle_h)
                ball_rect = pygame.Rect(int(bx - ball_size / 2), int(by - ball_size / 2), ball_size, ball_size)

                # draw paddles and ball
                pygame.draw.rect(screen, (255, 255, 255), p1_rect)
                pygame.draw.rect(screen, (255, 255, 255), p2_rect)
                pygame.draw.ellipse(screen, (255, 255, 255), ball_rect)

                # draw score (center top)
                font = pygame.font.SysFont(None, 36)
                score_surf = font.render(f"{score1} : {score2}", True, (255, 255, 255))
                screen.blit(score_surf, ((width - score_surf.get_width()) // 2, 10))

                pygame.display.flip()
                clock.tick(FPS)
        finally:
            pygame.quit()
            self.stop()

# CLI
def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--player", type=int, choices=[1,2], default=1)
    ap.add_argument("--discover-timeout", type=float, default=1.0)
    return ap.parse_args()

def main():
    args = parse_args()
    client = PongClient(player=args.player, discover_timeout=args.discover_timeout)
    try:
        client.start()
    except KeyboardInterrupt:
        client.stop()

if __name__ == "__main__":
    main()
