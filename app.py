"""
app.py — WinGo Alert (desktop app for Mac, Linux and Windows)
=============================================================
Run it with:   python3 app.py        (or ./start.sh / start.bat)

Tabs (left to right, like the sketch):
  Home         Start / Stop watching, live results, current streak, log
  Alert Page   your message boxes + the program message. When a Big/Small
               streak hits 3 or 4 (⚙ "Alert at"), ALL of them are sent to the
               channel one by one, top to bottom.
  Broadcast    message boxes you send to the channel right now ("Send now")
  ⚙            bot token, channel, game, alert rules

Each message box = text + "+ Add File" + "+ Paste link". Press 💾 Save:
alerts always use the SAVED version, so half-typed edits are never sent.

Tkinter (the window library) is built into Python and works on all three
systems, so the app stays small. Telegram + API work runs in background
threads (wingo.py, sender.py); they talk to the window through a queue that
`_drain_queue()` empties every 100 ms (Tkinter may only be used from the
main thread).
"""

import queue
import sys
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import storage
from links import parse_many
from sender import get_sender, sample_info
from telegram_api import TelegramBot, TelegramError
from wingo import GAMES, Watcher

PAD = 8  # standard spacing between widgets
PLACEHOLDERS = "{game} {size} {SIZE} {count} {icon} {results} {issue} {next_issue} {time}"


# =============================================================================
# A frame that scrolls (so you can add as many message boxes as you want)
# =============================================================================
class ScrollFrame(ttk.Frame):
    """Put widgets inside `.inner`; a scrollbar appears when they don't fit."""

    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        # Scroll area = size of the content; content = width of the window
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        # Mouse wheel while the pointer is over the list.
        # macOS/Windows send <MouseWheel>; Linux sends Button-4 (up) / Button-5 (down).
        self.bind("<Enter>", self._wheel_on)
        self.bind("<Leave>", self._wheel_off)

    def _wheel_on(self, _event=None):
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

    def _wheel_off(self, _event=None):
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.unbind_all(seq)

    def scroll_to_bottom(self):
        self.update_idletasks()
        self.canvas.yview_moveto(1.0)


# =============================================================================
# One message box:  text  |  + Add File   + Paste link   ↑ ↓ ✕
# =============================================================================
class BoxWidget(ttk.LabelFrame):
    def __init__(self, editor: "BlocksEditor", block=None):
        super().__init__(editor.scroll.inner, text="Message Box", padding=6)
        self.editor = editor
        self.file_path = ""

        # ---- left: the text you type ----
        self.text = tk.Text(self, height=3, wrap="word", undo=True, width=40)
        self.text.grid(row=0, column=0, rowspan=3, sticky="nsew", padx=(0, PAD))
        self.text.bind("<<Modified>>", self._text_changed)

        # ---- right top: file ----
        file_row = ttk.Frame(self)
        file_row.grid(row=0, column=1, sticky="ew")
        ttk.Button(file_row, text="+ Add File", command=self.pick_file).pack(side="left")
        self.file_label = ttk.Label(file_row, text="no file", foreground="gray")
        self.file_label.pack(side="left", padx=4)
        self.file_clear = ttk.Button(file_row, text="✕", width=2, command=lambda: self.set_file(""))

        # ---- right middle: message link ----
        link_row = ttk.Frame(self)
        link_row.grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(link_row, text="+ Paste link", command=self.paste_link).pack(side="left")
        self.link_var = tk.StringVar()
        ttk.Entry(link_row, textvariable=self.link_var).pack(side="left", fill="x", expand=True, padx=4)
        self.link_var.trace_add("write", lambda *_: (self._show_link_status(), editor.changed()))
        self.link_status = ttk.Label(self, foreground="gray")
        self.link_status.grid(row=2, column=1, sticky="w")

        # ---- far right: move up / down / delete ----
        tools = ttk.Frame(self)
        tools.grid(row=0, column=2, rowspan=3, sticky="n", padx=(PAD, 0))
        ttk.Button(tools, text="↑", width=2, command=lambda: editor.move(self, -1)).pack()
        ttk.Button(tools, text="↓", width=2, command=lambda: editor.move(self, 1)).pack()
        ttk.Button(tools, text="✕", width=2, command=lambda: editor.remove(self)).pack()

        self.columnconfigure(0, weight=3)
        self.columnconfigure(1, weight=2)

        if block:
            self.text.insert("1.0", block.get("text", ""))
            self.text.edit_modified(False)
            self.set_file(block.get("file", ""), notify=False)
            self.link_var.set(block.get("link", ""))
        self._show_link_status()

    # ---- reading / writing ----
    def get_block(self):
        return {"type": "box", "text": self.text.get("1.0", "end-1c"),
                "file": self.file_path, "link": self.link_var.get().strip()}

    def is_empty(self):
        b = self.get_block()
        return not (b["text"].strip() or b["file"] or b["link"])

    # ---- buttons ----
    def pick_file(self):
        path = filedialog.askopenfilename(title="Choose a file to send")
        if path:
            self.set_file(path)

    def set_file(self, path, notify=True):
        import os
        self.file_path = path
        if path:
            self.file_label.configure(text="📄 " + os.path.basename(path), foreground="#1a8f4e")
            self.file_clear.pack(side="left")
        else:
            self.file_label.configure(text="no file", foreground="gray")
            self.file_clear.pack_forget()
        if notify:
            self.editor.changed()

    def paste_link(self):
        """Pastes the clipboard into the link field (adds to what's there)."""
        try:
            clip = self.clipboard_get().strip()
        except tk.TclError:
            clip = ""
        if not clip:
            messagebox.showinfo("Clipboard empty", "Copy a message link first:\n"
                                "in Telegram right-click a message → Copy Message Link.")
            return
        current = self.link_var.get().strip()
        self.link_var.set(f"{current} {clip}".strip())

    def _show_link_status(self):
        value = self.link_var.get().strip()
        if not value:
            self.link_status.configure(text="link: e.g. https://t.me/c/2583777886/414  (range: /410-414)",
                                       foreground="gray")
            return
        pairs, errors = parse_many(value)
        if errors:
            self.link_status.configure(text="⚠ " + errors[0], foreground="#d33")
        else:
            self.link_status.configure(text=f"🔗 {len(pairs)} message(s), copied without “Forwarded from”",
                                       foreground="#1a8f4e")

    def _text_changed(self, _event=None):
        if self.text.edit_modified():
            self.text.edit_modified(False)
            self.editor.changed()


# =============================================================================
# The program message (the alert text the app writes) — movable, not deletable
# =============================================================================
class ProgramWidget(ttk.LabelFrame):
    def __init__(self, editor: "BlocksEditor", template: str):
        super().__init__(editor.scroll.inner, text="🎯 The Program message (alert)", padding=6)
        self.editor = editor
        self.text = tk.Text(self, height=6, wrap="word", undo=True, background="#fff8e6", foreground="#222")
        self.text.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, PAD))
        self.text.insert("1.0", template)
        self.text.edit_modified(False)
        self.text.bind("<<Modified>>", self._text_changed)

        ttk.Label(self, text=f"Filled in automatically:\n{PLACEHOLDERS}", foreground="gray",
                  wraplength=220, justify="left").grid(row=0, column=1, sticky="nw")
        ttk.Button(self, text="Reset text", command=self.reset).grid(row=1, column=1, sticky="sw")

        tools = ttk.Frame(self)
        tools.grid(row=0, column=2, rowspan=2, sticky="n", padx=(PAD, 0))
        ttk.Button(tools, text="↑", width=2, command=lambda: editor.move(self, -1)).pack()
        ttk.Button(tools, text="↓", width=2, command=lambda: editor.move(self, 1)).pack()
        self.columnconfigure(0, weight=1)

    def get_block(self):
        return {"type": "program"}

    def get_template(self):
        return self.text.get("1.0", "end-1c")

    def reset(self):
        self.text.delete("1.0", "end")
        self.text.insert("1.0", storage.DEFAULT_PROGRAM_TEMPLATE)

    def _text_changed(self, _event=None):
        if self.text.edit_modified():
            self.text.edit_modified(False)
            self.editor.changed()


# =============================================================================
# A scrollable list of blocks (boxes + maybe the program message)
# =============================================================================
class BlocksEditor(ttk.Frame):
    def __init__(self, parent, on_change):
        super().__init__(parent)
        self.on_change = on_change
        self.widgets = []
        self.program = None  # the ProgramWidget, if this list has one
        self.scroll = ScrollFrame(self)
        self.scroll.pack(fill="both", expand=True)
        self._loading = False

    def load(self, blocks, template=""):
        """Builds the widgets from saved blocks."""
        self._loading = True
        for w in self.widgets:
            w.destroy()
        self.widgets, self.program = [], None
        for b in blocks:
            if b.get("type") == "program":
                self.program = ProgramWidget(self, template)
                self.widgets.append(self.program)
            else:
                self.widgets.append(BoxWidget(self, b))
        self._relayout()
        self._loading = False

    def get_blocks(self):
        return [w.get_block() for w in self.widgets]

    def add_box(self):
        self.widgets.append(BoxWidget(self))
        self._relayout()
        self.changed()
        self.scroll.scroll_to_bottom()
        self.widgets[-1].text.focus_set()

    def move(self, widget, step):
        i = self.widgets.index(widget)
        j = i + step
        if 0 <= j < len(self.widgets):
            self.widgets[i], self.widgets[j] = self.widgets[j], self.widgets[i]
            self._relayout()
            self.changed()

    def remove(self, widget):
        if not widget.is_empty() and not messagebox.askyesno("Delete box", "Delete this message box?"):
            return
        self.widgets.remove(widget)
        widget.destroy()
        self._relayout()
        self.changed()

    def _relayout(self):
        """Re-packs widgets in list order and renumbers boxes 1, 2, 3…"""
        number = 0
        for w in self.widgets:
            w.pack_forget()
        for w in self.widgets:
            if isinstance(w, BoxWidget):
                number += 1
                w.configure(text=f"Message Box {number}")
            w.pack(fill="x", pady=(0, PAD), padx=(0, 4))

    def changed(self):
        if not self._loading:
            self.on_change()


# =============================================================================
# The main window
# =============================================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WinGo Alert")
        self.geometry("980x760")
        self.minsize(760, 560)
        if sys.platform.startswith("linux"):
            ttk.Style(self).theme_use("clam")  # nicer than Linux's default look

        self.q: "queue.Queue" = queue.Queue()   # messages from background threads
        self.watcher = None                      # running Watcher thread (or None)
        self.alerts_sent = 0
        self.sender = get_sender(lambda text: self.q.put(("log", text)))
        settings = storage.load_settings()

        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True, padx=PAD, pady=PAD)
        self.home_tab = ttk.Frame(tabs, padding=PAD)
        self.alert_tab = ttk.Frame(tabs, padding=PAD)
        self.broadcast_tab = ttk.Frame(tabs, padding=PAD)
        self.settings_tab = ttk.Frame(tabs, padding=PAD)
        tabs.add(self.home_tab, text="  Home  ")
        tabs.add(self.alert_tab, text="  Alert Page  ")
        tabs.add(self.broadcast_tab, text="  Broadcast  ")
        tabs.add(self.settings_tab, text="  ⚙  ")

        self._build_home()
        self._build_alert(settings)
        self._build_broadcast(settings)
        self._build_settings(settings)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._drain_queue)
        self.log("App opened. Fill in ⚙ Settings, write your Alert Page, then press ▶ Start on Home.")
        if settings.get("auto_start") and settings.get("token") and settings.get("channel"):
            self.after(500, self.start_watching)

    # -------------------------------------------------------------------------
    # Home tab
    # -------------------------------------------------------------------------
    def _build_home(self):
        top = ttk.Frame(self.home_tab)
        top.pack(fill="x")
        self.start_btn = ttk.Button(top, text="▶ Start", width=12, command=self.toggle_watching)
        self.start_btn.pack(side="left")
        self.status_label = ttk.Label(top, text="● Stopped", foreground="#d33", font=("", 13, "bold"))
        self.status_label.pack(side="left", padx=PAD)
        ttk.Button(top, text="Send test alert", command=self.send_test_alert).pack(side="right")

        info = ttk.LabelFrame(self.home_tab, text="Live", padding=PAD)
        info.pack(fill="x", pady=PAD)
        ttk.Label(info, text="Last results (newest first):").grid(row=0, column=0, sticky="w")
        self.results_row = ttk.Frame(info)
        self.results_row.grid(row=0, column=1, sticky="w", padx=PAD)
        self.result_labels = []
        for _ in range(10):
            lbl = tk.Label(self.results_row, text=" ", width=3, relief="groove", font=("", 12, "bold"))
            lbl.pack(side="left", padx=1)
            self.result_labels.append(lbl)
        ttk.Label(info, text="Current streak:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.streak_label = ttk.Label(info, text="—", font=("", 13, "bold"))
        self.streak_label.grid(row=1, column=1, sticky="w", padx=PAD, pady=(6, 0))
        ttk.Label(info, text="Alerts sent:").grid(row=2, column=0, sticky="w")
        self.count_label = ttk.Label(info, text="0")
        self.count_label.grid(row=2, column=1, sticky="w", padx=PAD)
        ttk.Label(info, text="Last check:").grid(row=3, column=0, sticky="w")
        self.check_label = ttk.Label(info, text="—", foreground="gray")
        self.check_label.grid(row=3, column=1, sticky="w", padx=PAD)

        ttk.Label(self.home_tab, text="Log", font=("", 12, "bold")).pack(anchor="w")
        self.log_box = ScrolledText(self.home_tab, height=14, state="disabled", wrap="word")
        self.log_box.pack(fill="both", expand=True)

    def log(self, text):
        """Adds a time-stamped line to the log (main thread only)."""
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{datetime.now():%H:%M:%S}] {text}\n")
        # Keep the log from growing forever
        if int(self.log_box.index("end-1c").split(".")[0]) > 2000:
            self.log_box.delete("1.0", "500.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def show_results(self, history, size, count):
        for lbl, r in zip(self.result_labels, history + [None] * 10):
            if r is None:
                lbl.configure(text=" ", bg="#ddd")
                continue
            big = r["size"] == "Big"
            lbl.configure(text=f"{r['number']}{'B' if big else 'S'}",
                          bg="#f4b183" if big else "#9dc3e6", fg="#000")
        self.streak_label.configure(text=f"{count}x {size}" if count else "—",
                                    foreground="#c55a11" if size == "Big" else "#2f5597")
        self.check_label.configure(text=datetime.now().strftime("%H:%M:%S"))

    # ---- start / stop ----
    def toggle_watching(self):
        if self.watcher:
            self.stop_watching()
        else:
            self.start_watching()

    def start_watching(self):
        settings = storage.load_settings()
        if not settings.get("token") or not settings.get("channel"):
            messagebox.showwarning("Settings missing", "Add the bot token and channel in ⚙ first, then Save.")
            return
        if not storage.parse_alert_at(settings.get("alert_at", "")):
            messagebox.showwarning("Alert at", "Set ⚙ 'Alert at' to numbers like 3,4.")
            return
        self.watcher = Watcher(
            get_settings=storage.load_settings,   # re-read each poll -> saved changes apply live
            on_alert=lambda info: self.q.put(("alert", info)),
            on_update=lambda h, s, c: self.q.put(("update", (h, s, c))),
            on_log=lambda text: self.q.put(("log", text)),
        )
        self.watcher.start()
        self.start_btn.configure(text="■ Stop")
        self.status_label.configure(text=f"● Watching {settings['game']}  (alert at {settings['alert_at']})",
                                    foreground="#1a8f4e")

    def stop_watching(self):
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
        self.start_btn.configure(text="▶ Start")
        self.status_label.configure(text="● Stopped", foreground="#d33")
        self.log("Stopped watching.")

    def fire_alert(self, info):
        """A streak hit: send the SAVED alert page, one by one."""
        settings = storage.load_settings()
        self.alerts_sent += 1
        self.count_label.configure(text=str(self.alerts_sent))
        self.log(f"🚨 {info['count']}x {info['SIZE']} on {info['game']} → sending alert messages")
        self.sender.add_job(f"Alert {info['count']}x {info['SIZE']}", settings["alert_blocks"], info, settings)

    def send_test_alert(self):
        if self.alert_dirty and not messagebox.askyesno(
                "Unsaved changes", "The Alert Page has unsaved changes.\nThe test sends the SAVED version. Continue?"):
            return
        settings = storage.load_settings()
        self.sender.add_job("Test alert", settings["alert_blocks"], sample_info(settings["game"]), settings)

    # -------------------------------------------------------------------------
    # Alert Page tab
    # -------------------------------------------------------------------------
    def _build_alert(self, settings):
        ttk.Label(self.alert_tab, text="Sent to the channel when a streak hits — top to bottom, one by one",
                  font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(self.alert_tab, text="Each box: type text (HTML ok: <b>bold</b> <i>italic</i>), add a file, "
                                       f"and/or paste a message link. Placeholders work in boxes too: {PLACEHOLDERS}",
                  foreground="gray", wraplength=900, justify="left").pack(anchor="w", pady=(0, 4))

        self.alert_dirty = False
        self.alert_editor = BlocksEditor(self.alert_tab, on_change=lambda: self._set_dirty("alert", True))
        self.alert_editor.pack(fill="both", expand=True)
        self.alert_editor.load(settings["alert_blocks"], settings["program_template"])

        bar = ttk.Frame(self.alert_tab)
        bar.pack(fill="x", pady=(PAD, 0))
        ttk.Button(bar, text="Add more +", command=self.alert_editor.add_box).pack(side="left")
        ttk.Button(bar, text="💾 Save", command=self.save_alert).pack(side="left", padx=PAD)
        self.alert_dirty_label = ttk.Label(bar, text="", foreground="#c55a11")
        self.alert_dirty_label.pack(side="left")
        ttk.Button(bar, text="Send test alert", command=self.send_test_alert).pack(side="right")

    def save_alert(self):
        settings = storage.load_settings()
        settings["alert_blocks"] = self.alert_editor.get_blocks()
        settings["program_template"] = self.alert_editor.program.get_template()
        storage.save_settings(settings)
        self._set_dirty("alert", False)
        self.log("💾 Alert Page saved.")

    # -------------------------------------------------------------------------
    # Broadcast tab
    # -------------------------------------------------------------------------
    def _build_broadcast(self, settings):
        ttk.Label(self.broadcast_tab, text="Send these to the channel now — top to bottom, one by one",
                  font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(self.broadcast_tab, text="Same boxes as the Alert Page, without the program message.",
                  foreground="gray").pack(anchor="w", pady=(0, 4))

        self.broadcast_dirty = False
        self.broadcast_editor = BlocksEditor(self.broadcast_tab,
                                             on_change=lambda: self._set_dirty("broadcast", True))
        self.broadcast_editor.pack(fill="both", expand=True)
        self.broadcast_editor.load([b for b in settings["broadcast_blocks"] if b.get("type") != "program"])

        bar = ttk.Frame(self.broadcast_tab)
        bar.pack(fill="x", pady=(PAD, 0))
        ttk.Button(bar, text="Add more +", command=self.broadcast_editor.add_box).pack(side="left")
        ttk.Button(bar, text="💾 Save", command=self.save_broadcast).pack(side="left", padx=PAD)
        self.broadcast_dirty_label = ttk.Label(bar, text="", foreground="#c55a11")
        self.broadcast_dirty_label.pack(side="left")
        ttk.Button(bar, text="📤 Send now", command=self.send_broadcast).pack(side="right")

    def save_broadcast(self):
        settings = storage.load_settings()
        settings["broadcast_blocks"] = self.broadcast_editor.get_blocks()
        storage.save_settings(settings)
        self._set_dirty("broadcast", False)
        self.log("💾 Broadcast saved.")

    def send_broadcast(self):
        blocks = [b for b in self.broadcast_editor.get_blocks()
                  if b["text"].strip() or b["file"] or b["link"]]
        if not blocks:
            messagebox.showinfo("Nothing to send", "Type a message, add a file or paste a link first.")
            return
        settings = storage.load_settings()
        if not messagebox.askyesno("Send now", f"Send {len(blocks)} box(es) to {settings.get('channel') or '?'} now?"):
            return
        # Sends what is on screen right now (saved or not)
        self.sender.add_job("Broadcast", blocks, sample_info(settings["game"]), settings)

    def _set_dirty(self, which, dirty):
        setattr(self, f"{which}_dirty", dirty)
        label = getattr(self, f"{which}_dirty_label", None)
        if label:
            label.configure(text="● unsaved changes" if dirty else "")

    # -------------------------------------------------------------------------
    # Settings tab
    # -------------------------------------------------------------------------
    def _build_settings(self, s):
        f = self.settings_tab
        self.token_var = tk.StringVar(value=s["token"])
        self.channel_var = tk.StringVar(value=s["channel"])
        self.game_var = tk.StringVar(value=s["game"])
        self.alert_at_var = tk.StringVar(value=s["alert_at"])
        self.poll_var = tk.StringVar(value=str(s["poll_seconds"]))
        self.delay_var = tk.StringVar(value=str(s["delay_seconds"]))
        self.auto_var = tk.BooleanVar(value=bool(s["auto_start"]))

        rows = [
            ("Bot token", "From @BotFather. The bot must be an ADMIN in your channel."),
            ("Channel", "@mychannel  or  -1001234567890"),
            ("Game", "Which WinGo game to watch"),
            ("Alert at", "Streak lengths that send the alert, e.g. 3,4  (3 = three Big or Small in a row)"),
            ("Check every (s)", "Seconds between WinGo checks (2 or more)"),
            ("Delay between messages (s)", "Pause between each message, keeps the order (1 is good)"),
        ]
        token_row = ttk.Frame(f)
        self.token_entry = ttk.Entry(token_row, textvariable=self.token_var, show="•", width=50)
        self.token_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(token_row, text="Show", width=6, command=self._toggle_token).pack(side="left", padx=4)
        widgets = [
            token_row,
            ttk.Entry(f, textvariable=self.channel_var, width=50),
            ttk.Combobox(f, textvariable=self.game_var, values=GAMES, state="readonly", width=20),
            ttk.Entry(f, textvariable=self.alert_at_var, width=20),
            ttk.Spinbox(f, from_=2, to=60, textvariable=self.poll_var, width=8),
            ttk.Spinbox(f, from_=0, to=30, increment=0.5, textvariable=self.delay_var, width=8),
        ]
        for i, ((label, hint), widget) in enumerate(zip(rows, widgets)):
            ttk.Label(f, text=label, font=("", 11, "bold")).grid(row=i * 2, column=0, sticky="w", pady=(PAD, 0))
            widget.grid(row=i * 2, column=1, sticky="w" if i > 1 else "ew", pady=(PAD, 0))
            ttk.Label(f, text=hint, foreground="gray").grid(row=i * 2 + 1, column=1, sticky="w")
        ttk.Checkbutton(f, text="Start watching automatically when the app opens", variable=self.auto_var) \
            .grid(row=20, column=1, sticky="w", pady=PAD)

        bar = ttk.Frame(f)
        bar.grid(row=21, column=1, sticky="w", pady=PAD)
        ttk.Button(bar, text="💾 Save", command=self.save_settings).pack(side="left")
        ttk.Button(bar, text="Check bot + channel", command=self.check_bot).pack(side="left", padx=PAD)
        self.check_result = ttk.Label(f, text="", wraplength=600, justify="left")
        self.check_result.grid(row=22, column=1, sticky="w")
        f.columnconfigure(1, weight=1)

    def _toggle_token(self):
        self.token_entry.configure(show="" if self.token_entry.cget("show") else "•")

    def save_settings(self):
        try:
            poll = max(2, int(float(self.poll_var.get())))
            delay = max(0.0, float(self.delay_var.get()))
        except ValueError:
            messagebox.showwarning("Settings", "'Check every' and 'Delay' must be numbers.")
            return
        alert_at = sorted(storage.parse_alert_at(self.alert_at_var.get()))
        if not alert_at:
            messagebox.showwarning("Settings", "'Alert at' needs numbers of 2 or more, e.g. 3,4")
            return
        settings = storage.load_settings()
        settings.update({
            "token": self.token_var.get().strip(),
            "channel": self.channel_var.get().strip(),
            "game": self.game_var.get(),
            "alert_at": ",".join(map(str, alert_at)),
            "poll_seconds": poll,
            "delay_seconds": delay,
            "auto_start": self.auto_var.get(),
        })
        storage.save_settings(settings)
        self.alert_at_var.set(settings["alert_at"])
        self.log("💾 Settings saved." + (" (Watcher picks up the new game/alert numbers on its next check.)"
                                         if self.watcher else ""))
        if self.watcher:
            self.status_label.configure(text=f"● Watching {settings['game']}  (alert at {settings['alert_at']})")

    def check_bot(self):
        """Checks the token and that the bot can see the channel (in a thread)."""
        token, channel = self.token_var.get().strip(), self.channel_var.get().strip()
        self.check_result.configure(text="Checking…", foreground="gray")

        def work():
            try:
                bot = TelegramBot(token)
                me = bot.get_me()
                text = f"✔ Bot @{me.get('username')} works."
                if channel:
                    chat = bot.get_chat(channel)
                    text += f"\n✔ Channel found: {chat.get('title', channel)}"
                self.q.put(("check", (text, "#1a8f4e")))
            except TelegramError as e:
                self.q.put(("check", (f"✖ {e}", "#d33")))
        import threading
        threading.Thread(target=work, daemon=True).start()

    # -------------------------------------------------------------------------
    # Background -> window messages
    # -------------------------------------------------------------------------
    def _drain_queue(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.log(data)
                elif kind == "update":
                    self.show_results(*data)
                elif kind == "alert":
                    if self.watcher:      # ignore alerts that arrive right after Stop
                        self.fire_alert(data)
                elif kind == "check":
                    self.check_result.configure(text=data[0], foreground=data[1])
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def on_close(self):
        if (self.alert_dirty or self.broadcast_dirty) and not messagebox.askyesno(
                "Unsaved changes", "You have unsaved message boxes. Close anyway?"):
            return
        if self.watcher:
            self.watcher.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
