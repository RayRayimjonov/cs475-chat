"""End-to-end tests: a real server on a random port, real TCP clients.

Run with:  python -m unittest -v
"""

import socket
import threading
import time
import unittest

import protocol as P
from server import ChatServer


def strip_msg_id(line):
    """A MSG line with its id removed: "MSG 7 alice hi" -> "MSG alice hi".

    Most tests are about who receives what, not about the numbering, so they match on
    the readable part. ReceiptTests covers the ids themselves."""
    if line is None or not line.startswith("MSG "):
        return line
    _cmd, msg_id, rest = line.split(" ", 2)
    int(msg_id)  # ids are numbers; a malformed one should fail loudly here
    return "MSG " + rest


class TestClient:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.reader = P.LineReader(self.sock)

    def send_raw(self, data):
        self.sock.sendall(data)

    def send(self, *parts):
        self.sock.sendall(P.encode(*parts))

    def recv(self):
        return self.reader.readline()

    def hello(self, nick):
        self.send("HELLO", nick)
        return self.recv()

    def recv_reply(self):
        """The next line that answers a command, skipping chatter that may be queued
        ahead of it: someone joining or leaving while we were idle, or a receipt for a
        message we sent earlier."""
        while True:
            line = self.recv()
            if line is None or not line.startswith(("NOTICE ", "RECEIPT ")):
                return line

    def welcome(self, nick):
        """Register and return the assigned user ID."""
        line = self.hello(nick)
        assert line.startswith(f"WELCOME {nick} "), line
        self.uid = line.split(" ")[2]
        return self.uid

    def close(self):
        self.sock.close()


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.server = ChatServer("127.0.0.1", 0, log=lambda *a: None)
        self.server.start()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.server.shutdown()

    def connect(self, nick=None):
        """Open a connection; with a nick, also register and consume WELCOME + JOINED."""
        c = TestClient(self.server.port)
        self.clients.append(c)
        if nick:
            c.welcome(nick)
            self.assertEqual(c.recv(), f"JOINED {P.LOBBY} {P.KIND_PUBLIC} {P.LOBBY}")
        return c

    def rooms_of(self, c):
        """Read a ROOM/ENDROOMS listing as {name: (count, kind, owner, shown)}."""
        out = {}
        while True:
            line = c.recv()
            if line == "ENDROOMS":
                return out
            assert line is not None and line.startswith("ROOM "), line
            name, count, kind, owner, shown = line[len("ROOM "):].split(" ", 4)
            out[name] = (count, kind, owner, shown)


class RegistrationTests(ServerTestCase):
    def test_welcome_then_lobby(self):
        self.connect("alice")

    def test_join_notice_goes_to_others(self):
        a = self.connect("alice")
        self.connect("bob")
        self.assertEqual(a.recv(), "NOTICE bob has joined the chat")

    def test_duplicate_nick_rejected_case_insensitive(self):
        self.connect("alice")
        b = self.connect()
        self.assertTrue(b.hello("Alice").startswith("ERROR 433"))
        self.assertTrue(b.hello("bob").startswith("WELCOME bob "))  # retry on same connection

    def test_invalid_nick(self):
        c = self.connect()
        self.assertTrue(c.hello("has space").startswith("ERROR 432"))
        self.assertTrue(c.hello("x" * 17).startswith("ERROR 432"))
        self.assertTrue(c.hello("").startswith("ERROR 432"))

    def test_hello_twice(self):
        a = self.connect("alice")
        a.send("HELLO", "alice2")
        self.assertTrue(a.recv().startswith("ERROR 409"))

    def test_must_register_first(self):
        c = self.connect()
        for cmd in ("MSG hi", "DM bob", "JOIN games", "WHO", "ROOMS"):
            c.send_raw(cmd.encode() + b"\n")
            self.assertTrue(c.recv().startswith("ERROR 401"), cmd)

    def test_commands_case_insensitive(self):
        c = self.connect()
        c.send_raw(b"hello alice\n")
        self.assertTrue(c.recv().startswith("WELCOME alice "))

    def test_quit_before_registering(self):
        c = self.connect()
        c.send("QUIT")
        self.assertEqual(c.recv(), "NOTICE Goodbye")
        self.assertIsNone(c.recv())


class MessagingTests(ServerTestCase):
    def test_unknown_command(self):
        a = self.connect("alice")
        a.send("DANCE")
        self.assertTrue(a.recv().startswith("ERROR 400"))

    def test_empty_msg_rejected(self):
        a = self.connect("alice")
        a.send("MSG")
        self.assertTrue(a.recv().startswith("ERROR 400"))

    def test_broadcast_reaches_everyone_including_sender(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.recv()  # bob has joined
        a.send("MSG", "hello world")
        self.assertEqual(strip_msg_id(a.recv()), "MSG alice hello world")
        self.assertEqual(strip_msg_id(b.recv()), "MSG alice hello world")

    def test_spaces_in_text_preserved(self):
        a = self.connect("alice")
        a.send("MSG", "  two  spaces  ")
        self.assertEqual(strip_msg_id(a.recv()), "MSG alice   two  spaces  ")

    def test_private_chat_is_a_room_only_its_two_people_can_enter(self):
        a = self.connect("alice")
        b = self.connect("bob")
        c = self.connect("carol")
        a.recv(); a.recv(); b.recv()  # join notices
        a.send("DM", "BOB")  # case-insensitive target
        joined = a.recv()
        self.assertTrue(joined.startswith("JOINED dm-"), joined)
        dm_room = joined.split(" ")[1]
        self.assertEqual(joined.split(" ")[2], P.KIND_DM)
        self.assertEqual(joined.split(" ")[3], "bob")  # labelled with the other person
        a.send("MSG", "secret stuff")
        self.assertEqual(strip_msg_id(a.recv()), "MSG alice secret stuff")
        # Carol is not one of the two people, so the room is closed to her.
        c.send("JOIN", dm_room)
        self.assertTrue(c.recv_reply().startswith(f"ERROR {P.ERR_FORBIDDEN}"))
        # Back in the lobby, the next thing carol hears from alice is the public
        # message: the private one never reached her at all.
        a.send("JOIN", P.LOBBY)
        self.assertEqual(a.recv(), f"JOINED {P.LOBBY} {P.KIND_PUBLIC} {P.LOBBY}")
        a.send("MSG", "public")
        self.assertEqual(strip_msg_id(a.recv()), "MSG alice public")
        self.assertEqual(strip_msg_id(c.recv_reply()), "MSG alice public")

    def test_dm_unknown_user(self):
        a = self.connect("alice")
        a.send("DM", "ghost")
        self.assertTrue(a.recv().startswith("ERROR 404"))

    def test_dm_missing_target(self):
        a = self.connect("alice")
        a.send("DM", "")
        self.assertTrue(a.recv().startswith("ERROR 400"))

    def test_unicode_message(self):
        a = self.connect("alice")
        a.send("MSG", "Salom! Привет 👋")
        self.assertEqual(strip_msg_id(a.recv()), "MSG alice Salom! Привет 👋")


class LeavingTests(ServerTestCase):
    def test_quit_announced(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.recv()
        b.send("QUIT")
        self.assertEqual(b.recv(), "NOTICE Goodbye")
        self.assertIsNone(b.recv())  # server closed the connection
        self.assertEqual(a.recv(), "NOTICE bob has left the chat")

    def test_unexpected_disconnect_announced_and_nick_freed(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.recv()
        b.close()
        self.assertEqual(a.recv(), "NOTICE bob has disconnected")
        self.connect("bob")  # nickname is available again

    def test_abortive_reset_announced(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.recv()
        # SO_LINGER 0 makes close() send a TCP RST, like a crashed program.
        b.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        b.close()
        self.assertEqual(a.recv(), "NOTICE bob has disconnected")

    def test_server_shutdown_notifies_clients(self):
        a = self.connect("alice")
        self.server.shutdown()
        self.assertEqual(a.recv(), "NOTICE Server is shutting down")
        self.assertIsNone(a.recv())


class RoomTests(ServerTestCase):
    def test_join_creates_room_and_confirms(self):
        a = self.connect("alice")
        a.send("JOIN", "#Games")  # leading # and capitals are normalized
        self.assertEqual(a.recv(), f"JOINED games {P.KIND_PRIVATE} games")

    def test_invalid_room_name(self):
        a = self.connect("alice")
        a.send("JOIN")  # nothing at all
        self.assertTrue(a.recv().startswith("ERROR 479"))
        a.send("JOIN", "!!!")  # no letter or digit to build a name from
        self.assertTrue(a.recv().startswith("ERROR 479"))
        a.send("JOIN", "x" * (P.MAX_TITLE + 1))  # too long to be a name
        self.assertTrue(a.recv().startswith("ERROR 479"))

    def test_join_same_room(self):
        a = self.connect("alice")
        a.send("JOIN", "lobby")
        self.assertEqual(a.recv(), "NOTICE You are already in lobby")

    def test_messages_stay_in_room(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.recv()  # bob joined
        b.send("JOIN", "games")
        self.assertEqual(b.recv(), f"JOINED games {P.KIND_PRIVATE} games")
        self.assertEqual(a.recv(), "NOTICE bob left lobby for games")
        a.send("MSG", "lobby only")
        self.assertEqual(strip_msg_id(a.recv()), "MSG alice lobby only")
        b.send("MSG", "games only")
        self.assertEqual(strip_msg_id(b.recv()), "MSG bob games only")
        # Neither saw the other's message: their next lines are these markers.
        a.send("WHO")
        self.assertEqual(a.recv(), f"USERS lobby alice={a.uid}")
        b.send("WHO")
        self.assertEqual(b.recv(), f"USERS games bob={b.uid}")

    def test_room_join_notice(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.recv()
        a.send("CREATE", P.KIND_PUBLIC, "games")
        a.recv()  # JOINED games
        b.recv()  # alice left lobby
        b.send("JOIN", "games")
        self.assertEqual(a.recv(), "NOTICE bob has joined games")

    def test_private_chat_is_reachable_from_any_room(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.recv()
        b.send("JOIN", "games")
        b.recv()
        a.recv()
        a.send("DM", "bob")
        self.assertTrue(a.recv().startswith("JOINED dm-"))

    def test_who_and_rooms(self):
        a = self.connect("alice")
        b = self.connect("Bob")
        a.recv()
        a.send("WHO")
        self.assertEqual(a.recv(), f"USERS lobby alice={a.uid} Bob={b.uid}")
        a.send("JOIN", "games")
        a.recv()
        a.send("ROOMS")
        self.assertEqual(self.rooms_of(a),
                         {"games": ("1", P.KIND_PRIVATE, "alice", "games"),
                          "lobby": ("1", P.KIND_PUBLIC, "-", "lobby")})

    def test_quit_leaves_room(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.recv()
        b.send("QUIT")
        a.recv()  # bob has left
        a.send("WHO")
        self.assertEqual(a.recv(), f"USERS lobby alice={a.uid}")


class HistoryTests(ServerTestCase):
    def test_late_joiner_gets_history(self):
        a = self.connect("alice")
        a.send("MSG", "first")
        a.send("MSG", "second")
        a.recv(); a.recv()
        b = TestClient(self.server.port)
        self.clients.append(b)
        self.assertTrue(b.hello("bob").startswith("WELCOME bob "))
        self.assertEqual(b.recv(), f"JOINED {P.LOBBY} {P.KIND_PUBLIC} {P.LOBBY}")
        h1, h2 = b.recv(), b.recv()
        self.assertRegex(h1, r"^HISTORY \d\d:\d\d alice first$")
        self.assertRegex(h2, r"^HISTORY \d\d:\d\d alice second$")

    def test_history_is_per_room(self):
        a = self.connect("alice")
        a.send("CREATE", P.KIND_PUBLIC, "games")
        a.recv()
        a.send("MSG", "in games")
        a.recv()
        b = self.connect("bob")  # lobby history is empty, so nothing follows JOINED
        b.send("JOIN", "games")
        self.assertEqual(b.recv(), f"JOINED games {P.KIND_PUBLIC} games")
        self.assertRegex(b.recv(), r"^HISTORY \d\d:\d\d alice in games$")

    def test_history_limited(self):
        a = self.connect("alice")
        for i in range(P.HISTORY_SIZE + 5):
            a.send("MSG", f"m{i}")
        for _ in range(P.HISTORY_SIZE + 5):
            a.recv()
        b = TestClient(self.server.port)
        self.clients.append(b)
        b.hello("bob")
        b.recv()  # JOINED
        history = [b.recv() for _ in range(P.HISTORY_SIZE)]
        self.assertTrue(history[0].endswith(" alice m5"))
        self.assertTrue(history[-1].endswith(f" alice m{P.HISTORY_SIZE + 4}"))

    def test_private_chat_keeps_its_own_history(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.recv()
        a.send("DM", "bob")
        self.assertTrue(a.recv().startswith("JOINED dm-"))
        b.recv()
        c = self.connect("carol")
        c.send("WHO")  # if history leaked, a HISTORY line would arrive first
        self.assertTrue(c.recv().startswith("USERS lobby"))


class IdentityTests(ServerTestCase):
    def test_each_user_gets_a_distinct_id(self):
        a = self.connect("alice")
        b = self.connect("bob")
        self.assertTrue(P.valid_uid(a.uid), a.uid)
        self.assertNotEqual(a.uid, b.uid)

    def test_id_survives_a_quit_and_reconnect(self):
        a = self.connect("alice")
        first = a.uid
        a.send("QUIT")
        a.close()
        again = self.connect("alice")
        self.assertEqual(again.uid, first)

    def test_reconnecting_restores_private_chat_history(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.recv()  # bob joined
        a.send("DM", "bob")
        dm = a.recv().split(" ")[1]
        a.send("MSG", "remember this")
        self.assertEqual(strip_msg_id(a.recv()), "MSG alice remember this")
        a.send("QUIT")
        a.close()

        back = self.connect("alice")
        back.send("JOIN", dm)
        self.assertEqual(back.recv(), f"JOINED {dm} {P.KIND_DM} bob")
        self.assertRegex(back.recv(), r"^HISTORY \d\d:\d\d alice remember this$")

    def test_who_reports_ids(self):
        a = self.connect("alice")
        a.send("WHO")
        self.assertEqual(a.recv(), f"USERS {P.LOBBY} alice={a.uid}")


class VisibilityTests(ServerTestCase):
    def test_new_room_is_private_and_hidden_from_others(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.recv()
        a.send("JOIN", "hideout")
        self.assertEqual(a.recv(), f"JOINED hideout {P.KIND_PRIVATE} hideout")
        b.recv()  # alice left lobby
        b.send("ROOMS")
        self.assertNotIn("hideout", self.rooms_of(b))

    def test_outsider_cannot_join_a_private_room(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.recv()
        a.send("JOIN", "hideout")
        a.recv()
        b.send("JOIN", "hideout")
        self.assertTrue(b.recv_reply().startswith(f"ERROR {P.ERR_FORBIDDEN}"))

    def test_public_room_is_visible_and_open_to_everyone(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.recv()
        a.send("CREATE", P.KIND_PUBLIC, "townsq")
        self.assertEqual(a.recv(), f"JOINED townsq {P.KIND_PUBLIC} townsq")
        b.recv()
        b.send("ROOMS")
        self.assertIn("townsq", self.rooms_of(b))
        b.send("JOIN", "townsq")
        self.assertEqual(b.recv_reply(), f"JOINED townsq {P.KIND_PUBLIC} townsq")

    def test_create_rejects_a_name_already_taken(self):
        a = self.connect("alice")
        a.send("CREATE", P.KIND_PUBLIC, "dup")
        a.recv()
        a.send("CREATE", P.KIND_PUBLIC, "dup")
        self.assertTrue(a.recv_reply().startswith(f"ERROR {P.ERR_ROOM_EXISTS}"))

    def test_rooms_listing_reports_kind_and_owner(self):
        a = self.connect("alice")
        a.send("CREATE", P.KIND_PRIVATE, "club")
        a.recv()
        a.send("ROOMS")
        listing = self.rooms_of(a)
        self.assertEqual(listing["club"], ("1", P.KIND_PRIVATE, "alice", "club"))
        self.assertEqual(listing[P.LOBBY][1], P.KIND_PUBLIC)


class RoomNameTests(ServerTestCase):
    """Room names people type: spaces, punctuation, any length within reason."""

    FANCY = "Study Group / CS 475"

    def test_name_with_spaces_and_punctuation_is_kept(self):
        a = self.connect("alice")
        a.send("CREATE", P.KIND_PRIVATE, self.FANCY)
        name, kind, shown = a.recv().split(" ", 3)[1:]
        self.assertEqual(kind, P.KIND_PRIVATE)
        self.assertEqual(shown, self.FANCY)
        # What travels in the protocol's non-final fields is a safe one-word name.
        self.assertTrue(P.NAME_RE.match(name), name)
        self.assertNotIn(" ", name)

    def test_room_can_be_entered_by_the_name_on_screen(self):
        a = self.connect("alice")
        a.send("CREATE", P.KIND_PRIVATE, self.FANCY)
        a.recv()
        a.send("JOIN", P.LOBBY)
        a.recv()
        a.send("JOIN", self.FANCY)
        self.assertTrue(a.recv().endswith(self.FANCY))

    def test_loose_spelling_finds_the_same_room(self):
        a = self.connect("alice")
        a.send("CREATE", P.KIND_PRIVATE, self.FANCY)
        made = a.recv().split(" ")[1]
        a.send("JOIN", P.LOBBY)
        a.recv()
        a.send("JOIN", "study group cs 475")  # no slash, different case
        self.assertEqual(a.recv().split(" ")[1], made)

    def test_invite_and_delete_accept_the_name_on_screen(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.recv()
        a.send("CREATE", P.KIND_PRIVATE, self.FANCY)
        a.recv()
        b.recv()  # alice left the lobby
        a.send("INVITE", "bob", self.FANCY)
        self.assertEqual(a.recv(), f"NOTICE Invited bob ({b.uid}) to {self.FANCY}")
        self.assertEqual(b.recv_reply().split(" ", 2)[2], f"alice {self.FANCY}")
        a.send("DELROOM", self.FANCY)
        self.assertEqual(a.recv(), f"NOTICE {self.FANCY} was deleted by alice")

    def test_listing_shows_the_name_on_screen(self):
        a = self.connect("alice")
        a.send("CREATE", P.KIND_PUBLIC, self.FANCY)
        made = a.recv().split(" ")[1]
        a.send("ROOMS")
        listing = self.rooms_of(a)
        self.assertEqual(listing[made][3], self.FANCY)
        self.assertEqual(listing[P.LOBBY][3], P.LOBBY)

    def test_a_plain_one_word_name_is_not_treated_as_a_title(self):
        a = self.connect("alice")
        a.send("JOIN", "games")
        self.assertEqual(a.recv(), f"JOINED games {P.KIND_PRIVATE} games")

    def test_two_names_that_slug_the_same_get_different_rooms(self):
        a = self.connect("alice")
        a.send("CREATE", P.KIND_PUBLIC, "Team One")
        first = a.recv().split(" ")[1]
        a.send("CREATE", P.KIND_PUBLIC, "team!one")
        second = a.recv().split(" ")[1]
        self.assertNotEqual(first, second)

    def test_duplicate_name_is_refused(self):
        a = self.connect("alice")
        a.send("CREATE", P.KIND_PUBLIC, self.FANCY)
        a.recv()
        a.send("CREATE", P.KIND_PUBLIC, self.FANCY)
        self.assertTrue(a.recv_reply().startswith(f"ERROR {P.ERR_ROOM_EXISTS}"))

    def test_a_name_with_nothing_usable_is_refused(self):
        a = self.connect("alice")
        a.send("CREATE", P.KIND_PUBLIC, "!!! ???")
        self.assertTrue(a.recv_reply().startswith(f"ERROR {P.ERR_BAD_ROOM}"))

    def test_a_private_room_cannot_be_found_by_guessing_its_name(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.recv()
        a.send("CREATE", P.KIND_PRIVATE, self.FANCY)
        a.recv()
        b.recv()
        b.send("JOIN", self.FANCY)
        self.assertTrue(b.recv_reply().startswith(f"ERROR {P.ERR_FORBIDDEN}"))

    def test_create_rejects_a_missing_or_unknown_kind(self):
        a = self.connect("alice")
        a.send("CREATE", "Study Group")  # no kind given
        self.assertTrue(a.recv_reply().startswith(f"ERROR {P.ERR_BAD_COMMAND}"))


class InviteTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.connect("alice")
        self.b = self.connect("bob")
        self.a.recv()  # bob joined
        self.a.send("JOIN", "hideout")
        self.a.recv()
        self.b.recv()  # alice left lobby

    def test_invite_by_id_lets_someone_in(self):
        self.a.send("INVITE", self.b.uid, "hideout")
        self.assertTrue(self.a.recv().startswith("NOTICE Invited bob"))
        self.assertEqual(self.b.recv_reply(), "INVITED hideout alice hideout")
        self.b.send("JOIN", "hideout")
        self.assertEqual(self.b.recv_reply(), f"JOINED hideout {P.KIND_PRIVATE} hideout")

    def test_invite_by_nickname_also_works(self):
        self.a.send("INVITE", "bob", "hideout")
        self.a.recv()
        self.assertEqual(self.b.recv_reply(), "INVITED hideout alice hideout")
        self.b.send("JOIN", "hideout")
        self.assertEqual(self.b.recv_reply(), f"JOINED hideout {P.KIND_PRIVATE} hideout")

    def test_only_the_owner_may_invite(self):
        self.a.send("INVITE", "bob", "hideout")
        self.a.recv()
        self.assertEqual(self.b.recv_reply(), "INVITED hideout alice hideout")
        self.b.send("JOIN", "hideout")
        self.assertEqual(self.b.recv_reply(), f"JOINED hideout {P.KIND_PRIVATE} hideout")
        c = self.connect("carol")
        self.b.send("INVITE", "carol", "hideout")
        self.assertTrue(self.b.recv_reply().startswith(f"ERROR {P.ERR_FORBIDDEN}"))
        c.send("JOIN", "hideout")
        self.assertTrue(c.recv_reply().startswith(f"ERROR {P.ERR_FORBIDDEN}"))

    def test_invited_room_becomes_visible_to_the_invitee(self):
        self.b.send("ROOMS")
        self.assertNotIn("hideout", self.rooms_of(self.b))
        self.a.send("INVITE", "bob", "hideout")
        self.a.recv()
        self.b.recv_reply()  # INVITED
        self.b.send("ROOMS")
        self.assertIn("hideout", self.rooms_of(self.b))

    def test_invite_to_a_missing_room_is_refused(self):
        self.a.send("INVITE", "bob", "nowhere")
        self.assertTrue(self.a.recv_reply().startswith(f"ERROR {P.ERR_NO_SUCH_ROOM}"))

    def test_invite_an_unknown_person_is_refused(self):
        self.a.send("INVITE", "ghost", "hideout")
        self.assertTrue(self.a.recv_reply().startswith(f"ERROR {P.ERR_NO_SUCH_NICK}"))


class DeleteRoomTests(ServerTestCase):
    def test_owner_deletes_room_and_members_land_in_the_lobby(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.recv()
        a.send("CREATE", P.KIND_PUBLIC, "club")
        a.recv()
        b.recv()
        b.send("JOIN", "club")
        b.recv_reply()
        a.recv()  # bob joined #club
        a.send("DELROOM", "club")
        self.assertEqual(a.recv(), "NOTICE club was deleted by alice")
        self.assertEqual(a.recv(), f"JOINED {P.LOBBY} {P.KIND_PUBLIC} {P.LOBBY}")
        self.assertEqual(b.recv_reply(), f"JOINED {P.LOBBY} {P.KIND_PUBLIC} {P.LOBBY}")

    def test_deleting_a_room_destroys_its_history(self):
        a = self.connect("alice")
        a.send("CREATE", P.KIND_PUBLIC, "club")
        a.recv()
        a.send("MSG", "this should not outlive the room")
        a.recv()
        a.send("DELROOM", "club")
        a.recv_reply()
        # A new room of the same name starts empty.
        a.send("CREATE", P.KIND_PUBLIC, "club")
        self.assertEqual(a.recv_reply(), f"JOINED club {P.KIND_PUBLIC} club")
        a.send("WHO")  # nothing queued between JOINED and this reply means no history
        self.assertTrue(a.recv().startswith("USERS club"))

    def test_non_owner_cannot_delete(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.recv()
        a.send("CREATE", P.KIND_PUBLIC, "club")
        a.recv()
        b.recv()
        b.send("DELROOM", "club")
        self.assertTrue(b.recv_reply().startswith(f"ERROR {P.ERR_FORBIDDEN}"))

    def test_lobby_cannot_be_deleted(self):
        a = self.connect("alice")
        a.send("DELROOM", P.LOBBY)
        self.assertTrue(a.recv_reply().startswith(f"ERROR {P.ERR_FORBIDDEN}"))

    def test_deleting_a_missing_room_is_refused(self):
        a = self.connect("alice")
        a.send("DELROOM", "nowhere")
        self.assertTrue(a.recv_reply().startswith(f"ERROR {P.ERR_NO_SUCH_ROOM}"))


class FramingTests(ServerTestCase):
    def test_message_split_across_packets(self):
        a = self.connect("alice")
        a.send_raw(b"MSG hel")
        time.sleep(0.05)
        a.send_raw(b"lo\nMSG second\n")
        self.assertEqual(strip_msg_id(a.recv()), "MSG alice hello")
        self.assertEqual(strip_msg_id(a.recv()), "MSG alice second")

    def test_many_messages_in_one_packet(self):
        a = self.connect("alice")
        a.send_raw(b"MSG one\nMSG two\nMSG three\n")
        self.assertEqual([strip_msg_id(a.recv()) for _ in range(3)],
                         ["MSG alice one", "MSG alice two", "MSG alice three"])

    def test_crlf_line_endings_accepted(self):
        a = self.connect()
        a.send_raw(b"HELLO alice\r\n")
        self.assertTrue(a.recv().startswith("WELCOME alice "))

    def test_line_too_long_then_recovers(self):
        a = self.connect("alice")
        a.send_raw(b"MSG " + b"x" * 5000 + b"\n")
        self.assertTrue(a.recv().startswith("ERROR 413"))
        a.send("MSG", "still here")
        self.assertEqual(strip_msg_id(a.recv()), "MSG alice still here")

    def test_invalid_utf8(self):
        a = self.connect("alice")
        a.send_raw(b"MSG \xff\xfe\n")
        self.assertTrue(a.recv().startswith("ERROR 400"))
        a.send("MSG", "ok")
        self.assertEqual(strip_msg_id(a.recv()), "MSG alice ok")

    def test_blank_lines_ignored(self):
        a = self.connect("alice")
        a.send_raw(b"\n\n   \nMSG hi\n")
        self.assertEqual(strip_msg_id(a.recv()), "MSG alice hi")


class ConcurrencyTests(ServerTestCase):
    def test_simultaneous_broadcasts_arrive_in_same_order_everywhere(self):
        n_clients, n_msgs = 20, 25
        clients = [self.connect(f"user{i}") for i in range(n_clients)]
        # Drain the join notices: client i sees the joins of clients i+1 .. n-1.
        for i, c in enumerate(clients):
            for _ in range(n_clients - 1 - i):
                self.assertIn("has joined the chat", c.recv())

        start = threading.Event()

        def blast(i, c):
            start.wait()
            for m in range(n_msgs):
                c.send("MSG", f"{i}-{m}")

        threads = [threading.Thread(target=blast, args=(i, c)) for i, c in enumerate(clients)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join()

        total = n_clients * n_msgs
        received = [[c.recv() for _ in range(total)] for c in clients]
        first = received[0]
        self.assertEqual(len(set(first)), total)  # nothing lost or duplicated
        for other in received[1:]:
            self.assertEqual(other, first)  # identical order for everyone
        # Each sender's own messages are still in the order it sent them.
        for i in range(n_clients):
            mine = [strip_msg_id(l) for l in first
                    if strip_msg_id(l).startswith(f"MSG user{i} ")]
            self.assertEqual(mine, [f"MSG user{i} {i}-{m}" for m in range(n_msgs)])

    def test_simultaneous_nick_claims_only_one_wins(self):
        n = 15
        clients = [self.connect() for _ in range(n)]
        replies = [None] * n
        start = threading.Event()

        def claim(i):
            start.wait()
            replies[i] = clients[i].hello("popular")

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join()
        self.assertEqual(sum(r.startswith("WELCOME popular ") for r in replies), 1)
        self.assertEqual(sum(r.startswith("ERROR 433") for r in replies), n - 1)

    def test_stalled_client_does_not_block_others(self):
        a = self.connect("alice")
        stalled = self.connect("stalled")  # never reads anything from here on
        a.recv()  # stalled has joined
        stalled.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        text = "x" * 1000
        deadline = time.time() + 30
        # Keep chatting until the stalled client's kernel buffers and its MAX_QUEUED
        # outbox are full and the server drops it. Alice must never wait on it.
        dropped = False
        for i in range(200_000):
            a.send("MSG", text)
            line = strip_msg_id(a.recv())
            if line.startswith("RECEIPT "):
                continue  # the stalled client acknowledging an earlier message
            if line == "NOTICE stalled has disconnected":
                dropped = True
                break
            self.assertEqual(line, f"MSG alice {text}")
            self.assertLess(time.time(), deadline, "alice was slowed down by the stalled client")
        self.assertTrue(dropped, "stalled client was never dropped")
        a.send("WHO")
        while (line := a.recv()).startswith("MSG"):
            pass
        self.assertEqual(line, f"USERS lobby alice={a.uid}")



class ReceiptTests(ServerTestCase):
    """MSG carries an id, and the sender is told when the others have displayed it."""

    def receipt(self, client):
        """The next RECEIPT line. recv_reply deliberately skips them, so a test that is
        waiting for one needs its own reader."""
        while True:
            line = client.recv()
            assert line is not None, "connection closed while waiting for a RECEIPT"
            if line.startswith("RECEIPT "):
                return line

    def msg_id(self, client):
        """The id of the next MSG line, skipping anything queued ahead of it."""
        while True:
            line = client.recv()
            assert line is not None, "connection closed while waiting for a MSG"
            if line.startswith("MSG "):
                return line.split(" ")[1]

    def ids(self, client, n):
        return [self.msg_id(client) for _ in range(n)]

    def test_message_carries_an_increasing_id(self):
        a = self.connect("alice")
        a.send("MSG", "one")
        a.send("MSG", "two")
        first, second = self.ids(a, 2)
        self.assertEqual(int(second), int(first) + 1)

    def test_alone_in_a_room_sends_no_receipt(self):
        """With nobody to receive it there is nothing to report, so the server must not
        put an extra line on the wire: the next thing back is the answer to WHO."""
        a = self.connect("alice")
        a.send("MSG", "to nobody")
        self.msg_id(a)
        a.send("WHO")
        self.assertEqual(a.recv(), f"USERS lobby alice={a.uid}")

    def test_receipt_after_the_other_acknowledges(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.send("MSG", "did you get this")
        msg_id = self.msg_id(a)
        self.assertEqual(strip_msg_id(b.recv_reply()), "MSG alice did you get this")
        b.send("SEEN", msg_id)
        self.assertEqual(self.receipt(a), f"RECEIPT {msg_id} 1 1")

    def test_receipt_counts_up_with_each_reader(self):
        a = self.connect("alice")
        b = self.connect("bob")
        c = self.connect("carol")
        a.send("MSG", "to both of you")
        msg_id = self.msg_id(a)
        b.send("SEEN", msg_id)
        self.assertEqual(self.receipt(a), f"RECEIPT {msg_id} 1 2")
        c.send("SEEN", msg_id)
        self.assertEqual(self.receipt(a), f"RECEIPT {msg_id} 2 2")

    def test_sender_does_not_acknowledge_its_own(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.send("MSG", "mine")
        msg_id = self.msg_id(a)
        a.send("SEEN", msg_id)      # the sender was never expected to
        b.send("SEEN", msg_id)
        self.assertEqual(self.receipt(a), f"RECEIPT {msg_id} 1 1")

    def test_leaving_stops_the_wait(self):
        a = self.connect("alice")
        b = self.connect("bob")
        a.send("MSG", "answer me")
        msg_id = self.msg_id(a)
        b.close()
        # Bob can no longer acknowledge it, so the message completes with him removed.
        self.assertEqual(self.receipt(a), f"RECEIPT {msg_id} 1 1")

    def test_unknown_id_is_ignored(self):
        a = self.connect("alice")
        b = self.connect("bob")
        b.send("SEEN", "99999")
        b.send("MSG", "still working")
        self.assertEqual(strip_msg_id(b.recv_reply()), "MSG bob still working")

    def test_seen_without_a_number_is_rejected(self):
        a = self.connect("alice")
        a.send("SEEN", "banana")
        self.assertTrue(a.recv_reply().startswith("ERROR 400 "))


class LineReaderTests(unittest.TestCase):
    """Unit tests for the framing code, using a fake socket that returns preset chunks."""

    class FakeSocket:
        def __init__(self, chunks):
            self.chunks = list(chunks)

        def recv(self, n):
            return self.chunks.pop(0) if self.chunks else b""

    def read_all(self, chunks):
        r = P.LineReader(self.FakeSocket(chunks))
        out = []
        while True:
            try:
                line = r.readline()
            except ValueError as e:
                out.append(f"<{e}>")
                continue
            if line is None:
                return out
            out.append(line)

    def test_byte_at_a_time(self):
        data = "MSG héllo\n".encode()
        self.assertEqual(self.read_all([bytes([b]) for b in data]), ["MSG héllo"])

    def test_multibyte_char_split_across_chunks(self):
        data = "MSG 👋\n".encode()
        self.assertEqual(self.read_all([data[:6], data[6:]]), ["MSG 👋"])

    def test_partial_line_at_eof_is_dropped(self):
        self.assertEqual(self.read_all([b"MSG a\nMSG unfinished"]), ["MSG a"])

    def test_too_long_line_skipped_in_pieces(self):
        chunks = [b"x" * 800, b"x" * 800, b"x" * 800, b"\nMSG ok\n"]
        self.assertEqual(self.read_all(chunks), ["<line too long>", "MSG ok"])

    def test_exactly_max_line(self):
        line = b"x" * (P.MAX_LINE - 1) + b"\n"
        self.assertEqual(self.read_all([line]), ["x" * (P.MAX_LINE - 1)])


if __name__ == "__main__":
    unittest.main()
