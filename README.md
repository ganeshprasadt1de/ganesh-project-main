# ganesh-project-main


Distributed 2D Ping Pong
A Fault-Tolerant, Distributed Multiplayer Game

This project implements a real-time multiplayer Pong game using a Primary-Backup distributed architecture. It features dynamic server discovery, automatic leader election, and reliable multicast communication to ensure game state consistency across a local network.

Quick Start
We have compiled executables to make launching the system simple. You do not need to install Python on the machine to run these, provided the source scripts are in the folder.

1. Start the Server (Leader)
-> Navigate to dist/server_launcher/.
-> Run server_launcher.exe (Run as Administrator is recommended for network discovery).

Enter the following details:

Server ID: s1
Bind IP: localhost (or your LAN IP, e.g., 192.168.1.5)

Click Start Server.

2. Start the Clients
->Navigate to dist/client_launcher/.
->Run client_launcher.exe.

->Enter Player Number: 1 and click Start.
->Run client_launcher.exe again.
->Enter Player Number: 2 and click Start.

P.S. Make sure that your Windows firewall is not restricting the UDP packets 
