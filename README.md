# Distributed 2D Ping Pong 🏓

> A fault-tolerant, distributed multiplayer game implementing dynamic discovery, leader election, and reliable ordered multicast using Python.

![Python](https://img.shields.io/badge/Python-3.x-blue.svg)
![Architecture](<https://img.shields.io/badge/Architecture-Hybrid%20(P2P%20%2B%20Client--Server)-orange>)
![Protocol](https://img.shields.io/badge/Protocol-UDP-green)
![Library](https://img.shields.io/badge/Library-Pygame-red)

## 📖 Overview

This project is a distributed implementation of the classic Pong game. Unlike a standard multiplayer game, this system is designed to demonstrate core **Distributed Systems (DS)** concepts. It features a **Hybrid Architecture** where servers form a Peer-to-Peer (P2P) cluster for coordination and fault tolerance, while clients connect to the active Leader via a Client-Server model.

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
    - Active nodes reply with their identity (`UUID`, `IP`).
    - **Code:** `discovery_protocol.py`.

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

## 🔌 Ports and Traffic

All communication uses UDP sockets. Ports are defined in `settings.py`.

- **50000 (DISCOVERY_PORT):** UDP broadcast for discovery requests/responses between servers and clients.
- **50010 (SERVER_CONTROL_PORT):** Server-to-server control plane (heartbeats, election, replication, snapshots, ACK/NACK).
- **50020 (CLIENT_PORT):** Client-to-leader game traffic (inputs, room ready, game updates, game over).

Clients bind to an ephemeral local port (bind port `0`) and send to the leader's `CLIENT_PORT`.

---

## 📂 Project Structure

```text
ganesh-project-main/
├── main_server.py            # Entry point for server nodes (current)
├── main_client.py            # Entry point for clients (current)
├── pong_server.py            # Server runtime (rooms, replication, control plane)
├── pong_client.py            # Client runtime (Pygame loop, inputs, rendering)
├── room.py                   # Room/session state and sequencing
├── game_room_state.py        # Physics engine used by rooms
├── bully_election.py         # Bully election + heartbeat logic
├── discovery_protocol.py     # UDP discovery (broadcast + responses)
├── game_message.py           # Message serialization + UDP helpers
├── settings.py               # Ports, timeouts, logging
├── logs/                     # Runtime logs
├── server.py                 # Legacy all-in-one server (not used by main_server)
├── client.py                 # Legacy all-in-one client (not used by main_client)
├── discovery.py              # Legacy discovery module (not used by current flow)
├── common.py                 # Legacy shared utilities
└── game_state.py             # Legacy physics model
```
