"""
friday_bar.py — Spotlight-style popup bar.

Press Win+Shift+K anywhere in Windows to summon a floating input box.
Type a task, hit Enter, watch it work. Esc hides it again.

Run it with pythonw so no console window hangs around:
    pythonw friday_bar.py

Why this file is short: all the thinking lives in agent.py. This is only a
face. The agent never knew what a terminal was, so it doesn't need changing
to grow a GUI.
"""

import queue
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont

try:
    import keyboard  # global hotkey capture
except ImportError:
    sys.exit("Missing dependency. Run:  pip install keyboard")

import agent
import memory

HOTKEY = "windows+shift+k"

# Dark, low-contrast, deliberately unobtrusive.
BG, FG, DIM, ACCENT = "#16181d", "#e8eaed", "#7b818c", "#5eb0ef"
WARN, ERR = "#e0a458", "#e06c75"
WIDTH = 720


class FridayBar:
    def __init__(self) -> None:
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.pending_confirm: dict | None = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("FRIDAY")
        self.root.overrideredirect(True)          # no title bar
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)

        mono = tkfont.Font(family="Consolas", size=11)
        entry_font = tkfont.Font(family="Segoe UI", size=15)

        outer = tk.Frame(self.root, bg=BG, highlightbackground="#2b2f36",
                         highlightthickness=1)
        outer.pack(fill="both", expand=True)

        row = tk.Frame(outer, bg=BG)
        row.pack(fill="x", padx=16, pady=(14, 10))

        self.prompt_label = tk.Label(row, text="▸", bg=BG, fg=ACCENT, font=entry_font)
        self.prompt_label.pack(side="left", padx=(0, 10))

        self.entry = tk.Entry(row, bg=BG, fg=FG, font=entry_font, relief="flat",
                              insertbackground=ACCENT, highlightthickness=0,
                              borderwidth=0)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", self.on_submit)
        self.entry.bind("<Escape>", lambda e: self.hide())

        # transcript area, hidden until there's something to show
        self.output_frame = tk.Frame(outer, bg=BG)
        self.output = tk.Text(self.output_frame, bg="#101216", fg=DIM, font=mono,
                              relief="flat", height=12, wrap="word",
                              highlightthickness=0, borderwidth=0,
                              padx=14, pady=10)
        self.output.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.output.configure(state="disabled")

        for tag, colour in (("tool", DIM), ("result", DIM), ("reply", FG),
                            ("warn", WARN), ("error", ERR), ("you", ACCENT)):
            self.output.tag_configure(tag, foreground=colour)

        self.hint = tk.Label(outer, text="", bg=BG, fg=DIM,
                             font=tkfont.Font(family="Segoe UI", size=9), anchor="w")
        self.hint.pack(fill="x", padx=18, pady=(0, 8))

        self.friday = agent.Friday(confirm_fn=self.confirm, on_event=self.emit)
        self.root.after(80, self.drain_events)

    # ------------------------------------------------------------ window

    def place(self) -> None:
        """Centre horizontally, sit a third of the way down — Spotlight position."""
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        h = self.root.winfo_height()
        self.root.geometry(f"{WIDTH}x{h}+{(sw - WIDTH) // 2}+{sh // 4}")

    def show(self) -> None:
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.place()
        self.entry.focus_force()
        self.root.focus_force()

    def hide(self) -> None:
        self.root.withdraw()

    def toggle(self) -> None:
        if self.root.state() == "withdrawn":
            self.show()
        else:
            self.hide()

    # ------------------------------------------------------------ output

    def write(self, text: str, tag: str = "result") -> None:
        if not self.output_frame.winfo_ismapped():
            self.output_frame.pack(fill="both", expand=True)
        self.output.configure(state="normal")
        self.output.insert("end", text + "\n", tag)
        self.output.see("end")
        self.output.configure(state="disabled")
        self.place()

    def emit(self, kind: str, text: str) -> None:
        """Called from the worker thread — never touch tkinter from here."""
        self.events.put((kind, text))

    def drain_events(self) -> None:
        """Runs on the UI thread; safe to draw."""
        while True:
            try:
                kind, text = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "tool":
                self.write(f"  ⚙ {text}", "tool")
            elif kind == "result":
                self.write(f"    → {text}", "result")
            elif kind == "reply":
                self.write(f"\n{text}\n", "reply")
            elif kind == "denied":
                self.write(f"  ✗ denied: {text}", "warn")
            elif kind == "warn":
                self.write(f"  {text}", "warn")
            elif kind == "error":
                self.write(f"  {text}", "error")
            elif kind == "done":
                self.busy = False
                self.prompt_label.configure(text="▸", fg=ACCENT)
                self.hint.configure(text="Esc to hide  ·  'reset' clears memory")
        self.root.after(80, self.drain_events)

    # ------------------------------------------------------------ confirm

    def confirm(self, tool_name: str, args: dict) -> bool:
        """
        Called from the worker thread. tkinter dialogs must run on the UI
        thread, so we hand the question over and block until it's answered.
        """
        answer: queue.Queue = queue.Queue()

        def ask() -> None:
            win = tk.Toplevel(self.root)
            win.title("Confirm")
            win.configure(bg=BG)
            win.attributes("-topmost", True)
            win.overrideredirect(True)
            body = "\n".join(f"{k}: {str(v)[:200]}" for k, v in args.items())
            tk.Label(win, text=f"Allow {tool_name}?", bg=BG, fg=WARN,
                     font=("Segoe UI", 12, "bold")).pack(padx=20, pady=(18, 6))
            tk.Label(win, text=body, bg=BG, fg=FG, font=("Consolas", 10),
                     justify="left", wraplength=460).pack(padx=20, pady=(0, 14))
            btns = tk.Frame(win, bg=BG)
            btns.pack(pady=(0, 18))

            def reply(ok: bool) -> None:
                answer.put(ok)
                win.destroy()

            tk.Button(btns, text="Allow", command=lambda: reply(True),
                      bg="#2d6a4f", fg=FG, relief="flat", width=12,
                      padx=8, pady=4).pack(side="left", padx=6)
            tk.Button(btns, text="Deny", command=lambda: reply(False),
                      bg="#3a3f47", fg=FG, relief="flat", width=12,
                      padx=8, pady=4).pack(side="left", padx=6)
            win.protocol("WM_DELETE_WINDOW", lambda: reply(False))
            win.update_idletasks()
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            w, h = win.winfo_width(), win.winfo_height()
            win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")
            win.grab_set()

        self.root.after(0, ask)
        return answer.get()          # blocks the worker, not the UI

    # ------------------------------------------------------------ submit

    def on_submit(self, _event=None) -> None:
        text = self.entry.get().strip()
        if not text or self.busy:
            return
        self.entry.delete(0, "end")
        low = text.lower()

        if low in {"exit", "quit"}:
            self.quit()
            return
        if low == "reset":
            self.friday.reset()
            self.clear()
            self.write("memory cleared", "warn")
            return
        if low == "clear":
            self.clear()
            return
        if low in {"memory", "/memory"}:
            self.write(memory.stats(), "warn")
            self.write(memory.recall(), "result")
            return
        if low == "tokens":
            self.write(f"~{self.friday.estimate_tokens():,} tokens per call "
                       f"(mode={self.friday.mode})", "warn")
            return
        if low.startswith("/mode"):
            parts = text.split()
            if len(parts) > 1:
                try:
                    self.write(self.friday.set_mode(parts[1]), "warn")
                except KeyError:
                    self.write(f"unknown mode '{parts[1]}'", "error")
            else:
                self.write(f"current mode: {self.friday.mode}", "warn")
            return

        self.write(f"▸ {text}", "you")
        self.busy = True
        self.prompt_label.configure(text="◌", fg=WARN)
        self.hint.configure(text="working…")
        threading.Thread(target=self.work, args=(text,), daemon=True).start()

    def work(self, text: str) -> None:
        try:
            self.friday.ask(text)
        except Exception as e:
            self.emit("error", f"{type(e).__name__}: {e}")
        finally:
            self.emit("done", "")

    def clear(self) -> None:
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")
        self.output_frame.pack_forget()
        self.place()

    # ------------------------------------------------------------ lifecycle

    def quit(self) -> None:
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        self.root.quit()
        self.root.destroy()

    def run(self) -> None:
        # The hotkey fires on keyboard's own thread — bounce it to the UI thread.
        keyboard.add_hotkey(HOTKEY, lambda: self.root.after(0, self.toggle))
        self.show()
        self.write(f"FRIDAY ready · {agent.MODEL} · mode {self.friday.mode}", "warn")
        self.write(f"Press {HOTKEY.replace('+', ' + ')} anytime to summon or hide.", "warn")
        self.root.mainloop()


if __name__ == "__main__":
    try:
        FridayBar().run()
    except RuntimeError as e:
        # no API key, most likely — show it rather than dying silently
        import tkinter.messagebox as mb
        r = tk.Tk(); r.withdraw()
        mb.showerror("FRIDAY", str(e))
