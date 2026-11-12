"""Put the chat system on a public URL with a Cloudflare quick tunnel.

    python share.py [--port 5050] [--web-port 8080]

Starts the chat server, the web gateway, and a tunnel to the gateway, then
prints one link anybody can open from any network.

Why only the gateway is tunnelled
---------------------------------
The browser client asks for "/api/..." relative to whatever address it was
loaded from, and the gateway reaches the chat server over localhost. So a
single HTTP tunnel in front of the gateway carries the whole system, and no
code has to change. The raw TCP port stays local: tunnelling it as well would
need a TCP tunnel and cloudflared installed on every machine that wanted to
run client.py, which is a lot of setup to avoid opening a browser.

Terminal clients therefore still connect over the local network, with --host.

Anyone with the link can join
-----------------------------
There is no password and no encryption (see PROTOCOL.md section 10), so while
this is running, the link is the only thing standing between a stranger and
your rooms. The URL is random and unguessable, which is real but thin
protection. Stop the tunnel when you are finished: Ctrl+C here ends all three
processes.
"""

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
CLOUDFLARED_HELP = """
cloudflared is not installed, so there is nothing to build the tunnel with.

    winget install --id Cloudflare.cloudflared

or download it from
https://github.com/cloudflare/cloudflared/releases (cloudflared-windows-amd64.exe),
rename it to cloudflared.exe, and put it somewhere on your PATH.
"""


def find_cloudflared():
    """Locate cloudflared, returning its path or None.

    Installing it does not always put it on PATH straight away: on Windows the
    change only reaches processes started after the next sign-in, so look in the
    usual places too rather than telling someone it is missing when it is not.
    """
    found = shutil.which("cloudflared")
    if found:
        return found
    candidates = [
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        r"C:\Program Files\cloudflared\cloudflared.exe",
        os.path.join(HERE, "cloudflared.exe"),
        os.path.join(HERE, "cloudflared"),
        "/usr/local/bin/cloudflared",
        "/usr/bin/cloudflared",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def port_is_busy(port):
    """True if something already answers on the port. Checked before starting
    anything: a second copy of the servers silently stealing half the traffic is
    far harder to diagnose than a refusal up front."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return True
    except OSError:
        return False


def wait_for_port(port, timeout=15):
    """Return True once something is listening on the port."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def spawn(args, label):
    """Start a child process, prefixing each of its output lines with a label."""
    proc = subprocess.Popen(
        [sys.executable, "-u"] + args, cwd=HERE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )

    def pump():
        for line in proc.stdout:
            print(f"[{label}] {line.rstrip()}", flush=True)

    threading.Thread(target=pump, daemon=True).start()
    return proc


def start_tunnel(exe, web_port, found):
    """Run cloudflared and hand the public URL back through `found`."""
    proc = subprocess.Popen(
        [exe, "tunnel", "--url", f"http://localhost:{web_port}",
         "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )

    def pump():
        for line in proc.stdout:
            match = URL_RE.search(line)
            if match and not found:
                found.append(match.group(0))
            # Chatty, so keep it to the lines that say whether this is working.
            if any(k in line for k in ("ERR", "WRN", "Registered tunnel", "Requesting new")):
                print(f"[tunnel] {line.rstrip()[:150]}", flush=True)

    threading.Thread(target=pump, daemon=True).start()
    return proc


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Share the chat system on a public URL")
    ap.add_argument("--port", type=int, default=5050, help="chat server port (local only)")
    ap.add_argument("--web-port", type=int, default=8080, help="web gateway port")
    args = ap.parse_args()

    cloudflared = find_cloudflared()
    if cloudflared is None:
        print(CLOUDFLARED_HELP)
        return 1

    for port, what in ((args.port, "chat server"), (args.web_port, "web gateway")):
        if port_is_busy(port):
            print(f"Port {port} is already in use, so the {what} would be a "
                  f"second copy sharing it. Stop the other one first, or pass "
                  f"a different port.", flush=True)
            return 1

    procs = []
    try:
        print("Starting the chat server...", flush=True)
        procs.append(spawn(["server.py", "--port", str(args.port)], "server"))
        if not wait_for_port(args.port):
            print(f"The chat server never came up on {args.port}.")
            return 1

        print("Starting the web gateway...", flush=True)
        procs.append(spawn(["webchat.py", "--port", str(args.web_port),
                            "--chat-port", str(args.port)], "web"))
        if not wait_for_port(args.web_port):
            print(f"The gateway never came up on {args.web_port}.")
            return 1

        print("Opening the tunnel...", flush=True)
        found = []
        procs.append(start_tunnel(cloudflared, args.web_port, found))

        deadline = time.time() + 90
        while not found and time.time() < deadline:
            if procs[-1].poll() is not None:
                print("cloudflared stopped before it gave us a URL.")
                return 1
            time.sleep(0.3)

        if not found:
            print("No tunnel URL after 90 seconds. Is this machine online?", flush=True)
            return 1

        url = found[0]
        bar = "=" * (len(url) + 10)
        print(f"\n{bar}\n    {url}\n{bar}")
        print("\nAnyone with that link can open the chat from any network, on a")
        print("phone or a laptop, with nothing installed.")
        print(f"\nOn this network, terminal clients still use:")
        print(f"    python client.py --host <this machine's IP> --port {args.port}")
        print("\nNo password, no encryption: the link is the only thing keeping")
        print("strangers out. Ctrl+C stops the tunnel and both servers.\n")

        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
        print("\nOne of the processes stopped; shutting the rest down.")
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        for p in procs:
            if p.poll() is None:
                # terminate() first so a child can tidy up; kill() if it will not go.
                # Leaving one behind means the next run quietly shares its ports.
                try:
                    p.terminate()
                except OSError:
                    pass
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        print(f"Could not stop PID {p.pid}; kill it by hand.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
