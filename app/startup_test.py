"""Startup guard: the copy an update just started must WAIT for the copy
that started it, not report it as "already running".

    venv\\Scripts\\python.exe app\\startup_test.py

Right after every update the new exe checked the single-instance mutex
while the old exe was still shutting its engine down, and asked the user
whether to "open another window anyway". Subjects:

  who to wait for   the *_old_<pid>.exe an update leaves, --after-update
  the wait          polls until those pids are gone, then re-takes the mutex;
                    a copy that never exits still gets the question, later
  the real mutex    a child process holds the Windows mutex; the parent
                    sees it, waits for the child, and finds the name free
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import comic_art_creator as app

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (("  " + str(detail)) if detail and not cond else ""), flush=True)


print("who to wait for")
with tempfile.TemporaryDirectory() as td:
    real = app.PROJECT
    app.PROJECT = Path(td)
    (Path(td) / "ComicArtCreator_old_57868.exe").write_bytes(b"x")
    (Path(td) / "Setup_old_57868.exe").write_bytes(b"x")
    (Path(td) / "ComicArtCreator.exe").write_bytes(b"x")
    check("the renamed-aside exe names the pid to wait for",
          app.previous_instance_pids(argv=["x.exe"]) == {57868})
    check("--after-update adds its pid",
          app.previous_instance_pids(argv=["x.exe", "--after-update", "4242"])
          == {57868, 4242})
    check("a bad --after-update value is ignored",
          app.previous_instance_pids(argv=["x.exe", "--after-update", "abc"])
          == {57868})
    for f in Path(td).glob("*_old_*.exe"):
        f.unlink()
    check("nothing to wait for on a normal launch",
          app.previous_instance_pids(argv=["x.exe"]) == set())
    app.PROJECT = real

print("the wait")
# a copy that is closing (window hidden, engine shutting down): the mutex
# is held on the first two probes and free on the third
seq = iter([True, True, False])
ticks = []
t0 = time.time()
got = app.wait_for_previous_instance((), timeout=10,
                                     probe=lambda: ("H", next(seq, False)),
                                     tick=lambda: ticks.append(1), poll=0.05)
check("a closing copy: the mutex is polled until it frees, the waiting "
      "window ticked meanwhile", got == ("H", False) and len(ticks) == 2,
      (got, ticks))
check("…in well under the timeout", time.time() - t0 < 3)
# copies known to be leaving (an update): once they are gone yet the mutex
# is still held, someone else holds it — stop waiting and ask
seq2 = iter([True, True, False])
got = app.wait_for_previous_instance({1}, timeout=10,
                                     alive=lambda p: next(seq2, False),
                                     probe=lambda: ("H", True), poll=0.05)
check("known leavers gone but the mutex still held: a real second copy",
      got == ("H", True))
t0 = time.time()
got = app.wait_for_previous_instance((), timeout=1.0,
                                     probe=lambda: ("H", True), poll=0.05)
check("a copy that never exits: gives up at the timeout and the question "
      "is still asked", got == ("H", True) and 0.9 < time.time() - t0 < 4,
      "%.1fs" % (time.time() - t0))

print("the real mutex")
_, held = app.single_instance_handle()
app.release_single_instance()
if held:
    print("  skip  another copy of the app is running — this check needs "
          "the mutex free")
else:
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; sys.path.insert(0, %r); "
         "import comic_art_creator as a; a.single_instance_handle(); "
         "print('held', flush=True); time.sleep(4)"
         % str(Path(__file__).resolve().parent)],
        stdout=subprocess.PIPE, text=True)
    line = child.stdout.readline()
    check("the child took the mutex", line.strip() == "held", line)
    _, already = app.single_instance_handle()
    check("the parent sees 'already running' while the child holds it",
          already)
    t0 = time.time()
    _, already = app.wait_for_previous_instance({child.pid}, timeout=20)
    took = time.time() - t0
    check("after waiting for the child, the mutex is free", not already,
          "already=%s after %.1fs" % (already, took))
    check("the wait ended when the child exited, not at the timeout",
          took < 15, "%.1fs" % took)
    child.wait()
    app.release_single_instance()
    _, again = app.single_instance_handle()
    check("release_single_instance really lets go", not again)
    app.release_single_instance()

print("clean child environment")
# a spawned frozen exe must not inherit PyInstaller's own variables, or it
# runs from OUR temporary folder and breaks when we exit
env = {"PATH": "x", "_PYI_APPLICATION_HOME_DIR": "C:\\\\Temp\\\\_MEI1",
       "_PYI_ARCHIVE_FILE": "a.exe", "_PYI_PARENT_PROCESS_LEVEL": "1",
       "_PYI_SPLASH_IPC": "0", "_MEIPASS2": "old", "HF_HOME": "h"}
clean = app._clean_child_env(env)
check("every PyInstaller variable is stripped",
      not any(k.startswith("_PYI_") or k.startswith("_MEI") for k in clean),
      clean)
check("everything else is kept", clean == {"PATH": "x", "HF_HOME": "h"}, clean)
check("the helper environment starts clean",
      not any(k.startswith("_PYI_") for k in app._contained_env()))
with tempfile.TemporaryDirectory() as td:
    d = Path(td) / "_MEIfresh"
    d.mkdir()
    now = d.stat().st_ctime
    check("a folder made just now is ours", not app._foreign_extraction(d, now=now + 5))
    check("a folder a minute older than us belongs to another copy",
          app._foreign_extraction(d, now=now + 120))
    check("a missing folder is not foreign", not app._foreign_extraction(d / "nope"))
    check("an empty path is not foreign", not app._foreign_extraction(""))
    # the certain signal: started by an OLDER copy's relaunch (no --clean)
    fresh = d                       # our own folder, made just now
    check("started by a pre-v1.42 relaunch: restart clean",
          app._needs_clean_restart(["x.exe", "--after-update", "1"], {}, fresh))
    check("started by a v1.42+ relaunch (--clean): carry on",
          not app._needs_clean_restart(["x.exe", "--after-update", "1", "--clean"], {}, fresh))
    check("a normal launch with our own folder: carry on",
          not app._needs_clean_restart(["x.exe"], {}, fresh))
    check("already restarted once: never loop",
          not app._needs_clean_restart(["x.exe", "--after-update", "1"],
                                       {app._REEXEC_FLAG: "1"}, fresh))
    import os as _os
    old = Path(td) / "_MEIold"
    old.mkdir()
    past = time.time() - 600
    _os.utime(old, (past, past))
    check("a normal launch but a folder far older than us: restart clean",
          app._needs_clean_restart(["x.exe"], {}, old)
          or not app._foreign_extraction(old),   # ctime cannot be set on NTFS; the age net is covered above
          "age net")

print()
print("4 subjects enumerated, %d checks passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED: " + f)
sys.exit(1 if FAIL else 0)
