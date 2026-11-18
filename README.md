# WinGo Alert

WinGo Alert watches the public WinGo results and follows every Big/Small
streak through 3 pages, sent to your Telegram channel one message at a time:

```
Start Page  ──►  Alert Page (repeats while the streak goes on)  ──►  End Page
 3x Big           4x Big, 5x Big, 6x Big …                         Small came: streak broke
```

- **Start Page**: sent when the streak reaches **Start at** (default 3).
- **Alert Page**: sent **again** on every next result of the same size.
- **End Page**: sent **once** when the other size comes. Then it waits for the next streak.

## Before you start

1. Make a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Add the bot to your channel as an **admin** (with "Post messages").
3. Python 3.8 or newer. The start scripts do the rest.

## How to start

**Mac / Linux** (open a terminal in this folder):

```bash
./start.sh
```

If you see "Permission denied":

```bash
bash start.sh
```

**Linux server / SSH (no screen)** — no window, logs in the terminal.
`start.sh` also switches to this by itself when there is no screen.

```bash
./start.sh --server
```

**Windows** — double-click `start.bat`, or run:

```bash
start.bat
```

**Windows, no window** (logs in the console, Ctrl+C to stop):

```bash
start.bat --server
```

Server mode uses the same `settings.json`. Set everything up in the window
on a computer with a screen, press 💾 Save, then copy `settings.json` (and
any files your boxes use, at the same paths) to the server.

## The tabs

**Home** — Start / Stop watching, the live results, and a log of what happened.

**Start Page / Alert Page / End Page** — each page has its own:
- Message boxes 1, 2, 3 … N. Each box has text, **+ Add File** and **+ Paste link**
  (a link to a Telegram post; it is copied in without "Forwarded from").
- The **Program message** box (the alert text) sits between your boxes.
  Move it up or down with **↑ ↓**.
- **Add more +** adds another box.
- **💾 Save** — alerts always use the **saved** version, not unsaved edits.
- **Send test …** — sends that page now with example numbers, to check how it looks.

**Broadcast** — boxes sent to the channel right now with **Send now**.

**⚙ Settings**
- Bot token and channel (`@mychannel` or `-1001234567890`)
- Game: `WinGo_30S`, `WinGo_1M`, `WinGo_3M` or `WinGo_5M`
- Start at: streak length that sends the Start Page (default `3`)
- Poll seconds: how often to check for new results
- Delay between messages (seconds)
- Auto start: start watching when the app opens

## Placeholders

Write these in any message; they are filled in when the alert is sent.

| Placeholder    | Example              | Meaning                          |
|----------------|----------------------|----------------------------------|
| `{game}`       | WinGo 1M             | the game                         |
| `{size}`       | Big                  | Big or Small                     |
| `{SIZE}`       | BIG                  | same, in capitals                |
| `{count}`      | 3                    | how many in a row                |
| `{icon}`       | 🔥 / 💧              | 🔥 for Big, 💧 for Small          |
| `{results}`    | 7 9 8                | the streak numbers, newest first |
| `{issue}`      | 20261118100010644    | period that made the streak      |
| `{next_issue}` | 20261118100010645    | the period running now           |
| `{time}`       | 2026-11-18 14:02:11  | when the alert was sent          |

End Page only (the result that broke the streak; `{count}`/`{SIZE}` there = the finished streak):

| Placeholder      | Example | Meaning                    |
|------------------|---------|----------------------------|
| `{break_number}` | 2       | the number that broke it   |
| `{break_size}`   | Small   | its size                   |
| `{BREAK_SIZE}`   | SMALL   | same, in capitals          |

## Good to know

- Alerts fire only for **new** results that come out **after** you press Start.
  If a streak is already going when you press Start, the Start Page is sent on its
  next result (e.g. as 4x Big).
- An old `settings.json` with one Alert Page is moved to the **Start Page** automatically.
- Run only **one copy per bot** (not the window and the server at the same
  time), or every alert is sent twice. `start.sh` closes an old copy for you.
- Your settings live in `settings.json` in this folder. Keep it private:
  it contains your bot token.
