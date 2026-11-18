# WinGo Alert

WinGo Alert watches the public WinGo results. When Big or Small comes out
3 or 4 times in a row (you choose the numbers), it sends your saved message
boxes and an alert message to your Telegram channel, one by one, in order.

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

## The 4 tabs

**Home** — Start / Stop watching, the live results, and a log of what happened.

**Alert Page** — what is sent when an alert fires:
- Message boxes 1, 2, 3 … N. Each box has text, **+ Add File** and **+ Paste link**
  (a link to a Telegram post; it is copied in without "Forwarded from").
- The **Program message** box (the alert text) sits between your boxes.
  Move it up or down with **↑ ↓**.
- **Add more +** adds another box.
- **💾 Save** — alerts always use the **saved** version, not unsaved edits.

**Broadcast** — boxes sent to the channel right now with **Send now**.

**⚙ Settings**
- Bot token and channel (`@mychannel` or `-1001234567890`)
- Game: `WinGo_30S`, `WinGo_1M`, `WinGo_3M` or `WinGo_5M`
- Alert at: streak lengths, e.g. `3,4`
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

## Good to know

- Alerts fire only for **new** results that come out **after** you press Start.
- Run only **one copy per bot** (not the window and the server at the same
  time), or every alert is sent twice. `start.sh` closes an old copy for you.
- Your settings live in `settings.json` in this folder. Keep it private:
  it contains your bot token.
