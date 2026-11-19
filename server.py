"""CS 475 chat server.

Concurrency design:
  * One reader thread per client parses incoming lines.
  * One writer thread per client drains that client's outgoing queue.
  * All shared state (users, rooms, history) is changed under one lock, and
    every message fan-out happens while holding it. Fan-out only *enqueues*
    (it never touches the network), so holding the lock stays cheap, and
    every client receives messages in exactly the same order.
  * A client too slow to keep up (its queue fills) is disconnected instead of
    stalling everyone else.

Usage:
    python server.py [--host 0.0.0.0] [--port 5050]
"""

import argparse
import collections
import queue
import secrets
import socket
import threading
import time

import protocol as P

MAX_QUEUED = 1000  # outgoing lines buffered per client before we give up on it


def enable_keepalive(sock, idle=10, interval=5, count=3):
    """Have TCP probe idle connections, so a laptop that vanishes (Wi-Fi off, lid
    closed) is noticed in about idle + interval * count seconds instead of never.
    Option names differ by OS, so set whichever ones this platform has."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    options = [("TCP_KEEPIDLE", idle), ("TCP_KEEPALIVE", idle),  # Linux/Windows, macOS
               ("TCP_KEEPINTVL", interval), ("TCP_KEEPCNT", count)]
    for name, value in options:
        if hasattr(socket, name):
            try:
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
            except OSError:
                pass


class Client:
    """One connection: its socket, identity, and an outgoing queue with a writer thread."""

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.user = None  # set once HELLO succeeds
        self.room = None
        self.outbox = queue.Queue(maxsize=MAX_QUEUED)
        threading.Thread(target=self._write_loop, daemon=True).start()

    @property
    def nick(self):
        return self.user.nick if self.user else None

    @property
    def uid(self):
        return self.user.uid if self.user else None

    @property
    def label(self):
        return self.nick or f"{self.addr[0]}:{self.addr[1]}"

    def send(self, *parts):
        """Queue one protocol line. Never blocks."""
        try:
            self.outbox.put_nowait(P.encode(*parts))
        except queue.Full:
            self.kill()

    def close(self):
        """Send whatever is queued, then close the connection."""
        try:
            self.outbox.put_nowait(None)
        except queue.Full:
            self.kill()

    def kill(self):
        """Abort the connection now, waking the reader and writer threads.

        shutdown() wakes threads blocked in recv/send on Linux and macOS; on
        Windows only close() does, so we do both.
        """
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    def _write_loop(self):
        try:
            while True:
                data = self.outbox.get()
                if data is None:
                    break
                self.sock.sendall(data)
        except OSError:
            pass
        finally:
            self.kill()


class User:
    """A person, identified by nickname and given a permanent ID.

    A User outlives any single connection: the server keeps the record for as long
    as it runs, so reconnecting under the same nickname restores the same ID, the
    same private rooms, and the history in them. There is no password, so whoever
    claims a free nickname inherits that identity -- see PROTOCOL.md.
    """

    def __init__(self, nick, uid):
        self.nick = nick
        self.uid = uid

    @property
    def key(self):
        return self.nick.lower()


class Room:
    """A conversation. Private messages are rooms too: a DM is a private room whose
    membership is fixed at the two people in it, which is what gives private chats
    the same stored history every other room has."""

    def __init__(self, name, kind=P.KIND_PUBLIC, owner=None, allowed=(), partners=None,
                 title=""):
        self.name = name
        self.title = title           # what people typed and what they see; may be empty
        self.kind = kind
        self.owner = owner            # uid of the creator; None for #lobby and DMs
        self.allowed = set(allowed)   # uids admitted to a private room or a DM
        self.partners = partners      # (uid, uid) for a DM, else None
        self.members = set()          # the Clients currently in the room
        self.history = collections.deque(maxlen=P.HISTORY_SIZE)
        self.next_id = 1              # message ids, counted within this room
        # id -> (sender uid, uids that were here when it was sent and have not
        # acknowledged it yet, how many were expected). An entry is dropped once
        # everyone has acknowledged, so this only holds messages still in flight.
        self.pending = {}

    def admits(self, uid):
        """A public room takes anyone; a private room or DM only its invitees."""
        return self.kind == P.KIND_PUBLIC or uid in self.allowed

    def label_for(self, uid, users):
        """What to call this room on screen, for `uid`. A DM is named after the other
        person, since "dm-4f2a91" means nothing to a reader; any other room goes by its
        title, falling back to its name when it has none."""
        if self.kind != P.KIND_DM:
            # A titled room goes by its title; an untitled one keeps the familiar
            # "#name" form. Either way this is the one string clients display.
            return self.title or "#" + self.name
        other = next((u for u in self.partners if u != uid), None)
        return users[other].nick if other in users else self.name

    def matches(self, text):
        """True if `text` is a way a person could refer to this room: its name, its
        title, or that title loosely spelled (case and punctuation ignored). People
        type what they see on screen, which is the title, not the wire name."""
        text = text.strip()
        if not text:
            return False
        if text.lstrip("#").lower() == self.name:
            return True
        if self.title and text.lower() == self.title.lower():
            return True
        return bool(self.title) and P.slugify(text) == P.slugify(self.title)


class ChatServer:
    def __init__(self, host="0.0.0.0", port=5050, log=None):
        self.host = host
        self.port = port
        self.log = log or (lambda msg: print(msg, flush=True))
        self.lock = threading.Lock()
        self.connections = set()  # every open Client, registered or not
        self.clients = {}         # lowercased nick -> Client, online users only
        self.users = {}           # lowercased nick -> User, kept after they disconnect
        self.by_uid = {}          # uid -> User
        self.rooms = {P.LOBBY: Room(P.LOBBY, P.KIND_PUBLIC)}
        self.dms = {}             # frozenset({uid, uid}) -> room name
        self.listener = None
        self.running = False

    # ---- lifecycle -------------------------------------------------------

    def start(self):
        """Bind and listen. After this, self.port holds the real port (useful with port 0)."""
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Windows only. SO_REUSEADDR does not mean here what it means on Unix:
            # it lets a second process bind a port that is already in use, and then
            # connections land on whichever socket Windows feels like. Two chat
            # servers end up sharing a port and each sees half the clients.
            # SO_EXCLUSIVEADDRUSE is the option that means what we actually want.
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            # Unix: lets us rebind immediately after a restart instead of waiting
            # out TIME_WAIT on the old connections.
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((self.host, self.port))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.running = True
        self.log(f"Server listening on {self.host}:{self.port}")

    def serve_forever(self):
        # A short timeout lets Ctrl+C get through; a blocking accept() ignores it on Windows.
        self.listener.settimeout(0.5)
        while self.running:
            try:
                sock, addr = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # listener closed by shutdown()
            sock.settimeout(None)  # client sockets stay blocking
            enable_keepalive(sock)
            self.log(f"Connection from {addr[0]}:{addr[1]}")
            client = Client(sock, addr)
            with self.lock:
                self.connections.add(client)
            threading.Thread(target=self.handle_client, args=(client,), daemon=True).start()

    def shutdown(self):
        self.running = False
        if self.listener:
            self.listener.close()
        with self.lock:
            for c in self.connections:
                c.send("NOTICE", "Server is shutting down")
                c.close()

    # ---- per-client reader thread ----------------------------------------

    def handle_client(self, client):
        reader = P.LineReader(client.sock)
        reason = "disconnected"
        try:
            while True:
                try:
                    line = reader.readline()
                except UnicodeDecodeError:
                    client.send("ERROR", P.ERR_BAD_COMMAND, "Message is not valid UTF-8")
                    continue
                except ValueError:
                    client.send("ERROR", P.ERR_LINE_TOO_LONG,
                                f"Line longer than {P.MAX_LINE} bytes")
                    continue
                if line is None:
                    break  # peer closed without QUIT
                if not line.strip():
                    continue
                with self.lock:
                    result = self.dispatch(client, line)
                if result == "quit":
                    reason = "left the chat"
                    break
        except OSError:
            pass  # connection reset, killed for being slow, server shutdown...
        finally:
            self.remove_client(client, reason)

    def dispatch(self, client, line):
        """Handle one line. Caller holds self.lock."""
        command, rest = P.parse(line)
        if command == "QUIT":
            client.send("NOTICE", "Goodbye")
            return "quit"
        if command == "HELLO":
            self.on_hello(client, rest.strip())
        elif client.nick is None:
            client.send("ERROR", P.ERR_NOT_REGISTERED, "Send HELLO <nick> first")
        elif command == "MSG":
            self.on_msg(client, rest)
        elif command == "JOIN":
            self.on_join(client, rest)
        elif command == "CREATE":
            self.on_create(client, rest)
        elif command == "DM":
            self.on_dm(client, rest.strip())
        elif command == "INVITE":
            self.on_invite(client, rest)
        elif command == "DELROOM":
            self.on_delroom(client, rest)
        elif command == "SEEN":
            self.on_seen(client, rest)
        elif command == "WHO":
            self.on_who(client)
        elif command == "ROOMS":
            self.on_rooms(client)
        else:
            client.send("ERROR", P.ERR_BAD_COMMAND, f"Unknown command {command}")
        return None

    # ---- command handlers (all called with self.lock held) ---------------

    def on_hello(self, client, nick):
        if client.user is not None:
            client.send("ERROR", P.ERR_ALREADY_REGISTERED, "Already registered")
            return
        if not P.valid_nick(nick):
            client.send("ERROR", P.ERR_BAD_NICK, "Nickname must be 1-16 letters, digits, _ or -")
            return
        key = nick.lower()
        if key in self.clients:
            client.send("ERROR", P.ERR_NICK_IN_USE, f"Nickname {nick} is already in use")
            return
        user = self.users.get(key)
        if user is None:
            user = User(nick, self.new_uid())
            self.users[key] = user
            self.by_uid[user.uid] = user
        else:
            # Same person coming back: keep the ID, and everything hanging off it,
            # but honour however they spelled the nickname this time.
            user.nick = nick
        client.user = user
        self.clients[key] = client
        client.send("WELCOME", user.nick, user.uid)
        self.log(f"{nick} ({user.uid}) registered from {client.addr[0]}:{client.addr[1]}")
        self.send_all("NOTICE", f"{nick} has joined the chat", exclude=client)
        self.enter_room(client, P.LOBBY, announce=False)

    def on_msg(self, client, text):
        if not text.strip():
            client.send("ERROR", P.ERR_BAD_COMMAND, "Usage: MSG <text>")
            return
        room = self.rooms[client.room]
        room.history.append((time.strftime("%H:%M"), client.nick, text))
        msg_id = room.next_id
        room.next_id += 1
        # Everyone in the room except the sender is expected to acknowledge it.
        awaiting = {m.uid for m in room.members if m is not client and m.uid}
        if awaiting:
            room.pending[msg_id] = [client.uid, awaiting, len(awaiting)]
            self.trim_pending(room)
        # Sender included, so it sees its message in the same position as everyone else.
        self.send_room(room, "MSG", msg_id, client.nick, text)

    def trim_pending(self, room):
        """Messages nobody ever acknowledges would accumulate, so keep only the most
        recent ones in flight."""
        while len(room.pending) > P.PENDING_SIZE:
            del room.pending[next(iter(room.pending))]

    def on_seen(self, client, arg):
        """A client reporting that it has displayed a message. Unknown ids are ignored:
        the message may have aged out, or the client may have just changed rooms."""
        try:
            msg_id = int(arg.strip())
        except ValueError:
            client.send("ERROR", P.ERR_BAD_COMMAND, "Usage: SEEN <id>")
            return
        room = self.rooms.get(client.room)
        if room is None:
            return
        self.acknowledge(room, msg_id, client.uid)

    def acknowledge(self, room, msg_id, uid):
        entry = room.pending.get(msg_id)
        if entry is None or uid not in entry[1]:
            return
        sender_uid, awaiting, expected = entry
        awaiting.discard(uid)
        # self.clients is keyed by nickname, so the uid has to go through by_uid first.
        # A sender who has since gone offline simply has nobody to tell.
        sender_user = self.by_uid.get(sender_uid)
        sender = self.clients.get(sender_user.key) if sender_user is not None else None
        if sender is not None:
            sender.send("RECEIPT", msg_id, expected - len(awaiting), expected)
        if not awaiting:
            del room.pending[msg_id]

    def forget_pending(self, room, uid):
        """Someone who leaves can no longer acknowledge anything, so stop waiting on
        them; a message everyone else has already seen becomes complete here."""
        for msg_id in list(room.pending):
            if uid in room.pending[msg_id][1]:
                self.acknowledge(room, msg_id, uid)

    def on_join(self, client, arg):
        title = arg.strip()
        if not title:
            client.send("ERROR", P.ERR_BAD_ROOM, "Usage: JOIN <room>")
            return
        room = self.resolve_room(client, title)
        if room is None:
            # Nothing by that name that this user may see. If it is a name they could
            # legally create, make it: a new room is private, so it stays invisible
            # until its owner invites someone in.
            hidden = any(r.matches(title) for r in self.rooms.values())
            if hidden:
                client.send("ERROR", P.ERR_FORBIDDEN,
                            f"{title} is private and you are not invited")
                return
            room = self.create_room(client, title, P.KIND_PRIVATE)
            if room is None:
                return
        if room.name == client.room:
            client.send("NOTICE", f"You are already in #{room.name}")
            return
        self.move(client, room.name)

    def on_create(self, client, rest):
        # The kind comes first so the title can run to the end of the line, where free
        # text needs no quoting and may hold spaces and punctuation.
        kind_arg, _, title = rest.strip().partition(" ")
        kind = kind_arg.strip().lower()
        if kind not in (P.KIND_PUBLIC, P.KIND_PRIVATE):
            client.send("ERROR", P.ERR_BAD_COMMAND,
                        "Usage: CREATE <public|private> <name>")
            return
        room = self.create_room(client, title.strip(), kind)
        if room is not None:
            self.move(client, room.name)

    def create_room(self, client, title, kind):
        """Make a room called `title`, reporting any problem to `client`. Returns the
        Room, or None if nothing was created."""
        if not P.valid_title(title):
            client.send("ERROR", P.ERR_BAD_ROOM,
                        f"A room name must be 1-{P.MAX_TITLE} characters and one line")
            return None
        if self.resolve_room(client, title) is not None:
            client.send("ERROR", P.ERR_ROOM_EXISTS, f"{title} already exists")
            return None
        base = P.slugify(title)
        if base is None:
            client.send("ERROR", P.ERR_BAD_ROOM,
                        "A room name needs at least one letter or digit")
            return None
        name = self.unique_name(base)
        # A plain one-word name is not a title: "games" is better shown as "#games"
        # than as itself. Only a name the wire could not carry is kept as a title.
        shown = "" if title.strip().lstrip("#").lower() == name else title
        room = self.rooms[name] = Room(name, kind, owner=client.uid,
                                       allowed={client.uid}, title=shown)
        self.log(f"{client.nick} created {kind} #{name} ({title!r})")
        return room

    def on_dm(self, client, arg):
        if not arg:
            client.send("ERROR", P.ERR_BAD_COMMAND, "Usage: DM <nick|@id>")
            return
        other = self.resolve_user(arg)
        if other is None:
            client.send("ERROR", P.ERR_NO_SUCH_NICK, f"No such user {arg}")
            return
        if other.uid == client.uid:
            client.send("ERROR", P.ERR_BAD_COMMAND, "You cannot open a private chat with yourself")
            return
        pair = frozenset((client.uid, other.uid))
        name = self.dms.get(pair)
        if name is None:
            name = self.new_room_name("dm-")
            self.rooms[name] = Room(name, P.KIND_DM, owner=None,
                                    allowed={client.uid, other.uid},
                                    partners=(client.uid, other.uid))
            self.dms[pair] = name
            self.log(f"private chat {name} opened between {client.nick} and {other.nick}")
        if name == client.room:
            client.send("NOTICE", f"You are already in #{name}")
            return
        self.move(client, name)

    def on_invite(self, client, rest):
        who, _, room_arg = rest.strip().partition(" ")
        room_arg = room_arg.strip()
        if not who or not room_arg:
            client.send("ERROR", P.ERR_BAD_COMMAND, "Usage: INVITE <nick|@id> <room>")
            return
        room = self.resolve_room(client, room_arg)
        if room is None:
            client.send("ERROR", P.ERR_NO_SUCH_ROOM, f"No such room {room_arg}")
            return
        shown = room.label_for(client.uid, self.by_uid)
        if room.kind != P.KIND_PRIVATE:
            client.send("ERROR", P.ERR_BAD_COMMAND,
                        f"{shown} is {room.kind}; there is nothing to invite anyone to")
            return
        if room.owner != client.uid:
            client.send("ERROR", P.ERR_FORBIDDEN, f"Only the owner of {shown} can invite people")
            return
        target = self.resolve_user(who)
        if target is None:
            client.send("ERROR", P.ERR_NO_SUCH_NICK, f"No such user {who}")
            return
        if target.uid in room.allowed:
            client.send("NOTICE", f"{target.nick} is already invited to {shown}")
            return
        room.allowed.add(target.uid)
        client.send("NOTICE", f"Invited {target.nick} ({target.uid}) to {shown}")
        online = self.clients.get(target.key)
        if online:
            online.send("INVITED", room.name, client.nick, shown)

    def on_delroom(self, client, rest):
        arg = rest.strip()
        if not arg:
            client.send("ERROR", P.ERR_BAD_ROOM, "Usage: DELROOM <room>")
            return
        room = self.resolve_room(client, arg)
        if room is None:
            client.send("ERROR", P.ERR_NO_SUCH_ROOM, f"No such room {arg}")
            return
        shown = room.label_for(client.uid, self.by_uid)
        if room.name == P.LOBBY:
            client.send("ERROR", P.ERR_FORBIDDEN, f"{shown} cannot be deleted")
            return
        if room.owner != client.uid:
            owner = self.by_uid.get(room.owner)
            whose = f"{owner.nick} owns it" if owner else "it has no owner"
            client.send("ERROR", P.ERR_FORBIDDEN, f"Only the owner can delete {shown}; {whose}")
            return
        # Everyone still reading the room has to go somewhere, and the history goes
        # out with the room object itself.
        for member in list(room.members):
            member.send("NOTICE", f"{shown} was deleted by {client.nick}")
            self.move(member, P.LOBBY, announce_leave=False)
        del self.rooms[room.name]
        if room.kind == P.KIND_DM and room.partners:
            self.dms.pop(frozenset(room.partners), None)
        self.log(f"{client.nick} deleted #{room.name}")
        client.send("NOTICE", f"Deleted {shown} and its history")

    def on_who(self, client):
        room = self.rooms[client.room]
        members = sorted(((c.nick, c.uid) for c in room.members), key=lambda p: p[0].lower())
        client.send("USERS", room.name, *(f"{n}={u}" for n, u in members))

    def on_rooms(self, client):
        # Only what this user is allowed to see: every public room, plus the private
        # rooms and private chats they belong to.
        #
        # One line per room, rather than one line listing them all, because a room's
        # displayed name is free text: it only needs no quoting if it is last on its
        # own line. ENDROOMS lets the client swap in the new list all at once.
        visible = [r for r in sorted(self.rooms.values(), key=lambda r: r.name)
                   if r.admits(client.uid)]
        for r in visible:
            owner = self.by_uid[r.owner].nick if r.owner in self.by_uid else "-"
            client.send("ROOM", r.name, len(r.members), r.kind, owner,
                        r.label_for(client.uid, self.by_uid))
        client.send("ENDROOMS")

    # ---- helpers (caller holds self.lock) --------------------------------

    def new_uid(self):
        """A short ID that cannot collide with a nickname, because "@" is not a legal
        nickname character. Random rather than sequential so it does not leak how many
        people have ever registered."""
        while True:
            uid = "@" + secrets.token_hex(P.UID_HEX // 2)
            if uid not in self.by_uid:
                return uid

    def new_room_name(self, prefix):
        while True:
            name = prefix + secrets.token_hex(3)
            if name not in self.rooms:
                return name

    def enter_room(self, client, name, announce):
        room = self.rooms[name]
        if announce:
            for member in room.members:
                member.send("NOTICE", f"{client.nick} has joined "
                            f"{room.label_for(member.uid, self.by_uid)}")
        room.members.add(client)
        client.room = name
        # The displayed name goes last, where free text needs no quoting: it is the
        # other person for a private chat, and the room's title otherwise.
        client.send("JOINED", name, room.kind, room.label_for(client.uid, self.by_uid))
        for stamp, sender, text in room.history:
            client.send("HISTORY", stamp, sender, text)

    def move(self, client, name, announce_leave=True):
        """Take a client out of whatever room it is in and put it in `name`.

        A client is in exactly one room at a time, private chats included, so this is
        the single path by which that one room changes.
        """
        old = self.rooms.get(client.room)
        if old is not None:
            old.members.discard(client)
            self.forget_pending(old, client.uid)
            if announce_leave:
                # Each reader is told where the room went using the name they know it
                # by. A private chat is never named: the room it was left for is nobody
                # else's business, and to the other participant it would read as their
                # own nickname.
                dest = self.rooms[name]
                for member in old.members:
                    here = old.label_for(member.uid, self.by_uid)
                    if dest.kind == P.KIND_DM:
                        member.send("NOTICE", f"{client.nick} left {here}")
                    else:
                        member.send("NOTICE", f"{client.nick} left {here} for "
                                    f"{dest.label_for(member.uid, self.by_uid)}")
        self.enter_room(client, name, announce=True)

    def resolve_room(self, client, text):
        """Find a room from what the user typed, which may be its title, spaces and all.
        Only rooms they are allowed to see are considered, so a private room cannot be
        discovered by guessing titles."""
        text = text.strip()
        if not text:
            return None
        for room in self.rooms.values():
            if room.admits(client.uid) and room.matches(text):
                return room
        return None

    def unique_name(self, base):
        """A free wire name built from `base`, numbered if that is already taken."""
        if base not in self.rooms:
            return base
        for n in range(2, 1000):
            suffix = f"-{n}"
            candidate = base[:P.NAME_MAX - len(suffix)].strip("-") + suffix
            if candidate not in self.rooms:
                return candidate
        return self.new_room_name("room-")

    def resolve_user(self, text):
        """Find a user by "@id" or by nickname, online or not. Returns a User or None."""
        text = text.strip()
        if P.looks_like_uid(text):
            return self.by_uid.get(text.lower())
        return self.users.get(text.lower())

    def find_nick(self, nick):
        """The Client of an online user, by nickname, case-insensitively."""
        return self.clients.get(nick.lower())

    def send_all(self, *parts, exclude=None):
        for c in self.clients.values():
            if c is not exclude:
                c.send(*parts)

    def send_room(self, room, *parts):
        for c in room.members:
            c.send(*parts)

    # ---- teardown --------------------------------------------------------

    def remove_client(self, client, reason):
        with self.lock:
            self.connections.discard(client)
            registered = client.user is not None and self.clients.get(client.user.key) is client
            if registered:
                # Only the online entry goes. The User record stays for as long as the
                # server runs, so coming back under the same nickname restores the same
                # ID, the same private rooms, and the history in them.
                del self.clients[client.user.key]
                room = self.rooms.get(client.room)
                if room is not None:
                    room.members.discard(client)
                    self.forget_pending(room, client.uid)
                self.send_all("NOTICE", f"{client.nick} has {reason}")
        client.close()
        self.log(f"{client.label} {reason if registered else 'closed before registering'}")


def main():
    ap = argparse.ArgumentParser(description="CS 475 chat server")
    ap.add_argument("--host", default="0.0.0.0", help="address to bind (default: all interfaces)")
    ap.add_argument("--port", type=int, default=5050)
    args = ap.parse_args()

    server = ChatServer(args.host, args.port)
    server.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down", flush=True)
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
