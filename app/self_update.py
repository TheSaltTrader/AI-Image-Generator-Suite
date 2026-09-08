r"""Self-update for Comic Book Art Creator — GitHub release -> this install.

The app checks its GitHub releases at startup (and on demand from the
"Check for updates" button). When a newer release exists an update window
opens showing what changed. Two modes:

* automatic (the default, since v1.36): the window opens on its own at
  startup and ASKS FIRST — Download and install, Not now (nothing
  downloaded; asked again next launch), or Skip this version. Only after
  a yes does it download, verify and install, and then it asks once more
  before restarting (Restart now / Later — the new version starts next
  launch). Since v1.51 nothing is even downloaded without a yes ("wait
  for confirmation before downloading"). An install that is never updated
  is how the bugs a release fixed live on.
* manual (the checkbox in the window turns automatic off, and the Check for
  updates button always uses it): nothing is downloaded until the user
  presses Update now; Skip this version and Continue are offered.

Why a module and not a few more functions in the main file: the swap itself
is the fiddly part on Windows (a running .exe cannot be overwritten, so the
old one is renamed aside and swept up on the next launch) and it is worth
keeping the download / verify / swap / roll-back sequence in one place where
it can be read top to bottom.

Rules this module follows, learned the hard way on this project:

* An exe-only update silently forks every data file the release ships —
  that is how installs ended up with a years-old model list. The docs and
  models_manifest.json are refreshed from the same zip. presets.json is
  deliberately left alone: users edit it.
* Nothing is swapped until the downloaded exe has been checked: it must be
  a real PE carrying a version resource NEWER than the running build. A
  truncated download or a mis-tagged release is caught here, rather than
  leaving the user with an app that will not start.
* If the swap fails halfway the originals are put back, so a failed update
  can never leave a half-new install.

The module talks to the app through callbacks only; it imports nothing from
comic_art_creator, so there is no import cycle.
"""

import ctypes
import json
import os
import re
import shutil
import threading
import zipfile
from pathlib import Path
from tkinter import (Toplevel, StringVar, BooleanVar, Text, END, WORD,
                     DISABLED)
from tkinter import ttk

import requests

RELEASES_API = ("https://api.github.com/repos/TheSaltTrader/"
                "Comic-Book-Art-Generator/releases/latest")
RELEASES_PAGE = ("https://github.com/TheSaltTrader/"
                 "Comic-Book-Art-Generator/releases/latest")

APP_EXE = "ComicArtCreator.exe"
SETUP_EXE = "Setup.exe"

# refreshed from the release zip alongside the exes — see the module note.
# presets.json is NOT here on purpose (the user's own edits live in it).
REFRESH_FILES = ("CHANGELOG.md", "KNOWLEDGE_BASE.md", "RAGMAP.md",
                 "README.md", "SECURITY.md", "TRAINING.md", "LICENSE",
                 "app/models_manifest.json")

_PROJECT = None
_STATE_FILE = None


def configure(project, app_dir):
    """Point the module at this install. Called once at import time by the
    app, which is the only thing that knows where it was unpacked."""
    global _PROJECT, _STATE_FILE
    _PROJECT = Path(project)
    _STATE_FILE = Path(app_dir) / "update_state.json"


# --------------------------------------------------------------------------
# versions
# --------------------------------------------------------------------------

def version_tuple(v):
    """'v1.35.0' / '1.35.0.0' -> a comparable 4-part tuple.

    Always four parts, zero-padded. That padding is not cosmetic: a tag
    reads as three numbers ("1.34.0") while an exe's version resource
    always yields four ("1.34.0.0"), and a short tuple compares LOWER than
    a longer one sharing its prefix — so (1,34,0,0) > (1,34,0) and an exe
    of exactly the running version would have sailed through the "is it
    actually newer" gate.

    Anything unparseable sorts lowest, so a garbage tag can never look
    like an upgrade.
    """
    nums = [int(n) for n in re.findall(r"\d+", v or "")][:4]
    return tuple(nums + [0] * (4 - len(nums)))


class _FixedFileInfo(ctypes.Structure):
    _fields_ = [("dwSignature", ctypes.c_uint32),
                ("dwStrucVersion", ctypes.c_uint32),
                ("dwFileVersionMS", ctypes.c_uint32),
                ("dwFileVersionLS", ctypes.c_uint32),
                ("dwProductVersionMS", ctypes.c_uint32),
                ("dwProductVersionLS", ctypes.c_uint32),
                ("dwFileFlagsMask", ctypes.c_uint32),
                ("dwFileFlags", ctypes.c_uint32),
                ("dwFileOS", ctypes.c_uint32),
                ("dwFileType", ctypes.c_uint32),
                ("dwFileSubtype", ctypes.c_uint32),
                ("dwFileDateMS", ctypes.c_uint32),
                ("dwFileDateLS", ctypes.c_uint32)]


def exe_version(path):
    """FileVersion out of a Windows exe's version resource, as a tuple.

    This is the check that makes the swap safe: the file about to be copied
    over the running app must actually be the app, and must actually be
    newer. Read straight from version.dll — no pywin32, which is not in the
    frozen bundle.
    """
    try:
        path = str(path)
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return None
        block = ctypes.c_void_p()
        length = ctypes.c_uint()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(block),
                                  ctypes.byref(length)):
            return None
        ffi = ctypes.cast(block, ctypes.POINTER(_FixedFileInfo)).contents
        if ffi.dwSignature != 0xFEEF04BD:
            return None
        return (ffi.dwFileVersionMS >> 16, ffi.dwFileVersionMS & 0xFFFF,
                ffi.dwFileVersionLS >> 16, ffi.dwFileVersionLS & 0xFFFF)
    except Exception:
        return None


# --------------------------------------------------------------------------
# "skip this version" — remembered between launches
# --------------------------------------------------------------------------

def _state():
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(d):
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except OSError:
        pass          # a read-only install just gets asked again next time


def skip_version(tag):
    d = _state()
    d["skipped"] = tag
    _write_state(d)


def skipped_version():
    return _state().get("skipped")


def clear_skip():
    d = _state()
    d.pop("skipped", None)
    _write_state(d)


def auto_update():
    """Whether the startup check downloads and installs on its own (the
    user still approves the restart). On by default — see the module
    note; the update window's checkbox turns it off."""
    return bool(_state().get("auto_update", True))


def set_auto_update(on):
    d = _state()
    d["auto_update"] = bool(on)
    _write_state(d)


# --------------------------------------------------------------------------
# leftovers from previous updates
# --------------------------------------------------------------------------

def sweep_old_exes():
    """Delete the ComicArtCreator_old_*.exe / Setup_old_*.exe left by an
    earlier update. They cannot be removed at swap time (Windows still has
    the running image open) so they are cleared on the next launch, when
    nothing holds them. Returns how many went."""
    if _PROJECT is None:
        return 0
    gone = 0
    for pat in (Path(APP_EXE).stem + "_old_*.exe",
                Path(SETUP_EXE).stem + "_old_*.exe"):
        for stale in _PROJECT.glob(pat):
            try:
                stale.unlink()
                gone += 1
            except OSError:
                pass      # still locked — next launch gets it
    return gone


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------

class Update:
    """One available release."""

    def __init__(self, tag, zip_url, size, notes, published):
        self.tag = tag
        self.zip_url = zip_url
        self.size = size              # bytes, 0 when GitHub did not say
        self.notes = notes or ""
        self.published = published or ""

    @property
    def size_mb(self):
        return self.size / (1024 * 1024) if self.size else 0


def check(current_version, include_skipped=False):
    """Return an Update when GitHub's latest release is newer than the
    running build, else None.

    include_skipped=True ignores a remembered "skip this version" — that is
    what the manual Check for updates button passes, so a skipped release
    stays reachable.

    Every failure path (no network, rate limit, odd payload) returns None:
    an update check must never be able to stop the app from starting.
    """
    try:
        r = requests.get(RELEASES_API, timeout=20,
                         headers={"Accept": "application/vnd.github+json"})
        if not r.ok:
            return None
        j = r.json()
    except Exception:
        return None
    tag = j.get("tag_name") or ""
    if version_tuple(tag) <= version_tuple(current_version):
        return None
    if not include_skipped and tag == skipped_version():
        return None
    asset = next((a for a in j.get("assets", [])
                  if (a.get("name") or "").lower().endswith(".zip")), None)
    if not asset or not asset.get("browser_download_url"):
        return None
    return Update(tag, asset["browser_download_url"],
                  asset.get("size") or 0, j.get("body"),
                  (j.get("published_at") or "")[:10])


def startup_check(current_version):
    """The launch-time check. With automatic updates on, a remembered
    "skip this version" is not honoured — automatic means the latest,
    always; the skip only means something in manual mode."""
    return check(current_version, include_skipped=auto_update())


# --------------------------------------------------------------------------
# the download + swap
# --------------------------------------------------------------------------

class _Cancelled(Exception):
    """The user pressed Cancel mid-download."""


def _make_tmp():
    """A fresh temp folder with a name no earlier run used, made with
    exist_ok. A leftover an earlier run could not delete (five .pyc files
    once blocked every installer in the app for weeks with "Cannot create
    a file when that file already exists") must never fail this one."""
    for n in range(1, 50):
        p = _PROJECT / ("_app_upd_tmp_%d_%d" % (os.getpid(), n))
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
            if p.exists():
                continue          # something in there will not go
        p.mkdir(parents=True, exist_ok=True)
        return p
    raise RuntimeError("could not create a temp folder under " + str(_PROJECT))


def _download_and_unpack(upd, tmp, status, progress, cancel):
    zpath = tmp / "release.zip"
    status("Downloading the new version…")
    done = 0
    with requests.get(upd.zip_url, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or upd.size or 0)
        with open(zpath, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                if cancel.is_set():
                    raise _Cancelled()
                fh.write(chunk)
                done += len(chunk)
                progress(done, total)
    status("Unpacking…")
    root = tmp.resolve()
    with zipfile.ZipFile(zpath) as z:
        # a zip entry must not be able to write outside the temp folder
        for m in z.namelist():
            if not str((tmp / m).resolve()).startswith(str(root)):
                raise RuntimeError("the release zip has a bad path: " + m)
        z.extractall(tmp)


class Staged:
    """A downloaded and verified release that is NOT installed yet. The
    automatic window holds one of these while it asks; Not now discards
    it and nothing on disk has changed."""

    def __init__(self, tmp, new_app, new_setup):
        self.tmp = tmp
        self.new_app = new_app
        self.new_setup = new_setup

    def discard(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def stage_update(upd, current_version, status, progress, cancel):
    """Download the release and verify it. Nothing outside the temp folder
    changes. Returns a Staged for install_staged(); raises on any problem
    (the temp folder is removed)."""
    tmp = _make_tmp()
    try:
        _download_and_unpack(upd, tmp, status, progress, cancel)

        status("Checking the download…")
        new_app = next(tmp.rglob(APP_EXE), None)
        if not new_app:
            raise RuntimeError("the release zip has no " + APP_EXE)
        got = exe_version(new_app)
        if got is None:
            raise RuntimeError("the downloaded app is not a valid Windows "
                               "program (the download may be damaged)")
        if got <= version_tuple(current_version):
            raise RuntimeError(
                "the downloaded app reports v"
                + ".".join(str(n) for n in got)
                + ", which is not newer than the v" + current_version
                + " you are running — the release looks mis-tagged")
        new_setup = next(tmp.rglob(SETUP_EXE), None)
        return Staged(tmp, new_app, new_setup)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def install_staged(staged, status):
    """Swap the verified exes in, refresh the data files the release ships,
    and return the exe to relaunch. Raises on any problem, having first
    put back whatever it moved. The staged folder is removed either way."""
    tmp, new_app, new_setup = staged.tmp, staged.new_app, staged.new_setup
    try:
        status("Installing…")
        moved = []          # (live path, renamed-aside path) for roll-back
        try:
            for name, new in ((APP_EXE, new_app), (SETUP_EXE, new_setup)):
                if not new:
                    continue
                cur = _PROJECT / name
                aside = None
                if cur.exists():
                    aside = cur.with_name(
                        cur.stem + "_old_" + str(os.getpid()) + ".exe")
                    try:
                        aside.unlink()
                    except OSError:
                        pass
                    # Windows will not overwrite the image it is running,
                    # but it will happily rename it out of the way
                    os.replace(cur, aside)
                shutil.copy2(new, cur)
                moved.append((cur, aside))
        except OSError as e:
            for cur, aside in reversed(moved):
                try:
                    if cur.exists():
                        cur.unlink()
                    if aside and aside.exists():
                        os.replace(aside, cur)
                except OSError:
                    pass
            raise RuntimeError("could not install the update: " + str(e)
                               + ". Your existing version was put back.")

        _refresh_data_files(tmp, status)
        return _PROJECT / APP_EXE
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def apply_update(upd, current_version, status, progress, cancel):
    """Download, verify, swap, refresh, in one go — the manual window's
    Update now, where pressing the button was the approval."""
    return install_staged(stage_update(upd, current_version, status,
                                       progress, cancel), status)


def _refresh_data_files(tmp, status):
    """Copy the release's docs and model list over the installed copies.

    Without this an exe-only update leaves them frozen at whatever version
    first installed the app — which is how the update check itself went
    stale and stopped offering newly added models.
    """
    n = 0
    for rel in REFRESH_FILES:
        src = next(tmp.rglob(Path(rel).name), None)
        if not src or not src.is_file():
            continue
        dest = _PROJECT / rel
        try:
            if dest.exists() and dest.read_bytes() == src.read_bytes():
                continue
            if src.suffix == ".json":
                # never put a broken file over a working one
                json.loads(src.read_text(encoding="utf-8"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            n += 1
        except Exception:
            continue      # a doc that will not copy is not worth failing on
    if n:
        status("Refreshed " + str(n) + " bundled file(s).")


# --------------------------------------------------------------------------
# the window
# --------------------------------------------------------------------------

# a **bold** or *italic* span may wrap onto the next line in the release
# body, so these run over the whole text rather than line by line — the
# first version stripped per line and left stray ** on wrapped spans. A
# single newline is allowed inside a span, a blank line is not, so an
# unclosed marker cannot swallow the rest of the notes.
_INLINE = ((r"\*\*((?:[^*\n]|\n(?!\n))+?)\*\*", r"\1"),      # bold
           (r"(?<!\*)\*(?!\*)((?:[^*\n]|\n(?!\n))+?)\*(?!\*)", r"\1"),
           (r"__((?:[^_\n]|\n(?!\n))+?)__", r"\1"),
           (r"`([^`]+)`", r"\1"),                              # inline code
           (r"\[([^\]]+)\]\([^)]+\)", r"\1"))                  # links


def _plain_text(md):
    """GitHub release bodies are markdown. Flatten them enough to read in a
    Tk Text widget: headings lose their #, list markers become bullets, and
    inline code / emphasis / links lose their punctuation."""
    lines = []
    for line in (md or "").replace("\r\n", "\n").split("\n"):
        line = line.rstrip()
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        line = re.sub(r"^\s{0,3}>\s?", "", line)            # block quotes
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
            line = "─" * 40                                  # a --- rule
        else:
            line = re.sub(r"^(\s*)[-*+]\s+", r"\1•  ", line)
            line = re.sub(r"^(\s*)(\d+)\.\s+", r"\1\2.  ", line)
        lines.append(line)
    text = "\n".join(lines)
    for pat, rep in _INLINE:
        text = re.sub(pat, rep, text)
    return text.strip() or "No release notes were published for this version."


class UpdateWindow(Toplevel):
    """The update window: what is new, and the user's choices.

    Manual mode (auto=False): Update now downloads and installs, showing
    progress in place, and relaunches when done — pressing it was the
    approval; Skip this version is remembered so the startup check stays
    quiet until there is a newer one still; Continue just closes and leaves
    the install alone.

    Automatic mode (auto=True, the startup default): the window opens on
    its own and asks — Download and install, Not now (nothing downloaded;
    asked again next launch), or Skip this version. After a yes it
    downloads, verifies and installs (the main window stays usable), then
    asks Restart now / Later. Nothing is downloaded without the yes.

    A checkbox in both modes turns automatic updates on or off.
    """

    def __init__(self, parent, upd, current_version, theme,
                 on_relaunch, on_status=None, auto=False):
        super().__init__(parent)
        self.upd = upd
        self.current_version = current_version
        self.on_relaunch = on_relaunch
        self.on_status = on_status or (lambda s: None)
        self.auto = bool(auto)
        self.cancel = threading.Event()
        self._busy = False
        self._done = False
        self._newexe = None
        self._staged = None      # the verified download, awaiting a yes

        bg = theme.get("bg", "#17171c")
        bg2 = theme.get("bg2", "#20202a")
        fg = theme.get("fg", "#e8e8f0")

        self.title("Update to " + upd.tag if self.auto
                   else "Update available")
        self.configure(bg=bg)
        self.transient(parent)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._continue)

        pad = ttk.Frame(self, padding=16)
        pad.grid(row=0, column=0, sticky="nsew")
        pad.columnconfigure(0, weight=1)
        r = 0

        ttk.Label(pad, text="Comic Book Art Creator " + upd.tag,
                  style="Head.TLabel").grid(row=r, column=0, sticky="w")
        r += 1
        sub = "You have v" + current_version
        if upd.size_mb:
            sub += "        Download %.0f MB" % upd.size_mb
        if upd.published:
            sub += "        Released " + upd.published
        ttk.Label(pad, text=sub, style="Dim.TLabel").grid(
            row=r, column=0, sticky="w", pady=(2, 10))
        r += 1

        ttk.Label(pad, text="What's new").grid(row=r, column=0, sticky="w")
        r += 1
        notes = ttk.Frame(pad)
        notes.grid(row=r, column=0, sticky="nsew", pady=(2, 10))
        notes.columnconfigure(0, weight=1)
        r += 1
        self.notes = Text(notes, width=68, height=14, wrap=WORD,
                          bg=bg2, fg=fg, relief="flat", padx=10, pady=8,
                          highlightthickness=0, font=("Segoe UI", 9))
        self.notes.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(notes, orient="vertical",
                           command=self.notes.yview)
        self.notes.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")
        self.notes.insert(END, _plain_text(upd.notes))
        self.notes.configure(state=DISABLED)

        prow = ttk.Frame(pad)
        prow.grid(row=r, column=0, sticky="ew")
        prow.columnconfigure(0, weight=1)
        r += 1
        self.bar = ttk.Progressbar(prow, mode="determinate")
        self.bar.grid(row=0, column=0, sticky="ew")
        self.prog_var = StringVar(value="")
        ttk.Label(prow, textvariable=self.prog_var, width=16,
                  style="Dim.TLabel").grid(row=0, column=1, padx=(8, 0))

        self.msg_var = StringVar(value="")
        ttk.Label(pad, textvariable=self.msg_var, style="Dim.TLabel",
                  wraplength=560, justify="left").grid(
            row=r, column=0, sticky="w", pady=(6, 0))
        r += 1

        # the switch between the two modes lives in the window itself, so
        # the first automatic update is also where you learn you can stop it
        self.auto_var = BooleanVar(value=auto_update())
        self.auto_box = ttk.Checkbutton(
            pad, text="Download and install updates automatically when the "
                      "app starts (you always approve the restart)",
            variable=self.auto_var, command=self._toggle_auto)
        self.auto_box.grid(row=r, column=0, sticky="w", pady=(10, 0))
        r += 1

        btns = ttk.Frame(pad)
        btns.grid(row=r, column=0, sticky="ew", pady=(14, 0))
        self.update_btn = ttk.Button(
            btns, text="Download and install" if self.auto else "Update now",
            style="Go.TButton", command=self._start)
        self.update_btn.pack(side="left")
        # every mode offers both ways out: this time, or this version
        self.skip_btn = ttk.Button(btns, text="Skip this version",
                                   command=self._skip)
        self.skip_btn.pack(side="left", padx=8)
        self.cont_btn = ttk.Button(btns, text="Not now",
                                   command=self._continue)
        self.cont_btn.pack(side="left")

        self.bind("<Escape>", lambda _e: self._continue())
        self.update_idletasks()
        self._centre(parent)
        if not self.auto:
            try:
                self.grab_set()
            except Exception:
                pass          # a grab is a nicety, never a reason to fail
        self.update_btn.focus_set()
        if self.auto:
            self.msg_var.set("Download and install it now? Not now keeps v"
                             + current_version + " and asks again next time.")

    def _centre(self, parent):
        try:
            w, h = self.winfo_width(), self.winfo_height()
            x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
            self.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        except Exception:
            pass

    # ---------------------------------------------------------- actions
    def _toggle_auto(self):
        set_auto_update(self.auto_var.get())
        self.on_status("Automatic updates "
                       + ("on." if self.auto_var.get() else
                          "off — the app will ask before downloading."))

    def _discard_staged(self):
        if self._staged is not None:
            try:
                self._staged.discard()
            except Exception:
                pass
            self._staged = None

    def _skip(self):
        if self._busy:
            self.cancel.set()
        self._discard_staged()
        skip_version(self.upd.tag)
        self.on_status(self.upd.tag + " skipped — use Check for updates "
                                      "when you want it.")
        self._close()

    def _continue(self):
        """Not now: this time, no. A download in progress is stopped; a
        verified download is thrown away; nothing on disk has changed and
        the question comes back next launch."""
        if self._busy:
            self.cancel.set()
            self.msg_var.set("Stopping…")
            return
        if self._staged is not None:
            self._discard_staged()
            self.on_status("Update to " + self.upd.tag + " skipped for now — "
                           "you will be asked again next time.")
        elif self._newexe is not None:
            self.on_status("Updated to " + self.upd.tag + " — it starts the "
                           "next time you open the app.")
        self._close()

    def _close(self):
        if self._done:
            return
        self._done = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _start(self):
        self._busy = True
        self.update_btn.state(["disabled"])
        self.skip_btn.state(["disabled"])
        self.cont_btn.configure(text="Cancel")
        self.msg_var.set("Downloading — you can keep working." if self.auto
                         else "Starting…")
        threading.Thread(target=self._worker, daemon=True).start()

    def _restart(self):
        """The approval, in automatic mode: install the verified download
        (off the UI thread — a few file copies) and hand over to it."""
        if self._staged is None:
            return
        self._busy = True
        self.update_btn.state(["disabled"])
        self.skip_btn.state(["disabled"])
        self.cont_btn.state(["disabled"])
        self.msg_var.set("Installing…")
        staged, self._staged = self._staged, None

        def work():
            try:
                newexe = install_staged(staged,
                                        lambda s: self.after(0, self.msg_var.set, s))
            except Exception as e:
                self.after(0, self._failed, str(e))
                return
            self.after(0, self._installed, newexe)

        threading.Thread(target=work, daemon=True).start()

    def _installed(self, newexe):
        """Automatic mode: installed after the user's yes — now ask about
        the restart (the running app is untouched until then)."""
        self._busy = False
        clear_skip()
        self._newexe = newexe
        self.msg_var.set("Installed " + self.upd.tag + ". Restart now to "
                         "use it — or Later, and the new version starts the "
                         "next time you open the app.")
        self.update_btn.configure(text="Restart now", command=self._relaunch)
        self.update_btn.state(["!disabled"])
        self.cont_btn.configure(text="Later")
        self.cont_btn.state(["!disabled"])
        self.update_btn.focus_set()
        self.on_status("Updated to " + self.upd.tag
                       + " — restart when you are ready.")

    def _relaunch(self):
        if self._newexe is None:
            return
        self.update_btn.state(["disabled"])
        self.cont_btn.state(["disabled"])
        self.msg_var.set("Restarting…")
        self.on_relaunch(self._newexe, self.upd.tag)

    # the worker runs off the UI thread; every touch of a widget goes back
    # through after(), because Tk is not safe to call from another thread
    def _worker(self):
        def status(s):
            self.after(0, self.msg_var.set, s)

        def progress(done, total):
            def paint():
                mb = 1024 * 1024
                if total:
                    self.bar.configure(maximum=total, value=done)
                    self.prog_var.set("%.0f / %.0f MB"
                                      % (done / mb, total / mb))
                else:
                    self.prog_var.set("%.0f MB" % (done / mb))
            self.after(0, paint)

        try:
            if self.auto:
                staged = stage_update(self.upd, self.current_version,
                                      status, progress, self.cancel)
            else:
                newexe = apply_update(self.upd, self.current_version,
                                      status, progress, self.cancel)
        except _Cancelled:
            self.after(0, self._cancelled)
            return
        except Exception as e:
            self.after(0, self._failed, str(e))
            return
        if self.auto:
            self.after(0, self._ready, staged)
        else:
            self.after(0, self._succeeded, newexe)

    def _cancelled(self):
        self._busy = False
        self.msg_var.set("")
        self.on_status("Update cancelled — you are still on v"
                       + self.current_version + ".")
        self._close()

    def _ready(self, staged):
        """Automatic mode: downloaded and verified after the user's yes —
        install straight away (the yes covered it), then ask to restart."""
        self._busy = False
        self._staged = staged
        self._restart()

    def _failed(self, err):
        self._busy = False
        self.bar.configure(value=0)
        self.prog_var.set("")
        if self.auto:
            self.update_btn.configure(text="Retry", command=self._start)
        self.update_btn.state(["!disabled"])
        self.skip_btn.state(["!disabled"])
        self.cont_btn.configure(text="Not now")
        self.cont_btn.state(["!disabled"])
        self.msg_var.set("Update failed: " + err + "\nYou can keep working, "
                         "or download it yourself from " + RELEASES_PAGE)
        self.on_status("Update failed: " + err)

    def _succeeded(self, newexe):
        """Manual mode: Update now was the approval — installed, relaunch."""
        self._busy = False
        clear_skip()
        self._newexe = newexe
        self.msg_var.set("Updated to " + self.upd.tag + ". Restarting…")
        self.cont_btn.state(["disabled"])
        self.on_relaunch(newexe, self.upd.tag)
