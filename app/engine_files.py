r"""Engine files for Comic Book Art Creator: updating the ComfyUI engine,
repairing a damaged one, installing add-on nodes, and the temp-folder
discipline all three share.

Why a module. On 2026-09-07 an engine update on a user's install ran
`shutil.rmtree` on the LIVE engine folder and died partway. Python 3.12's
rmtree walks bottom-up and the first error stops it, so what remained was a
tree of empty package folders behind a main.py that could no longer
`import comfy`. The same function's `finally:` clause then deleted the temp
folder it had moved the user's custom_nodes / input / user folders into —
the folders the update was preserving went with the update that failed.
Five .pyc files that would not delete then made every later user of that
temp folder (the engine updater, the IP-Adapter installer) fail on `mkdir`
with "Cannot create a file when that file already exists" for three weeks.

Rules this module follows:

* The live engine is never deleted in place. An update is a RENAME: the
  live folder goes aside as ComfyUI_old_<stamp>, the staged tree is renamed
  in. A rename happens whole or not at all — a locked folder means nothing
  changed, and the caller is told so.
* The user's folders (custom_nodes, input, user) are never put under a temp
  folder. They stay in the renamed-aside old tree and are COPIED into the
  new one; if that copy fails the swap rolls back and the old tree, still
  complete, goes back where it was.
* Everything that can fail is done before the swap: the download, the zip,
  the staged tree's integrity check, the pip requirements. After the rename
  only the copy of the user's folders remains, and that rolls back.
* Temp folders are unique per run (pid + counter) and made with exist_ok, so
  a leftover a previous run could not delete never stops the next run.
* Cleanup never raises and never hides: every path that would not delete is
  returned to the caller.
* Nothing here imports comic_art_creator. The app calls configure() once and
  passes callbacks, so there is no import cycle and the module is testable
  against a throw-away folder.
"""

import itertools
import json
import os
import shutil
import stat
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import requests

COMFY_ZIP_URL = ("https://github.com/comfyanonymous/ComfyUI/"
                 "archive/refs/heads/master.zip")
COMFY_COMMITS_API = ("https://api.github.com/repos/comfyanonymous/"
                     "ComfyUI/commits/master")
IPA_NODE_ZIP = ("https://github.com/cubiq/ComfyUI_IPAdapter_plus/"
                "archive/refs/heads/main.zip")

# the user's folders inside the engine — carried across every update
PRESERVED = ("custom_nodes", "input", "user")

# files ComfyUI ships in custom_nodes — duplicated into every nest level by
# the pre-v1.34 update bug, so they are safe to discard when cleaning
STOCK_NODE_FILES = {"example_node.py.example", "websocket_image_save.py",
                    "__pycache__", ".gitignore"}

# a tree missing any of these cannot start. They have all existed in
# ComfyUI for years, so they are safe to demand of any release.
SENTINELS = ("main.py", "nodes.py", "server.py", "execution.py",
             "folder_paths.py", "comfy/options.py",
             "comfy/model_management.py", "comfy/samplers.py", "comfy/sd.py",
             "comfy_extras/nodes_upscale_model.py")

# what a damaged engine writes to engine.log when it fails to start
DAMAGE_SIGNS = ("No module named 'comfy", "No module named 'nodes'",
                "No module named 'server'", "No module named 'execution'",
                "can't open file")

_PROJECT = None
_ENGINE = None
_PYTHON = None          # Path or a callable returning one
_ENV = None             # callable returning the subprocess environment
_VERSION_FILE = None
_NO_WINDOW = 0
_counter = itertools.count(1)
_counter_lock = threading.Lock()


def configure(project, engine_dir=None, python_exe=None, env=None,
              version_file=None, no_window=0):
    """Point the module at this install. The app calls this once."""
    global _PROJECT, _ENGINE, _PYTHON, _ENV, _VERSION_FILE, _NO_WINDOW
    _PROJECT = Path(project)
    _ENGINE = Path(engine_dir) if engine_dir else _PROJECT / "ComfyUI"
    _PYTHON = python_exe
    _ENV = env
    _VERSION_FILE = (Path(version_file) if version_file
                     else _PROJECT / "engine_version.json")
    _NO_WINDOW = no_window


def engine_dir():
    return _ENGINE


def comfy_zip_url(sha=None):
    """The GitHub archive for one commit — or master when no sha is known."""
    if not sha or sha == "master":
        return COMFY_ZIP_URL
    return "https://github.com/comfyanonymous/ComfyUI/archive/%s.zip" % sha


class Cancelled(Exception):
    """The caller's cancel event was set mid-download."""


# --------------------------------------------------------------------------
# paths, temp folders, deletion that reports instead of raising
# --------------------------------------------------------------------------

def long_path(p):
    """Windows extended-length form. Plain shutil/os calls fail past
    MAX_PATH, and a nested engine folder blows through it quickly."""
    s = os.path.abspath(str(p))
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        s = "\\\\?\\" + s
    return s


def remove_tree(path):
    """Delete a tree, deleting everything that will go and reporting what
    will not. Returns a list of "path: error" strings — empty means clean.
    Never raises: a leftover is information for the caller, not a reason
    to abandon whatever the caller was doing."""
    failed = []

    def onexc(fn, p, err):
        # a read-only file (git checkouts leave them) gets one more try
        if isinstance(err, PermissionError):
            try:
                os.chmod(p, stat.S_IWRITE)
                fn(p)
                return
            except OSError as e2:
                err = e2
        failed.append("%s: %s" % (p, err))

    lp = long_path(path)
    if not os.path.lexists(lp):
        return failed
    try:
        shutil.rmtree(lp, onexc=onexc)
    except Exception as e:                       # pragma: no cover
        failed.append("%s: %s" % (path, e))
    return failed


def make_tmp(tag):
    """A fresh, empty, uniquely named temp folder under the project.

    Unique per process AND per call, so two installers running at once (the
    boot-time add-on heal and a user-triggered install used to share one
    folder) cannot delete each other's downloads. Made with exist_ok, and if
    a same-named leftover cannot be emptied the name is bumped: a stuck file
    from an earlier run is never allowed to fail this one on mkdir.
    """
    if _PROJECT is None:
        raise RuntimeError("engine_files.configure() was not called")
    for _ in range(50):
        with _counter_lock:
            n = next(_counter)
        p = _PROJECT / ("_tmp_%s_%d_%d" % (tag, os.getpid(), n))
        if p.exists():
            remove_tree(p)
            if p.exists():
                continue          # something in there will not go — next name
        p.mkdir(parents=True, exist_ok=True)
        return p
    raise RuntimeError("could not create a temp folder under " + str(_PROJECT))


def sweep_leftovers():
    """At launch: clear what earlier runs could not — old engines renamed
    aside, temp folders (including the pre-v1.36 shared ones). Returns
    (folders removed, list of paths that still would not go)."""
    if _PROJECT is None:
        return 0, []
    gone, stuck = 0, []
    pats = ("ComfyUI_old_*", "ComfyUI_failed_*", "_tmp_*", "_upd_tmp",
            "_app_upd_tmp*")
    for pat in pats:
        for p in _PROJECT.glob(pat):
            if not p.is_dir():
                continue
            left = remove_tree(p)
            if left:
                stuck.extend(left)
            else:
                gone += 1
    return gone, stuck


# --------------------------------------------------------------------------
# download / unpack
# --------------------------------------------------------------------------

def download(url, dest, status=None, progress=None, cancel=None,
             timeout=60):
    """Stream a URL to dest. progress(done, total) if given; a set cancel
    event raises Cancelled."""
    done = 0
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    return dest


def extract_zip(zpath, dest):
    """Unpack, refusing any entry that would land outside dest."""
    dest = Path(dest)
    root = str(dest.resolve())
    with zipfile.ZipFile(zpath) as z:
        for m in z.namelist():
            if not str((dest / m).resolve()).startswith(root):
                raise RuntimeError("the zip has a bad path: " + m)
        z.extractall(dest)


def _single_dir(tmp, prefix=""):
    """The one folder a GitHub archive unpacks to."""
    for d in sorted(Path(tmp).iterdir()):
        if d.is_dir() and d.name.startswith(prefix):
            return d
    return None


# --------------------------------------------------------------------------
# integrity
# --------------------------------------------------------------------------

def check_integrity(engine=None):
    """The sentinel files missing from an engine tree — empty means it can
    start. A missing folder reports every sentinel."""
    root = Path(engine) if engine else _ENGINE
    if root is None:
        return list(SENTINELS)
    return [s for s in SENTINELS if not os.path.isfile(long_path(root / s))]


def log_shows_damage(log_path):
    """True when engine.log says the engine could not even import itself —
    the signature of a tree with its packages emptied."""
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    tail = text[-4000:]
    return any(sign in tail for sign in DAMAGE_SIGNS)


def recorded_sha():
    """The engine commit the install records, or None."""
    try:
        return json.loads(_VERSION_FILE.read_text(encoding="utf-8"))["sha"]
    except Exception:
        return None


def _write_sha(sha):
    _VERSION_FILE.write_text(json.dumps({"sha": sha}), encoding="utf-8")


# --------------------------------------------------------------------------
# the user's folders
# --------------------------------------------------------------------------

def copy_preserved(old_engine, new_engine, subs=PRESERVED):
    """Copy the user's folders from the old tree into the new one, MERGING
    into the folders the fresh engine ships: the fresh engine's own copy of
    a same-named item wins, everything else comes across. Copies, never
    moves — the old tree stays complete until the swap is known good.
    Returns how many items came across."""
    n = 0
    for sub in subs:
        s = Path(old_engine) / sub
        if not os.path.isdir(long_path(s)):
            continue
        d = Path(new_engine) / sub
        if not os.path.isdir(long_path(d)):
            shutil.copytree(long_path(s), long_path(d))
            n += 1
            continue
        for entry in os.listdir(long_path(s)):
            src, dst = s / entry, d / entry
            if os.path.lexists(long_path(dst)):
                continue          # the fresh engine's own copy wins
            if os.path.isdir(long_path(src)):
                shutil.copytree(long_path(src), long_path(dst))
            else:
                shutil.copy2(long_path(src), long_path(dst))
            n += 1
    return n


def flatten_nested_dir(parent, name, dedupe=False):
    """Repair `custom_nodes/custom_nodes/…` nesting left by pre-v1.34
    engine updates: hoist the buried content back to the top level and
    drop the empty shells. Each update pushed the previous folder one
    level deeper, so the SHALLOWEST copy is the most recent one and wins.

    dedupe=True also discards nested items whose name already exists at
    the top — right for custom_nodes, where those are just older copies
    of the same node package; left off for input/user, where a
    same-named file may hold different data.

    Returns how many items were rescued."""
    top = Path(parent) / name
    if not os.path.isdir(long_path(top)):
        return 0
    # every probe goes through long_path: a plain Path check silently
    # returns False past MAX_PATH, which would stop the walk before the
    # deepest (and most buried) levels
    chain, cur = [], top / name
    while os.path.isdir(long_path(cur)):
        chain.append(cur)
        cur = cur / name
    rescued = 0
    for nest in chain:                    # shallowest first = newest wins
        try:
            entries = os.listdir(long_path(nest))
        except OSError:
            continue
        for entry in entries:
            if entry == name:
                continue                  # that is the next nest level
            target = top / entry
            if os.path.exists(long_path(target)):
                continue
            try:
                shutil.move(long_path(nest / entry), long_path(target))
                rescued += 1
            except OSError:
                pass

    def _disposable(d):
        """True when only stock files and empty nest levels are left."""
        try:
            entries = os.listdir(long_path(d))
        except OSError:
            return False
        for entry in entries:
            if entry == name:
                if not _disposable(d / entry):
                    return False
            elif entry in STOCK_NODE_FILES:
                continue
            elif dedupe and os.path.exists(long_path(top / entry)):
                continue          # an older copy of something rescued
            else:
                return False
        return True

    if chain and _disposable(chain[0]):
        remove_tree(chain[0])
    return rescued


# --------------------------------------------------------------------------
# the update
# --------------------------------------------------------------------------

def _python():
    return Path(_PYTHON() if callable(_PYTHON) else _PYTHON)


def pip_requirements(req_path, status=None):
    """pip install -r for the engine runtime. Raises RuntimeError with the
    tail of pip's output when it fails — a silent pip failure used to be
    found only when the engine would not start."""
    if _PYTHON is None:
        raise RuntimeError("no engine python configured")
    env = _ENV() if callable(_ENV) else None
    r = subprocess.run([str(_python()), "-m", "pip", "install", "-q",
                        "-r", str(req_path)],
                       capture_output=True, text=True, errors="replace",
                       creationflags=_NO_WINDOW, env=env, timeout=1800)
    if r.returncode != 0:
        tail = ((r.stderr or "") + (r.stdout or "")).strip()[-600:]
        raise RuntimeError("engine packages did not install: " + tail)


def _rename_aside(path, tag):
    """Rename a folder to <name>_<tag>_<stamp>. Retries for a few seconds
    because Windows can hold a just-killed process's folder briefly.
    Returns the new path, or None when there was nothing to rename."""
    path = Path(path)
    if not os.path.lexists(long_path(path)):
        return None
    aside = path.with_name("%s_%s_%s" % (path.name, tag,
                                         time.strftime("%Y%m%d_%H%M%S")))
    last = None
    for _ in range(10):
        try:
            os.rename(long_path(path), long_path(aside))
            return aside
        except OSError as e:
            last = e
            time.sleep(1)
    raise RuntimeError("the engine folder is in use (%s) — is the engine "
                       "still running? Nothing was changed." % last)


def stage_engine(url, tmp, status=None, progress=None, cancel=None):
    """Download + unpack an engine archive into tmp and check it can start.
    Returns the staged tree. Nothing outside tmp is touched."""
    z = Path(tmp) / "engine.zip"
    if status:
        status("Downloading engine update…")
    download(url, z, status, progress, cancel, timeout=120)
    if status:
        status("Unpacking engine update…")
    extract_zip(z, tmp)
    staged = _single_dir(tmp, "ComfyUI")
    if staged is None:
        raise RuntimeError("the engine archive holds no ComfyUI folder")
    missing = check_integrity(staged)
    if missing:
        raise RuntimeError("the downloaded engine is incomplete (no %s)"
                           % missing[0])
    return staged


def update_engine(new_sha, status, zip_url=None, requirements=True,
                  strict_pip=True, progress=None, cancel=None):
    """Replace the engine with the archive for new_sha, carrying the user's
    folders across, and record the sha. Returns the engine folder.

    Order of operations — everything fallible first, the swap last:
      1. download, unpack, integrity-check the staged tree (temp only)
      2. pip the staged requirements into the runtime (strict_pip: a
         failure aborts here with the live engine untouched; repair mode
         passes False because a damaged engine has nothing left to lose)
      3. rename the live engine aside (fails whole if it is in use)
      4. rename the staged tree in, copy the user's folders across
         — any failure here rolls the old tree back into place
      5. record the sha, then delete the old tree (leftovers are reported
         and swept at the next launch)
    Raises on any failure, with the install as it was.
    """
    if _ENGINE is None:
        raise RuntimeError("engine_files.configure() was not called")
    tmp = make_tmp("engine")
    try:
        staged = stage_engine(zip_url or comfy_zip_url(new_sha), tmp,
                              status, progress, cancel)
        if requirements:
            status("Updating engine packages…")
            try:
                pip_requirements(staged / "requirements.txt", status)
            except RuntimeError as e:
                if strict_pip:
                    raise
                status("Note: " + str(e)[:200])
        status("Installing engine update…")
        old = _rename_aside(_ENGINE, "old")
        try:
            shutil.move(str(staged), str(_ENGINE))
            if old is not None:
                n = copy_preserved(old, _ENGINE)
                if n:
                    status("Carried %d of your item(s) across." % n)
        except Exception as e:
            # roll back: the old tree is still complete — put it back
            failed = None
            if os.path.lexists(long_path(_ENGINE)):
                failed = _rename_aside(_ENGINE, "failed")
            if old is not None:
                os.rename(long_path(old), long_path(_ENGINE))
            if failed is not None:
                remove_tree(failed)
            raise RuntimeError("could not install the engine update (%s). "
                               "Your previous engine was put back." % e)
        _write_sha(new_sha)
        if old is not None:
            left = remove_tree(old)
            if left:
                status("Note: %d old engine file(s) could not be removed "
                       "yet — cleared at the next launch." % len(left))
        return _ENGINE
    finally:
        remove_tree(tmp)


def repair_engine(status, progress=None):
    """Rebuild a damaged engine from the commit the install records (or
    master when nothing is recorded). pip is attempted but not required:
    the runtime already matches the recorded commit, and a damaged engine
    has nothing to lose to a package that will not install."""
    sha = recorded_sha()
    status("Repairing the engine files…")
    return update_engine(sha or "master", status, zip_url=comfy_zip_url(sha),
                         strict_pip=False, progress=progress)


# --------------------------------------------------------------------------
# add-on nodes
# --------------------------------------------------------------------------

def install_node_zip(url, node_dir, status=None):
    """Download a custom node's GitHub archive into custom_nodes/<name>.
    Returns True when installed, False when it was already there (another
    thread may have won the race — that is not an error)."""
    node_dir = Path(node_dir)
    if node_dir.exists():
        return False
    tmp = make_tmp("node")
    try:
        z = tmp / "node.zip"
        if status:
            status("Downloading " + node_dir.name + "…")
        download(url, z, status, timeout=60)
        extract_zip(z, tmp)
        inner = _single_dir(tmp)
        if inner is None:
            raise RuntimeError("the node archive holds no folder")
        # the last step is serialised: two installers (the boot-time heal
        # and a user's click) can both have downloaded, but only one may
        # move in — the other must see the folder and stand down
        with _install_lock:
            node_dir.parent.mkdir(parents=True, exist_ok=True)
            if node_dir.exists():
                return False
            shutil.move(str(inner), str(node_dir))
        return True
    finally:
        remove_tree(tmp)


_install_lock = threading.Lock()
