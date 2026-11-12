"""Web front end for the CS 475 chat system.

A small HTTP gateway (standard library only) that lets a browser join the chat.
The gateway does not understand chat at all: for each browser it opens one TCP
connection to the chat server and relays protocol lines unchanged in both
directions. The JavaScript in webchat.html speaks the same protocol as
client.py (see PROTOCOL.md).

    browser --HTTP--> webchat.py --TCP (our protocol)--> server.py

Endpoints:
    GET  /                    the chat page
    POST /api/open            open a TCP connection to the chat server -> {"session": id}
    GET  /api/events?s=<id>   Server-Sent Events stream: one event per protocol line
    POST /api/send?s=<id>     body = one protocol line to send to the chat server

Usage:
    python webchat.py [--port 8080] [--chat-host 127.0.0.1] [--chat-port 5050]
"""

import argparse
import json
import os
import queue
import secrets
import select
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import protocol as P

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webchat.html")
KEEPALIVE = 10        # seconds between SSE pings, so idle proxies keep the stream open
ATTACH_TIMEOUT = 20   # seconds a session may wait for the browser to open /api/events


class Session:
    """One browser's TCP connection to the chat server."""

    def __init__(self, sock):
        self.id = secrets.token_urlsafe(16)
        self.sock = sock
        self.lines = queue.Queue()  # lines from the chat server, waiting for the browser
        self.attached = False
        self.send_lock = threading.Lock()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        reader = P.LineReader(self.sock)
        try:
            while True:
                line = reader.readline()
                if line is None:
                    break
                self.lines.put(line)
        except (OSError, ValueError):
            pass
        self.lines.put(None)  # tells the event stream the chat server hung up

    def send_line(self, line):
        with self.send_lock:
            self.sock.sendall(P.encode(line))

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class Gateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, chat_host, chat_port):
        super().__init__(addr, Handler)
        self.chat_host = chat_host
        self.chat_port = chat_port
        self.sessions = {}
        self.lock = threading.Lock()

    def open_session(self):
        sock = socket.create_connection((self.chat_host, self.chat_port), timeout=5)
        sock.settimeout(None)
        session = Session(sock)
        with self.lock:
            self.sessions[session.id] = session
        threading.Timer(ATTACH_TIMEOUT, self._reap_if_unattached, args=(session,)).start()
        return session

    def get_session(self, sid):
        with self.lock:
            return self.sessions.get(sid)

    def drop_session(self, session):
        with self.lock:
            self.sessions.pop(session.id, None)
        session.close()

    def _reap_if_unattached(self, session):
        if not session.attached:
            self.drop_session(session)


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1, so the event stream can be sent as chunks a proxy will pass
    # straight through. Every other reply sets Content-Length, which 1.1 needs.
    protocol_version = "HTTP/1.1"

    server_version = "CS475Chat/1.0"

    def log_message(self, fmt, *args):
        pass  # keep the console quiet; the chat server already logs joins and leaves

    # ---- routing ---------------------------------------------------------

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.serve_page()
        elif url.path == "/api/events":
            self.stream_events(self.session_from(url))
        else:
            self.reply(404, b"Not found")

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/api/open":
            self.open_session()
        elif url.path == "/api/send":
            self.send_line(self.session_from(url))
        else:
            self.reply(404, b"Not found")

    # ---- endpoints -------------------------------------------------------

    def serve_page(self):
        with open(PAGE, "rb") as f:
            self.reply(200, f.read(), "text/html; charset=utf-8")

    def open_session(self):
        try:
            session = self.server.open_session()
        except OSError:
            self.reply(502, b'{"error": "chat server is not reachable"}', "application/json")
            return
        self.reply(200, json.dumps({"session": session.id}).encode(), "application/json")

    def send_line(self, session):
        if session is None:
            self.reply(404, b"No such session")
            return
        length = int(self.headers.get("Content-Length", 0))
        if length > P.MAX_LINE:
            self.reply(413, b"Line too long")
            return
        line = self.rfile.read(length).decode(P.ENCODING, errors="replace")
        try:
            session.send_line(line)
        except OSError:
            self.reply(502, b"Chat server connection closed")
            return
        self.reply(204, b"")

    def stream_events(self, session):
        if session is None or session.attached:
            self.reply(404, b"No such session")
            return
        session.attached = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        # Asks nginx-style proxies not to buffer; harmless where unrecognised.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        idle = 0
        try:
            while True:
                try:
                    line = session.lines.get(timeout=1)
                except queue.Empty:
                    # Check once a second so a closed tab leaves the chat promptly.
                    if self.browser_gone():
                        break
                    idle += 1
                    if idle >= KEEPALIVE:
                        idle = 0
                        self.write_chunk(b": ping\n\n")
                    continue
                if line is None:
                    self.write_chunk(b"event: closed\ndata: \n\n")
                    break
                self.write_chunk(f"data: {line}\n\n".encode(P.ENCODING))
        except OSError:
            pass  # browser tab closed or navigated away
        finally:
            try:
                self.wfile.write(b"0\r\n\r\n")  # the chunked stream ends here
                self.wfile.flush()
            except OSError:
                pass
            # Closing the TCP connection makes the chat server announce the departure.
            self.server.drop_session(session)
            self.close_connection = True

    def write_chunk(self, payload):
        """Send one HTTP chunk: its length in hex, the bytes, then a blank line."""
        self.wfile.write(b"%X\r\n" % len(payload) + payload + b"\r\n")
        self.wfile.flush()

    # ---- helpers ---------------------------------------------------------

    def browser_gone(self):
        """True if the browser closed the event stream (tab closed or reloaded).

        The browser never sends anything on this connection after the request,
        so "readable" can only mean it was closed.
        """
        readable, _, _ = select.select([self.connection], [], [], 0)
        if not readable:
            return False
        try:
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def session_from(self, url):
        sid = parse_qs(url.query).get("s", [""])[0]
        return self.server.get_session(sid)

    def reply(self, status, body, content_type="text/plain; charset=utf-8"):
        self.send_response(status)
        if body:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


def lan_addresses():
    """Best-effort list of this machine's IPv4 addresses, to print for the demo."""
    addrs = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # UDP connect sends nothing; it just picks a route
            addrs.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(addrs)


def main():
    ap = argparse.ArgumentParser(description="Web front end for the CS 475 chat server")
    ap.add_argument("--host", default="0.0.0.0", help="address to serve the web page on")
    ap.add_argument("--port", type=int, default=8080, help="web port (default 8080)")
    ap.add_argument("--chat-host", default="127.0.0.1", help="where server.py is running")
    ap.add_argument("--chat-port", type=int, default=5050)
    args = ap.parse_args()

    gateway = Gateway((args.host, args.port), args.chat_host, args.chat_port)
    print(f"Web chat on http://localhost:{args.port}/ -> chat server "
          f"{args.chat_host}:{args.chat_port}", flush=True)
    for ip in lan_addresses():
        print(f"Other devices on this network can open http://{ip}:{args.port}/", flush=True)
    try:
        gateway.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down", flush=True)
    finally:
        gateway.server_close()


if __name__ == "__main__":
    main()
