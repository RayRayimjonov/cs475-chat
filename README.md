# CS 475 Chat System

A multi-user chat system with a protocol we designed ourselves. It has a TCP server, a terminal client, and a browser client. Everything uses **only the Python standard library**, so there's nothing to install.

Team: Rakhmatillo Rayimjonov (server), Aden John (client), Alexis Paulin (concurrency, testing, demo).

**Features:** several users at once, nicknames, broadcast and private messages, clean join/leave, and detection of crashed clients. Stretch goals: **chat rooms**, **message history for late joiners**, **delivery receipts**, and a **web front end**.

In the browser you can also set a **profile picture** (🙂 in the header) and pick a
**chat background** (🎨). The background is yours alone and is remembered by your
browser. Profile pictures are held by `webchat.py`, not by the chat protocol, so they
are shared between browser users and disappear when the gateway restarts; terminal
clients are unaffected by either.

## Files

| File | What it is |
|---|---|
| `server.py` | Chat server: a reader and writer thread per client, one lock for shared state |
| `client.py` | Terminal client: separate threads for the keyboard and the socket |
| `webchat.py` + `webchat.html` | Web front end: an HTTP gateway and the browser page |
| `protocol.py` | Shared framing, name rules, error codes |
| `PROTOCOL.md` | **The protocol specification**, plus design notes |
| `DEMO.md` | Step-by-step script for the live demo |
| `test_server.py`, `test_webchat.py` | 55 end-to-end and unit tests |

## Requirements

Python 3.8 or newer (`python --version`). On macOS/Linux you may need to type `python3` instead of `python`.

## Quick start (one computer)

Start the server:

```bash
python server.py
```

Open two or more terminals and start a client in each, with a different nickname:

```bash
python client.py --nick alice
```

```bash
python client.py --nick bob
```

Leave out `--nick` and the client will ask for one.

For the browser version, run this in another terminal while the server is running:

```bash
python webchat.py
```

Then open http://localhost:8080 in a browser.

## Across several laptops (demo setup)

1. On the **server laptop** run `python server.py`, and optionally `python webchat.py` too.
2. Find that laptop's IP address: `ipconfig` on Windows (the "IPv4 Address" line), or `ipconfig getifaddr en0` on macOS. `webchat.py` also prints a usable address when it starts.
3. On every **other laptop**:
   ```bash
   python client.py --host 192.168.1.23 --nick yourname
   ```
   Replace `192.168.1.23` with the server laptop's IP address. Phones and laptops on the same network can also open `http://192.168.1.23:8080`.
4. **If nobody can connect:**
   - Windows asks to allow Python through the firewall the first time the server runs. Allow it on **Private networks**. If you missed that prompt, allow `python.exe` in Windows Defender Firewall → *Allow an app*.
   - Make sure every device is on the same Wi-Fi network.
   - Campus Wi-Fi often blocks connections between devices. A **phone hotspot** is the reliable fallback: connect every laptop to it and repeat step 2.

Use `--port` to change the chat port (default 5050). `webchat.py` uses `--port` for its web port (default 8080), `--chat-host`/`--chat-port` to reach the chat server, and `--host` for the address the page is served on (default: all interfaces).

## Commands (terminal and browser)

| Type | Effect |
|---|---|
| any text | Send to everyone in your current room |
| `/dm bob` or `/dm @7c1f20` | Open your private chat with someone. It is a room, so you move into it. |
| `/join Study Group` | Move to that room. Creates it as a private room you own if it doesn't exist. |
| `/new public Study Group` | Create a room. Private unless you say `public`. Names may contain spaces and punctuation. |
| `/invite bob Study Group` | Let someone into a private room you own |
| `/delete Study Group` | Delete a room you own, and its history |
| `/who` | Who is in your room, with their user IDs |
| `/rooms` | The rooms you're allowed to see |
| `/quit` | Leave cleanly |
| `/help` | List commands |

Everyone gets a user ID at sign-in (`@7c1f20`), shown by `/who`; that ID is how you invite
someone to a room. The ID belongs to your nickname for as long as the server runs, so if
you quit and come back under the same nickname, your private chats and their history are
still there.

A new room is private and invisible to everyone else until you invite someone, and only
the person who created a room can invite to it or delete it.

In the browser, click a room to switch to it, or a name under **In this room** to open a
private chat with that person. A room you own shows **Invite** and **Delete room** buttons.

Private chats and private rooms are private from other *users*, not from the network: there
is no password and no encryption, so this belongs on a trusted LAN only. See §10 of
[PROTOCOL.md](PROTOCOL.md).

## Tests

```bash
python -m unittest -v
```

These start real servers on random ports and connect real sockets. They cover the full protocol, all error codes, rooms, history, framing edge cases (split packets, oversize lines, bad UTF-8), crashed clients, a 20-client broadcast storm (every client must see the same order), 15 clients racing for one nickname, a client that stops reading, and the web gateway.

## Watching the protocol by hand

The protocol is plain text, so you can type it directly. With the server running, on macOS/Linux:

```bash
nc localhost 5050
```

Then type `HELLO tester`, `MSG hi`, `ROOMS`, `QUIT`.

To see the real traffic between our clients and the server on any OS, capture in Wireshark with the filter `tcp.port == 5050`, then right-click a packet → *Follow* → *TCP Stream*. Every protocol line appears as readable text. (For two programs on the same computer, capture on the loopback adapter, "Adapter for loopback traffic capture" on Windows.)
