import tkinter as tk
import subprocess
import sys
import os

def start_client():
    player = entry_player.get().strip()

    if player not in ("1", "2"):
        status.config(text="Player must be 1 or 2", fg="red")
        return

    status.config(text="Starting client...", fg="green")

    # Determine directory
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))

    client_script = os.path.join(base_dir, "client.py")

    # [FIX] Logic for EXE vs Script
    if getattr(sys, 'frozen', False):
        executable_cmd = "python"
    else:
        executable_cmd = sys.executable

    subprocess.Popen(
        [
            executable_cmd, 
            client_script,
            "--player", player
        ],
        cwd=base_dir,
        creationflags=subprocess.CREATE_NEW_CONSOLE
    )

root = tk.Tk()
root.title("Distributed Pong – Client Launcher")
root.geometry("300x150")

tk.Label(root, text="Player Number (1 or 2)").pack()
entry_player = tk.Entry(root)
entry_player.pack()

tk.Button(root, text="Start Client", command=start_client).pack(pady=10)
status = tk.Label(root, text="")
status.pack()

root.mainloop()