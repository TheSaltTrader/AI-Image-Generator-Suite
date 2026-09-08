r"""app.log for Comic Book Art Creator.

Every status-bar message, every message box, every error the app shows,
and every exception it catches or fails to catch — timestamped, in one
file next to the exe. A frozen --windowed exe has no console, so until
v1.39 an exception in a worker thread or a Tk callback vanished, and a
problem could only be reported from memory ("it said something about an
import"). Now it is answered from the file.

Rules:
* Logging never raises. A log that cannot be written is not a reason to
  stop the app, so every write is wrapped.
* The file is capped (~3 MB, the newest ~1 MB kept), so it can never
  fill a drive or grow into something nobody opens.
* Imports nothing from comic_art_creator.
"""

import os
import sys
import threading
import time
import traceback
from pathlib import Path

MAX_BYTES = 3 * 1024 * 1024
KEEP_BYTES = 1024 * 1024

_PATH = None
_LOCK = threading.Lock()
_LAST = None


def configure(path):
    """Point the module at the log file. Called once by the app."""
    global _PATH, _LAST
    _PATH = Path(path) if path else None      # None = logging off
    _LAST = None


def path():
    return _PATH


def log(msg, level="INFO"):
    """Append one timestamped line. Immediate repeats of the same text
    (progress messages) are written once."""
    global _LAST
    if _PATH is None:
        return
    text = str(msg).replace("\r", "")
    line = "%s %-5s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), level,
                             text.replace("\n", "\n    "))
    with _LOCK:
        if (level, text) == _LAST:
            return
        _LAST = (level, text)
        try:
            _rotate()
            with open(_PATH, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(line)
        except Exception:
            pass


def error(msg):
    log(msg, "ERROR")


def exception(msg, exc=None):
    """msg plus the traceback of exc — or of the exception being handled
    when called from an except block."""
    if exc is None:
        tb = traceback.format_exc()
    else:
        tb = "".join(traceback.format_exception(type(exc), exc,
                                                exc.__traceback__))
    log("%s\n%s" % (msg, tb.rstrip()), "ERROR")


def _rotate():
    try:
        if _PATH.exists() and _PATH.stat().st_size > MAX_BYTES:
            data = _PATH.read_bytes()[-KEEP_BYTES:]
            nl = data.find(b"\n")
            _PATH.write_bytes(b"(older lines dropped)\n" + data[nl + 1:])
    except Exception:
        pass


def install_hooks():
    """Unhandled exceptions from anywhere — the main thread, any worker
    thread — go to the log (and still wherever they went before)."""
    prev_sys = sys.excepthook

    def sys_hook(t, v, tb):
        log("unhandled: " + "".join(traceback.format_exception(t, v, tb))
            .rstrip(), "ERROR")
        try:
            prev_sys(t, v, tb)
        except Exception:
            pass

    sys.excepthook = sys_hook
    if hasattr(threading, "excepthook"):
        prev_thr = threading.excepthook

        def thr_hook(args):
            log("unhandled in thread %s: %s"
                % (getattr(args.thread, "name", "?"),
                   "".join(traceback.format_exception(
                       args.exc_type, args.exc_value,
                       args.exc_traceback)).rstrip()), "ERROR")
            try:
                prev_thr(args)
            except Exception:
                pass

        threading.excepthook = thr_hook


def tk_report(root):
    """A Tk callback that raises is logged with its traceback, instead of
    printing to a console a windowed exe does not have."""
    def report(t, v, tb):
        log("unhandled in a Tk callback: "
            + "".join(traceback.format_exception(t, v, tb)).rstrip(),
            "ERROR")
    root.report_callback_exception = report


_BOXES = ("showinfo", "showwarning", "showerror", "askyesno", "askokcancel",
          "askretrycancel", "askquestion", "askyesnocancel")


def wrap_messagebox(mb):
    """Log every message box the app shows (title + text) as it opens.
    Wraps the functions on the tkinter.messagebox module itself, so every
    caller — however it imported the module — is covered. Idempotent."""
    for name in _BOXES:
        fn = getattr(mb, name, None)
        if fn is None or getattr(fn, "_applog_wrapped", False):
            continue

        def make(name, fn):
            def wrapped(title=None, message=None, **kw):
                log("%s [%s] %s" % (name, title, message),
                    "ERROR" if name == "showerror" else "INFO")
                return fn(title, message, **kw)
            wrapped._applog_wrapped = True
            wrapped.__name__ = name
            return wrapped

        setattr(mb, name, make(name, fn))


def open_log():
    """Open the log in whatever the system uses for .log files."""
    if _PATH is None or not _PATH.exists():
        return False
    try:
        os.startfile(str(_PATH))
        return True
    except Exception:
        return False
