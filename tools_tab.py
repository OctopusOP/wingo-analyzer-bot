"""
tools_tab.py — the 🛠 Channel Tools tab
======================================
Separate from the WinGo alerts. It can use its OWN bot and works with MANY
channels that you save once:

  Bots & Channels   save bots (name + token) and channels (name + @name / -100…),
                    check them (is the bot admin? which rights?)
  Copy              copy a range of messages from one channel into one or more
                    channels (no "Forwarded from" tag, or forward with the tag)
  Delete            delete a range of messages from a channel
  Post to many      send message boxes (text / file / link) to many channels
  More              channel info, make an invite link, pin / unpin

Everything slow runs in a background thread (tools.py). Results come back
through the app's queue as ("call", function) and run on the main thread,
because Tkinter may only be touched from there.
"""

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import storage
import tools
from links import parse_many
from telegram_api import TelegramBot, TelegramError

PAD = 8
ALERT_BOT = "Same bot as the alerts (⚙ Settings)"


def mask(token: str) -> str:
    """'123456:ABCdef…' -> '123456:••••••' (never show a whole token)."""
    return token.split(":")[0] + ":••••••" if token else ""


class ToolsTab(ttk.Frame):
    def __init__(self, parent, app, editor_class):
        super().__init__(parent, padding=PAD)
        self.app = app                  # the main window (for its queue)
        self.editor_class = editor_class  # BlocksEditor from app.py (message boxes)
        self.job = None                 # the running tools.Job (only one at a time)
        self.combos = []                # every "pick a channel" combobox (refreshed together)
        self.target_lists = []          # every "pick many channels" listbox
        s = storage.load_settings()

        # ---- top bar: which bot + delay ----
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="Bot for these tools:", font=("", 11, "bold")).pack(side="left")
        self.bot_var = tk.StringVar(value=s.get("tools_bot") or ALERT_BOT)
        self.bot_combo = ttk.Combobox(top, textvariable=self.bot_var, state="readonly", width=34)
        self.bot_combo.pack(side="left", padx=4)
        self.bot_combo.bind("<<ComboboxSelected>>", lambda e: self._save_key("tools_bot", self._bot_name()))
        ttk.Label(top, text="Delay between calls (s):").pack(side="left", padx=(PAD * 2, 2))
        self.delay_var = tk.StringVar(value=str(s.get("tools_delay", 1.0)))
        ttk.Spinbox(top, from_=0, to=30, increment=0.5, textvariable=self.delay_var, width=6).pack(side="left")

        # ---- the sub-tabs ----
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, pady=PAD)
        for title, build in (("Bots & Channels", self._build_setup), ("Copy", self._build_copy),
                             ("Delete", self._build_delete), ("Post to many", self._build_post),
                             ("More", self._build_more)):
            frame = ttk.Frame(nb, padding=PAD)
            nb.add(frame, text=f" {title} ")
            build(frame, s)

        # ---- bottom: progress + stop + log ----
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self.progress = ttk.Progressbar(bar, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_label = ttk.Label(bar, text="", width=14)
        self.progress_label.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(bar, text="■ Stop", command=self.stop_job, state="disabled")
        self.stop_btn.pack(side="left")
        self.log_box = ScrolledText(self, height=8, state="disabled", wrap="word")
        self.log_box.pack(fill="both", expand=False, pady=(4, 0))

        self.refresh_lists()

    # =========================================================================
    # Saved bots / channels
    # =========================================================================
    def _settings(self):
        return storage.load_settings()

    def _save_key(self, key, value):
        s = self._settings()
        s[key] = value
        storage.save_settings(s)

    def _bot_name(self):
        name = self.bot_var.get()
        return "" if name == ALERT_BOT else name

    def token(self) -> str:
        """Token of the bot picked at the top ('' if none)."""
        s = self._settings()
        name = self._bot_name()
        if not name:
            return s.get("token", "")
        for b in s.get("tools_bots", []):
            if b.get("name") == name:
                return b.get("token", "")
        return ""

    def channel_names(self):
        """'Name  (id)' for every saved channel."""
        return [f"{c['name']}  ({c['id']})" for c in self._settings().get("tools_channels", [])]

    def resolve(self, text: str) -> str:
        """A picked 'Name  (id)' -> id. Anything typed by hand is used as it is."""
        text = (text or "").strip()
        for c in self._settings().get("tools_channels", []):
            if text == f"{c['name']}  ({c['id']})" or text == c["name"]:
                return c["id"]
        return text

    def refresh_lists(self):
        """Puts the saved bots / channels into every list and combobox."""
        s = self._settings()
        bots = s.get("tools_bots", [])
        self.bot_combo.configure(values=[ALERT_BOT] + [b["name"] for b in bots])
        if self.bot_var.get() not in self.bot_combo.cget("values"):
            self.bot_var.set(ALERT_BOT)
        names = self.channel_names()
        for combo in self.combos:
            combo.configure(values=names)
        for lb in self.target_lists:
            picked = {lb.get(i) for i in lb.curselection()}
            lb.delete(0, "end")
            for n in names:
                lb.insert("end", n)
                if n in picked:
                    lb.selection_set("end")
        # the two tables on "Bots & Channels"
        self.bot_tree.delete(*self.bot_tree.get_children())
        for b in bots:
            self.bot_tree.insert("", "end", values=(b["name"], mask(b["token"])))
        self.chan_tree.delete(*self.chan_tree.get_children())
        for c in s.get("tools_channels", []):
            self.chan_tree.insert("", "end", values=(c["name"], c["id"]))

    def channel_combo(self, parent, width=40):
        combo = ttk.Combobox(parent, width=width)  # editable: pick a saved one OR type @name / -100…
        self.combos.append(combo)
        return combo

    def targets_box(self, parent, height=6):
        """A multi-select list of saved channels + a field for extra ids. Returns (frame, getter)."""
        frame = ttk.Frame(parent)
        lb = tk.Listbox(frame, selectmode="multiple", height=height, exportselection=False)
        lb.pack(fill="x")
        self.target_lists.append(lb)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(2, 0))
        ttk.Button(row, text="All", width=5, command=lambda: lb.selection_set(0, "end")).pack(side="left")
        ttk.Button(row, text="None", width=5, command=lambda: lb.selection_clear(0, "end")).pack(side="left")
        ttk.Label(row, text="  + other ids:").pack(side="left")
        extra = tk.StringVar()
        ttk.Entry(row, textvariable=extra).pack(side="left", fill="x", expand=True)

        def get():
            picked = [self.resolve(lb.get(i)) for i in lb.curselection()]
            picked += [x for x in extra.get().replace(",", " ").split() if x]
            return list(dict.fromkeys(picked))  # no duplicates, same order
        return frame, get

    # =========================================================================
    # Log / progress / jobs
    # =========================================================================
    def log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def later(self, fn, *args):
        """Run fn(*args) on the main thread (safe to call from any thread)."""
        self.app.q.put(("call", lambda: fn(*args)))

    def _set_progress(self, done, total):
        self.progress.configure(maximum=max(total, 1), value=done)
        self.progress_label.configure(text=f"{done}/{total}")

    def _job_done(self):
        self.job = None
        self.stop_btn.configure(state="disabled")

    def callbacks(self):
        return {"on_log": lambda t: self.later(self.log, t),
                "on_progress": lambda d, t: self.later(self._set_progress, d, t),
                "on_done": lambda: self.later(self._job_done)}

    def start_job(self, job):
        self.job = job
        self._set_progress(0, 1)
        self.stop_btn.configure(state="normal")
        job.start()

    def stop_job(self):
        if self.job:
            self.job.stop()
            self.log("Stopping after the current step…")

    def ready(self) -> bool:
        """Checks there's a bot and nothing else running."""
        if self.job:
            messagebox.showinfo("Busy", "Another job is running. Wait or press ■ Stop.")
            return False
        if not self.token():
            messagebox.showwarning("No bot", "Pick a bot at the top (or add one in Bots & Channels).")
            return False
        return True

    def delay(self) -> float:
        try:
            value = max(0.0, float(self.delay_var.get()))
        except ValueError:
            value = 1.0
        self._save_key("tools_delay", value)
        return value

    def in_thread(self, work, *args):
        """Run a quick Telegram call in a thread; log its text result or error."""
        def run():
            try:
                self.later(self.log, work(*args))
            except (TelegramError, ValueError) as e:
                self.later(self.log, f"✖ {e}")
        threading.Thread(target=run, daemon=True).start()

    # =========================================================================
    # Sub-tab: Bots & Channels
    # =========================================================================
    def _build_setup(self, f, s):
        # ---- bots ----
        ttk.Label(f, text="Bots", font=("", 12, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(f, text="Use a different bot from the alerts if you want. It must be ADMIN in the channels.",
                  foreground="gray").grid(row=1, column=0, sticky="w")
        self.bot_tree = ttk.Treeview(f, columns=("name", "token"), show="headings", height=4)
        self.bot_tree.heading("name", text="Name")
        self.bot_tree.heading("token", text="Token")
        self.bot_tree.grid(row=2, column=0, sticky="ew")
        row = ttk.Frame(f)
        row.grid(row=3, column=0, sticky="ew", pady=4)
        self.new_bot_name, self.new_bot_token = tk.StringVar(), tk.StringVar()
        ttk.Label(row, text="Name").pack(side="left")
        ttk.Entry(row, textvariable=self.new_bot_name, width=14).pack(side="left", padx=4)
        ttk.Label(row, text="Token").pack(side="left")
        ttk.Entry(row, textvariable=self.new_bot_token, show="•", width=30).pack(side="left", padx=4)
        ttk.Button(row, text="+ Add bot", command=self.add_bot).pack(side="left")
        ttk.Button(row, text="Remove", command=self.remove_bot).pack(side="left", padx=4)
        ttk.Button(row, text="Check", command=self.check_bot).pack(side="left")

        # ---- channels ----
        ttk.Label(f, text="Channels", font=("", 12, "bold")).grid(row=4, column=0, sticky="w", pady=(PAD, 0))
        ttk.Label(f, text="Id = @publicname or -100… (forward a post to @userinfobot, or copy a post link: "
                          "t.me/c/1234567890/5 → -1001234567890)", foreground="gray").grid(row=5, column=0, sticky="w")
        self.chan_tree = ttk.Treeview(f, columns=("name", "id"), show="headings", height=6)
        self.chan_tree.heading("name", text="Name")
        self.chan_tree.heading("id", text="Id")
        self.chan_tree.grid(row=6, column=0, sticky="nsew")
        row = ttk.Frame(f)
        row.grid(row=7, column=0, sticky="ew", pady=4)
        self.new_chan_name, self.new_chan_id = tk.StringVar(), tk.StringVar()
        ttk.Label(row, text="Name").pack(side="left")
        ttk.Entry(row, textvariable=self.new_chan_name, width=14).pack(side="left", padx=4)
        ttk.Label(row, text="Id / link").pack(side="left")
        ttk.Entry(row, textvariable=self.new_chan_id, width=30).pack(side="left", padx=4)
        ttk.Button(row, text="+ Add channel", command=self.add_channel).pack(side="left")
        ttk.Button(row, text="Remove", command=self.remove_channel).pack(side="left", padx=4)
        ttk.Button(row, text="Check (admin rights)", command=self.check_channels).pack(side="left")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(6, weight=1)

    def add_bot(self):
        name, token = self.new_bot_name.get().strip(), self.new_bot_token.get().strip()
        if not name or ":" not in token:
            messagebox.showwarning("Add bot", "Give a name and a token like 123456:ABC… (from @BotFather).")
            return
        s = self._settings()
        bots = [b for b in s.get("tools_bots", []) if b["name"] != name]  # same name = replace
        bots.append({"name": name, "token": token})
        s["tools_bots"] = bots
        storage.save_settings(s)
        self.new_bot_name.set("")
        self.new_bot_token.set("")
        self.refresh_lists()
        self.log(f"Bot '{name}' saved.")

    def remove_bot(self):
        names = {self.bot_tree.item(i)["values"][0] for i in self.bot_tree.selection()}
        if names:
            s = self._settings()
            s["tools_bots"] = [b for b in s.get("tools_bots", []) if b["name"] not in names]
            storage.save_settings(s)
            self.refresh_lists()

    def check_bot(self):
        """Checks the bots picked in the table (or, if none, the bot picked at the top)."""
        tokens = {b["name"]: b["token"] for b in self._settings().get("tools_bots", [])}
        picked = [str(self.bot_tree.item(i)["values"][0]) for i in self.bot_tree.selection()]
        to_check = [(n, tokens.get(n, "")) for n in picked] or [(self.bot_var.get(), self.token())]
        for name, token in to_check:
            if not token:
                self.log(f"✖ {name}: no token")
                continue
            self.in_thread(lambda t=token, n=name: f"✔ {n}: @{TelegramBot(t).get_me().get('username')} works")

    def add_channel(self):
        name, cid = self.new_chan_name.get().strip(), self.new_chan_id.get().strip()
        if cid and ("t.me/" in cid):     # a pasted post link -> its channel id
            try:
                cid = tools.message_number(cid)[0] or cid
            except ValueError:
                pass
        if not cid:
            messagebox.showwarning("Add channel", "Give an id like @mychannel or -1001234567890 (or paste a post link).")
            return
        s = self._settings()
        chans = [c for c in s.get("tools_channels", []) if c["id"] != cid]
        chans.append({"name": name or cid, "id": cid})
        s["tools_channels"] = chans
        storage.save_settings(s)
        self.new_chan_name.set("")
        self.new_chan_id.set("")
        self.refresh_lists()
        self.log(f"Channel '{name or cid}' saved.")

    def remove_channel(self):
        ids = {str(self.chan_tree.item(i)["values"][1]) for i in self.chan_tree.selection()}
        if ids:
            s = self._settings()
            s["tools_channels"] = [c for c in s.get("tools_channels", []) if c["id"] not in ids]
            storage.save_settings(s)
            self.refresh_lists()

    def check_channels(self):
        if not self.token():
            messagebox.showwarning("No bot", "Pick a bot at the top first.")
            return
        ids = [str(self.chan_tree.item(i)["values"][1]) for i in self.chan_tree.selection()]
        ids = ids or [c["id"] for c in self._settings().get("tools_channels", [])]
        bot = TelegramBot(self.token())
        for cid in ids:
            self.in_thread(tools.channel_report, bot, cid)

    # =========================================================================
    # Sub-tab: Copy
    # =========================================================================
    def range_rows(self, f, row, with_channel_from_link=None):
        """'From message' + 'To message' + 'Find newest'. Returns (from_var, to_var)."""
        from_var, to_var = tk.StringVar(), tk.StringVar()
        line = ttk.Frame(f)
        line.grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Entry(line, textvariable=from_var, width=34).pack(side="left")
        ttk.Label(line, text="  to  ").pack(side="left")
        ttk.Entry(line, textvariable=to_var, width=34).pack(side="left")
        ttk.Button(line, text="Find newest #", command=lambda: self.find_newest(with_channel_from_link, to_var)) \
            .pack(side="left", padx=4)
        ttk.Label(f, text="Message number (e.g. 120) or a post link (https://t.me/c/123…/120). "
                          "Leave 'to' empty for one message.", foreground="gray") \
            .grid(row=row + 1, column=1, sticky="w")

        # Pasting a link while the channel box is empty fills the channel in
        def autofill(*_):
            combo = with_channel_from_link
            if combo is not None and not combo.get().strip():
                try:
                    chat = tools.message_number(from_var.get())[0]
                except ValueError:
                    chat = None
                if chat:
                    combo.set(chat)
        from_var.trace_add("write", autofill)
        return from_var, to_var

    def find_newest(self, combo, to_var):
        chat = self.resolve(combo.get()) if combo is not None else ""
        if not chat or not self.token():
            messagebox.showwarning("Find newest", "Pick the channel (and a bot) first.")
            return
        bot = TelegramBot(self.token())

        def work():
            mid = tools.find_last_id(bot, chat)
            self.later(to_var.set, str(mid))
            return f"Newest message in {chat}: #{mid} (a tiny message was posted and deleted to find it)"
        self.in_thread(work)

    def _build_copy(self, f, s):
        ttk.Label(f, text="Copy messages from one channel into others (same order, albums kept)",
                  font=("", 12, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="From channel").grid(row=1, column=0, sticky="w", pady=4)
        self.copy_source = self.channel_combo(f)
        self.copy_source.grid(row=1, column=1, sticky="w")
        ttk.Label(f, text="Messages").grid(row=2, column=0, sticky="w")
        self.copy_from, self.copy_to = self.range_rows(f, 2, self.copy_source)
        ttk.Label(f, text="To channel(s)").grid(row=4, column=0, sticky="nw", pady=4)
        box, self.copy_targets = self.targets_box(f)
        box.grid(row=4, column=1, sticky="ew", pady=4)
        self.copy_mode = tk.StringVar(value="copy")
        modes = ttk.Frame(f)
        modes.grid(row=5, column=1, sticky="w")
        ttk.Radiobutton(modes, text="Copy (no “Forwarded from”)", value="copy", variable=self.copy_mode).pack(side="left")
        ttk.Radiobutton(modes, text="Forward (keep the tag)", value="forward", variable=self.copy_mode).pack(side="left", padx=PAD)
        ttk.Button(f, text="▶ Start copy", command=self.start_copy).grid(row=6, column=1, sticky="w", pady=PAD)
        ttk.Label(f, text="Or paste many links (any channels, any order) — they are copied in that order:",
                  foreground="gray").grid(row=7, column=1, sticky="w")
        self.copy_links = tk.Text(f, height=3, wrap="word")
        self.copy_links.grid(row=8, column=1, sticky="ew")
        ttk.Button(f, text="▶ Copy pasted links", command=self.start_copy_links).grid(row=9, column=1, sticky="w", pady=4)
        f.columnconfigure(1, weight=1)

    def start_copy(self):
        if not self.ready():
            return
        try:
            link_chat, ids = tools.parse_range(self.copy_from.get(), self.copy_to.get())
        except ValueError as e:
            messagebox.showwarning("Copy", str(e))
            return
        source = self.resolve(self.copy_source.get()) or link_chat
        targets = self.copy_targets()
        if not source or not targets:
            messagebox.showwarning("Copy", "Pick the source channel and at least one target channel.")
            return
        if source in targets:
            messagebox.showwarning("Copy", "The source channel is also a target. Remove it from the targets.")
            return
        if not messagebox.askyesno("Copy", f"Copy messages {ids[0]}–{ids[-1]} ({len(ids)} numbers)\n"
                                           f"from {source}\nto {len(targets)} channel(s)?"):
            return
        self.start_job(tools.CopyJob(self.token(), source, ids, targets, self.copy_mode.get(),
                                     self.delay(), **self.callbacks()))

    def start_copy_links(self):
        """Pasted links -> copies grouped per source channel, keeping the pasted order."""
        if not self.ready():
            return
        from links import group_into_batches
        pairs, errors = parse_many(self.copy_links.get("1.0", "end"))
        targets = self.copy_targets()
        if errors:
            messagebox.showwarning("Links", "\n".join(errors[:5]))
            return
        if not pairs or not targets:
            messagebox.showwarning("Copy", "Paste some links and pick at least one target channel.")
            return
        groups = group_into_batches(pairs)
        callbacks, mode, delay, token = self.callbacks(), self.copy_mode.get(), self.delay(), self.token()

        class LinksJob(tools.Job):   # runs one CopyJob-step per group, in order
            title = "Copy links"

            def work(job):
                for i, (chat, ids) in enumerate(groups, start=1):
                    if job.stop_event.is_set():
                        job.on_log("■ Stopped.")
                        return
                    step = tools.CopyJob(token, chat, ids, targets, mode, delay, **callbacks)
                    step.stop_event = job.stop_event
                    step.work()
                    job.on_progress(i, len(groups))
        self.start_job(LinksJob(token, delay, **callbacks))

    # =========================================================================
    # Sub-tab: Delete
    # =========================================================================
    def _build_delete(self, f, s):
        ttk.Label(f, text="Delete a range of messages from a channel", font=("", 12, "bold")) \
            .grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="⚠ Deleted messages can NOT be brought back. The bot needs the "
                          "'Delete messages' admin right. Telegram may refuse messages older than 48 hours.",
                  foreground="#d33", wraplength=700, justify="left").grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Channel").grid(row=2, column=0, sticky="w", pady=4)
        self.del_chat = self.channel_combo(f)
        self.del_chat.grid(row=2, column=1, sticky="w")
        ttk.Label(f, text="Messages").grid(row=3, column=0, sticky="w")
        self.del_from, self.del_to = self.range_rows(f, 3, self.del_chat)
        ttk.Button(f, text="🗑 Delete these messages", command=self.start_delete).grid(row=5, column=1, sticky="w", pady=PAD)
        f.columnconfigure(1, weight=1)

    def start_delete(self):
        if not self.ready():
            return
        try:
            link_chat, ids = tools.parse_range(self.del_from.get(), self.del_to.get())
        except ValueError as e:
            messagebox.showwarning("Delete", str(e))
            return
        chat = self.resolve(self.del_chat.get()) or link_chat
        if not chat:
            messagebox.showwarning("Delete", "Pick the channel.")
            return
        if not messagebox.askyesno("Delete — can't be undone",
                                   f"Delete messages {ids[0]}–{ids[-1]} ({len(ids)} numbers)\n"
                                   f"in {chat}?\n\nThis can NOT be undone.", icon="warning"):
            return
        self.start_job(tools.DeleteJob(self.token(), chat, ids, self.delay(), **self.callbacks()))

    # =========================================================================
    # Sub-tab: Post to many
    # =========================================================================
    def _build_post(self, f, s):
        ttk.Label(f, text="Send message boxes to many channels (one channel after another, boxes in order)",
                  font=("", 12, "bold")).pack(anchor="w")
        box, self.post_targets = self.targets_box(f, height=4)
        box.pack(fill="x", pady=4)
        self.post_editor = self.editor_class(f, on_change=lambda: None)
        self.post_editor.pack(fill="both", expand=True)
        self.post_editor.load(s.get("tools_post_blocks") or [storage.empty_box(), storage.empty_box()])
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(4, 0))
        ttk.Button(bar, text="Add more +", command=self.post_editor.add_box).pack(side="left")
        ttk.Button(bar, text="💾 Save", command=self.save_post).pack(side="left", padx=PAD)
        ttk.Button(bar, text="📤 Send to picked channels", command=self.start_post).pack(side="right")

    def save_post(self):
        self._save_key("tools_post_blocks", self.post_editor.get_blocks())
        self.log("💾 Post boxes saved.")

    def start_post(self):
        if not self.ready():
            return
        blocks = [b for b in self.post_editor.get_blocks() if b["text"].strip() or b["file"] or b["link"]]
        targets = self.post_targets()
        if not blocks or not targets:
            messagebox.showwarning("Post", "Fill at least one box and pick at least one channel.")
            return
        if not messagebox.askyesno("Post", f"Send {len(blocks)} box(es) to {len(targets)} channel(s)?"):
            return
        self.start_job(tools.PostJob(self.token(), targets, blocks, self.delay(), **self.callbacks()))

    # =========================================================================
    # Sub-tab: More
    # =========================================================================
    def _build_more(self, f, s):
        ttk.Label(f, text="Channel").grid(row=0, column=0, sticky="w", pady=4)
        self.more_chat = self.channel_combo(f)
        self.more_chat.grid(row=0, column=1, sticky="w")

        ttk.Button(f, text="ℹ Channel info + bot rights", command=self.more_info) \
            .grid(row=1, column=1, sticky="w", pady=4)

        line = ttk.Frame(f)
        line.grid(row=2, column=1, sticky="w", pady=4)
        self.invite_name = tk.StringVar()
        self.invite_limit = tk.StringVar(value="0")
        self.invite_request = tk.BooleanVar()
        ttk.Button(line, text="🔗 Make invite link", command=self.more_invite).pack(side="left")
        ttk.Label(line, text=" name").pack(side="left")
        ttk.Entry(line, textvariable=self.invite_name, width=14).pack(side="left")
        ttk.Label(line, text=" max people (0 = no limit)").pack(side="left")
        ttk.Spinbox(line, from_=0, to=99999, textvariable=self.invite_limit, width=7).pack(side="left")
        ttk.Checkbutton(line, text="needs approval", variable=self.invite_request).pack(side="left", padx=4)

        line = ttk.Frame(f)
        line.grid(row=3, column=1, sticky="w", pady=4)
        self.pin_id = tk.StringVar()
        ttk.Button(line, text="📌 Pin message", command=self.more_pin).pack(side="left")
        ttk.Entry(line, textvariable=self.pin_id, width=34).pack(side="left", padx=4)
        ttk.Label(line, text="number or post link", foreground="gray").pack(side="left")

        ttk.Button(f, text="Unpin all messages", command=self.more_unpin).grid(row=4, column=1, sticky="w", pady=4)

        line = ttk.Frame(f)
        line.grid(row=5, column=1, sticky="w", pady=4)
        newest = tk.StringVar()
        ttk.Button(line, text="Find newest message #", command=lambda: self.find_newest(self.more_chat, newest)) \
            .pack(side="left")
        ttk.Entry(line, textvariable=newest, width=12, state="readonly").pack(side="left", padx=4)
        f.columnconfigure(1, weight=1)

    def _more_bot_chat(self):
        chat = self.resolve(self.more_chat.get())
        if not chat or not self.token():
            messagebox.showwarning("Channel", "Pick a channel (and a bot at the top) first.")
            return None, None
        return TelegramBot(self.token()), chat

    def more_info(self):
        bot, chat = self._more_bot_chat()
        if bot:
            self.in_thread(tools.channel_report, bot, chat)

    def more_invite(self):
        bot, chat = self._more_bot_chat()
        if not bot:
            return
        try:
            limit = int(self.invite_limit.get() or 0)
        except ValueError:
            limit = 0
        name, request = self.invite_name.get().strip(), self.invite_request.get()
        self.in_thread(lambda: f"🔗 New invite link for {chat}: "
                               f"{bot.create_invite_link(chat, name, limit, request)}")

    def more_pin(self):
        bot, chat = self._more_bot_chat()
        if not bot:
            return
        try:
            mid = tools.message_number(self.pin_id.get())[1]
        except ValueError as e:
            messagebox.showwarning("Pin", str(e))
            return
        self.in_thread(lambda: (bot.pin_message(chat, mid), f"📌 Pinned #{mid} in {chat}")[1])

    def more_unpin(self):
        bot, chat = self._more_bot_chat()
        if bot and messagebox.askyesno("Unpin all", f"Unpin ALL pinned messages in {chat}?"):
            self.in_thread(lambda: (bot.unpin_all(chat), f"Unpinned everything in {chat}")[1])
