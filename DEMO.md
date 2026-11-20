# Live Demo Script

About 8–10 minutes. Three laptops: **S** runs the server and Rakhmatillo's client, **A** is Aden, **L** is Alexis. Optional: a phone or two from the audience.

## Before class (day of)

- [ ] All three laptops on the same network. Bring a phone hotspot as backup.
- [ ] `python --version` works on every laptop (3.8+).
- [ ] Copy of the project folder on every laptop.
- [ ] On S: `python -m unittest` passes.
- [ ] On S: start `python server.py` once and **allow it through the firewall** (Private networks).
- [ ] Rehearsal: A and L can connect to S's IP address. Write the IP on a sticky note.
- [ ] Terminal font enlarged so the room can read it (Ctrl + mouse wheel).

## Setup (1 min)

On **S**, in two terminals:

```bash
python server.py
```

```bash
python webchat.py
```

Put the server log on the projector: it shows every connection and departure as it happens.

## 1. Multiple clients and nicknames (1 min)

- **S:** `python client.py --nick rakhmatillo`
- **A:** `python client.py --host <S-IP> --nick aden`
- **L:** `python client.py --host <S-IP>`, then at the prompt type `aden`.
  - It shows `error 433: Nickname aden is already in use`. Type `alexis` and it works.

*Talking point:* nickname uniqueness is checked under the server's lock, so two people can never claim the same name at the same moment. The test suite races 15 clients for one name.

## 2. Broadcast (1 min)

- Each person types a line. Everyone sees all three, **in the same order on every screen**.

*Talking point:* the server echoes your own message back, so what you see is the server's order, not your local guess. Each client has an outgoing queue that is filled under one lock, which is why the order matches everywhere.

## 3. Private chats (2 min)

- **A:** `/who` — every name comes with its user ID, e.g. `alexis @7c1f20`.
- **A:** `/dm @7c1f20` (or `/dm alexis`). Aden is moved into the private chat; the header
  names Alexis, and the lobby sees `aden left lobby for dm-…`.
- **A and L:** exchange a line or two. S sees none of it.
- **S:** `/rooms` — the private chat is not in Rakhmatillo's list at all. Only its two
  people can see it, and only they can enter it.
- **A:** `/quit`, reconnect as the same nickname, `/dm alexis`. **The conversation is still
  there**, replayed from the server.

*Talking point:* a private chat is just a room with exactly two members. That one decision
gives it stored history and a closed door without any special-case code — and it is why the
history survives a quit, which lives on the server rather than in the client.

## 4. Rooms, invitations and history (3 min)

- **L:** `/new public Games Night`. S and A see `alexis left lobby for Games Night` — room names take spaces and punctuation.
- **L:** types a couple of lines in games. S and A don't see them.
- **S:** `/rooms` shows `games (1)` and `lobby (2)`.
- **A:** `/join games`. Aden immediately sees **the lines Alexis already sent**, dimmed with
  timestamps. That's history for late joiners.
- **L:** `/new Hideout / Planning` — no `public`, so it is private. **S:** `/rooms` does not
  list it; `/join Hideout / Planning` gives `error 403`.
- **L:** `/invite @<S's id> Hideout / Planning`. Rakhmatillo gets the invitation, and now
  `/rooms` lists it and `/join Hideout / Planning` works. Commands take the name you see.
- **A:** `/delete Hideout / Planning` → `error 403`, because Alexis owns it, not Aden.
- **L:** `/delete Hideout / Planning`. Everyone inside is moved to `lobby` and the history
  is gone: creating it again starts empty.

## 5. Web front end (1–2 min, stretch)

- Projector: open `http://<S-IP>:8080` and join as `browser`. Optionally invite someone in the audience to open it on their phone.
- Send a message from the browser and it appears in the terminals. Click `games` in the sidebar to switch rooms, or a name under **In this room** to open a private chat with that person — both are the same action, because both are rooms.
- Private rooms carry a lock in the sidebar. For one you own, the header offers **Invite** and **Delete room**; for anyone else's, neither appears.
- Click a name under **In this room** to open a private chat with them. It gets its own transcript under **Private chats**, with an unread badge, and anything typed there goes to that person instead of the room.

*Talking point:* the gateway knows nothing about chat. It relays our protocol lines between HTTP and TCP. The page's JavaScript speaks the same protocol as `client.py`, so the server can't tell them apart.

## 6. Leaving: clean and unclean (1 min)

- **A:** `/quit`. Everyone sees `aden has left the chat`. The server log says `aden left the chat`.
- **L:** close the terminal window with the ✕ (don't use Ctrl+C: the client catches it and sends a polite `QUIT`). Everyone sees `alexis has disconnected`.
- **L** rejoins as `alexis` right away: the nickname was freed.
- Optional, more dramatic: **turn off L's Wi-Fi** and keep talking about something else. After about **25 seconds** everyone sees `alexis has disconnected`.

*Talking point:* `recv()` returning 0 bytes means an orderly close; an exception means a reset. Both lead to the same cleanup. A machine that just vanishes sends neither, so the server turns on **TCP keepalive**: after 10 idle seconds the OS probes the peer every 5 seconds, and after 3 unanswered probes it reports the connection dead.

> If the room is chatting while L's Wi-Fi is off, detection is governed by TCP's retransmission timeout instead of keepalive, and on Windows that can take longer. For a crisp demo, stay quiet for the 25 seconds.

## 7. Wrap-up (30 s)

- Show `PROTOCOL.md` briefly: message table, error codes.
- Mention the test suite: 55 tests, including a 20-client concurrent broadcast and a client that stops reading.

## If something goes wrong

| Problem | Fix |
|---|---|
| A/L can't connect | Firewall on S (see README). Otherwise switch everyone to the phone hotspot and get S's new IP. |
| Campus network blocks it and no hotspot | Run all clients on S in separate terminals and explain that it's the same code path over loopback. |
| Port 5050 already in use | `python server.py --port 5051` and `--port 5051` on the clients. |
| Garbled colors in an old Windows console | `python client.py --no-color ...` |
