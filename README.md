# Distributed 2D Ping Pong 🏓

> A fault-tolerant, distributed multiplayer game implementing dynamic discovery, leader election, and reliable ordered multicast using Python.

![Python](https://img.shields.io/badge/Python-3.x-blue.svg)
![Architecture](<https://img.shields.io/badge/Architecture-Hybrid%20(P2P%20%2B%20Client--Server)-orange>)
![Protocol](https://img.shields.io/badge/Protocol-UDP-green)
![Library](https://img.shields.io/badge/Library-Pygame-red)

## 📖 Overview

This project is a distributed implementation of the classic Pong game. Unlike a standard multiplayer game, this system is designed to demonstrate core **Distributed Systems (DS)** concepts. It features a **Hybrid Architecture** where servers form a Peer-to-Peer (P2P) ring for coordination and fault tolerance, while clients connect to the active Leader via a Client-Server model.

The system is resilient to server failures, automatically handling leader crashes through the **Bully Algorithm** and allowing new nodes to discover the cluster dynamically without hardcoded IP addresses.

---

## 🏗️ System Architecture

The system operates on a **Hybrid Model** combining P2P and Client-Server patterns:

1.  **Server Cluster (P2P Layer):**
    - Servers communicate via UDP to maintain a synchronized game state.
    - **Dynamic Discovery:** Servers use UDP broadcasting to find each other on the local subnet.
    - **Leader Election:** The **Bully Algorithm** ensures that the server with the highest UUID becomes the Leader (Coordinator).
    - **Replication:** The Leader acts as a **Sequencer**, processing game logic and replicating the state to Follower servers using a primary-backup approach.

2.  **Client Layer (Client-Server Layer):**
    - Clients (Players) discover the cluster via broadcast and connect to the current Leader.
    - Clients are "dumb terminals"—they send inputs (`UP`/`DOWN`) and render the `GameState` received from the Leader.
    - **Consistency:** Clients use a "Latest State Wins" strategy to handle out-of-order UDP packets, ensuring smooth gameplay.

---

## 🧩 Key Distributed Features

### 1. Dynamic Discovery 📡

- **Goal:** Eliminate the need for hardcoded IP addresses/ports in configuration files.
- **Implementation:**
    - New nodes send a `MSG_DISCOVER_REQUEST` to the subnet broadcast address.
    - Active nodes reply with their identity (`UUID`, `IP`, `Port`).
    - **Code:** `discovery_protocol.py`, `discovery.py`.

### 2. Fault Tolerance (Leader Election) 👑

- **Goal:** Ensure the game continues if the server hosting the game crashes.
- **Algorithm:** **Bully Algorithm**.
- **Mechanism:**
    - Servers exchange `HEARTBEAT` messages.
    - If the Leader fails (heartbeat timeout), a Follower initiates an election.
    - The node with the highest `UUID` bullies others to become the new Coordinator.
    - **Code:** `bully_election.py`.

### 3. Reliable Ordered Multicast (Sequencer) 🔄

- **Goal:** Ensure all participants see the same game events in the same order.
- **Implementation:**
    - **Total Ordering:** The Leader assigns a monotonically increasing sequence number (`seq`) to every game update.
    - **Gap Detection:** Follower servers use `ACK`/`NACK` to request missing state updates from the Leader (Strict Consistency).
    - **Client Optimization:** Clients discard updates with old sequence numbers to prevent "rubber-banding" (Real-time Consistency).
    - **Code:** `room.py`, `pong_server.py`.

---

## 📂 Project Structure

```text
distributed-pong/
├── main_server.py            # Entry point for Server nodes
├── main_client.py            # Entry point for Clients (Players)
├── config/
│   └── settings.py           # Constants (Ports, Timeouts, Logging)
├── components/
│   ├── pong_server.py        # Server logic (State replication, Room mgmt)
│   ├── pong_client.py        # Client logic (Pygame loop, Input handling)
│   └── game_message.py       # UDP Message serialization/deserialization
├── discovery/
│   ├── discovery_protocol.py # Broadcast logic
│   └── discovery.py          # Listener & Sender wrappers
├── election/
│   └── bully_election.py     # Leader Election implementation
└── game/
    ├── room.py               # Game Session (Sequencer logic)
    └── game_state.py         # Physics engine (Pure logic)
```
