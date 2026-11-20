# Chat Protocol Specification

CS 475 Team Project · Rakhmatillo Rayimjonov, Aden John, Alexis Paulin

## 1. Transport

- **TCP**, default port **5050**. Chat needs every message delivered, in order, so we use TCP. Each client keeps one long-lived connection.
- Messages are **lines of UTF-8 text terminated by `\n`**. The server also accepts `\r\n`.
- A line may be at most **1024 bytes** including the newline. A longer line is discarded and answered with `ERROR 413`. The connection stays open.
- Each line is a `COMMAND` followed by space-separated arguments. Commands are case-insensitive. The **last argument of a message that carries text is free text**: it runs to the end of the line and may contain spaces.
- Blank lines are ignored.

TCP is a byte stream, not a message stream: one `recv()` can return half a line or several lines. Both sides keep a buffer and only act on complete lines (`LineReader` in `protocol.py`).

## 2. Names

| | Rule |
|---|---|
| Nicknames | 1–16 characters from `A–Z a–z 0–9 _ -`. Unique, compared **case-insensitively** (`Bob` and `bob` can't both be connected). The server keeps the capitalization chosen at `HELLO`. |
| Room names | What people type and see, up to 48 characters on one line: spaces and punctuation are allowed, so `Study Group / CS 475` is a room name. |
| Room wire names | Every room also has a one-word name from `A–Z a–z 0–9 _ -`, at most 16 characters, derived from the room name by lowercasing it and replacing every run of other characters with `-` (`Study Group / CS 475` → `study-group-cs`). This is what appears in the protocol's **non-final** fields, where a space would split the message. A leading `#` is accepted and stripped, so `JOIN #Games` and `JOIN games` mean the same room. A room whose name is already one word, like `games`, has no separate name to show and is displayed as `games`. |
| User IDs | Assigned by the server at `HELLO`: `@` followed by six hex digits, e.g. `@4f2a91`. `@` is not a legal nickname character, so an ID can never be mistaken for a nickname. An ID belongs to a nickname for as long as the server runs: disconnect and reconnect under the same nickname and you get the same ID back, along with the private rooms and history that go with it. There is no password, so whoever claims a free nickname inherits that identity — see §10. |

## 3. Client → Server

| Message | Meaning | Allowed before `HELLO`? |
|---|---|---|
| `HELLO <nick>` | Register with a nickname. | yes |
| `MSG <text>` | Send `<text>` to everyone in your current room. | no |
| `JOIN <room>` | Move to `<room>`. Creates it as a **private** room you own if it does not exist. | no |
| `CREATE <public\|private> <room>` | Create `<room>` and move into it. The kind comes **first** so the name can run to the end of the line. | no |
| `DM <nick\|@id>` | Move into your private chat with that person, opening it the first time. | no |
| `INVITE <nick\|@id> <room>` | Let someone into a private room **you own**. | no |
| `DELROOM <room>` | Delete a room **you own**, and its history. | no |
| `SEEN <id>` | Report that the message numbered `<id>` has been displayed here, so its sender can be told it arrived. Sent for other people's messages, never your own. | no |

Wherever a command takes `<room>`, it is the last field, so it accepts either the wire name or the name as shown on screen, spaces and all. Matching ignores case and punctuation, so `study group cs 475` finds `Study Group / CS 475`. Only rooms you are allowed to see are searched, so a private room cannot be found by guessing names.
| `WHO` | List the users in your current room, with their IDs. | no |
| `ROOMS` | List the rooms you are allowed to see. | no |
| `QUIT` | Leave cleanly. | yes |

## 4. Server → Client

| Message | Meaning |
|---|---|
| `WELCOME <nick> <@id>` | Nickname accepted. The client is registered and `<@id>` is its user ID. |
| `JOINED <room> <kind> <shown>` | You are now in `<room>` (a wire name). `<kind>` is `public`, `private` or `dm`. `<shown>` is free text running to the end of the line: the other person's nickname for a `dm`, the room's name otherwise (the room name itself when it has no separate title). Followed by that room's history. |
| `HISTORY <HH:MM> <from> <text>` | A message sent in the room before you arrived (oldest first, at most 20). |
| `MSG <id> <from> <text>` | A message from `<from>` to your room. `<id>` numbers it within that room and is what `SEEN` refers to. |
| `RECEIPT <id> <seen> <total>` | Only to the sender of `<id>`: `<seen>` of the `<total>` people who were in the room when it was sent have displayed it. Sent each time the count rises; not sent at all when `<total>` is 0. |
| `INVITED <room> <from> <shown>` | `<from>` has let you into the private room `<room>`, whose name on screen is `<shown>`. |
| `USERS <room> <nick>=<@id> …` | Reply to `WHO`, sorted alphabetically. |
| `ROOM <name> <count> <kind> <owner> <shown>` | One room you may see, in reply to `ROOMS`. `<owner>` is the creator's nickname, or `-` for `lobby` and private chats. `<shown>` is the name on screen, free text to the end of the line. |
| `ENDROOMS` | Closes a run of `ROOM` lines. Clients swap in the new list here, so a listing is never seen half-applied. |
| `NOTICE <text>` | A human-readable announcement (joins, leaves, goodbye, shutdown). |
| `ERROR <code> <reason>` | Your last request was rejected. Nothing changed, and the connection stays open. |

### Delivery receipts, and what they do not mean

A `RECEIPT` says that a client **displayed** the line, because that client sent `SEEN`.
It does not say a person read it, and it cannot: nothing in the protocol observes a
human.

The people a message waits on are fixed when it is sent: everyone in the room at that
moment except the sender. Someone who arrives later is never expected to acknowledge
it, and someone who leaves before acknowledging is dropped from the wait, which can
complete a message at the moment they go. So `<total>` is the size of the room when
the message was sent, and `<seen>` can reach it because people left rather than
because they read anything.

A `SEEN` naming an id the server does not recognise is ignored rather than refused:
the message may have aged out, or the sender may have changed rooms in between. Only
the 200 most recent unacknowledged messages per room are tracked, so a message nobody
ever acknowledges is eventually forgotten and simply never completes.

`client.py` sends `SEEN` but shows nothing for `RECEIPT`, because a terminal cannot
revise a line it has already printed. The browser client shows one tick for sent and
two once everyone expected has displayed it.

## 5. Error codes

| Code | When |
|---|---|
| 400 | Unknown command, missing arguments, empty message text, or invalid UTF-8 |
| 401 | Any command other than `HELLO`/`QUIT` before a successful `HELLO` |
| 403 | Not invited to a private room, or not the owner of the room you tried to invite to or delete |
| 404 | No user by that nickname or ID |
| 409 | `HELLO` again after already registering |
| 413 | Line longer than 1024 bytes |
| 432 | Invalid nickname |
| 433 | Nickname already in use |
| 479 | Room name missing, longer than 48 characters, spanning lines, or with no letter or digit to build a wire name from |
| 480 | `CREATE` named a room that already exists |
| 481 | No room by that name |

## 6. Who sends what, when

### Connecting and choosing a nickname
```
C: HELLO alice
S: WELCOME alice @4f2a91             (to alice: her nickname and her user ID)
S: NOTICE alice has joined the chat  (to every other user, in every room)
S: JOINED lobby public lobby        (to alice: everyone starts in lobby)
S: HISTORY 14:02 bob hi all          (to alice: up to 20 recent lobby messages)
```
After `ERROR 432` or `ERROR 433`, the client is still connected and can send `HELLO` again.

### Talking to the room
```
C(alice): MSG hello everyone
S:        MSG alice hello everyone   (to everyone in alice's room, alice included)
```
The sender gets its own message back. That confirms delivery and puts the message in the same position for the sender as for everyone else. Every `MSG` is also added to the room's history.

### Private chats
A private chat is **a room**, not a separate kind of message. `DM` moves you into the
room you share with one other person, creating it the first time:
```
C(alice): DM @7c1f20
S:        NOTICE alice left lobby                  (to the users still in lobby)
S:        JOINED dm-4f2a91 dm bob                   (to alice)
S:        HISTORY ...                               (to alice: what you two said before)
```
The room's wire name is generated by the server; clients show it under the other
person's nickname, which `JOINED` and `ROOM` supply as `<shown>`. Once inside, an ordinary
`MSG` goes to that room, exactly as in any other room. Where a client went is not
announced when the destination is a private chat: that is nobody else's business, and to
the other participant it would read as their own nickname.

Because a private chat is a room, it gets everything a room has: stored history that is
replayed when you come back, and a membership nobody else can enter. Only the two people
in it are ever admitted; anyone else attempting to `JOIN` it gets `ERROR 403`. The target
may be named by nickname or by ID, and `DM` with an unknown target gives `ERROR 404`.

A private chat has no owner, so no one can delete it.

### Changing rooms
```
C(alice): JOIN games
S:        NOTICE alice left lobby for games   (to the users still in lobby)
S:        NOTICE alice has joined games        (to the users already in games)
S:        JOINED games private games           (to alice)
S:        HISTORY ...                           (to alice: games history)
```
A client is in exactly one room at a time — private chats included, since those are rooms
too. Rooms are kept, with their history, until their owner deletes them or the server
stops. Joining the room you're already in returns `NOTICE You are already in #<room>`.

### Naming a room
```
C(alice): CREATE private Study Group / CS 475
S:        JOINED study-group-cs private Study Group / CS 475   (to alice)
```
A room is named by whatever the person typed. The server derives a one-word **wire name**
from it (`study-group-cs`) for the fields where a space would split the message, and keeps
the typed name to send as free text at the end of `JOINED`, `ROOM` and `INVITED`.

Two different names can slug down to the same wire name; the second room gets a numbered
one (`study-group-2`), so wire names stay unique while the names on screen stay as typed.
A name that is already one word is not kept separately: `JOIN games` gives a room shown as
`games`.

Because every command takes the room last, people refer to rooms by the name they can see:
`DELROOM Study Group / CS 475` and `INVITE bob Study Group / CS 475` both work, as does a
loose spelling like `study group cs 475`.

### Who can see a room
Every room is either **public** or **private**, and the person who creates it chooses:

* `CREATE public <room>` — listed by `ROOMS` for everyone, and anyone may `JOIN`.
* `CREATE private <room>`, or plain `JOIN <room>` for a room that does not exist yet —
  listed only for its owner and the people invited to it, and only they may `JOIN`.
  Everyone else gets `ERROR 403`, and the room does not appear in their `ROOMS` at all.

`lobby` is public, has no owner, and always exists.

### Inviting someone
```
C(alice): INVITE @7c1f20 hideout
S:        NOTICE Invited bob (@7c1f20) to #hideout   (to alice)
S:        INVITED hideout alice                      (to bob, if he is connected)
```
Only the room's owner may invite, and only to a private room; anyone else gets `ERROR 403`.
The invitation is addressed by nickname or by ID, and it is remembered against the
invitee's ID, so it survives their disconnecting and coming back. From then on the room
appears in their `ROOMS` and they may `JOIN` it. An invitation to a room that does not
exist gives `ERROR 481`; for someone who does not exist, `ERROR 404`.

### Deleting a room
```
C(alice): DELROOM hideout
S:        NOTICE #hideout was deleted by alice   (to everyone in it, alice included)
S:        JOINED lobby public lobby             (to each of them: everyone lands in lobby)
S:        NOTICE Deleted #hideout and its history   (to alice)
```
Only the room's owner may delete it; anyone else gets `ERROR 403`, and so does an attempt
on `lobby`. The room's history goes with it, so a later room of the same name starts
empty. `DELROOM` on a room that does not exist gives `ERROR 481`.

### Leaving cleanly
```
C(alice): QUIT
S:        NOTICE Goodbye                  (to alice; then the server closes the connection)
S:        NOTICE alice has left the chat  (to every other user)
```

### Unexpected disconnect
If the connection closes or resets without `QUIT` (crashed program, closed laptop, killed terminal), the server removes the user, frees the nickname, and sends `NOTICE <nick> has disconnected` to every other user.

Leaving — cleanly or not — ends the *connection*, not the *identity*. The server keeps the
user record for as long as it runs, so reconnecting under the same nickname restores the
same ID, the rooms that ID was invited to, and their history.

### Server shutdown
The server sends `NOTICE Server is shutting down` to every connection, then closes them.

### Asynchrony
Once registered, a client can receive `MSG`, `INVITED`, `NOTICE` and the rest **at any time**, not only in reply to its own requests. So both clients read the socket on a dedicated receiving thread (terminal) or event stream (browser), separate from user input.

## 7. Ordering guarantees

1. Every client sees the messages of a room in **the same order**, the order in which the server accepted them.
2. Messages from one sender arrive in the order they were sent. TCP guarantees this per connection, and the server handles each connection's lines one at a time.
3. A client receives `JOINED <room>` and that room's `HISTORY` before any live message from the new room.

## 8. Server design

| Concern | How |
|---|---|
| Accepting clients | One listening socket. Each accepted connection gets a **reader thread** and a **writer thread**. |
| Shared state | The user table, rooms and histories are guarded by **one lock**. Every command is handled entirely while holding it. |
| Fan-out | Sending to a room only *puts the line on each member's outgoing queue* while holding the lock. That is cheap and never blocks, and doing it under the lock is what makes guarantee 1 hold. The writer threads do the actual network writes. |
| Slow or stalled clients | Each outgoing queue holds at most 1000 lines. If it fills, the client isn't reading, so the server drops it and announces `has disconnected`. One stuck laptop can't freeze the chat. |
| Detecting disconnects | `recv()` returning 0 bytes (orderly close) or raising (reset) ends the reader thread, which removes the user. A peer that vanishes without a word (Wi-Fi off) is caught by **TCP keepalive**: 10 s idle, then 3 probes 5 s apart, so about 25 s. On Windows, a thread blocked in `recv`/`send` is woken only by `close()`, not `shutdown()`, so the server does both. |

## 9. Web front end (stretch)

`webchat.py` is an HTTP gateway that lets a browser join. It doesn't interpret chat messages. For each browser it opens one ordinary TCP connection to the chat server and relays protocol lines unchanged:

```
browser ──HTTP──▶ webchat.py ──TCP, this protocol──▶ server.py
```

| Endpoint | Purpose |
|---|---|
| `POST /api/open` | Opens the TCP connection. Returns `{"session": "<id>"}`. |
| `GET /api/events?s=<id>` | Server-Sent Events: each line from the chat server becomes one `data:` event. An `event: closed` event is sent when the chat server hangs up. |
| `POST /api/send?s=<id>` | Request body is one protocol line to forward. |

The JavaScript in `webchat.html` builds and parses the same lines as `client.py`, so to the chat server a browser user is indistinguishable from a terminal user. If the tab is closed, the gateway notices within about a second and closes that TCP connection, and the server announces `has disconnected`.

## 10. Identity, and what it does not protect

A nickname is an account: claim it and you are that user, with their ID, their rooms, their
invitations and their history. There is no password, no token and no other proof, so this
holds only as far as everyone on the network is trusted to use their own name. Anyone who
connects while a nickname is free can take it and read every private room its owner was
invited to.

Nothing here is encrypted either: the protocol is plain text over plain TCP, so anyone who
can see the traffic can read every message, private chats included.

Both are acceptable for a classroom LAN and are not acceptable anywhere else. Closing the
gap would mean a password or token at `HELLO`, checked against a stored record, and TLS on
the transport — neither of which this protocol has.

## 11. Why the protocol looks like this

- **Text lines, not binary.** Easy to read in Wireshark, easy to debug with any TCP tool, and simple to parse in both Python and JavaScript. Chat is human text, so binary framing would gain little.
- **Free text last.** Putting the message text at the end means it needs no quoting or escaping. Everything before it is a single word.
- **Two names per room.** Letting people name a room `Study Group / CS 475` conflicts with a space-separated format in which the room is not always the last field. Rather than add quoting or escaping to every message, each room carries a one-word wire name for those fields and its real name as free text at the end. `ROOMS` became one `ROOM` line per room, closed by `ENDROOMS`, for the same reason: a free-text name can only be last, so a listing of them has to be several lines.
- **Echo to the sender (`MSG`).** The client shows a message only after the server accepted it. A message that failed is never displayed as sent, and the order on screen is the server's order.
- **One room at a time, and everything is a room.** A private chat is a two-person private room rather than a separate kind of message. One rule covers both, so private chats inherit stored history and a closed membership for free, and a client needs only one notion of "where I am". The cost is that opening a private chat moves you out of the room you were reading.
- **Identity separate from connection.** A nickname maps to a permanent ID, and rooms, invitations and history hang off that ID rather than off a socket. This is what lets a private chat still be there when you come back.
- **Errors never close the connection.** A typo shouldn't disconnect anyone. Only `QUIT`, a network failure, or falling 1000 lines behind ends a session.
