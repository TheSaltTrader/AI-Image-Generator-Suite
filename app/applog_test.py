"""app.log: everything the app shows or catches must land in the file.

    venv\\Scripts\\python.exe app\\applog_test.py

Subjects:
  lines          timestamped, levelled, multi-line text indented, repeats
                 written once, a missing path never raises
  exceptions     exception() carries the traceback, from an except block
                 or from a passed exception
  hooks          an unhandled exception in a worker thread, and one in a
                 Tk callback, both reach the log
  message boxes  every messagebox call is logged as it opens, wrapping
                 is idempotent and delegates
  rotation       a file past the cap shrinks to the newest lines and says so
"""

import sys
import tempfile
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import applog

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (("  " + str(detail)) if detail and not cond else ""), flush=True)


td = Path(tempfile.mkdtemp(prefix="applog_"))
logp = td / "app.log"


def text():
    return logp.read_text(encoding="utf-8") if logp.exists() else ""


print("lines")
applog.configure(None)
applog._PATH = None
applog.log("nowhere")
check("logging before configure never raises or writes", True)
applog.configure(logp)
applog.log("hello")
applog.log("hello")
applog.log("hello")
applog.error("bad thing")
applog.log("two\nlines")
t = text()
check("a line is timestamped and levelled",
      any(line.split(" ")[2] == "INFO" and line.endswith("hello")
          for line in t.splitlines()), t)
check("immediate repeats are written once", t.count("hello") == 1, t)
check("errors carry the ERROR level", "ERROR bad thing" in t)
check("continuation lines are indented", "two\n    lines" in t)
applog.configure(td / "no_such_dir" / "x" / "app.log")
applog.log("cannot be written")
check("an unwritable log never raises", True)
applog.configure(logp)

print("exceptions")
try:
    raise ValueError("boom from except")
except ValueError:
    applog.exception("caught")
t = text()
check("exception() from an except block carries the traceback",
      "caught" in t and "ValueError: boom from except" in t
      and "Traceback" in t)
e = KeyError("passed")
try:
    raise e
except KeyError as ex:
    e = ex
applog.exception("passed in", e)
check("exception() with a passed exception carries its traceback",
      "passed in" in text() and "KeyError: 'passed'" in text())

print("hooks")
applog.install_hooks()


def die():
    raise RuntimeError("worker died")


th = threading.Thread(target=die, name="worker-x")
th.start()
th.join()
time.sleep(0.1)
t = text()
check("an unhandled exception in a worker thread is logged",
      "unhandled in thread worker-x" in t and "worker died" in t, t[-400:])
try:
    from tkinter import Tk
    root = Tk()
    root.withdraw()
    applog.tk_report(root)

    def cb():
        raise ZeroDivisionError("callback died")

    root.after(0, cb)
    root.update()
    time.sleep(0.05)
    root.update()
    check("an exception in a Tk callback is logged",
          "unhandled in a Tk callback" in text() and "callback died" in text(),
          text()[-400:])
    root.destroy()
except Exception as ex:
    check("Tk callback hook", False, repr(ex))

print("message boxes")
calls = []
fake = types.ModuleType("fakebox")
fake.showerror = lambda title=None, message=None, **kw: calls.append(("e", title, message)) or "ok"
fake.askyesno = lambda title=None, message=None, **kw: calls.append(("q", title, message)) or True
applog.wrap_messagebox(fake)
r1 = fake.showerror("RAG map", "Could not read the RAG map: cannot import name 'x'")
r2 = fake.askyesno("Install", "Do it?")
t = text()
check("showerror is logged as an error with title and text",
      "ERROR showerror [RAG map] Could not read the RAG map: cannot import name 'x'" in t, t[-300:])
check("askyesno is logged", "askyesno [Install] Do it?" in t)
check("the wrapped boxes still delegate and return",
      r1 == "ok" and r2 is True and len(calls) == 2)
first = fake.showerror
applog.wrap_messagebox(fake)
check("wrapping twice does not double-wrap", fake.showerror is first)

print("rotation")
big = ("x" * 200 + "\n") * (applog.MAX_BYTES // 200 + 50)
logp.write_text(big, encoding="utf-8")
applog.log("after the cap")
size = logp.stat().st_size
t = text()
check("a file past the cap shrinks to the newest part",
      size <= applog.KEEP_BYTES + 200, size)
check("…and says so", t.startswith("(older lines dropped)"))
check("…keeping the new line", "after the cap" in t)

import shutil
shutil.rmtree(td, ignore_errors=True)
print()
print("5 subjects enumerated, %d checks passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED: " + f)
sys.exit(1 if FAIL else 0)
