"""Always-on-top sticky note listing GitHub PRs and issues, grouped in panels.

Panels and items are added by hand (paste a URL or `owner/repo#123`); each
item's title, state, diff size and CI status are read with `gh api graphql`
every few minutes. On Windows run it with `pythonw` so no console stays open.
State (panels, items, window position) is kept in STATE_FILE, or in the file
given with `--state`.
"""
import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import font as tkfont

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"

if WINDOWS:
    CONFIG_DIR = os.environ.get("APPDATA", os.path.expanduser("~"))
elif MACOS:
    CONFIG_DIR = os.path.expanduser("~/Library/Application Support")
else:
    CONFIG_DIR = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
# Default state file; `--state <path>` replaces it.
STATE_FILE = os.path.join(CONFIG_DIR, "gh-sticky", "state.json")
# Tk 8.6 on macOS reports the right mouse button as Button-2.
RIGHT_CLICK = "<Button-2>" if MACOS else "<Button-3>"
REFRESH_MS = 5 * 60 * 1000

BG = "#1f1f1e"
CARD_BG = "#252523"
BG_HOVER = "#30302d"
MENU_BG = "#262624"
INPUT_BG = "#2a2a28"
BORDER = "#3a3a37"
ACCENT = "#4c8bf5"
FG = "#e8e6e3"
FG_DIM = "#9a9893"
GREEN = "#3fb950"
RED = "#f85149"
YELLOW = "#e3b341"

# Layout: panels are rounded cards PAD from the window edge, with CARD_PAD
# inside; in a card the arrow sits at the left and titles and item icons at
# INDENT.
PAD = 6
CARD_PAD = 5
CARD_RADIUS = 10
INDENT = 22

# Button glyphs from Windows' icon font (Segoe Fluent Icons / MDL2 Assets)...
ICON_FONT_GLYPHS = {
    "add": "\ue710",
    "edit": "\ue70f",
    "up": "\ue74a",
    "down": "\ue74b",
    "delete": "\ue711",
    "more": "\ue712",
    "refresh": "\ue72c",
    "expanded": "\ue70d",
    "collapsed": "\ue76c",
}
# ...and plain text where that font is missing (Linux, macOS).
TEXT_GLYPHS = {
    "add": "+",
    "edit": "✎",
    "up": "↑",
    "down": "↓",
    "delete": "✕",
    "more": "⋯",
    "refresh": "⟳",
    "expanded": "▾",
    "collapsed": "▸",
}

ITEM_RE = re.compile(
    r"(?:https?://github\.com/)?([\w.-]+)/([\w.-]+)(?:/(?:pull|issues)/|#)(\d+)"
)
# The last few comments/reviews are fetched to find the newest one written by
# someone else (not you, not a bot): that is what lights the "new" dot.
QUERY_ITEM = """
  i%d: repository(owner: %s, name: %s) {
    issueOrPullRequest(number: %d) {
      __typename
      ... on Issue {
        id title state stateReason url viewerSubscription viewerCanSubscribe
        comments(last: 5) { nodes { id author { __typename login } } }
      }
      ... on PullRequest {
        id title state isDraft additions deletions url reviewDecision
        viewerSubscription viewerCanSubscribe
        comments(last: 5) { nodes { id author { __typename login } } }
        reviews(last: 5) { nodes { id author { __typename login } } }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      }
    }
  }"""
SUBSCRIBE_MUTATION = """
mutation($id: ID!, $state: SubscriptionState!) {
  updateSubscription(input: {subscribableId: $id, state: $state}) {
    subscribable { viewerSubscription }
  }
}"""
NOTIFICATION_RE = re.compile(r"/repos/([^/]+)/([^/]+)/(?:pulls|issues)/(\d+)$")


def parse_items(text):
    """Return `owner/repo#number` keys for every GitHub reference in text."""
    return [f"{o}/{r}#{n}" for o, r, n in ITEM_RE.findall(text or "")]


def split_key(key):
    repo, number = key.rsplit("#", 1)
    return repo, int(number)


def gh(*args):
    return subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def utc_now():
    """Current time in GitHub's timestamp format, so the two compare as text."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def last_by_others(nodes, viewer):
    """Id of the newest comment/review not written by you or a bot."""
    for node in reversed(nodes):
        author = node.get("author") or {}
        if author.get("login") != viewer and author.get("__typename") != "Bot":
            return node["id"]
    return None


def fetch(keys):
    """Query GitHub for all keys at once, return {key: info}."""
    parts = []
    for index, key in enumerate(keys):
        repo, number = split_key(key)
        owner, name = repo.split("/", 1)
        parts.append(
            QUERY_ITEM % (index, json.dumps(owner), json.dumps(name), number)
        )
    proc = gh("api", "graphql", "-f", f"query=query {{ viewer {{ login }}{''.join(parts)}}}")
    try:
        data = json.loads(proc.stdout).get("data") or {}
    except ValueError:
        raise RuntimeError(proc.stderr.strip() or "gh failed") from None
    viewer = (data.get("viewer") or {}).get("login")
    notified = fetch_notifications()

    result = {}
    for index, key in enumerate(keys):
        node = (data.get(f"i{index}") or {}).get("issueOrPullRequest")
        if not node:
            result[key] = {"error": "not found"}
            continue
        info = {
            "kind": "pr" if node["__typename"] == "PullRequest" else "issue",
            "id": node["id"],
            "title": node["title"],
            "state": node["state"],
            "url": node["url"],
            "subscription": node["viewerSubscription"],
            "can_subscribe": node["viewerCanSubscribe"],
            "notified_at": notified.get(key),
        }
        # Anything changing here since you last looked lights the dot.
        activity = [
            node["state"],
            node.get("reviewDecision"),
            last_by_others(node["comments"]["nodes"], viewer),
        ]
        if info["kind"] == "pr":
            commits = node["commits"]["nodes"]
            rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
            info.update(
                draft=node["isDraft"],
                additions=node["additions"],
                deletions=node["deletions"],
                checks=rollup["state"] if rollup else None,
                review=node["reviewDecision"],
            )
            activity.append(last_by_others(node["reviews"]["nodes"], viewer))
        else:
            info["reason"] = node["stateReason"]
        info["activity"] = activity
        result[key] = info
    return result


def fetch_notifications():
    """Return {key: updated_at} for your unread GitHub notifications.

    Best effort: fine-grained tokens can't read notifications, so any failure
    just means no notification-based dots.
    """
    proc = gh("api", "notifications?per_page=100")
    try:
        threads = json.loads(proc.stdout)
    except ValueError:
        return {}
    if not isinstance(threads, list):
        return {}
    notified = {}
    for thread in threads:
        match = NOTIFICATION_RE.search((thread.get("subject") or {}).get("url") or "")
        if match and thread.get("unread"):
            owner, name, number = match.groups()
            notified[f"{owner}/{name}#{number}"] = thread["updated_at"]
    return notified


def update_subscription(node_id, subscribe):
    state = "SUBSCRIBED" if subscribe else "UNSUBSCRIBED"
    proc = gh(
        "api", "graphql",
        "-f", f"query={SUBSCRIBE_MUTATION}",
        "-f", f"id={node_id}",
        "-f", f"state={state}",
    )  # fmt: skip
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        data = None
    if proc.returncode or not data or data.get("errors"):
        errors = (data or {}).get("errors") or [{"message": proc.stderr.strip()}]
        raise RuntimeError(errors[0].get("message") or "gh failed")


def icon_name(info):
    """Pick the item icon like Claude does: CI result for open PRs, else state."""
    state = info.get("state")
    if info.get("kind") == "issue":
        if state == "OPEN":
            return "issue-open"
        if info.get("reason") == "COMPLETED":
            return "issue-done"
        return "issue-not-planned"
    if info.get("kind") == "pr":
        if state == "MERGED":
            return "pr-merged"
        if state == "CLOSED":
            return "pr-closed"
        if info.get("draft"):
            return "pr-draft"
        return {
            "SUCCESS": "ci-success",
            "FAILURE": "ci-failure",
            "ERROR": "ci-failure",
            "PENDING": "ci-pending",
            "EXPECTED": "ci-pending",
        }.get(info.get("checks"), "pr-open")
    return "unknown"


def elide(text, font, width):
    if font.measure(text) <= width:
        return text
    while text and font.measure(text + "…") > width:
        text = text[:-1]
    return text + "…"


def rounded_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]  # fmt: skip
    return canvas.create_polygon(points, smooth=True, **kwargs)


def monitor_area(x, y):
    """Work area (left, top, right, bottom) of the monitor holding the point.

    Tk's winfo_screenwidth only knows the primary monitor, so ask Windows.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        # 2 = MONITOR_DEFAULTTONEAREST
        monitor = user32.MonitorFromPoint(wintypes.POINT(x, y), 2)
        info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
        user32.GetMonitorInfoW(monitor, ctypes.byref(info))
        work = info.rcWork
        return work.left, work.top, work.right, work.bottom
    except Exception:  # noqa: BLE001 - not on Windows
        return None


def is_inside(widget, container):
    path, root = str(widget), str(container)
    return path == root or path.startswith(root + ".")


class Popup:
    """Themed stand-in for tk.Menu: a borderless list closing on focus loss.

    Entries are `(label, command)`, `(label, command, "danger")` or None for
    a separator.
    """

    def __init__(self, app, event, entries):
        self.app = app
        self.top = top = tk.Toplevel(app.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=BORDER)
        frame = tk.Frame(top, bg=MENU_BG, padx=4, pady=4)
        frame.pack(padx=1, pady=1)
        for entry in entries:
            if entry is None:
                tk.Frame(frame, bg=BORDER, height=1).pack(fill="x", padx=4, pady=4)
                continue
            text, command, *flags = entry
            label = tk.Label(
                frame,
                text=text,
                bg=MENU_BG,
                fg=RED if "danger" in flags else FG,
                font=app.font,
                anchor="w",
                padx=10,
                pady=3,
            )
            label.pack(fill="x")
            label.bind("<Enter>", lambda e, w=label: w.configure(bg=BG_HOVER))
            label.bind("<Leave>", lambda e, w=label: w.configure(bg=MENU_BG))
            label.bind("<Button-1>", lambda e, c=command: self.choose(c))

        top.update_idletasks()
        width, height = top.winfo_reqwidth(), top.winfo_reqheight()
        x, y = event.x_root + 2, event.y_root + 2
        left, top_edge, right, bottom = monitor_area(x, y) or (
            0, 0, top.winfo_screenwidth(), top.winfo_screenheight()
        )
        # Open towards the inside when the menu would cross the monitor edge.
        if x + width > right:
            x = max(left, x - width - 4)
        if y + height > bottom:
            y = max(top_edge, y - height - 4)
        top.geometry(f"+{x}+{y}")
        top.bind("<FocusOut>", lambda e: self.close())
        top.bind("<Escape>", lambda e: self.close())
        top.focus_force()

    def choose(self, command):
        self.close()
        self.app.root.after_idle(command)

    def close(self):
        if self.top.winfo_exists():
            self.top.destroy()
        if self.app.popup is self:
            self.app.popup = None


class Tooltip:
    """Small themed hint shown after hovering a widget for a moment."""

    def __init__(self, app, widget, text):
        self.app, self.widget, self.text = app, widget, text
        self.job = self.top = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def schedule(self, _event=None):
        self.hide()
        self.job = self.widget.after(500, self.show)

    def show(self):
        if not self.widget.winfo_exists():
            return
        self.top = top = tk.Toplevel(self.widget)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=BORDER)
        tk.Label(
            top,
            text=self.text,
            bg=MENU_BG,
            fg=FG,
            font=self.app.font_small,
            justify="left",
            wraplength=260,
            padx=8,
            pady=4,
        ).pack(padx=1, pady=1)
        x, y = self.widget.winfo_pointerxy()
        top.geometry(f"+{x + 12}+{y + 16}")

    def hide(self, _event=None):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.top is not None:
            self.top.destroy()
            self.top = None


class StickyApp:
    def __init__(self, root):
        self.root = root
        self.state = self.load_state()
        self.results = queue.Queue()
        self.refreshing = False
        self.refresh_job = None
        self.render_job = None
        self.click_job = None
        self.popup = None
        # Inline edit in progress: {"panel", "field": "title"|"add", "var", ...}
        self.edit = None
        self.edit_entry = None

        families = tkfont.families()
        family = (
            "Segoe UI" if WINDOWS
            else tkfont.nametofont("TkDefaultFont").actual("family")
        )  # fmt: skip
        self.font = tkfont.Font(family=family, size=10)
        self.font_bold = tkfont.Font(family=family, size=10, weight="bold")
        self.font_small = tkfont.Font(family=family, size=9)
        icon_family = next(
            (f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in families),
            None,
        )
        self.glyphs = ICON_FONT_GLYPHS if icon_family else TEXT_GLYPHS
        self.font_icon = tkfont.Font(family=icon_family or family, size=9)
        self.font_icon_small = tkfont.Font(
            family=icon_family or family, size=7 if icon_family else 9
        )
        self.icons = {name: tk.PhotoImage(data=data) for name, data in ICONS.items()}

        root.title("GitHub Sticky")
        root.geometry(self.state.get("geometry", "320x480+80+80"))
        root.configure(bg=BG)
        root.attributes("-topmost", True)
        if WINDOWS:
            # Small title bar and no taskbar button.
            root.attributes("-toolwindow", True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.dark_title_bar()

        toolbar = tk.Frame(root, bg=BG)
        toolbar.pack(fill="x", padx=PAD, pady=(6, 0))
        self.flat_button(toolbar, "+ Panel", self.add_panel).pack(side="left")
        self.flat_button(toolbar, self.glyphs["refresh"], self.refresh, icon=True).pack(
            side="right"
        )
        self.status = tk.Label(
            toolbar, bg=BG, fg=FG_DIM, font=self.font_small, anchor="e"
        )
        self.status.pack(side="right", padx=6)

        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self.inner_id = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind("<Configure>", self.on_resize)
        root.bind_all("<MouseWheel>", self.on_wheel)
        # X11 reports the wheel as buttons 4 and 5.
        root.bind_all("<Button-4>", self.on_wheel)
        root.bind_all("<Button-5>", self.on_wheel)
        # Labels don't take focus, so the entry gets no FocusOut when they are
        # clicked; end inline edits on any click instead. This "all" binding
        # runs after the clicked widget's own one, and is skipped when that
        # destroys the widget, so widget click actions go through later().
        root.bind_all("<Button-1>", self.click_anywhere, add="+")

        self.render()
        self.refresh()
        self.root.after(200, self.poll_results)

    # --- state ---------------------------------------------------------------
    @staticmethod
    def load_state():
        try:
            with open(STATE_FILE, encoding="utf-8") as stream:
                return json.load(stream)
        except (OSError, ValueError):
            return {"panels": [{"title": "My PRs", "items": []}], "cache": {}}

    def save_state(self):
        self.state["geometry"] = self.root.geometry()
        keys = {k for p in self.state["panels"] for k in p["items"]}
        for section in ("cache", "seen"):
            values = self.state.setdefault(section, {})
            for key in list(values):
                if key not in keys:
                    del values[key]
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as stream:
            json.dump(self.state, stream, indent=2)

    def close(self):
        self.save_state()
        self.root.destroy()

    def changed(self):
        self.save_state()
        self.render(force=True)

    def later(self, command):
        """Return a click handler running command once the click is handled."""
        return lambda _event=None: self.root.after_idle(command)

    # --- refresh -------------------------------------------------------------
    def refresh(self):
        if self.refreshing:
            return
        keys = sorted({k for p in self.state["panels"] for k in p["items"]})
        if not keys:
            self.status.configure(text="")
            self.schedule_refresh()
            return
        self.refreshing = True
        self.status.configure(text="refreshing…")

        def worker():
            try:
                self.results.put(("ok", fetch(keys)))
            except Exception as exc:  # noqa: BLE001 - shown in the status label
                self.results.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def schedule_refresh(self):
        if self.refresh_job:
            self.root.after_cancel(self.refresh_job)
        self.refresh_job = self.root.after(REFRESH_MS, self.refresh)

    def poll_results(self):
        try:
            status, payload = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            if status == "subscription-error":
                self.subscription_failed(*payload)
            else:
                self.refreshing = False
                if status == "ok":
                    self.update_cache(payload)
                    self.status.configure(text="updated " + time.strftime("%H:%M"))
                    self.save_state()
                    self.render()
                else:
                    self.status.configure(text="error: " + payload.splitlines()[0][:40])
                self.schedule_refresh()
        self.root.after(200, self.poll_results)

    def update_cache(self, fetched):
        self.state.setdefault("cache", {}).update(fetched)
        seen = self.state.setdefault("seen", {})
        for key, info in fetched.items():
            # A newly added item starts as read; only later activity counts.
            if key not in seen and "activity" in info:
                seen[key] = {"activity": info["activity"], "at": utc_now()}

    # --- news dot and subscriptions ------------------------------------------
    def has_news(self, key):
        """True when others commented/reviewed or the state changed since you
        last opened the item, or GitHub has a newer unread notification."""
        info = self.state.get("cache", {}).get(key, {})
        seen = self.state.get("seen", {}).get(key)
        if not seen or "activity" not in info:
            return False
        if info["activity"] != seen["activity"]:
            return True
        notified_at = info.get("notified_at")
        return bool(notified_at and notified_at > seen["at"])

    def mark_seen(self, key):
        info = self.state.get("cache", {}).get(key, {})
        if "activity" in info:
            self.state.setdefault("seen", {})[key] = {
                "activity": info["activity"],
                "at": utc_now(),
            }
            self.changed()

    def set_subscription(self, key, subscribe):
        """Flip the bell right away; put it back if GitHub refuses."""
        info = self.state["cache"][key]
        old = info.get("subscription")
        info["subscription"] = "SUBSCRIBED" if subscribe else "UNSUBSCRIBED"
        self.changed()

        def worker():
            try:
                update_subscription(info["id"], subscribe)
            except Exception as exc:  # noqa: BLE001 - shown in the status label
                self.results.put(("subscription-error", (key, old, str(exc))))

        threading.Thread(target=worker, daemon=True).start()

    def subscription_failed(self, key, old, message):
        info = self.state.get("cache", {}).get(key)
        if info:
            info["subscription"] = old
            self.changed()
        if "notifications" in message and "scope" in message:
            message = "run: gh auth refresh -s notifications"
        self.status.configure(text="bell: " + message.splitlines()[0][:45])

    # --- panels and items ----------------------------------------------------
    def add_panel(self):
        panel = {"title": "", "items": []}
        self.state["panels"].append(panel)
        self.start_edit(panel, "title", new=True)
        self.canvas.yview_moveto(1)

    def delete_panel(self, panel):
        self.state["panels"].remove(panel)
        self.changed()

    def move(self, items, item, offset):
        index = items.index(item)
        new_index = index + offset
        if 0 <= new_index < len(items):
            items.insert(new_index, items.pop(index))
            self.changed()

    def move_item(self, key, source, target):
        source["items"].remove(key)
        if key not in target["items"]:
            target["items"].append(key)
        self.changed()

    def remove_item(self, panel, key):
        panel["items"].remove(key)
        self.changed()

    def toggle(self, panel):
        self.click_job = None
        panel["collapsed"] = not panel.get("collapsed", False)
        self.changed()

    def open_item(self, key):
        info = self.state.get("cache", {}).get(key, {})
        repo, number = split_key(key)
        webbrowser.open(info.get("url") or f"https://github.com/{repo}/issues/{number}")
        self.mark_seen(key)

    def copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    # --- inline editing ------------------------------------------------------
    def start_edit(self, panel, field, new=False):
        """Show an inline entry: the panel title, or the add-items box."""
        self.end_edit()
        if field == "title":
            text = panel["title"]
        else:
            try:
                text = " ".join(parse_items(self.root.clipboard_get()))
            except tk.TclError:
                text = ""
            panel["collapsed"] = False
        self.edit = {
            "panel": panel,
            "field": field,
            "var": tk.StringVar(value=text),
            "new": new,
            "select": True,
        }
        self.render(force=True)

    def end_edit(self):
        """Close the inline edit without rendering, keeping a typed title."""
        edit, self.edit = self.edit, None
        if not edit or edit["field"] != "title":
            return
        text = edit["var"].get().strip()
        if text:
            edit["panel"]["title"] = text
        elif edit["new"]:
            self.state["panels"].remove(edit["panel"])
        self.save_state()

    def click_anywhere(self, event):
        # Click actions run via later(), so this sees the edit open before
        # the click, not one the click itself starts.
        if not self.edit:
            return
        if event.widget is self.edit_entry:
            return
        if self.edit["field"] == "title":
            self.submit_edit()
        elif not self.edit["var"].get().strip():
            self.cancel_edit()

    def make_entry(self, parent, font):
        entry = tk.Entry(
            parent,
            textvariable=self.edit["var"],
            font=font,
            bg=INPUT_BG,
            fg=FG,
            insertbackground=FG,
            selectbackground=ACCENT,
            selectforeground=FG,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
        )
        entry.bind("<Return>", lambda e: self.submit_edit())
        entry.bind("<Escape>", lambda e: self.cancel_edit())
        entry.bind(
            "<FocusOut>", lambda e: self.root.after(1, lambda: self.focus_left(entry))
        )
        self.edit_entry = entry
        return entry

    def focus_left(self, entry):
        if entry is not self.edit_entry or not self.edit:
            return
        try:
            focus = self.root.focus_get()
        except KeyError:
            focus = None
        if focus is entry:
            return
        if self.edit["field"] == "title":
            self.submit_edit()
        elif not self.edit["var"].get().strip():
            self.cancel_edit()

    def submit_edit(self):
        edit = self.edit
        panel = edit["panel"]
        text = edit["var"].get().strip()
        if edit["field"] == "title":
            self.end_edit()
            self.render(force=True)
            return

        if not text:
            self.cancel_edit()
            return
        keys = parse_items(text)
        if not keys:
            self.edit_entry.configure(highlightbackground=RED, highlightcolor=RED)
            return
        panel["items"].extend(k for k in dict.fromkeys(keys) if k not in panel["items"])
        edit["var"].set("")
        self.changed()
        self.refresh()

    def cancel_edit(self):
        edit, self.edit = self.edit, None
        if edit and edit["new"]:
            self.state["panels"].remove(edit["panel"])
        self.changed()

    # --- drawing -------------------------------------------------------------
    def flat_button(self, parent, text, command, fg=FG_DIM, bg=BG, icon=False):
        label = tk.Label(
            parent,
            text=text,
            bg=bg,
            fg=fg,
            font=self.font_icon if icon else self.font,
            cursor="hand2",
            padx=5,
        )
        label.bind("<Button-1>", self.later(command))
        label.bind("<Enter>", lambda e: label.configure(fg=FG))
        label.bind("<Leave>", lambda e: label.configure(fg=fg))
        return label

    def show_popup(self, event, entries):
        if self.popup:
            self.popup.close()
        self.popup = Popup(self, event, entries)

    def hover(self, container, widgets, on_enter, on_leave):
        """Call on_enter/on_leave when the pointer enters/leaves the container.

        Leave events also fire when moving onto a child widget, so on leave
        check what is under the pointer before calling on_leave.
        """

        def check():
            if not container.winfo_exists():
                return
            under = self.root.winfo_containing(*self.root.winfo_pointerxy())
            if under is None or not is_inside(under, container):
                on_leave()

        for widget in widgets:
            widget.bind("<Enter>", lambda e: on_enter(), add="+")
            widget.bind("<Leave>", lambda e: self.root.after(10, check), add="+")

    def on_resize(self, event):
        self.canvas.itemconfigure(self.inner_id, width=event.width)
        if self.render_job:
            self.root.after_cancel(self.render_job)
        self.render_job = self.root.after(100, self.render)

    def on_wheel(self, event):
        if self.inner.winfo_height() <= self.canvas.winfo_height():
            return
        if event.num in (4, 5):
            step = -1 if event.num == 4 else 1
        elif MACOS:
            step = -event.delta
        else:
            step = int(-event.delta / 120)
        self.canvas.yview_scroll(step, "units")

    def render(self, force=False):
        # Rebuilding would drop the inline entry being typed in; wait for it.
        self.render_job = None
        if self.edit and not force:
            return
        self.edit_entry = None
        for child in self.inner.winfo_children():
            child.destroy()
        width = max(self.canvas.winfo_width(), 200) - 2 * (PAD + CARD_PAD)
        panels = self.state["panels"]
        for panel in panels:
            self.render_panel(panel, panels, width)

        if self.edit_entry:
            entry = self.edit_entry
            entry.focus_set()
            entry.icursor("end")
            if self.edit.pop("select", False):
                entry.select_range(0, "end")

    def make_card(self):
        """Add a rounded box like Claude's groups, return the frame inside it."""
        canvas = tk.Canvas(self.inner, bg=BG, highlightthickness=0, height=1)
        canvas.pack(fill="x", padx=PAD, pady=(PAD, 0))
        body = tk.Frame(canvas, bg=CARD_BG)
        window = canvas.create_window(CARD_PAD, CARD_PAD, window=body, anchor="nw")

        def redraw(_event):
            width = canvas.winfo_width()
            height = body.winfo_reqheight() + 2 * CARD_PAD
            canvas.configure(height=height)
            canvas.itemconfigure(window, width=max(width - 2 * CARD_PAD, 1))
            canvas.delete("card")
            rounded_rect(
                canvas, 0, 0, width - 1, height - 1, CARD_RADIUS,
                fill=CARD_BG, outline=BORDER, tags="card",
            )  # fmt: skip
            canvas.tag_lower("card")

        body.bind("<Configure>", redraw)
        canvas.bind("<Configure>", redraw)
        return body

    def render_panel(self, panel, panels, width):
        editing = self.edit if self.edit and self.edit["panel"] is panel else None
        collapsed = panel.get("collapsed", False)
        card = self.make_card()

        header = tk.Frame(card, bg=CARD_BG)
        header.pack(fill="x", pady=(1, 1))
        arrow = tk.Label(
            header,
            text=self.glyphs["collapsed" if collapsed else "expanded"],
            bg=CARD_BG,
            fg=FG_DIM,
            font=self.font_icon_small,
            cursor="hand2",
        )
        arrow.pack(side="left", padx=(4, 7))
        arrow.bind("<Button-1>", self.later(lambda: self.toggle(panel)))

        if editing and editing["field"] == "title":
            self.make_entry(header, self.font_bold).pack(
                side="left", fill="x", expand=True, padx=(0, 4)
            )
        else:
            self.render_title(header, arrow, panel, panels, width)

        if editing and editing["field"] == "add":
            self.render_add_box(card)
        if collapsed:
            return
        if not panel["items"] and not editing:
            hint = tk.Label(
                card,
                text="Empty. Hover the title and click + to add items.",
                bg=CARD_BG,
                fg=FG_DIM,
                font=self.font_small,
                anchor="w",
            )
            hint.pack(fill="x", padx=(INDENT, 4), pady=(0, 4))
        for key in panel["items"]:
            self.render_item(card, panel, panels, key, width)

    def render_title(self, header, arrow, panel, panels, width):
        """Panel title plus its actions, shown while hovering the header."""
        title = tk.Label(
            header,
            text=elide(panel["title"], self.font_bold, width - 130),
            bg=CARD_BG,
            fg=FG,
            font=self.font_bold,
            anchor="w",
            cursor="hand2",
        )
        title.pack(side="left")
        title.bind("<Button-1>", lambda e: self.click_title(panel))
        title.bind("<Double-Button-1>", self.later(lambda: self.rename(panel)))

        actions = tk.Frame(header, bg=CARD_BG)
        delete = self.flat_button(
            actions, self.glyphs["delete"], lambda: None, bg=CARD_BG, icon=True
        )
        delete.bind(
            "<Button-1>",
            self.later(
                lambda: self.confirm(delete, "Delete?", lambda: self.delete_panel(panel))
            ),
        )
        delete.pack(side="right")
        buttons = [
            ("down", lambda: self.move(panels, panel, 1)),
            ("up", lambda: self.move(panels, panel, -1)),
            ("edit", lambda: self.rename(panel)),
            ("add", lambda: self.start_edit(panel, "add")),
        ]
        for glyph, command in buttons:
            self.flat_button(
                actions, self.glyphs[glyph], command, bg=CARD_BG, icon=True
            ).pack(side="right")

        def show():
            actions.pack(side="right")

        def hide():
            actions.pack_forget()
            self.reset_confirm(delete)

        self.hover(header, [header, arrow, title], show, hide)
        menu = [
            ("Add items", lambda: self.start_edit(panel, "add")),
            ("Rename", lambda: self.rename(panel)),
            ("Move up", lambda: self.move(panels, panel, -1)),
            ("Move down", lambda: self.move(panels, panel, 1)),
            None,
            ("Delete panel", lambda: self.delete_panel(panel), "danger"),
        ]
        for widget in (header, arrow, title):
            widget.bind(RIGHT_CLICK, lambda e: self.show_popup(e, menu))

    def render_add_box(self, card):
        box = tk.Frame(card, bg=CARD_BG)
        box.pack(fill="x", padx=(INDENT, 4), pady=(2, 4))
        self.make_entry(box, self.font).pack(fill="x", ipady=2)
        tk.Label(
            box,
            text="Paste PR/issue links · Enter adds · Esc closes",
            bg=CARD_BG,
            fg=FG_DIM,
            font=self.font_small,
            anchor="w",
        ).pack(fill="x")

    def click_title(self, panel):
        # Delay the toggle so a double click can rename instead.
        if self.click_job:
            self.root.after_cancel(self.click_job)
        self.click_job = self.root.after(250, lambda: self.toggle(panel))

    def rename(self, panel):
        if self.click_job:
            self.root.after_cancel(self.click_job)
            self.click_job = None
        self.start_edit(panel, "title")

    def confirm(self, button, question, action):
        """Two-click delete: the first click turns the ✕ into a red question."""
        if not button.winfo_exists():
            return
        if button.cget("text") == self.glyphs["delete"]:
            button.configure(text=question, font=self.font_small, fg=RED)
        else:
            action()

    def reset_confirm(self, button):
        button.configure(text=self.glyphs["delete"], font=self.font_icon, fg=FG_DIM)

    def render_item(self, card, panel, panels, key, width):
        info = self.state.get("cache", {}).get(key, {})
        repo, number = split_key(key)

        row = tk.Frame(card, bg=CARD_BG, cursor="hand2")
        row.pack(fill="x")
        icon = tk.Label(row, image=self.icons[icon_name(info)], bg=CARD_BG)
        icon.pack(side="left", anchor="n", padx=(INDENT, 10), pady=(6, 0))
        # Right column, like Claude's: the ⋯ menu with the "new activity" dot
        # under it.
        side = tk.Frame(row, bg=CARD_BG)
        side.pack(side="right", anchor="n", fill="y")
        more = tk.Label(
            side, text=self.glyphs["more"], bg=CARD_BG, fg=FG_DIM, font=self.font_icon, padx=8
        )
        more.pack(pady=(8, 0))
        dot = tk.Canvas(side, width=8, height=8, bg=CARD_BG, highlightthickness=0)
        dot.pack(pady=(6, 0))
        if self.has_news(key):
            dot.create_oval(1, 1, 7, 7, fill=YELLOW, outline=YELLOW)
            Tooltip(self, dot, "New activity since you last opened it. Open it to clear.")

        body = tk.Frame(row, bg=CARD_BG)
        body.pack(side="left", fill="x", expand=True, pady=4)
        title = tk.Label(
            body,
            text=elide(
                info.get("title") or info.get("error") or "loading…",
                self.font,
                width - INDENT - 60,
            ),
            bg=CARD_BG,
            fg=FG,
            font=self.font,
            anchor="w",
        )
        title.pack(fill="x")
        sub = tk.Frame(body, bg=CARD_BG)
        sub.pack(fill="x")
        widgets = [row, icon, side, more, dot, body, title, sub]
        parts = [(f"{repo.split('/', 1)[1]} #{number}", FG_DIM)]
        if info.get("kind") == "pr" and info.get("state") == "OPEN":
            parts += [(f"+{info['additions']}", GREEN), (f"−{info['deletions']}", RED)]
            parts += {
                "APPROVED": [("approved", GREEN)],
                "CHANGES_REQUESTED": [("changes requested", RED)],
            }.get(info.get("review"), [])
        has_review = info.get("kind") == "pr" and len(parts) == 4
        if has_review:
            # Room left on the second line: card width minus the icon column
            # (INDENT + 30), the ⋯ column (32) and the bell (22); each label
            # takes its text width plus 8px of padding.
            room = width - INDENT - 30 - 32 - 22
            room -= sum(self.font_small.measure(t) + 8 for t, _ in parts[:-1])
            text, color = parts[-1]
            text = elide(text, self.font_small, room - 8)
            if len(text) < 6:  # "cha…" says nothing
                parts.pop()
                has_review = False
            else:
                parts[-1] = (text, color)
        labels = []
        for text, color in parts:
            label = tk.Label(
                sub, text=text, bg=CARD_BG, fg=color, font=self.font_small, anchor="w"
            )
            label.pack(side="left", padx=(0, 4))
            labels.append(label)
        widgets += labels
        review_label = labels[-1] if has_review else None

        # Bell at the end of the second line. Unsubscribed items always show
        # it (click to subscribe); subscribed or muted ones show a crossed-out
        # bell on hover only. Not in `widgets`, so clicking it doesn't open.
        bell_mode = None
        if info.get("can_subscribe") and info.get("id"):
            bell_mode = {
                "SUBSCRIBED": "unsubscribe",
                "IGNORED": "unmute",
            }.get(info.get("subscription"), "subscribe")
        bell = tk.Label(sub, image=self.icons["blank"], bg=CARD_BG, cursor="hand2")
        # Packing order decides who is clipped first when space runs out: bell,
        # then hover actions, then the labels.
        bell.pack(side="right", padx=(2, 0), before=labels[0])
        if bell_mode:
            bell.bind(
                "<Button-1>",
                self.later(lambda: self.set_subscription(key, bell_mode != "unsubscribe")),
            )
            Tooltip(self, bell, {
                "subscribe": "Subscribe: GitHub will notify you about this one, "
                "on the web and by email, depending on your GitHub settings.",
                "unsubscribe": "Unsubscribe: stop GitHub notifications for this one.",
                "unmute": "You muted this on GitHub. Click to subscribe again "
                "(web and email notifications, per your GitHub settings).",
            }[bell_mode])  # fmt: skip

        def show_bell(hovered):
            if bell_mode == "subscribe":
                name = "bell-hover" if hovered else "bell"
            elif bell_mode:
                name = "bell-off-hover" if hovered else "blank"
            else:
                name = "blank"
            bell.configure(image=self.icons[name], bg=BG_HOVER if hovered else CARD_BG)

        show_bell(False)
        if bell_mode == "unsubscribe":
            # Red while the pointer is on the bell itself: it's a "turn off".
            bell.bind(
                "<Enter>",
                lambda e: bell.configure(image=self.icons["bell-off-red"]),
                add="+",
            )
            bell.bind(
                "<Leave>",
                lambda e: bell.configure(image=self.icons["bell-off-hover"]),
                add="+",
            )

        # Item actions, shown on the second line while hovering the row; that
        # line has room to spare, so the title keeps its width.
        actions = tk.Frame(sub, bg=BG_HOVER)
        delete = self.flat_button(
            actions, self.glyphs["delete"], lambda: None, bg=BG_HOVER, icon=True
        )
        delete.bind(
            "<Button-1>",
            self.later(
                lambda: self.confirm(
                    delete, "Remove?", lambda: self.remove_item(panel, key)
                )
            ),
        )
        delete.pack(side="right")
        buttons = [
            ("down", lambda: self.move(panel["items"], key, 1)),
            ("up", lambda: self.move(panel["items"], key, -1)),
            ("add", lambda: self.start_edit(panel, "add")),
        ]
        for glyph, command in buttons:
            self.flat_button(
                actions, self.glyphs[glyph], command, bg=BG_HOVER, icon=True
            ).pack(side="right")

        def set_hover(hovered):
            for widget in widgets:
                widget.configure(bg=BG_HOVER if hovered else CARD_BG)
            more.configure(fg=FG if hovered else FG_DIM)
            show_bell(hovered)
            if hovered:
                # Ahead of the labels, so a narrow window clips the end of the
                # second line, not the buttons.
                actions.pack(side="right", before=labels[0])
                # A review word cut down to a few letters reads as garbage;
                # hide it while hovering when the buttons leave no room.
                needed = sum(w.winfo_reqwidth() + 4 for w in labels)
                needed += actions.winfo_reqwidth() + bell.winfo_reqwidth() + 2
                if review_label and needed > sub.winfo_width():
                    review_label.pack_forget()
            else:
                actions.pack_forget()
                self.reset_confirm(delete)
                if review_label and not review_label.winfo_manager():
                    review_label.pack(side="left", padx=(0, 4), after=labels[-2])

        menu = [
            ("Open in browser", lambda: self.open_item(key)),
            ("Copy URL", lambda: self.copy(info.get("url") or key)),
        ]
        if self.has_news(key):
            menu.append(("Mark as read", lambda: self.mark_seen(key)))
        menu += [
            None,
            ("Move up", lambda: self.move(panel["items"], key, -1)),
            ("Move down", lambda: self.move(panel["items"], key, 1)),
        ]
        menu += [
            (f"Move to {p['title']}", lambda p=p: self.move_item(key, panel, p))
            for p in panels
            if p is not panel
        ]
        menu += [None, ("Remove", lambda: self.remove_item(panel, key), "danger")]

        self.hover(row, widgets, lambda: set_hover(True), lambda: set_hover(False))
        for widget in widgets:
            if widget is not more:
                widget.bind("<Button-1>", self.later(lambda: self.open_item(key)))
            widget.bind(RIGHT_CLICK, lambda e: self.show_popup(e, menu))
        more.bind("<Button-1>", lambda e: self.show_popup(e, menu))

    def dark_title_bar(self):
        try:
            import ctypes

            self.root.update()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            value = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(value), ctypes.sizeof(value)
            )
        except Exception:  # noqa: BLE001, S110 - cosmetic, Windows 10+ only
            pass


# 16px status icons (GitHub octicon-like), pre-rendered as 4x supersampled PNGs.
ICONS = {
    "pr-open": (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAACOklEQVR42qVSMWhTURQ99733"
        "f35jLEQtCIKTSEldFERw6K+b1BIayquTQ0WXrt26/P5BJ9eG6qBUJ38GRaTEtNh2EIqjUlBw"
        "EbqYIdIQ+3+S/991sElsUifvdLnn3sPh3AN0yvNEt2fQsf0/63DJfTM1OvF+5lr/4cTWzKJb"
        "nho9jlDoQEsQ2K0UtBi2d62M2nHfTT8CAe6mq8bXC0+kEg86Kj14Rwm6neElTvhTsxatyrS1"
        "AAZMlL2XPpu+n0TxTwj7Knzf9IsX1d0qAQABASnKSUflk7C9DgILprfRj3DHGrazqTOp5255"
        "etkn33jc80ts+9uxDrTcuvnaTyLzkoiyUiR3AGB78tVeVJO32o1W0KyGRfuUMz9ezt/wyTc6"
        "0BIAFABUR/6oAJtvpplwlHHqh4aJHSrVANwGgImN6TxILALYHPQAABFSIJAwTdX9D4M89hQY"
        "ZGLeI+A0AOR0jgcIAGIAsNop7o3AAAwIgCALhPjvC9ULEgQRGQBoW83B8HggMLg/Cz0FPoyJ"
        "EwcEHq63WgMEPgwAAkEdqgIASB1ouTa1lriVgpaOeihtabdiMfT9xZd1XdJiTI+J4qWicSsF"
        "LW0xL2x57vzsxZOrFx5X9JiWR4NkzNf4V+upcOTC9Q/5TGm2lHQ+xMYsMeNzHMbP5AnVxbtB"
        "AnFJptRlOWTdTaJkw2rvh0cCAwpkSl5RQ2rOhPGG1d4PdaAldU0hsFvOz0nHHmkcNFY+Tpbr"
        "nXk/bh80Vsod/H/rNwR89niFp8hzAAAAAElFTkSuQmCC"
    ),
    "pr-draft": (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAACS0lEQVR42qWTvWtTURjGn+ck"
        "9yNNUSzNaBVcJJsuruYf6JZMrdAuCgodAq7nnsEl9iI1IFZIi9bFE3Bxk66Coy1kEBSHLk4d"
        "EpukJ7n3dchHU+0iPnCG8x5e3o/nd4CxtNYKUwlxsTg+f2qUsPl8++az7Td38A9S1toMQIlf"
        "7Ja9MN/KhXOfa/WdTRGhFlEzlaG1VjOdEgBUszm6JalESZIetjud10HgV2uNxrwhU621EhHE"
        "sc3lF5cO8otLB3FscyICEaEqFluTmWwmo4q+7y875z52j270tNbKGCME0F4oCIBjAMfthYJw"
        "nKWMMcNy2WYeP1o3g4F7R+KKNwzvGVMaThako4hmrdQXyiuhvDJrpb7WEUlKFgCKxQIBQFL5"
        "5pyTa0m+DQBRFEkURSSZ6q29S35W3gKA3tr7YDZW2yJCdX6lKiDJH5f97NQ3UrTWymystt3Q"
        "rbihWzEbq22ttZp2cOamCEjk+05mQiQg+vrdkL++PwAU4ti+r1bLfZGI2XMgESkAhKH/Nyyu"
        "owAGgCS4Csi4xHQEY0yapBISkJMT382OIADM/eVuOkjWAuVVq5VKjyRIirLWZowpDeP6bjkM"
        "god+EPJU/XwiIqw0m8pamyEpcX23HObnvnj53KfNEWiw1mbOQKJEkqZf+93ezgSkZqWStFoj"
        "ThJIJGlyeNJp73hhUK3VGvOVSiWZgkSRpuf5t4IwWHfO7U9AmhpEWM8PbgdhuD44Pd3vdo96"
        "5bLN8OwzUZ7WG2ueHxTYl5cbY59JCkQIXvyO/9VvbQ8x+qRI8FYAAAAASUVORK5CYII="
    ),
    "pr-merged": (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAACPElEQVR42q1TPWsUURQ99703"
        "+zGuCQlJUCEqWBhWBQ2I+Bcs7GYrTdAUgh+Q7IJYBCZjYSXEEBRJYTQxgrOV2IgEVLCwEMSP"
        "FUsbEYJk4zq7szOb967FZsIWagh4qsfl3cs599wDbMB1WSRvBhO2g6RhvtgcelSKT2ynV/gO"
        "SwLxUjFybKkquYz1ZmG8fpOZiTtY/XUAUAYAaKOnjDEfao3oQSZll56MIUcema3kiMqKk3zw"
        "hRCHlLROR3H4rHsQIbssCMRb6vAdlgCwON6YfzrJ/HDi15HO+r+gAKA/jzYLwue0BV5nPgjg"
        "Y24XFMAGIGZmKhcgAKCSB3semc0BHchqDbJExp0frS6fmqW1hAkRaQD6jwwSEChVbbQiQWSn"
        "+rKvF0uty+lw9W3hDgUzV350DaS6bwspdketsDQynXvvuiwIAF64rF5iyhz4ee26ILpar9f2"
        "5XJdz3fa6cP1ZlxfN+aVEmIHQEcBsWa0RryaGvq6fyre9NnzPMMMSwpp9VT7V4Lg03AQtY4b"
        "wzcIok+SPKk1f2PGFxD2yD5Iz/OYfIdloUx6qRg5Sqr7Sgo7CMPp0Rm72Cnv3qVgOJvNPJaC"
        "BppxdHHklr3kOyzJd3xZKBf0wnijYikVaW3eWSp9Pm6i+0wvgrnvkD1VmEKZtOu6oqs2li5N"
        "7w03l1jJtw+JiMopZblG4FjYCpcHe7MNALgwR60kbG3rvPaBbdhISZgIxAsT4bmMZfVHobx7"
        "dpZqSb0jdsRttxj/C78BewP9n4FfbwwAAAAASUVORK5CYII="
    ),
    "pr-closed": (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAACS0lEQVR42qVTO2hUQRQ9dz7Z"
        "lzX4CQa0sLCTgIIGTLpdG01cWVx1rJQQxcpKFiuR3e21DyL+ME0eBsUlhiCatbISVFIIgoVE"
        "MKBkg9m838y12OwzGwsLDwzM3DPcuXfOucAGuAKR7gHCJjBAzCBGe2ErOsHgzPED0dmTw/gH"
        "loaGsjye8wBAsDGSAA5LBaNFz6LWPW/XT4/eYoBe53IKAFYKhV3huVNv1kqjI19yOW9g/97F"
        "YKX3BoA/ZTt2Vef4QxiGDz2tyigW+441GgkbI3cQhcTumybh79u9rQGiHx7cHQaEwPIyAQCB"
        "pgXRoJayGCZ2Hs3mOlcqAoODTPV6S6+5cQL6ZW/2aNBau0bP5r/CGCIAYGMk+b4NSmOPMkpe"
        "RNDaQ89ffWdjJHzfNQuFnVkP79jxkiB+D6WuJIk94c3MLrRb2KjCMX+OrWN4rVUAgO87AngH"
        "UcjA48RG5/XMi6ts7TQ5PkQAq82/S0CGAEIouuP1egvAzc6558nshc6+6yKIGACQ0dwlMzMt"
        "5PNypN+7DCL2Dg7fBQCq1VyqQgUQIHIAgDDuNku1Svl8wwlQWTDKqNXSB9IENcA56zwAjGh7"
        "tNU8VINj4CeImgSkCaijQFgqGCXxQAiZDaLotvd07jqMEQCQ8oKmhCAdxHHKbzXSpziO73la"
        "llEs9pHv24UNhSwnVQf3MYzj+57SKZ8aCQRfKXVYK3UpjO3L1EidXoWcVlIdyWg9EdmkzbfH"
        "oD1M7XkYm5BSDMhfdpLm5lY78b/4jJ2kqTaP/8VvCZUYTw+NrvoAAAAASUVORK5CYII="
    ),
    "issue-open": (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAACjElEQVR42n2TzUtUURjGn/fM"
        "nU8GzcCFm3AREbScKBHv3HHK0SK8jnYHzIWLkcj+hYib0cJFLfsSddPOgdKo+Sh0ZnIqBS2I"
        "Fm2DNiE5Ruh8nvu2yLFxUM/q8MDz8J7z/F6g4ZimKYw5w9aog0EH6vvMbIravS8+0qQm9c5g"
        "bkjtzYbbanpjCNWbJ2jC6pgLnXS3eu+whWuK104kCLJQBUt+L7crt99deZk25gxbLBKTewE1"
        "c+f8pbPOFk+WLdisknwEUILYqjBwzuawjduPOdsLP3fGli8vzOyFmKYpwCAtobcHc8ZWMDu0"
        "1jXX13rACymwNPg49HmY/fH+MABopqaIDDICBGbwJEvLVf5R7MlFkhu+NZ+95tTSmgIGMsHn"
        "4+XN4kdhFw+19KgrgIAFALj41mjuXhpkNdF/HwB8a9ftYJCa0qNqSo+CQbsh5E+Ge0Lrw6wl"
        "9AAAKABQkuUzrmYXk4XXYNA6TVUCKT1qb3FPA0DgjY5M78IMAHhWPKvF/A4zkR9ARvz7SXZC"
        "EFsCZRD4qKo3lN9VthgEOAFAAAC7bd9koSqsUvU8GOR76rNnQguzxXxhrJgvjGVCC7OaqSkm"
        "m8L7y3bKccxJzHK9HgzSEvpqYDH8vVarwXXAsCmMr4YDAPxJ/Vl3ZqjYkTSOg0F7ZGnxga7Q"
        "+jBri4NPDhtfTQ2M9n4ZYf+r/lu1dvaBpMb1qLvNM13Jl3LVUnWSvfJD+Y+UTuE6rSh009Hq"
        "Hi1uFKayF17cMGKGiEVikuoZj0ViUov3XxUe5YHidZyobJUBZihNDsjtSsWS1t1sz/w9MGh3"
        "CZjqR/yPJ0RAG1BZQhXCZmcFn4oFsbzSF9sEM4Ho8KaOWlktrSmN2l9xHCwy6NZXEgAAAABJ"
        "RU5ErkJggg=="
    ),
    "issue-done": (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAC6ElEQVR42nWTT2hcVRTGv3Pf"
        "fX/mTXyJNUWKWKtIBSPWhejGtmQjuFLEGVCMTampbbSxbVpcSH0zIi7EkMSiEa2oaZX2zcKV"
        "ihsn0RZKsaJQ0EURKZSKf6aZTvL+3Xfv6SJM2oZ6lh983+Fwfh+wasKQRVRha7XOzHQzXa42"
        "1+tkAODoHg6krR6Qrm0JwnkiugRAR5XIqjaquuuhlQ0hC6qT+Xh04d4e368x83NlzyFBQKqA"
        "wuhTWaoODR8uNaMKW9UG6ZWArvnIywsPB+XyvGGyVKE+YI1viaCY+BFp2bsDX25otZdefOG9"
        "nk+6IRSGLGp18PHX0rsckr9og/OdJHlix+Hgn+vPiyqRpTc8+X5vyXnp70789PBk+atwa1NS"
        "uLUp6/ODxezepeOuI5/6r5OsG53pu/zTTrb/uAyz9n7QYJ2KbtAX+5PTAK3Pb3Hv+RO1nAAg"
        "2sm9OtALmcomhqfLB86F7AzUoBpViGqD9Mwri5vuCLypTlrsznKz9vY1pR9a7XRwaLo0JwFA"
        "u2rAd21WhfiGwUR1ylEHmNlMj/0b9PvOiVTpsmLd6c/9C+24YAPeAmBOAICGdgWBCawIxMf2"
        "JSeO7UvHagRa5/V+Lwh3thezx4cnyxfbLqQ2DBLkAoAAALa83zMFoYFHASZm+lVKZ3rgYHGS"
        "CA+12vGWkZngt6jCFsv87qBkExtzFgBEVImsoSn8FWfpGcey9gDEQ1Pe26rIdnmOeHAxybaP"
        "zPSdnR2/VK42SIPNgTRXWS78OQYTunh+vj9+7Os3mL8cTz9cIbNyzrn+lUfH423f1Zg/fTV+"
        "HQCaIcsbQJodS3b093lHrsTmZF6k75BO5l3/Norz+D7XckZv7ZHbWp3ko2ffLe3qfoiugbJM"
        "1md7l57xbXfC96z1VxINNgY9JRtJVqiC9ZvPT3hvMZiWjcR0I23LISFYbDyoNnNhNkvLsi3I"
        "nxd0+8eRyb4WMxMRMf5vblbZa21tytXaVek2dGlno03nAAAAAElFTkSuQmCC"
    ),
    "issue-not-planned": (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAC7klEQVR42nWTT2gUVxzHf+/N"
        "m/0zu+5sUnMQlF5EKPEiKJSi4oIKijc7cxDNVtKYVdDFREGr+BwJcSEq2Y1YR40WRQ8z0JuC"
        "WjEgqPiP9hDw4FlTYmPWtbszO++9n4dt0iXo7/iFz+/v9wewIDjn1PM8baGOiORLOlsIO46j"
        "AADK5RuZiEUrY3pSEyjeEELeAYC0LE/zfVvOMWQeRqQOIYoPn1+e7TRPIqodyWSKEEqg2QxB"
        "SPkkDOsnjh7o/8PzPM22W0nofGVC1NC5sdUdXdm/gMCPQsjRau3j5lp1JhfUG0cBIZsyFt0d"
        "qYz32rYt58YhrbZP4ulff/s2HUv8qZR686kebDlW7J1uH2/o3MXVpmk+Z4xBtTq7/ciBvt85"
        "54y2uiCoCVWSUibeva9uOlbsnXZdVy+X78QBAIZH3TVmJvM0bIaV+r/1B7oeO8+vXUsAgCIA"
        "ACXXNWMqOdsUzbNH9v98aI/r6kverkDHyYnTZy6sSmWzL6SQNw8WdvWUxi5v6uxYfK/64X3u"
        "8P6+CQoAwITenTQM1GPsNiKS7mAZbYeFiK4fLOzq4ZzTjpj5rPaxiqBg/fwSCag4pRSlwqZt"
        "+7RY3Bq2wwOF/G7OOWudOBEhKgBK4/8nYPJ1GAYUI/mD79tyeNRd0w63Nr4BOOe0Gv2zIpXO"
        "EED1EgCAWpanDfT3T4X14AXVtD2l0fG1KSP9TETRrYFCfjd/+JBNTk4S6J6mjuMoRBwMwiCk"
        "SZgAQMIsC8D3ARXioWQsPgFx8khEUWVgb74IAODkcmLulCNjV/PfdHXtnPp76vgvxb4Zzpcz"
        "0jISUschaqQy3ps2M1cajeB+FDUrrKkeC5NEWMfvdF3fl8125mdnP1waLPQUPN+ntm3LeStb"
        "nqf5ti1L5Ut2PGGcNQxj6adaDQAQjFQaGkEjklKeOrzvpyFEJIQQAAAk7W6b87hlWdr3G7et"
        "VZFcpzGma5S+ErXGo8HBvpn/YISvxZdetu1b2ULtM/y3fOdJaNWLAAAAAElFTkSuQmCC"
    ),
    "ci-success": (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAC5ElEQVR42n1TTWhcZRQ99/ve"
        "vDc/aSYuRsnCWIsgtKCCUqU68xJt0qRK/uob0CyKKNoKuupO5GVEXAgFXSgiunJj51ukiWaa"
        "oDZ5kxGKGFERdCEuRKGZiuNPMzPv5/uuC52SDupdXQ6cw+Wec4C+8X1feFVP9uNg0L/i15HZ"
        "F719srYwWFybOfJg40TxWDA33MP7RWgvuUIVc1914rZMYWCRDR63BlJEgqA7CVjzp3o3frH+"
        "yMqGV/WkKit9TaBHPnJ+6h7nhmzABtKE+k2ALhCbmIHD0panU0PO/s5O+6mt48vv9kTI931R"
        "WaywuzZzi9xnfwltvo92dqca5bUre0/1qp5sFvQbTt55pnt5d75+fGXJ9V2LXN+1gkqQlC5M"
        "vy+zqdn4p+5wY2G1dffnT6cO/NAyzUKTgrEg6Qm5H89dIuYRtvIHRjf3RwQARz/y8lrq35Iw"
        "Obs1tXLG+8az1SEVe8oTqqz0/R88fGfmxtxrcSs5zawL6Zuy9XCnPRZMLW9aABDq6FA6n2bE"
        "XAODFKkIABQrc/i9yUEnnz5nQpOTVvhndoh+vNoKmYlKADbF359kB4JYGxODwO76zLnS+tzz"
        "WAQNjOQuQoqbk1/aExePrv7cbCYWawMCHAAQAMAZ+Z3uJIIZ9wIgJnwlbfH62OiJBgh3Rc12"
        "qT5f+9arejLnZG61hxxi1tsAILyqJ7fcpcv6avyZdMRzALg+sfxKEplTMmfdEf2RPNGYW90e"
        "Xx/PqbLSOjZnknYSRpTeBIOuJcutzT4wsf0Yu5/Mv9X7+EH/oL3XyuL67MljXy9w6cPpFwDA"
        "3XCt64JUrM08mRnOvhO3wobu6lezCIMktY9CHd9uWfSsXcic7F7pvB08tHSq5xDtDYoqK+3W"
        "ph8VWeusNWCPxL9HgGFYgzb0bhwbbV4Kxs+/DAb9UwKm/rSpstLwIUbd2SJrFIWQKbbwRbcj"
        "ti5Nql/BTCDi/2zk/1XW3XCtfuwvs8pfM4+2dl4AAAAASUVORK5CYII="
    ),
    "ci-failure": (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAClklEQVR42n2TzUuUYRTFz33e"
        "zxnBsp2LokWLyG2JRjYSpmOTX6OToJVBbZRoUdQmYhxpEUGbKEzIlbsGtCTNlFBJwwrb1p8Q"
        "LUopfT+e531uC50pP+quLgfOgcv9HWDbcDYrOJMxdugA7abvMBf37mRp2H7mOLe31HBbQ3lB"
        "f7YthP42Uy6nV5rqD8Vtsx9Al2VZBCJARdBaL4ZS3omNT89yJmNQPh8VA4rm5rqjJbYzL0BG"
        "qKIhEF5Ca8lEVZYQfabr7PfXvCuxF1PDxRDOZgUDxC0NB+W5ppWwI7XMqVTZbicG6dQgd6d5"
        "rTnZBgCcSJgCc3OCAPaJ7kGzu+6t1n2L/5JeOlVXMHpNp0/56cYbzuhEb+j7S6YpHnNPwkVt"
        "rRY0P684U7fHNoxOqeTg3omFH/tUvNp17Zn11mRP0NRQ4cRjb8BcyQBpFfXbrlPur7hVlMtp"
        "AQBS2xXCMpmEMc4AWWOvZsIgvGQJ8ciwrXdSRg/dsalOAtizoyXlB8xEJwFAAEDEcEDEDA4J"
        "YADgQL5nwDQssxQ6+lg4p8xzJZhBgFMMcDn6AhUJMFczQGFrqtIpiX3WGsNhGF6047ERP91w"
        "i7NZsWbJw6ZjkwYvA8AGdWOvvwZKfjBJXCOAtVaroVID7vPJq87o5EjgeTcJ4hjlctpkcV2q"
        "KIhbPMcAoYDnemvyBHe1sZ9ufPIvUoPWZA+fb+f15vrbhTduAclrSV52S+JPdRAsSq3u+yYv"
        "qJ9CxWI4YoP6zHjsQugFQ/boRC8yGUH5fPQH5U2y1pqTHY5lPDBs60AUhmAGTNtCJJXUzAP2"
        "6ORd3iSYAKYtZdoMYUDIzNkaRLqGiCwT/GlVqLd789PfmUFEG5/avZH/qexsImFu134DkshO"
        "OGW2OaUAAAAASUVORK5CYII="
    ),
    "ci-pending": (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAACwElEQVR42nVTTU9TURA9c99r"
        "Xy0CUmmiSISW+gUYo2h0IagLf4AJKYkLXeBXdOHCxGiMKTUu2JBo1LR8JBh1VeLGxK0loiIR"
        "EBJSP0IlGhICKFSB8tq+e8cFFCvKWU7uOXPmzhlgFTgAwRFo/9QZFPlPXV9NpiAUAHx+7CtI"
        "Jbna6RSaBm2U6OMEABmJQPP7IbMcyukgiKDetJT6XEVGk1I4WZgnSBOEeVNBSn6dXMTNvZfi"
        "0VwRsdKZoPruefe7NxrDANdbFt/5/jNzZDIhaxYWratSYUuJW3sxFPI0+v1LTv7MzKAPnWXl"
        "Y498idGHFf0v7/rcADB41+f+0OHdDwAMUKyjIjzdtZ3fh8pOAEA0AF10A4IIbJqi2ZLKMTGR"
        "OV53eXQ6GoCu2dW1dQ7xbqSl0kUAV56JX5hKWL02XXsw1lnm6AaUOBaE1d/qLXTYRcO8ifu1"
        "17/N9rdudh4LwhJE83NJacFAOhKBxgxKp1VTcYG2OWHiUDAIJQDAAFXlOwXrNvmcGVRg5skl"
        "2+x02IUei/+Sfj8kETjP0t/++KWYQHUrn2jBMjQBBnOaCJyesTMziIl6DJ0S+/YYvcPh8jpm"
        "ULLIKRUDQsBYETCw7mPSZCGldpABqjrqVgCw53z82XiCDygFEOjKQFuNTosLO1z5ghSLAQDA"
        "8jpoOOzti7VXfM1uJhqFnpNIMdJe6gKAobD3yadOrznSXuliBiH7aKjVe3gyso1jHRVhrIGR"
        "Vu/pmafbeeCB5wYARKPQKTeFQyFPY0mxrWP2p3yVTHGzRtabZMYuHZq1y+YQFzcV6aemZmXb"
        "zsb4BXRBkB9yJcrZeA6GPPXrHaIl36ltnZ2TUAxsyBOYN1UmY/Gt3ee+3GYG0RKTKddiViQQ"
        "gGjY6quVUtXqgmy6DYOpdKqn+uz4zDKZ1xoT/zvlLKKBv68XAH4DAZhIauCgHQ0AAAAASUVO"
        "RK5CYII="
    ),
    "unknown": (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAACoUlEQVR42n1TT0gUYRR/75sZ"
        "dVZZd122Q0FBRAQFXYIuGevBoGu4c+iQwRaaFysJEYlxQmygtHRN2/Vv5iFmDh4iuqXQHygq"
        "6iB0kPCWoKbubu7M7nzf67DtVpv2Th8Pfr/v93vv9wDKStd1ZlmWVN4nItypL5eDDcMQAACD"
        "g4/9eTl/rEJRJY+8JUT8BgA8GrUk29Z4EYMlMBEzEIXeN3woUFfbQyTOq2o1IkPI5VzgnL/O"
        "Oe7NzvZL85ZlSZpWIGGlnxFF70D8RDAc+AwITZ7H72+lU2fSW6kGZ/tHpyDaFwyFXphDEzFN"
        "03jRDhZk99Dt0ekDNRVVn4QQS5lt52x3e2y1zC7eG50ZqfHXtq6trp3ruhqb03Vdlgsq0JO8"
        "CZNLvGplPdVodrVtJBLvlWDwqwAACIfD2BCJ8GuIV/pHpo8rFdIDfWrqOSwv5wAAwEwkagdG"
        "Z8iMj98FAEgkEso/25mfl4kIzfhkY3J2ju7ExyKlGcieclT1+UipkJ8REQaDQVFO0BOJcEQk"
        "VShv06ktAgGnSwQIopIxRlxQDhEJ/lNVVbUekQBgrPI3gcy/uK7DwOMnAQAXw2EsB9q2zXRd"
        "Z1v59cPVNX4EEh8AAFg0aknXW1pWHCf7TpLkdgAgWFgQRc/FBC4CSIZhCCLqcFzHZSosABBC"
        "cZ/m8MSp5OwcDYw8erib/DvxyebJJ0+pb3Csu5CfeRkLD2KGgcIcSsZCoT3jmUz6lee6pier"
        "b5z8GlfRd0RRlLZAoK55c3Mj2dF6odWybaZpGi95jVqWZGsaNweTTZWqr9+n+vZn0mkAIPBV"
        "10DWyeY557dutF3sJSJERAAA+mtYxYzrus78ew/Wizyvl2RZkRj76KWzLzs6Ln//Bd59Uzud"
        "7B/XKpf3fgLOckgZqQ/2oQAAAABJRU5ErkJggg=="
    ),
    "bell": (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAACHUlEQVR42q2TsWsTURzHv793"
        "L5cLpsJhqJQK0sXBGxwcxDGuriZrF6GNQQk4SAe5y1QEl16kaWIdBLFw+RNEDtwcuih1cRNE"
        "2loObWk0uXdfhxhItUVBf9Pj+x6f3/f33vcB/1hynBhFkQUAe3vb14TZ3ELtznqv15NqtWr+"
        "ikpSAKDbaa122+GHX/UTHYwPhGE4NVW0H4CYB1mg4MX+waDeaDTekxQR4bEA3/cVAHVudvql"
        "ndPnv/X7dw1Uks/peyCv9ge8sLu7uxMEASchR+budMLrTx6vcnV52Z3cX+88etdph20AiONY"
        "j3U1tl6pVLiyspKXDPeHw+HGraWlxPd9O4oim6QYYx7mcvpGtxvOlctl89PtCAAAIpLNOI4j"
        "Sl2hqC5J8TzPVCqVoYjQ7acbaWpcEhcB0PM8OQIAgCRJQPI7meZ/m7FYtEkaY0w6KU8CpGBZ"
        "KUltWcjHsa+LxaLe3NzUcezr/mCgAVhK5fqTz6kBIAgCAZCZQmEKyPZFrLflcjMFMNltr9sO"
        "DzKml0XkVRRFo64kJQgCKZVKrmPLa4CnSD4jSRGlR5ecpaM1q07emd0/PLxUrzfe+L6vZByM"
        "Vqt1xrHluSgpZCY7DbDEDJ8AQBRmCPlsifoK4MswU4u1Wu0jSZGT4txZa63bOncWAAbpcHth"
        "8fbNP36mcRI9z2OSJNOWpE8BwFDPu667s7W1JQCyZrNJAMT/qB+x1wdLc41V8QAAAABJRU5E"
        "rkJggg=="
    ),
    "bell-hover": (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAABzklEQVR42rVTsY7TUBCcec/E"
        "RBFBkZA4ICkQoqbgB5DQFegqGjoahPgK7juuoUT8AB1NWigQUogorADH2Y7t5OIgQDmb2B6K"
        "i6UgckQUbLdvRrszu2+B/xGSrCQbh+FuPPafSDKS7CauOaNGRbI0BvcBPCVZkSwlcVtnSqLn"
        "ee15OjlIj5PFbBornSavRqPRzZrzNwUEYDvt1kvH2nvZyc+Hiyy7C0idduttknzeAcCNSmqP"
        "UeTvzaaxBoNBZx2fJuGHKDw6WHGdTdKN53luPA5ej/0vLwBgOBw2JDUkMQqOHs2mk2kUHV6v"
        "+b8VAIA0TS9O4lB+5N9ZkWyNSWrG46CI/MO9dcV/bKEsq9xUcklq/X0+nzcklZUxxcYhSmKe"
        "5wUAR5K78umscmexWDgALImT9SHWBUhSxpgLAL4Xhd6TLEjmJJcki263OwPwoyzL2+vq6pUw"
        "DMNOw/INgFaF6nlVQcTptAUWhBySD1qt1rWv3+a3er0bA0nGWbWvgiAAqI+EaaLSLsFLEKJT"
        "HFcAHFtjP+VZ9q6pc7Pa+ZlfMx77z1z3/GUAyPMs2bnae7z1FiSZfr/vSLLGcfeLcukW5dI1"
        "jrsvya4ws/Um/iV+AU7nKlvE98NqAAAAAElFTkSuQmCC"
    ),
    "bell-off": (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAC4klEQVR42o2TX0hTcRTHz/nd"
        "63bv9G7JlhE99eLLcpAsCCyXRA9JBJErtVLSWn/MrNDoJXZ96CVF0pnLmZblvy5FEEH0pK+F"
        "jcqGig+VPhRTNLfc/HPv7/RQphlSXzhvXw7nnM/5AvwUgxXhr/pvMcYYuN3uqo0ZG+8iIng8"
        "HlHTNKEtGNh3p6XRR0RM0zRh3QZut7uq/uYNys3dRVarNSSKIgAABFsag6Fg0/iykYj+mkz0"
        "er04MDDgevb8BRQeOZwgzk+/Cb/Vux+2xxOJ5EnOubmjPfgyFl+4gIhjRISISKv3ZYjIFUUJ"
        "uVzbThcXepcedPWm2KyKXllx9uj454lvZsl8DQB2Jhcpc3JyMqqqKv3VhDEGiqK0ut3ZevD2"
        "rUR29vYkANQtm0KtgZHWYFMQAKC/v1+EVdcnv98PnHOMxWKVktn8tfNhj3yqrETI3Z1TnZHh"
        "6GSMARn8piiK3lCoaWteXp7h9/vZb3yqqhIAUHNzs3LGd0peWtLfddzvSikqLFjIzMwskWWp"
        "5eLlqx26Ydh0nTsBgJxOJwIAiKsvqk9NGQnBrjQ11J3P8ew9ZJGl6uJC7zw3jHMfP43b01JT"
        "5+Jzcf0PhKsfyJ6WtgQIwtDwB7sgCDUjo2MPHmlPpOPHivQtWzYf6dMe21wu5zQAYCQSWZlA"
        "VVUEAG7IsgLAY5wbEcMwIBqNlkaj0Ynvc3M15WUlifud3XJ+foGPMfa6traWAACRiFBVVXQ4"
        "HOmSCV8BUCoRdREhEefihnSbHmq7V5FMJi0njhct9vRqpvdDkbZ4PO4jIobLjxEIBOySCXuQ"
        "ocwNbgUgB3H6AohosVhM9Q2N85Ik7Sg9UTzfpz2RhkdGO/Z49vjWDU3rncBdk5iyiRPh4uLC"
        "dEXllRKbzdqelbWt7OCB/dCnPb00ODgY+IPCL7bM6XTSzMzMdYPrnYhIOheucc6F2dlY+fDI"
        "KCUS80PhcLhxDYR/am3MGQDADw+YS4CU/ujgAAAAAElFTkSuQmCC"
    ),
    "bell-off-hover": (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAACjklEQVR42o2TO0+TcRTGn/8l"
        "pVJsG1ECbJr4EXQwYtvX1krgbasJowwa8ZKYxk/QxUVJHCHWQoKDiay6wFIGNxJgMpq05b21"
        "by/UGg1QezsOFAWBhCc52zm/nPOcPMCeOAAwxgCAdetU4gC4lLITVJS4x3M2JYSgRMInFhcX"
        "RTGfDxUL5hQRcSISxxISiQRXFCW+sbZKsViEpJTJnp4eAIBtGrPFgmns9xLR0c0mJiaE1+ue"
        "i8UiZGjZbVUdJwAzRM2XtWq5Xq0UqVYtL2UymcsnQTjnHFKKpKqOkaFlG+FwiCYn7zWJ6K6m"
        "ZZRq2V6ulu2fpdLmYPecQxAGgEshwRh7Ewj4Wqae27kxcn0XwPR+U8k2v9p5Y7a7hTziAwBG"
        "RI6IOm6FwyEytGwjGlXJ7XYvSCFhGZv3tyqlLdvWLhIRIyKO/82xrC/99Z1fVb/ft6EofjL1"
        "XF1VxwjATF9fH4oFs6lp2fHujDgCWF9f99p5o0FE1wBMR6MqGXp2d3Q0TAMDFz406ts/CoZx"
        "+1hAt1wFS2/buq46HA54PJ6FO7EoGVq2GQj46emTR0REVwGwdDotDwI4AJRKpUE7b9Z0/dul"
        "Axa98PlGfht6bjsYVDpOpzMlhQQAAYDxf/db/azT+iw4r/c4ep8U8uYry8i9Jmo6hoaGWw+n"
        "HvfOp942g0HlARglOedtAIwREWOMkWVZ/Q6J9wz8TLvTchOx8x1q2xycec95HTeDt+oul+vK"
        "fCpZfxZ/7lxZWZkPhcJTJ4akWDBTtWrl4/dK8ZNlbL6TUsLpdM7FYhHaWFslRVHi3ffjkBfp"
        "dFoSkSiXy0OVUmG5WrGXdF0fBiCkFPB63amgosSllH9TfFqxvcSzg0nGH9DgaIJ2LAjFAAAA"
        "AElFTkSuQmCC"
    ),
    "blank": (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAEklEQVR42mNgGAWjYBSMAggA"
        "AAQQAAGvRYgsAAAAAElFTkSuQmCC"
    ),
    "bell-off-red": (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAC3ElEQVR42o2TS0iUYRSG3/P9"
        "F2cczZyJmWm6al4qqWghWhSWJZN2m6Es2gh2g25YUNEiGIVWtQqhSM0uVIuCIqOFZaEgibsk"
        "mArT0KhMasZLzsX5//+0mNQpizqrA985L+85z3eAeAgAoHhOU+m/gwAIBTCKc7OqOr6Hl419"
        "/Lj/bFGRXG23c0QbLgaLDNOKggb4/UT37unTFHyAcC/JqurdUsI7l+cxOZ11JoobCHk3XY54"
        "S/snavkPzmR/eTl1vGhffrrvExoXzQ3pBh94YLDG6wtGNc2oFOAkbWdZ81h4/Cg9bulmgAjg"
        "X0YQgMFOZ51nlvXA9UXzYnu6+xSXImn1edm7h0ajQ8kKnQFQGJOQY1maP4iaGp4mIhMAh+PK"
        "xsU52uh2d2j94pwwrNYLE0Vhb9mbiHfTZQDgoiIZCdtnHwCNQfys9liqEAO7u/vMD3MzpB1z"
        "Zp9McbluyASAjfMQUnl4a0kG2tp09vnEJL7quB0eOXsl9X5eljmqG1073r5XrmXOjZbYZlZo"
        "dscl+6MnjWwYaeMS5RHA8PsJAOTEjQaCmq6ka6ktq1YepqZn3kpFOXk9c34kZhiHOhXFZlLk"
        "MT2ma4k9IhHRQlWNASSFgsM2Nfjt1NNvwZv7ej+Ybmct0FakJO860dOfZrHaAgCodXBwCin/"
        "FOKyMmfYUxoc2rwhc/IxLf1ccW52dMTjHnMvyTVUl6tBjrdKAIgYIPh8NNLVnm4SaieYLQbT"
        "LRCzzixbVFWreN1zJKAbyXeyF4xX9PSrTV+D9TQwcNAABE18jGGPx2YS0TuCYdYZMwCeZQCf"
        "QSCzLKtrX76JpMhS/t2cjMje3g+mJ4FAo3v1moN/PZqQt7QhSRIOnUFRXQ9YHzZXkMN5dZvd"
        "uvf8fBcO9bw/Xvj6Xe0vAuzzidbWVrHObmdoI/YYSTcEwFFtvNLS1PJFJtKTXa6GVSnmV8/f"
        "vrsYS4Dwv5dL9BvBHw/EJhY9B4spAAAAAElFTkSuQmCC"
    ),
}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Always-on-top list of hand-picked GitHub PRs and issues."
    )
    parser.add_argument(
        "--state",
        default=STATE_FILE,
        metavar="PATH",
        help="state file to read and save (default: %(default)s)",
    )
    STATE_FILE = os.path.abspath(os.path.expanduser(parser.parse_args().state))
    root = tk.Tk()
    StickyApp(root)
    root.mainloop()
