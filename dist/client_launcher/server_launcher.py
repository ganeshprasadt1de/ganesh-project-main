import tkinter as tk
import subprocess
import sys
import os

def start_server():
    sid = entry_id.get().strip()
    ip = entry_ip.get().strip()

    if not sid or not ip:
        status.config(text="Server ID and IP required", fg="red")
        return

    status.config(text="Starting server...", fg="green")

    # Determine the directory where the exe (or script) is located
    if getattr(sys, 'frozen', False):
        # If running as compiled exe
        base_dir = os.path.dirname(sys.executable)
    else:
        # If running as python script
        base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))

    # Locate the server.py file next to the exe
    server_script = os.path.join(base_dir, "server.py")

    # [FIX] Check if we are frozen (running as exe)
    # If frozen, we CANNOT use sys.executable (which is the exe itself).
    # We must use "python" to run the external .py script.
    if getattr(sys, 'frozen', False):
        executable_cmd = "python"
    else:
        executable_cmd = sys.executable

    subprocess.Popen(
        [
            executable_cmd,  # <--- CHANGED THIS
            server_script,
            "--id", sid,
            "--ip", ip
        ],
        cwd=base_dir,
        creationflags=subprocess.CREATE_NEW_CONSOLE
    )

root = tk.Tk()
root.title("Distributed Pong – Server Launcher")
root.geometry("300x200")

tk.Label(root, text="Server ID").pack()
entry_id = tk.Entry(root)
entry_id.pack()

tk.Label(root, text="Bind IP").pack()
entry_ip = tk.Entry(root)
entry_ip.pack()

tk.Button(root, text="Start Server", command=start_server).pack(pady=10)
status = tk.Label(root, text="")
status.pack()

root.mainloop()