"""Census of the engine file manager. Run:
    venv\\Scripts\\python.exe app\\engine_files_test.py

Every way the 2026-09 engine-update disaster could recur is a subject here,
and each subject is enumerated before it is tested so what is NOT covered
is counted rather than averaged away:

  temp folders     unique names, exist_ok, a stuck leftover bumps the name
  remove_tree      read-only files go, a locked file is reported not raised
  integrity        a damaged tree is told from a healthy one, and from a log
  update           happy path carries the user's folders and records the sha
  update refused   engine in use / bad archive / pip failure -> untouched
  update rollback  a failure after the rename puts the old tree back
  repair           a damaged tree comes back from the recorded commit
  node install     installs once, tolerates a race, survives a stuck temp
  sweep            old engines and temp folders (old names too) are cleared
  nesting          the v1.34 flatten repair still works
  zip guard        an archive that escapes its folder is refused

Everything runs against throw-away folders and a local HTTP server; nothing
touches the real engine, GitHub, or pip (pip is a .cmd that records its
arguments).
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import engine_files as ef

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (("  " + str(detail)) if detail and not cond else ""))


# ------------------------------------------------------------ test helpers
class _Handler(BaseHTTPRequestHandler):
    files = {}
    hits = []

    def do_GET(self):
        self.hits.append(self.path)
        data = self.files.get(self.path)
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


_srv = HTTPServer(("127.0.0.1", 0), _Handler)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % _srv.server_address[1]


def serve(name, data):
    _Handler.files["/" + name] = data
    return BASE + "/" + name


def make_tree(root, marker, user_items=True):
    """A fake engine that passes the integrity check."""
    root = Path(root)
    for s in ef.SENTINELS:
        p = root / s
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# %s %s\n" % (marker, s), encoding="utf-8")
    (root / "requirements.txt").write_text("requests\n", encoding="utf-8")
    cn = root / "custom_nodes"
    cn.mkdir(exist_ok=True)
    (cn / "websocket_image_save.py").write_text("# stock " + marker,
                                                encoding="utf-8")
    (cn / "example_node.py.example").write_text("x", encoding="utf-8")
    if user_items:
        my = cn / "ComfyUI_IPAdapter_plus"
        my.mkdir()
        (my / "IPAdapterPlus.py").write_text("# the user's node",
                                             encoding="utf-8")
        (root / "input").mkdir()
        (root / "input" / "ref.png").write_bytes(b"\x89PNG fake")
        (root / "user").mkdir()
        (root / "user" / "comfy.settings.json").write_text("{}",
                                                            encoding="utf-8")
    return root


def make_zip(top, marker, damaged=False):
    """Bytes of a GitHub-style archive: one top folder holding the tree."""
    with tempfile.TemporaryDirectory() as td:
        tree = make_tree(Path(td) / top, marker, user_items=False)
        if damaged:
            (tree / "comfy" / "options.py").unlink()
        buf = Path(td) / "a.zip"
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(tree.rglob("*")):
                z.write(p, str(p.relative_to(Path(td))))
        return buf.read_bytes()


def fake_pip(folder, ok=True):
    """A stand-in engine python: records its arguments, exits 0 or 1."""
    log = Path(folder) / "pip_calls.txt"
    cmd = Path(folder) / ("pyok.cmd" if ok else "pybad.cmd")
    body = "@echo off\r\necho %%* >> \"%s\"\r\n" % log
    body += "exit /b 0\r\n" if ok else "echo boom 1>&2\r\nexit /b 1\r\n"
    cmd.write_text(body, encoding="ascii")
    return cmd, log


def marker_of(engine):
    return (Path(engine) / "main.py").read_text(encoding="utf-8")


def fresh_project():
    """A project folder with a live engine, a fake pip and the module
    pointed at it. Returns (project, engine, pip log)."""
    td = Path(tempfile.mkdtemp(prefix="ef_"))
    eng = make_tree(td / "ComfyUI", "LIVE")
    cmd, log = fake_pip(td)
    ef.configure(td, eng, python_exe=cmd, version_file=td / "engine_version.json")
    (td / "engine_version.json").write_text('{"sha": "oldsha"}',
                                            encoding="utf-8")
    return td, eng, log


def status_sink():
    msgs = []
    return msgs, msgs.append


# ------------------------------------------------------------ temp folders
print("temp folders")
with tempfile.TemporaryDirectory() as td:
    ef.configure(td)
    a = ef.make_tmp("x")
    b = ef.make_tmp("x")
    check("two temp folders never share a name", a != b, (a, b))
    check("temp folders live under the project",
          a.parent == Path(td) and b.parent == Path(td))
    check("a temp folder is created empty", a.is_dir() and not any(a.iterdir()))
    # a stuck leftover: a same-named folder holding a file nobody can delete
    ef._counter = iter([1, 2, 3])          # force the next name to be _1
    stuck = Path(td) / ("_tmp_y_%d_1" % os.getpid())
    stuck.mkdir()
    fh = open(stuck / "locked.pyc", "wb")
    fh.write(b"x")
    fh.flush()
    got = ef.make_tmp("y")
    check("a stuck leftover does not fail the next run (name is bumped)",
          got.is_dir() and got != stuck, got)
    fh.close()
    ef._counter = __import__("itertools").count(100)

# ------------------------------------------------------------- remove_tree
print("remove_tree")
with tempfile.TemporaryDirectory() as td:
    t = Path(td) / "tree"
    (t / "sub").mkdir(parents=True)
    ro = t / "sub" / "readonly.txt"
    ro.write_text("x")
    os.chmod(ro, 0o444)
    left = ef.remove_tree(t)
    check("a read-only file is deleted, not reported", not left and not t.exists(),
          left)
    t2 = Path(td) / "tree2"
    (t2 / "a").mkdir(parents=True)
    (t2 / "a" / "free.txt").write_text("x")
    fh = open(t2 / "a" / "held.txt", "w")
    left = ef.remove_tree(t2)
    check("a locked file is reported, everything else still goes",
          len(left) >= 1 and not (t2 / "a" / "free.txt").exists()
          and (t2 / "a" / "held.txt").exists(), left)
    fh.close()
    check("removing a missing tree is a no-op", ef.remove_tree(t2 / "nope") == [])

# --------------------------------------------------------------- integrity
print("integrity")
with tempfile.TemporaryDirectory() as td:
    eng = make_tree(Path(td) / "ComfyUI", "ok")
    check("a healthy tree passes", ef.check_integrity(eng) == [])
    (eng / "comfy" / "options.py").unlink()
    check("the 2026-09 damage (comfy/options.py gone) is caught",
          ef.check_integrity(eng) == ["comfy/options.py"])
    check("a missing engine folder reports every sentinel",
          len(ef.check_integrity(Path(td) / "none")) == len(ef.SENTINELS))
    log = Path(td) / "engine.log"
    log.write_text('Traceback (most recent call last):\n  File "H:\\\\x\\\\'
                   'ComfyUI\\\\main.py", line 1, in <module>\n    import '
                   "comfy.options\nModuleNotFoundError: No module named "
                   "'comfy.options'\n", encoding="utf-8")
    check("the real failure log reads as damage", ef.log_shows_damage(log))
    log.write_text("[INFO] Starting server\n[INFO] To see the GUI go to: "
                   "http://127.0.0.1:8188\n", encoding="utf-8")
    check("a healthy start log does not", not ef.log_shows_damage(log))
    check("a missing log does not", not ef.log_shows_damage(Path(td) / "no.log"))

# ------------------------------------------------------------ update: happy
print("update — happy path")
proj, eng, piplog = fresh_project()
url = serve("new1.zip", make_zip("ComfyUI-new1", "NEW1"))
msgs, status = status_sink()
try:
    out = ef.update_engine("new1", status, zip_url=url)
    check("update returns the engine folder", out == eng)
    check("the engine is the new tree", "NEW1" in marker_of(eng))
    check("the user's custom node came across",
          (eng / "custom_nodes" / "ComfyUI_IPAdapter_plus"
           / "IPAdapterPlus.py").is_file())
    check("the user's input files came across",
          (eng / "input" / "ref.png").is_file())
    check("the user's settings came across",
          (eng / "user" / "comfy.settings.json").is_file())
    check("the fresh engine's own stock node file wins",
          "NEW1" in (eng / "custom_nodes"
                     / "websocket_image_save.py").read_text(encoding="utf-8"))
    check("the new sha is recorded", ef.recorded_sha() == "new1")
    check("pip ran against the staged requirements",
          piplog.exists() and "requirements.txt" in piplog.read_text())
    check("the old tree is gone", not list(proj.glob("ComfyUI_old_*")))
    check("no temp folder is left", not list(proj.glob("_tmp_*")))
    check("the user was told what was carried", any("Carried" in m for m in msgs),
          msgs)
except Exception as e:
    check("update happy path raised", False, repr(e))
shutil.rmtree(proj, ignore_errors=True)

# --------------------------------------------------------- update: refused
print("update — refused, install untouched")
proj, eng, piplog = fresh_project()
msgs, status = status_sink()
held = open(eng / "comfy" / "sd.py", "a")          # the engine "running"
ef_sleep = ef.time.sleep
ef.time.sleep = lambda s: None                     # no 10 s of retries here
try:
    ef.update_engine("new1", status, zip_url=url)
    check("a locked engine folder raises", False)
except RuntimeError as e:
    check("a locked engine folder raises", "in use" in str(e), str(e))
    check("…saying nothing was changed", "Nothing was changed" in str(e))
finally:
    ef.time.sleep = ef_sleep
    held.close()
check("the live tree is untouched after a refused rename",
      "LIVE" in marker_of(eng) and (eng / "custom_nodes"
                                     / "ComfyUI_IPAdapter_plus").is_dir())
check("the sha is untouched", ef.recorded_sha() == "oldsha")
check("no temp folder is left", not list(proj.glob("_tmp_*")))
check("nothing was renamed aside", not list(proj.glob("ComfyUI_old_*")))

bad = serve("bad.zip", make_zip("ComfyUI-bad", "BAD", damaged=True))
try:
    ef.update_engine("bad", status, zip_url=bad)
    check("an incomplete archive raises", False)
except RuntimeError as e:
    check("an incomplete archive raises", "incomplete" in str(e), str(e))
check("the live tree is untouched after a bad archive", "LIVE" in marker_of(eng))
check("the sha is untouched", ef.recorded_sha() == "oldsha")

empty = serve("empty.zip", (lambda: (lambda b: (zipfile.ZipFile(b, "w")
                                                 .close(), b.getvalue())[1])
                            (__import__("io").BytesIO()))())
try:
    ef.update_engine("e", status, zip_url=empty)
    check("an archive with no folder raises", False)
except RuntimeError as e:
    check("an archive with no folder raises", "no ComfyUI folder" in str(e),
          str(e))

badpy, _ = fake_pip(proj, ok=False)
ef.configure(proj, eng, python_exe=badpy, version_file=proj / "engine_version.json")
try:
    ef.update_engine("new1", status, zip_url=url)
    check("a pip failure aborts a strict update", False)
except RuntimeError as e:
    check("a pip failure aborts a strict update",
          "packages did not install" in str(e) and "boom" in str(e), str(e))
check("the live tree is untouched after a pip failure", "LIVE" in marker_of(eng))
msgs, status = status_sink()
ef.update_engine("new1", status, zip_url=url, strict_pip=False)
check("a non-strict update proceeds past a pip failure and says so",
      "NEW1" in marker_of(eng) and any("Note:" in m for m in msgs), msgs)
shutil.rmtree(proj, ignore_errors=True)

# -------------------------------------------------------- update: rollback
print("update — rollback after the rename")
proj, eng, piplog = fresh_project()
msgs, status = status_sink()
real_copy = ef.copy_preserved


def _boom(*a, **k):
    raise OSError("disk full while copying custom_nodes")


ef.copy_preserved = _boom
try:
    ef.update_engine("new1", status, zip_url=url)
    check("a failure after the rename raises", False)
except RuntimeError as e:
    check("a failure after the rename raises",
          "put back" in str(e) and "disk full" in str(e), str(e))
finally:
    ef.copy_preserved = real_copy
check("the OLD tree is back in place", "LIVE" in marker_of(eng))
check("the user's node is still there",
      (eng / "custom_nodes" / "ComfyUI_IPAdapter_plus"
       / "IPAdapterPlus.py").is_file())
check("the sha is untouched", ef.recorded_sha() == "oldsha")
check("no old/failed copies are left",
      not list(proj.glob("ComfyUI_old_*")) and not list(proj.glob("ComfyUI_failed_*")),
      list(proj.glob("ComfyUI_*")))
check("no temp folder is left", not list(proj.glob("_tmp_*")))
shutil.rmtree(proj, ignore_errors=True)

# ------------------------------------------------------------------ repair
print("repair")
proj, eng, piplog = fresh_project()
(proj / "engine_version.json").write_text('{"sha": "abc123"}', encoding="utf-8")
# the 2026-09 damage: packages emptied, custom_nodes deleted
for s in ("comfy/options.py", "comfy/samplers.py"):
    (eng / s).unlink()
shutil.rmtree(eng / "custom_nodes")
check("the damaged tree fails integrity", ef.check_integrity(eng) != [])
repaired = serve("abc123.zip", make_zip("ComfyUI-abc123", "REPAIRED"))
real_url = ef.comfy_zip_url
ef.comfy_zip_url = lambda sha=None: BASE + "/" + (sha or "master") + ".zip"
badpy, _ = fake_pip(proj, ok=False)
ef.configure(proj, eng, python_exe=badpy, version_file=proj / "engine_version.json")
msgs, status = status_sink()
try:
    ef.repair_engine(status)
    check("repair rebuilds from the recorded commit", "REPAIRED" in marker_of(eng))
    check("the repaired tree passes integrity", ef.check_integrity(eng) == [])
    check("the user's input survived the repair", (eng / "input" / "ref.png").is_file())
    check("repair tolerates a pip failure", any("Note:" in m for m in msgs))
    check("the recorded sha is kept", ef.recorded_sha() == "abc123")
except Exception as e:
    check("repair raised", False, repr(e))
finally:
    ef.comfy_zip_url = real_url
shutil.rmtree(proj, ignore_errors=True)

# ----------------------------------------------------------- node install
print("node install")
proj, eng, piplog = fresh_project()
with tempfile.TemporaryDirectory() as td:
    src = Path(td) / "ComfyUI_Fake-main"
    src.mkdir()
    (src / "__init__.py").write_text("# node", encoding="utf-8")
    buf = Path(td) / "n.zip"
    with zipfile.ZipFile(buf, "w") as z:
        z.write(src / "__init__.py", "ComfyUI_Fake-main/__init__.py")
    nurl = serve("node.zip", buf.read_bytes())
node_dir = eng / "custom_nodes" / "ComfyUI_Fake"
_Handler.hits.clear()
check("a node installs", ef.install_node_zip(nurl, node_dir) is True
      and (node_dir / "__init__.py").is_file())
check("the archive's folder name is not kept", not (eng / "custom_nodes"
                                                     / "ComfyUI_Fake-main").exists())
n_hits = len(_Handler.hits)
check("an already-installed node is not downloaded again",
      ef.install_node_zip(nurl, node_dir) is False and len(_Handler.hits) == n_hits)
check("no temp folder is left", not list(proj.glob("_tmp_*")))
# a stuck temp leftover from an earlier run must not stop the install
stuck = proj / ("_tmp_node_%d_1" % os.getpid())
stuck.mkdir()
fh = open(stuck / "ipa.zip", "wb")
ef._counter = iter([1, 2, 3, 4])
shutil.rmtree(node_dir)
check("a stuck temp leftover does not stop a node install",
      ef.install_node_zip(nurl, node_dir) is True and (node_dir / "__init__.py").is_file())
fh.close()
shutil.rmtree(stuck)               # the test's own prop, not a module leftover
ef._counter = __import__("itertools").count(200)
# two installers at once (boot-time heal + the user's click)
shutil.rmtree(node_dir)
results, errors = [], []


def _race():
    try:
        results.append(ef.install_node_zip(nurl, node_dir))
    except Exception as e:
        errors.append(e)


ts = [threading.Thread(target=_race) for _ in range(2)]
[t.start() for t in ts]
[t.join() for t in ts]
check("two concurrent installs: no error, the node is there",
      not errors and (node_dir / "__init__.py").is_file(), errors)
check("no temp folder is left after the race", not list(proj.glob("_tmp_*")))
shutil.rmtree(proj, ignore_errors=True)

# ------------------------------------------------------------------- sweep
print("sweep")
proj, eng, piplog = fresh_project()
for name in ("ComfyUI_old_20260907_210700", "ComfyUI_failed_x", "_tmp_engine_1_1",
             "_upd_tmp", "_app_upd_tmp"):
    d = proj / name
    (d / "keep" / "custom_nodes" / "x" / "__pycache__").mkdir(parents=True)
    (d / "keep" / "custom_nodes" / "x" / "__pycache__" / "a.pyc").write_bytes(b"x")
(proj / "_tmp_stuck_1_1").mkdir()
fh = open(proj / "_tmp_stuck_1_1" / "held.zip", "wb")
gone, stuck = ef.sweep_leftovers()
check("old engines and every temp-folder name are cleared", gone == 5, gone)
check("the pre-v1.36 shared temp folder is among them",
      not (proj / "_upd_tmp").exists() and not (proj / "_app_upd_tmp").exists())
check("a folder that will not go is reported, not raised",
      len(stuck) >= 1 and (proj / "_tmp_stuck_1_1").exists(), stuck)
check("the live engine is never swept", eng.is_dir() and "LIVE" in marker_of(eng))
fh.close()
shutil.rmtree(proj, ignore_errors=True)

# ----------------------------------------------------------------- nesting
print("nesting repair (v1.34)")
with tempfile.TemporaryDirectory() as td:
    eng = make_tree(Path(td) / "ComfyUI", "x", user_items=False)
    buried = eng / "custom_nodes" / "custom_nodes" / "custom_nodes"
    (buried / "MyNode").mkdir(parents=True)
    (buried / "MyNode" / "__init__.py").write_text("# buried", encoding="utf-8")
    (buried / "websocket_image_save.py").write_text("stock", encoding="utf-8")
    n = ef.flatten_nested_dir(eng, "custom_nodes", dedupe=True)
    check("a buried node is hoisted to the top",
          n == 1 and (eng / "custom_nodes" / "MyNode" / "__init__.py").is_file(), n)
    check("the empty shells are dropped",
          not (eng / "custom_nodes" / "custom_nodes").exists())
    check("a flat folder is a no-op", ef.flatten_nested_dir(eng, "custom_nodes") == 0)

# --------------------------------------------------------------- zip guard
print("zip guard")
with tempfile.TemporaryDirectory() as td:
    evil = Path(td) / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../escaped.txt", "x")
    try:
        ef.extract_zip(evil, Path(td) / "out")
        check("an archive that escapes its folder is refused", False)
    except RuntimeError as e:
        check("an archive that escapes its folder is refused", "bad path" in str(e))
    check("nothing was written outside", not (Path(td) / "escaped.txt").exists())

# ----------------------------------------------------------------- census
SUBJECTS = ["temp folders", "remove_tree", "integrity", "update — happy path",
            "update — refused, install untouched",
            "update — rollback after the rename", "repair", "node install",
            "sweep", "nesting repair (v1.34)", "zip guard"]
print()
print("%d subjects enumerated, %d checks passed, %d failed"
      % (len(SUBJECTS), len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED: " + f)
_srv.shutdown()
sys.exit(1 if FAIL else 0)
