"""Tests for the web gateway: browser-style HTTP clients talking to a real chat server."""

import http.client
import json
import threading
import unittest

import protocol as P
from server import ChatServer
from test_server import TestClient
from webchat import Gateway


class WebSession:
    """Acts like the JavaScript in webchat.html: one SSE stream plus POSTs."""

    def __init__(self, port):
        self.port = port
        status, body = self.request("POST", "/api/open")
        assert status == 200, status
        self.id = json.loads(body)["session"]
        self.stream = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.stream.request("GET", f"/api/events?s={self.id}")
        self.events = self.stream.getresponse()
        assert self.events.status == 200
        assert self.events.getheader("Content-Type").startswith("text/event-stream")

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def send(self, line):
        return self.request("POST", f"/api/send?s={self.id}", line.encode())[0]

    def recv(self):
        """Next protocol line from the event stream, or ('closed') when the server hung up."""
        event = None
        while True:
            raw = self.events.fp.readline().decode().rstrip("\n")
            if raw.startswith(":"):
                continue  # keepalive
            if raw.startswith("event: "):
                event = raw[7:]
            elif raw.startswith("data: "):
                data = raw[6:]
            elif raw == "":
                return "(closed)" if event == "closed" else data

    def recv_reply_skipping_receipts(self):
        """Receipts for our own messages arrive unprompted, so skip past them when a
        test is waiting for one particular line."""
        while True:
            line = self.recv()
            if not line.startswith("RECEIPT "):
                return line

    def close(self):
        self.stream.close()


def strip_msg_id(line):
    """A MSG line with its id removed: "MSG 7 alice hi" -> "MSG alice hi".

    Most tests are about who receives what, not about the numbering, so they match on
    the readable part. ReceiptTests covers the ids themselves."""
    if line is None or not line.startswith("MSG "):
        return line
    _cmd, msg_id, rest = line.split(" ", 2)
    int(msg_id)  # ids are numbers; a malformed one should fail loudly here
    return "MSG " + rest


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.chat = ChatServer("127.0.0.1", 0, log=lambda *a: None)
        self.chat.start()
        threading.Thread(target=self.chat.serve_forever, daemon=True).start()
        self.gw = Gateway(("127.0.0.1", 0), "127.0.0.1", self.chat.port)
        threading.Thread(target=self.gw.serve_forever, daemon=True).start()
        self.port = self.gw.server_address[1]
        self.open = []

    def tearDown(self):
        for s in self.open:
            s.close()
        self.gw.shutdown()
        self.gw.server_close()
        self.chat.shutdown()

    def web(self, nick=None):
        s = WebSession(self.port)
        self.open.append(s)
        if nick:
            self.assertEqual(s.send(f"HELLO {nick}"), 204)
            self.assertTrue(s.recv().startswith(f"WELCOME {nick} "))
            self.assertEqual(s.recv(), f"JOINED {P.LOBBY} {P.KIND_PUBLIC} #{P.LOBBY}")
        return s

    def test_serves_page(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn(b"<title>CS 475 Chat</title>", resp.read())

    def test_web_and_terminal_clients_talk(self):
        w = self.web("webby")
        t = TestClient(self.chat.port)
        self.addCleanup(t.close)
        t.hello("term")
        t.recv()  # JOINED
        self.assertEqual(w.recv(), "NOTICE term has joined the chat")
        w.send("MSG hi from the browser 👋")
        self.assertEqual(strip_msg_id(t.recv()), "MSG webby hi from the browser 👋")
        self.assertEqual(strip_msg_id(w.recv()), "MSG webby hi from the browser 👋")
        # The terminal opens a private chat with the browser user: it is an ordinary
        # room, so the terminal is moved into it and the lobby is told it left.
        t.send("DM", "webby")
        joined = t.recv()
        self.assertTrue(joined.startswith("JOINED dm-"), joined)
        self.assertEqual(joined.split(" ")[2:], [P.KIND_DM, "webby"])
        # The lobby is told term left, but not that it was for a private chat.
        self.assertEqual(w.recv_reply_skipping_receipts(), "NOTICE term left #lobby")

    def test_nick_error_then_retry_same_session(self):
        self.web("taken")
        w = self.web()
        w.send("HELLO taken")
        self.assertTrue(w.recv().startswith("ERROR 433"))
        w.send("HELLO fresh")
        self.assertTrue(w.recv().startswith("WELCOME fresh "))

    def test_quit_closes_stream(self):
        w = self.web("webby")
        w.send("QUIT")
        self.assertEqual(w.recv(), "NOTICE Goodbye")
        self.assertEqual(w.recv(), "(closed)")

    def test_pagehide_beacon_leaves_cleanly(self):
        t = TestClient(self.chat.port)
        self.addCleanup(t.close)
        t.hello("term")
        t.recv()
        w = self.web("webby")
        self.assertEqual(t.recv(), "NOTICE webby has joined the chat")
        w.send("QUIT")  # what the page's pagehide beacon sends
        self.assertEqual(t.recv(), "NOTICE webby has left the chat")

    def test_closed_tab_without_quit_disconnects_quickly(self):
        t = TestClient(self.chat.port)
        self.addCleanup(t.close)
        t.hello("term")
        t.recv()
        w = self.web("webby")
        self.assertEqual(t.recv(), "NOTICE webby has joined the chat")
        w.events.close()  # like a tab killed before it could send QUIT
        w.stream.close()
        t.sock.settimeout(4)  # the gateway checks about once a second
        self.assertEqual(t.recv(), "NOTICE webby has disconnected")

    def test_unknown_session(self):
        w = self.web()
        status, _ = w.request("POST", "/api/send?s=nope", b"MSG hi")
        self.assertEqual(status, 404)

    def test_line_too_long_rejected(self):
        w = self.web("webby")
        status, _ = w.request("POST", f"/api/send?s={w.id}", b"MSG " + b"x" * 2000)
        self.assertEqual(status, 413)

    def test_avatar_round_trip(self):
        """A picture uploaded by one browser is served to every other browser."""
        w = self.web("picasso")
        # The gateway stores bytes and checks the declared type and size; it does
        # not decode the image, so a stand-in is enough here.
        png = bytes.fromhex("89504e470d0a1a0a") + b"fake image bytes" * 4
        status, _ = w.request("POST", f"/api/avatar?s={w.id}", png,
                              headers={"Content-Type": "image/png"})
        self.assertEqual(status, 204)

        status, body = w.request("GET", "/api/avatars")
        self.assertEqual(status, 200)
        self.assertIn("picasso", json.loads(body))

        status, body = w.request("GET", "/api/avatar?nick=PICASSO")  # case-insensitive
        self.assertEqual(status, 200)
        self.assertEqual(body, png)

    def test_avatar_missing_is_404(self):
        w = self.web("nobody")
        status, _ = w.request("GET", "/api/avatar?nick=ghost")
        self.assertEqual(status, 404)

    def test_avatar_rejects_non_image(self):
        w = self.web("sneaky")
        status, _ = w.request("POST", f"/api/avatar?s={w.id}", b"<html>nope</html>",
                              headers={"Content-Type": "text/html"})
        self.assertEqual(status, 415)

    def test_avatar_rejects_oversized(self):
        w = self.web("hefty")
        status, _ = w.request("POST", f"/api/avatar?s={w.id}", b"x" * (64 * 1024 + 1),
                              headers={"Content-Type": "image/png"})
        self.assertEqual(status, 413)

    def test_chat_server_down(self):
        self.chat.shutdown()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/open")
        self.assertEqual(conn.getresponse().status, 502)


if __name__ == "__main__":
    unittest.main()
