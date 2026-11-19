"""Shared protocol helpers for the CS 475 chat system.

Every message is one line of UTF-8 text ending in "\n". The first word is the
command; the rest are arguments separated by a single space. The last argument
of MSG / PRIVMSG / HISTORY / NOTICE / ERROR is free text and may itself contain
spaces. See PROTOCOL.md for the full specification.
"""

import re

ENCODING = "utf-8"
MAX_LINE = 1024  # bytes, including the trailing newline
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")  # nicknames and room names
NAME_MAX = 16    # longest room name / nickname
MAX_TITLE = 48   # longest human room title
UID_RE = re.compile(r"^@[0-9a-f]{6}$")  # user IDs: "@" is not legal in a nickname,
UID_HEX = 6                             # so an ID can never be mistaken for one
LOBBY = "lobby"  # every client starts here
HISTORY_SIZE = 20  # messages replayed to someone entering a room
PENDING_SIZE = 200  # per room: messages still waiting to be acknowledged (see SEEN)

# Room kinds, reported by ROOMS.
KIND_PUBLIC = "public"    # listed to everyone, anyone may join
KIND_PRIVATE = "private"  # listed only to its owner and invitees, who alone may join
KIND_DM = "dm"            # a private room for exactly two people, made by DM

# Error codes sent in "ERROR <code> <reason>"
ERR_BAD_COMMAND = 400         # unknown command or malformed arguments
ERR_NOT_REGISTERED = 401      # sent a command other than HELLO/QUIT before HELLO
ERR_NO_SUCH_NICK = 404        # PRIVMSG target is not connected
ERR_ALREADY_REGISTERED = 409  # sent HELLO twice
ERR_LINE_TOO_LONG = 413       # line exceeded MAX_LINE bytes
ERR_BAD_NICK = 432            # nickname has invalid characters or length
ERR_NICK_IN_USE = 433         # nickname already taken
ERR_BAD_ROOM = 479            # room name has invalid characters or length
ERR_ROOM_EXISTS = 480         # CREATE named a room that already exists
ERR_NO_SUCH_ROOM = 481        # the named room does not exist
ERR_FORBIDDEN = 403           # not invited to a private room, or not the room's owner


def encode(*parts):
    """Join parts with spaces into one wire line (bytes, newline-terminated)."""
    line = " ".join(str(p) for p in parts)
    # A newline inside a field would split the message in two on the wire.
    line = line.replace("\r", " ").replace("\n", " ")
    return (line + "\n").encode(ENCODING)


def parse(line):
    """Split a decoded line into (command, rest). rest is "" if absent."""
    command, _, rest = line.partition(" ")
    return command.upper(), rest


def valid_nick(nick):
    return bool(NAME_RE.match(nick))


def valid_uid(uid):
    return bool(UID_RE.match(uid))


def looks_like_uid(text):
    """True for anything addressed as an ID. Nicknames cannot contain "@", so a
    leading "@" is enough to tell "invite this ID" from "invite this nickname"
    without ever having to guess."""
    return text.startswith("@")


def normalize_room(name):
    """'#Games' -> 'games'. Returns None if the name is not a valid room name."""
    name = name.strip().lstrip("#").lower()
    return name if NAME_RE.match(name) else None


def valid_title(title):
    """A room title is free text: anything printable, within a sane length. The wire
    never has to parse it, because every message that carries a title puts it last."""
    title = title.strip()
    if not title or len(title) > MAX_TITLE:
        return False
    # encode() turns a newline into a space, which would silently alter the title.
    return not any(c in title for c in "\r\n")


def slugify(title):
    """Turn a human title into the short, space-free name the protocol puts in its
    non-final fields: 'Study Group / CS 475' -> 'study-group-cs-475'.

    Returns None only if nothing usable is left, e.g. a title of pure punctuation.
    """
    slug = re.sub(r"[^a-z0-9_-]+", "-", title.strip().lower()).strip("-")
    if len(slug) > NAME_MAX:
        # Cut back to a separator rather than through a word, so a long title gives
        # "study-group-cs" instead of "study-group-cs-4".
        cut = slug[:NAME_MAX + 1].rfind("-")
        slug = slug[:cut] if cut > 0 else slug[:NAME_MAX]
    return slug.strip("-") or None


class LineReader:
    """Reads newline-terminated lines from a TCP socket.

    TCP is a byte stream, so one recv() can return half a message or several
    messages at once. This buffers bytes until a full line is available.
    """

    def __init__(self, sock):
        self.sock = sock
        self.buffer = b""
        self.discarding = False  # True while skipping the tail of a too-long line

    def readline(self):
        """Return the next line as str (without newline), or None on EOF.

        Raises ValueError if a line is longer than MAX_LINE or is not UTF-8.
        The offending line is dropped, so the caller can keep reading.
        """
        while True:
            if b"\n" in self.buffer:
                raw, _, self.buffer = self.buffer.partition(b"\n")
                if self.discarding:
                    self.discarding = False
                    continue
                if len(raw) + 1 > MAX_LINE:
                    raise ValueError("line too long")
                return raw.decode(ENCODING).rstrip("\r")
            if len(self.buffer) >= MAX_LINE:
                # No newline in sight: drop what we have and skip to the next one.
                self.buffer = b""
                if not self.discarding:
                    self.discarding = True
                    raise ValueError("line too long")
            data = self.sock.recv(4096)
            if not data:
                return None
            self.buffer += data
