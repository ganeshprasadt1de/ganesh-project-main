# Distributed Ping-Pong Game

A distributed multiplayer Pong game built with the same architectural patterns
as the **Connected Mobility System**.

## Architecture

```
ping-pong/
├── main_server.py              # Server entry point (argparse)
├── main_client.py              # Client entry point (argparse + pygame)
├── config/
│   ├── __init__.py
│   └── settings.py             # Ports, message types, logging, game config
├── components/
│   ├── __init__.py
│   ├── game_message.py         # Typed message class + UDP helpers
│   ├── pong_server.py          # PongServer (rooms, replication, game loop)
│   └── pong_client.py          # PongClient (pygame rendering, input)
├── discovery/
│   ├── __init__.py
│   └── discovery_protocol.py   # UDP beacon-based server discovery
├── election/
│   ├── __init__.py
│   └── bully_election.py       # Bully leader-election algorithm
└── game/
    ├── __init__.py
    ├── game_state.py            # Pure game physics (ball, paddles, scoring)
    └── room.py                  # PongRoom — per-match state container
```

### Key Design Decisions (mirroring CMS)

| CMS Concept                       | Pong Equivalent                               |
| --------------------------------- | --------------------------------------------- |
| `config/settings.py`              | Centralised constants, loggers, file logging  |
| `ClientMessage` / `ServerMessage` | `GameMessage` with type validation            |
| `discovery_protocol.py`           | UDP broadcast request/response discovery      |
| `bully_election.py`               | Extracted Bully Election module               |
| `TCPServer`                       | `PongServer` (rooms, replication, game loop)  |
| `TCPClient`                       | `PongClient` (pygame + reconnection watchdog) |
| Event logging                     | Per-room sequence counters + NACK recovery    |

## How to Run

### Prerequisites

```bash
pip install pygame
```

### Start a server

```bash
cd ping-pong
python main_server.py
# Optional: python main_server.py --logging_level=DEBUG
```

You can start **multiple servers** on different machines (or the same machine
by changing `SERVER_CONTROL_PORT` / `CLIENT_PORT` in `config/settings.py`).
They will auto-discover each other and elect a leader.

### Start clients (players)

```bash
# Terminal 1 — Player 1
python main_client.py --player=1

# Terminal 2 — Player 2
python main_client.py --player=2
```

Players 1 & 2 are matched into **room 0**. Players 3 & 4 go to room 1, etc.

### Controls

| Key | Action    |
| --- | --------- |
| ↑   | Move up   |
| ↓   | Move down |

First to **3 points** wins (configurable via `WIN_SCORE` in settings).

## Distributed Features

- **Leader Election** — Bully algorithm elects the highest-UUID server as
  leader. If the leader crashes, followers detect the timeout and re-elect.
- **State Replication** — The leader simulates the game and replicates room
  snapshots (with sequence numbers) to all followers.
- **Snapshot Sync** — New servers joining the cluster request the full room
  state from peers to catch up.
- **NACK Recovery** — Followers that detect a gap in sequence numbers send a
  NACK to the leader, which retransmits the missing state.
- **Client Reconnection** — Clients automatically re-discover servers if
  updates stop arriving (watchdog timeout).
- **Multi-Room** — Supports concurrent matches with deterministic player-to-room
  routing.
