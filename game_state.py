from dataclasses import dataclass, asdict
import random

@dataclass
class GameState:
    width: int = 800
    height: int = 400
    paddle_height: int = 80
    paddle_width: int = 10
    paddle_speed: int = 5
    ball_size: int = 10
    p1_y: int = 200
    p2_y: int = 200
    ball_x: int = 400
    ball_y: int = 200
    ball_vx: int = 3
    ball_vy: int = 3
    score1: int = 0
    score2: int = 0
    paddle_x_offset: int = 30

    def step(self, p1_dir: int, p2_dir: int):
        half_p = self.paddle_height // 2
        self.p1_y = max(half_p, min(self.height - half_p, self.p1_y + p1_dir * self.paddle_speed))
        self.p2_y = max(half_p, min(self.height - half_p, self.p2_y + p2_dir * self.paddle_speed))

        self.ball_x += self.ball_vx
        self.ball_y += self.ball_vy

        half_ball = self.ball_size // 2

        if self.ball_y - half_ball <= 0:
            self.ball_y = half_ball
            self.ball_vy = -self.ball_vy
        elif self.ball_y + half_ball >= self.height:
            self.ball_y = self.height - half_ball
            self.ball_vy = -self.ball_vy

        left_paddle_right_x = self.paddle_x_offset + self.paddle_width
        if (self.ball_x - half_ball) <= left_paddle_right_x:
            paddle_top = self.p1_y - half_p
            paddle_bottom = self.p1_y + half_p
            if paddle_top <= self.ball_y <= paddle_bottom:
                self.ball_x = left_paddle_right_x + half_ball
                self.ball_vx = abs(self.ball_vx)

        right_paddle_left_x = self.width - self.paddle_x_offset - self.paddle_width
        if (self.ball_x + half_ball) >= right_paddle_left_x:
            paddle_top = self.p2_y - half_p
            paddle_bottom = self.p2_y + half_p
            if paddle_top <= self.ball_y <= paddle_bottom:
                self.ball_x = right_paddle_left_x - half_ball
                self.ball_vx = -abs(self.ball_vx)

        if self.ball_x + half_ball < 0:
            self.score2 += 1
            self._reset_ball(direction=1)
        elif self.ball_x - half_ball > self.width:
            self.score1 += 1
            self._reset_ball(direction=-1)

    def _reset_ball(self, direction: int = 1):
        self.ball_x = self.width // 2
        self.ball_y = self.height // 2
        self.ball_vx = abs(self.ball_vx) * (1 if direction >= 0 else -1)
        self.ball_vy = random.choice([-1, 1]) * max(1, abs(self.ball_vy))

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**filtered)