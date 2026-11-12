"""CS 475 chat client (terminal).

Usage:
    python client.py [--host 127.0.0.1] [--port 5050] [--nick NAME] [--no-color]

Type a line to send it to everyone in your room. A private chat is a room too,
so /dm switches into one exactly the way /join switches into any other.

    /dm <nick|@id>       open the private chat with someone
    /join <room>         switch to a room (created private if it doesn't exist)
    /new [public] <name> create a room; private unless you say public
    /invite <nick|@id> <room>   let someone into a room you own
    /delete <room>       delete a room you own, and its history

Room names may contain spaces and punctuation ("Study Group / CS 475"); refer to a
room by the name you see on screen.
    /who                 who is in this room, with their IDs
    /rooms               rooms you can see
    /quit                leave the chat
    /help                show this list
"""

import argparse
import os
import socket
import sys
import threading

import protocol as P

HELP = ("Commands: /dm <nick|@id>, /join <room>, /new [public] <name>, "
        "/invite <nick|@id> <room>, /delete <room>, /who, /rooms, /quit, /help. "
        "Anything else goes to everyone in your current room.")

# ANSI colors, switched off in main() when unsupported or not wanted.
COLORS = {"dim": "\033[2m", "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
          "magenta": "\033[35m", "cyan": "\033[36m", "bold": "\033[1m", "reset": "\033[0m"}


def paint(text, *styles):
    if not COLORS:
        return text
    return "".join(COLORS[s] for s in styles) + text + COLORS["reset"]


def show(text):
    print(text, flush=True)


def format_server_line(line, me):
    """Turn one protocol line from the server into something readable."""
    command, rest = P.parse(line)
    if command == "MSG":
        sender, _, text = rest.partition(" ")
        style = ("bold", "green") if sender == me else ("bold", "cyan")
        return f"{paint('<' + sender + '>', *style)} {text}"
    if command == "INVITED":
        name, _, tail = rest.partition(" ")
        who, _, shown = tail.partition(" ")
        shown = shown or "#" + name
        return paint(f"-- {who} invited you to {shown}. Type /join {shown} to go there.",
                     "magenta")
    if command == "HISTORY":
        stamp, _, rest = rest.partition(" ")
        sender, _, text = rest.partition(" ")
        return paint(f"[{stamp}] <{sender}> {text}", "dim")
    if command == "JOINED":
        name, _, tail = rest.partition(" ")
        kind, _, shown = tail.partition(" ")
        where = f"the private chat with {shown}" if kind == P.KIND_DM else shown
        return paint(f"-- You are now in {where}", "yellow")
    if command == "USERS":
        room, _, pairs = rest.partition(" ")
        people = [t.replace("=", " ") for t in pairs.split()]
        return paint(f"-- In #{room}: {', '.join(people)}", "yellow")
    if command == "ROOM":
        ROOM_LIST.append(rest)
        return None
    if command == "ENDROOMS":
        listing = ", ".join(describe_rooms(ROOM_LIST)) or "none"
        ROOM_LIST.clear()
        return paint(f"-- Rooms: {listing}", "yellow")
    if command == "NOTICE":
        return paint(f"-- {rest}", "yellow")
    if command == "ERROR":
        code, _, reason = rest.partition(" ")
        return paint(f"!! error {code}: {reason}", "red")
    return f"?? {line}"


# ROOM lines arrive one per room and are collected until ENDROOMS closes the list.
ROOM_LIST = []


def describe_rooms(lines):
    """Each ROOM line is "<name> <count> <kind> <owner> <shown...>", with the displayed
    name last because it is free text and may hold spaces. A private chat is listed
    under the other person's name, because "dm-4f2a91" tells the reader nothing."""
    out = []
    for line in lines:
        parts = line.split(" ", 4)
        if len(parts) < 5:
            continue
        _name, count, kind, _owner, shown = parts
        if kind == P.KIND_DM:
            out.append(f"private chat with {shown} ({count})")
        elif kind == P.KIND_PRIVATE:
            out.append(f"{shown} ({count}, private)")
        else:
            out.append(f"{shown} ({count})")
    return out


def register(sock, reader, nick):
    """Send HELLO until the server accepts a nickname. Returns the accepted nick or None."""
    while True:
        if not nick:
            nick = input("Nickname: ").strip()
        sock.sendall(P.encode("HELLO", nick))
        line = reader.readline()
        if line is None:
            return None
        command, rest = P.parse(line)
        if command == "WELCOME":
            accepted, _, uid = rest.strip().partition(" ")
            show(paint(f"-- Your ID is {uid}. Others can invite you with it.", "yellow"))
            return accepted
        text = format_server_line(line, None)
        if text is not None:
            show(text)
        nick = None  # ask again


def receive_loop(reader, me, done, quitting):
    """Runs on its own thread: print everything the server sends, until it closes."""
    try:
        while True:
            line = reader.readline()
            if line is None:
                break
            text = format_server_line(line, me)
            if text is not None:
                show(text)
    except (OSError, ValueError):
        pass
    if not quitting.is_set():
        show(paint("-- Disconnected from server. Press Enter to exit.", "red"))
    done.set()


def handle_input(sock, line, quitting):
    """Turn one typed line into a protocol message. Returns False when the user quits."""
    if not line.startswith("/"):
        sock.sendall(P.encode("MSG", line))
        return True
    cmd, _, rest = line.partition(" ")
    cmd = cmd.lower()
    rest = rest.strip()
    if cmd == "/quit":
        quitting.set()
        sock.sendall(P.encode("QUIT"))
        return False
    if cmd == "/dm":
        if not rest:
            show("Usage: /dm <nick|@id>")
        else:
            sock.sendall(P.encode("DM", rest))
    elif cmd == "/join":
        if not rest:
            show("Usage: /join <room>")
        else:
            sock.sendall(P.encode("JOIN", rest))
    elif cmd == "/new":
        first, _, tail = rest.partition(" ")
        if first.lower() in (P.KIND_PUBLIC, P.KIND_PRIVATE) and tail.strip():
            kind, name = first.lower(), tail.strip()
        else:
            kind, name = P.KIND_PRIVATE, rest
        if not name:
            show("Usage: /new [public|private] <name>")
        else:
            sock.sendall(P.encode("CREATE", kind, name))
    elif cmd == "/invite":
        who, _, where = rest.partition(" ")
        if not who or not where.strip():
            show("Usage: /invite <nick|@id> <room>")
        else:
            sock.sendall(P.encode("INVITE", who, where.strip()))
    elif cmd == "/delete":
        if not rest:
            show("Usage: /delete <room>")
        else:
            sock.sendall(P.encode("DELROOM", rest))
    elif cmd == "/who":
        sock.sendall(P.encode("WHO"))
    elif cmd == "/rooms":
        sock.sendall(P.encode("ROOMS"))
    elif cmd == "/help":
        show(HELP)
    else:
        show(f"Unknown command {cmd}. {HELP}")
    return True


def main():
    ap = argparse.ArgumentParser(description="CS 475 chat client")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--nick")
    ap.add_argument("--no-color", action="store_true", help="disable colored output")
    args = ap.parse_args()

    if args.no_color or not sys.stdout.isatty():
        COLORS.clear()
    elif os.name == "nt":
        os.system("")  # turns on ANSI color handling in the Windows console

    try:
        sock = socket.create_connection((args.host, args.port), timeout=5)
    except OSError as e:
        sys.exit(f"Could not connect to {args.host}:{args.port}: {e}")
    sock.settimeout(None)
    reader = P.LineReader(sock)

    try:
        nick = register(sock, reader, args.nick)
    except (KeyboardInterrupt, EOFError):
        sock.close()
        return
    if nick is None:
        sys.exit("Server closed the connection.")
    show(paint(f"-- Connected as {nick}. {HELP}", "yellow"))

    done = threading.Event()      # receiver thread has stopped
    quitting = threading.Event()  # we asked to leave, so the close is expected
    threading.Thread(target=receive_loop, args=(reader, nick, done, quitting),
                     daemon=True).start()

    try:
        while not done.is_set():
            line = input()
            if done.is_set():
                break
            if line.strip() and not handle_input(sock, line, quitting):
                done.wait(2)  # let the receiver print the server's goodbye
                break
    except (KeyboardInterrupt, EOFError):
        quitting.set()
        try:
            sock.sendall(P.encode("QUIT"))
            done.wait(2)
        except OSError:
            pass
    except OSError:
        show(paint("-- Connection lost.", "red"))
    finally:
        sock.close()


if __name__ == "__main__":
    main()
