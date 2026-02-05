# Distributed 2D Ping Pong

A fault-tolerant, distributed two-player Ping Pong game using a client-server model with leader election and reliable ordered multicast.

## Prerequisites
- Python 3.x
- Pygame (`pip install pygame`)

## How to Run

### 1. Start the Server(s)
You can run multiple instances of the server on the same machine or different machines in the same network. The servers will automatically discover each other and elect a leader using the Bully Algorithm.

Open a terminal and run:
```bash
python server.py


Open another 2 terminals and run:
python client.py --player 1

python client.py --player 2
