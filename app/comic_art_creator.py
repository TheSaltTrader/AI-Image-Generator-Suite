r"""AI Image Generator Suite — local, unrestricted image studio.

A desktop frontend for a headless ComfyUI engine. Everything runs on the
local GPU; nothing leaves the machine.

Layout of the project folder:
    ComicArtCreator\  packaged exe (this file frozen) — or run app\ from venv
    ComfyUI\    engine (started automatically, headless)
    venv\       python environment for the engine + helpers
    models\     checkpoints / loras / vae  (wired via extra_model_paths.yaml)
    output\     finished art (auto-saved) ; output/_raw is the engine scratch
"""

import base64
import ctypes
import json
import os
import re
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
import queue as queue_mod


# A copy started by ANOTHER frozen copy (the relaunch after an update, or
# Setup.exe started from the app) inherits that copy's PyInstaller
# variables (_PYI_APPLICATION_HOME_DIR & co.) and runs from ITS temporary
# folder. When the starter exits, its bootloader deletes that folder — as
# much of it as the running copy's open files allow — and every import
# from then on fails: "cannot import name …", WinError 3 on numpy\_core,
# "no cacert.pem". Two defences: children get a clean environment, and a
# copy that finds itself in a folder older than it is starts over.
def _clean_child_env(env=None):
    """The environment for a child process: PyInstaller's own variables
    stripped, so a spawned frozen exe extracts and owns its own folder."""
    return {k: v for k, v in (os.environ if env is None else env).items()
            if not (k.startswith("_PYI_") or k.startswith("_MEIPASS"))}


def _foreign_extraction(meipass, now=None, age=60):
    """True when the PyInstaller folder predates this process by more than
    `age` seconds — it belongs to the copy that started us."""
    try:
        return (time.time() if now is None else now) \
            - os.stat(meipass).st_ctime > age
    except (OSError, TypeError):
        return False


_REEXEC_FLAG = "CBAC_REEXEC"


def _needs_clean_restart(argv, environ, meipass):
    """True when this frozen copy must start over in a clean environment.

    The certain signal: we were started by an OLDER copy's relaunch —
    --after-update without the --clean marker a v1.42+ relaunch adds — so
    we inherited its PyInstaller folder. A v1.41 auto-update completed 17
    seconds after the old copy started, under the folder-age net below,
    and the new copy died the same way the day it was meant to fix. The
    age check stays as a second net. Never twice (the flag)."""
    if environ.get(_REEXEC_FLAG) == "1":
        return False
    if "--after-update" in argv and "--clean" not in argv:
        return True
    return _foreign_extraction(meipass)


if getattr(sys, "frozen", False) and _needs_clean_restart(
        sys.argv, os.environ, getattr(sys, "_MEIPASS", "")):
    _env = _clean_child_env()
    _env[_REEXEC_FLAG] = "1"          # the fresh copy must never loop
    subprocess.Popen([sys.executable] + sys.argv[1:]
                     + ([] if "--clean" in sys.argv else ["--clean"]),
                     env=_env, cwd=os.getcwd())
    os._exit(0)

from ctypes import wintypes
from datetime import datetime
from io import BytesIO
from pathlib import Path
from tkinter import (Tk, Text, Canvas, Listbox, StringVar, IntVar, BooleanVar,
                     DoubleVar, filedialog, messagebox, Toplevel, END, WORD,
                     NSEW, W, E)
from tkinter import ttk

import requests
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk
from PIL.PngImagePlugin import PngInfo

import self_update
import engine_files
import applog
import tkinter.messagebox as _tk_messagebox

APP_VERSION = "2.3.1"

if getattr(sys, "frozen", False):
    # packaged onefile exe lives in the project root, next to Setup.exe
    PROJECT = Path(sys.executable).resolve().parent
    APP_DIR = PROJECT / "app"
else:
    APP_DIR = Path(__file__).resolve().parent
    PROJECT = APP_DIR.parent

ENGINE_DIR = PROJECT / "ComfyUI"

# the self-updater needs to know where this install lives before
# anything calls it (it deliberately imports nothing from here)
self_update.configure(PROJECT, APP_DIR)
# app.log next to the exe: every status, message box, shown error and
# caught exception, timestamped — a windowed exe has no console, and a
# problem used to be reportable only from memory
applog.configure(PROJECT / "app.log")
applog.install_hooks()
applog.wrap_messagebox(_tk_messagebox)
applog.log("start v%s frozen=%s python=%s exe=%s%s" % (
    APP_VERSION, bool(getattr(sys, "frozen", False)),
    sys.version.split()[0], sys.executable,
    " (restarted clean: the first copy had inherited another copy's "
    "program folder)" if os.environ.get(_REEXEC_FLAG) == "1" else ""))


def engine_python():
    """Python runtime for the engine: the bundled runtime installed by
    Setup.exe first (works on machines with nothing installed), a dev venv
    as fallback."""
    for cand in (PROJECT / "python" / "python.exe",
                 PROJECT / "venv" / "Scripts" / "python.exe"):
        if cand.exists():
            return cand
    return PROJECT / "python" / "python.exe"

MODELS = PROJECT / "models"
OUTPUT = PROJECT / "output"
RAW_OUT = OUTPUT / "_raw"
SETTINGS_FILE = APP_DIR / "settings.json"
PRESETS_FILE = APP_DIR / "presets.json"
MANIFEST_FILE = APP_DIR / "models_manifest.json"
EXTRA_PATHS_YAML = PROJECT / "extra_model_paths.yaml"

def _contained_env():
    """Environment that keeps every helper's downloads/caches inside the
    project folder (nothing lands in the user profile or %LOCALAPPDATA%)."""
    env = _clean_child_env()
    env["U2NET_HOME"] = str(MODELS / "rembg")      # rembg BG-removal model
    env["HF_HOME"] = str(MODELS / "hf_cache")      # any HuggingFace caching
    env["PIP_NO_CACHE_DIR"] = "1"
    return env


UPSCALE_MODEL = "RealESRGAN_x4plus.pth"   # optional 4x hi-res pass

ENGINE_HOST = "127.0.0.1"   # loopback only — engine is never exposed to LAN
ENGINE_PORT = 8188
ENGINE_URL = f"http://{ENGINE_HOST}:{ENGINE_PORT}"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

SIZE_PRESETS = {
    "Portrait — cover (832x1216)": (832, 1216),
    "Portrait — page (896x1152)": (896, 1152),
    "Square (1024x1024)": (1024, 1024),
    "Landscape — panel (1216x832)": (1216, 832),
    "Wide — splash (1344x768)": (1344, 768),
    "Large portrait (1024x1536)": (1024, 1536),
    "Large square (1408x1408)": (1408, 1408),
}

NONE_LORA = "— none —"
NONE_PRESET = "— none (raw prompt) —"

# colors \u2014 two themes; apply_theme() rebinds the globals below before the
# UI is built, and again (best-effort) when the user switches in Settings.
THEMES = {
    "dark": dict(BG="#17171c", BG2="#20202a", BG3="#2a2a38", FG="#e8e8f0",
                 FG_DIM="#9a9ab0", ACCENT="#e94560", ACCENT2="#4ecca3",
                 RULE="#3d7dff", BTN_ACTIVE="#3a3a4e", TIP_BG="#33334a",
                 TIP_FG="#e8e8f0", BADGE_FG="#0d0d12", GO_ACTIVE="#ff5e7a"),
    "light": dict(BG="#f4f4f7", BG2="#e7e7ee", BG3="#d8d8e2", FG="#1b1b24",
                  FG_DIM="#5a5a68", ACCENT="#e94560", ACCENT2="#1f9d6b",
                  RULE="#3d7dff", BTN_ACTIVE="#c8c8d6", TIP_BG="#fffbe6",
                  TIP_FG="#1b1b24", BADGE_FG="#ffffff", GO_ACTIVE="#ff5e7a"),
}
BG = BG2 = BG3 = FG = FG_DIM = ACCENT = ACCENT2 = RULE = None
BTN_ACTIVE = TIP_BG = TIP_FG = BADGE_FG = GO_ACTIVE = None
UI_FONT = "Segoe UI"     # the menu font family; Settings can change it
UI_FONTS = ["Segoe UI", "Arial", "Calibri", "Verdana", "Tahoma",
            "Georgia", "Times New Roman", "Comic Sans MS", "Consolas"]


def apply_theme(name):
    """Rebind the colour globals to the chosen theme ("dark"/"light")."""
    pal = THEMES.get(name) or THEMES["dark"]
    g = globals()
    for k, v in pal.items():
        g[k] = v
    return name


apply_theme("dark")     # default until settings say otherwise


# --------------------------------------------------------------------------
# Windows DPAPI — encrypts the CivitAI key with the user's login credentials
# so settings.json never holds it in plaintext.
# --------------------------------------------------------------------------

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_encrypt(text):
    if not text:
        return ""
    inb, out = _blob(text.encode("utf-8")), _DATA_BLOB()
    if ctypes.windll.crypt32.CryptProtectData(ctypes.byref(inb), None, None,
                                              None, None, 0, ctypes.byref(out)):
        try:
            return base64.b64encode(
                ctypes.string_at(out.pbData, out.cbData)).decode()
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)
    return ""


def dpapi_decrypt(b64):
    if not b64:
        return ""
    try:
        inb, out = _blob(base64.b64decode(b64)), _DATA_BLOB()
        if ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(out)):
            try:
                return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
            finally:
                ctypes.windll.kernel32.LocalFree(out.pbData)
    except Exception:
        pass
    return ""


# --------------------------------------------------------------------------
# engine management + API
# --------------------------------------------------------------------------

VRAM_HEADROOM_GB = 2.0   # a model needs its size + this much working room


def gpu_vram_gb():
    """Total VRAM of the NVIDIA GPU in GB, or None if no NVIDIA GPU /
    driver is present. Works before the engine is up."""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.total",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=15,
                           creationflags=NO_WINDOW)
        if r.returncode == 0 and r.stdout.strip():
            return int(r.stdout.strip().splitlines()[0]) / 1024
    except Exception:
        pass
    return None


def engine_alive(timeout=2):
    try:
        requests.get(f"{ENGINE_URL}/system_stats", timeout=timeout)
        return True
    except requests.RequestException:
        return False


_ENGINE_PROC = None   # the last engine we spawned, so a crash is detectable


def engine_log_tail(n=12):
    """The last few lines of engine.log, to show WHY the engine did not come
    up instead of only saying it didn't. Empty string if unreadable."""
    try:
        lines = (PROJECT / "engine.log").read_text(
            encoding="utf-8", errors="replace").splitlines()
        return "\n".join(ln for ln in lines[-n:] if ln.strip()).strip()
    except Exception:
        return ""


def start_engine():
    """Spawn headless ComfyUI with our model paths and scratch output dir."""
    # (re)write the model-paths config with THIS machine's absolute path,
    # so the folder works wherever it is unzipped
    EXTRA_PATHS_YAML.write_text(
        "comic_art_creator:\n"
        f"  base_path: {MODELS}\n"
        "  checkpoints: checkpoints\n"
        "  loras: loras\n"
        "  vae: vae\n"
        "  upscale_models: upscale_models\n"
        "  ipadapter: ipadapter\n"
        "  clip_vision: clip_vision\n"
        "  diffusion_models: diffusion_models\n"
        "  text_encoders: text_encoders\n", encoding="utf-8")
    cmd = [str(engine_python()), "main.py",
           "--listen", ENGINE_HOST, "--port", str(ENGINE_PORT),
           "--extra-model-paths-config", str(EXTRA_PATHS_YAML),
           "--output-directory", str(RAW_OUT),
           "--disable-auto-launch"]
    log = open(PROJECT / "engine.log", "w", encoding="utf-8", errors="replace")
    global _ENGINE_PROC
    _ENGINE_PROC = subprocess.Popen(cmd, cwd=str(ENGINE_DIR), stdout=log,
                                    stderr=subprocess.STDOUT,
                                    creationflags=NO_WINDOW,
                                    env=_contained_env())
    _mark_engine_owned()
    return _ENGINE_PROC


ENGINE_OWNER_FILE = PROJECT / "engine_owner.json"


def _mark_engine_owned():
    """Record that THIS process started the engine, so a later boot can
    tell its own engine from a stray one left by another instance."""
    try:
        ENGINE_OWNER_FILE.write_text(
            json.dumps({"pid": os.getpid()}), encoding="utf-8")
    except OSError:
        pass


def engine_is_ours():
    """True if the live engine was started by a process that is still us
    or still running (our marker pid). A foreign/stale engine returns
    False so the caller can warn or restart it."""
    try:
        pid = json.loads(ENGINE_OWNER_FILE.read_text(encoding="utf-8"))["pid"]
    except (OSError, ValueError, KeyError):
        return False
    if pid == os.getpid():
        return True
    # is that pid still alive? (Windows tasklist check, no extra deps)
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, creationflags=NO_WINDOW,
            timeout=5).stdout
        return str(pid) in out
    except Exception:
        return False


def _engine_owner_pid():
    try:
        return json.loads(
            ENGINE_OWNER_FILE.read_text(encoding="utf-8"))["pid"]
    except (OSError, ValueError, KeyError):
        return None


def engine_ours_to_stop():
    """True when shutting the engine down is safe: either this process
    started it, or the session that did has since died and left it
    orphaned. An engine owned by another LIVE app window is left running
    — engine_is_ours() calls that one "ours" too, which is fine for
    trusting it but would be wrong for killing it."""
    pid = _engine_owner_pid()
    if pid is None:
        return False
    if pid == os.getpid():
        return True
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, creationflags=NO_WINDOW,
            timeout=5).stdout
        return str(pid) not in out      # owner gone: an orphan to clean up
    except Exception:
        return False


_MUTEX_HANDLE = None


def single_instance_handle():
    """Windows named mutex: returns (handle, already_running). The handle
    is kept alive for the process lifetime (module global, so the
    updating copy can let go of it on purpose); None on non-Windows."""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return None, False
    try:
        h = ctypes.windll.kernel32.CreateMutexW(
            None, False, "Global\\ComicBookArtCreator_singleton")
        already = ctypes.windll.kernel32.GetLastError() == 183  # ALREADY_EXISTS
        _MUTEX_HANDLE = h
        return h, already
    except Exception:
        return None, False


def release_single_instance():
    """Let go of the single-instance mutex — the last thing an updating
    copy does before it starts its replacement, so the replacement's own
    check does not see a ghost of the copy that launched it."""
    global _MUTEX_HANDLE
    h, _MUTEX_HANDLE = _MUTEX_HANDLE, None
    if h:
        try:
            ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            pass


def _pid_alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True,
                             creationflags=NO_WINDOW, timeout=5).stdout
        return str(pid) in out
    except Exception:
        return False


def previous_instance_pids(argv=None):
    """PIDs of copies of this app that are on their way out: the one an
    update just replaced renamed its exe to *_old_<pid>.exe, and a v1.37+
    copy also passes --after-update <pid> to the copy it starts. Such a
    copy is closing, not competing — it must be waited for, never
    reported as "already running" (which is what every user saw right
    after an update, because the new exe checked the mutex while the old
    one was still shutting its engine down)."""
    argv = sys.argv if argv is None else argv
    pids = set()
    for f in PROJECT.glob("*_old_*.exe"):
        m = re.search(r"_old_(\d+)\.exe$", f.name)
        if m:
            pids.add(int(m.group(1)))
    if "--after-update" in argv:
        i = argv.index("--after-update")
        if i + 1 < len(argv) and argv[i + 1].isdigit():
            pids.add(int(argv[i + 1]))
    return pids


def wait_for_previous_instance(pids=(), timeout=30, alive=None, probe=None,
                               tick=None, poll=0.5):
    """The single-instance mutex is held: wait (up to timeout s) for it to
    free, re-probing every poll seconds. Returns (handle, already_running)
    like single_instance_handle().

    pids are copies known to be on their way out (an update's
    *_old_<pid>.exe, --after-update); once all of them are gone but the
    mutex is still held, whoever holds it is a real second copy and the
    wait ends early. With no pids — the copy that is closing right now,
    its window already hidden while its engine shuts down (v1.37 hides the
    window first, which made "close, reopen" ask about a second copy) —
    the mutex itself is the only signal, so it is simply polled. Our own
    handle is released before each probe: while we hold one, the name
    never goes away. tick() runs each cycle (the waiting window's update)."""
    alive = alive or _pid_alive
    probe = probe or single_instance_handle
    deadline = time.time() + timeout
    while True:
        release_single_instance()
        time.sleep(0.2)          # the name vanishes as the last handle closes
        h, already = probe()
        if not already or time.time() >= deadline:
            return h, already
        if pids and not any(alive(p) for p in pids):
            # the leavers are gone — but a process drops off tasklist a
            # moment before the kernel closes its handles, so look once
            # more before concluding that someone else holds the mutex
            release_single_instance()
            time.sleep(0.5)
            return probe()
        if tick:
            try:
                tick()
            except Exception:
                pass
        time.sleep(poll)


def api_get(path):
    r = requests.get(f"{ENGINE_URL}{path}", timeout=15)
    r.raise_for_status()
    return r.json()


def scan_models(sub):
    """List model files straight from disk — works before the engine is up.

    safetensors only: .ckpt files are pickle-based and can execute code
    when loaded, so they are deliberately not offered.
    """
    d = MODELS / sub
    if not d.is_dir():
        return []
    return sorted((f.name for f in d.glob("*.safetensors")), key=str.lower)


def _ensure_lora_dir():
    d = MODELS / "loras"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _api_choices(node, field):
    try:
        return api_get(f"/object_info/{node}")[node]["input"]["required"][field][0]
    except Exception:
        return []


def list_checkpoints():
    return list(dict.fromkeys(
        _api_choices("CheckpointLoaderSimple", "ckpt_name")
        + scan_models("checkpoints")))


def list_loras():
    return list(dict.fromkeys(
        _api_choices("LoraLoader", "lora_name") + scan_models("loras")))


# the app's own versioned LoRAs (e.g. SDXL_BorderFrames_v3): only the
# current shipped version should be offered — hide superseded ones so
# old attempts that didn't work don't clutter the list. The canonical
# names are the module constants (e.g. BORDER_LORA_FILE), read at call
# time so there's a single source of truth.
def _canonical_family(fname):
    """('sdxl_borderframes', 3) for 'SDXL_BorderFrames_v3.safetensors',
    else None — the family key and version of a versioned LoRA name."""
    mobj = re.match(r"^(.*?)_v(\d+)\.safetensors$", fname, re.IGNORECASE)
    if not mobj:
        return None
    return mobj.group(1).lower(), int(mobj.group(2))


def _visible_loras(loras):
    canonical = {}   # family -> the one shipped filename to keep
    for fname in (BORDER_LORA_FILE,):
        fam = _canonical_family(fname)
        if fam:
            canonical[fam[0]] = fname
    out = []
    for name in loras:
        fam = _canonical_family(name)
        if fam and fam[0] in canonical and name != canonical[fam[0]]:
            continue   # a superseded version of one of our shipped LoRAs
        out.append(name)
    return out


# --------------------------------------------------------------------------
# model update check (HuggingFace)
# --------------------------------------------------------------------------

def sync_bundled_manifest():
    """The self-updater swaps only the exe, so app\\models_manifest.json
    on disk can be YEARS behind the app — and the update check then never
    offers models added since (this is how installs missed Qwen). The exe
    now carries the manifest it was built with; write it over a stale disk
    copy at startup so every app update also delivers the current model
    list. The disk copy stays the single source Setup.exe reads."""
    if not getattr(sys, "frozen", False):
        return False
    try:
        src = Path(sys._MEIPASS) / "models_manifest.json"
        if not src.exists():
            return False
        new = src.read_bytes()
        json.loads(new)     # never replace a good file with a broken one
        if MANIFEST_FILE.exists() and MANIFEST_FILE.read_bytes() == new:
            return False
        MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_FILE.write_bytes(new)
        return True
    except Exception:
        return False


def check_model_updates():
    """Compare managed model files against their HuggingFace originals.

    Returns manifest entries whose remote size differs from the local file
    (an updated release) or whose local file is missing. Size comparison is
    cheap and catches every re-released file.
    """
    try:
        manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    updates, repo_cache = [], {}
    for entry in manifest:
        # direct-URL entry (e.g. our own trained LoRA on a GitHub release):
        # size comes from a HEAD request, not the HF blob API
        if entry.get("url"):
            local = MODELS / entry["dir"] / entry["local"]
            size = 0
            try:
                h = requests.head(entry["url"], timeout=20,
                                  allow_redirects=True)
                size = int(h.headers.get("content-length", 0))
            except requests.RequestException:
                pass
            if not local.exists():
                updates.append(dict(entry, size=size, missing=True))
            elif size and local.stat().st_size != size:
                updates.append(dict(entry, size=size, missing=False))
            continue
        repo = entry["repo"]
        if repo not in repo_cache:
            try:
                r = requests.get(
                    f"https://huggingface.co/api/models/{repo}?blobs=true",
                    timeout=20)
                repo_cache[repo] = r.json().get("siblings", []) if r.ok else []
            except requests.RequestException:
                repo_cache[repo] = []
        sib = next((s for s in repo_cache[repo]
                    if s.get("rfilename") == entry["remote_file"]), None)
        if not sib or not sib.get("size"):
            continue
        local = MODELS / entry["dir"] / entry["local"]
        if not local.exists() or local.stat().st_size != sib["size"]:
            updates.append(dict(entry, size=sib["size"],
                                missing=not local.exists()))
    return updates


def download_model_update(entry, status_cb, prog_cb=None):
    """Stream one updated model to a temp file, then swap it in place."""
    url = entry.get("url") or (
        f"https://huggingface.co/{entry['repo']}/resolve/main/"
        f"{entry['remote_file']}")
    if not str(url).lower().startswith("https://"):
        raise RuntimeError("refusing a non-HTTPS model URL: " + str(url))
    dest = MODELS / entry["dir"] / entry["local"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    done = 0
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0)) \
            or entry.get("size") or 0
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 22):
                fh.write(chunk)
                done += len(chunk)
                if prog_cb and total:
                    prog_cb(done, total)
                if done % (1 << 26) < (1 << 22):  # every ~64 MB
                    status_cb(f"Updating {entry['local']}: "
                              f"{done / (1024**2):.0f}"
                              + (f"/{total / (1024**2):.0f} MB"
                                 if total else " MB"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(tmp, dest)
        return True
    except PermissionError:
        # engine holds the old file — leave .tmp; applied on next start
        return False


# --------------------------------------------------------------------------
# engine (tool) update check — GitHub master vs the recorded install
# --------------------------------------------------------------------------

ENGINE_VERSION_FILE = PROJECT / "engine_version.json"
IPA_NODE_ZIP = engine_files.IPA_NODE_ZIP
COMFY_COMMITS_API = engine_files.COMFY_COMMITS_API
COMFY_ZIP_URL = engine_files.COMFY_ZIP_URL
# engine file management (update / repair / add-on install) lives in
# engine_files.py — its module note carries the 2026-09 post-mortem that
# put it there: an in-place rmtree of the live engine that died part-way
engine_files.configure(PROJECT, ENGINE_DIR, python_exe=engine_python,
                       env=_contained_env, version_file=ENGINE_VERSION_FILE,
                       no_window=NO_WINDOW)


def check_engine_update():
    """Return {'sha': new} when GitHub has a newer engine than recorded.
    First run on an existing install records the current state silently."""
    try:
        r = requests.get(COMFY_COMMITS_API, timeout=20,
                         headers={"Accept": "application/vnd.github+json"})
        if not r.ok:
            return None
        sha = r.json()["sha"]
    except Exception:
        return None
    try:
        local = json.loads(
            ENGINE_VERSION_FILE.read_text(encoding="utf-8"))["sha"]
    except Exception:
        try:
            ENGINE_VERSION_FILE.write_text(json.dumps({"sha": sha}),
                                           encoding="utf-8")
        except OSError:
            pass
        return None
    return {"sha": sha} if sha != local else None



def kill_engine():
    """Stop any ComfyUI engine belonging to THIS install (ours from this
    or a previous session).

    Matched on the project path, which the engine always carries in its
    --extra-model-paths-config argument. Matching on "ComfyUI" instead
    finds nothing: the engine is launched with cwd=ComfyUI and a bare
    "main.py", so that word never appears in its command line — which is
    how stale engines used to survive every restart and then hold port
    8188 against the new one. .Contains() is used rather than -like so a
    bracket in the path can't be read as a wildcard.
    """
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
         "Where-Object { $_.CommandLine -and "
         f"$_.CommandLine.Contains('{PROJECT}') -and "
         "$_.CommandLine.Contains('main.py') } | "
         "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
        capture_output=True, creationflags=NO_WINDOW, timeout=60)


_long = engine_files.long_path
_STOCK_NODE_FILES = engine_files.STOCK_NODE_FILES
flatten_nested_dir = engine_files.flatten_nested_dir


def update_engine(new_sha, status_cb):
    """Swap in the latest engine, carrying the user's folders across, then
    refresh its python packages. engine_files renames rather than deletes:
    a failure at any step leaves the engine exactly as it was."""
    return engine_files.update_engine(new_sha, status_cb)


# --------------------------------------------------------------------------
# workflow builder
# --------------------------------------------------------------------------

def model_family(name):
    n = name.lower()
    if "schnell" in n:
        return "schnell"
    if "flux" in n:
        return "flux"
    if "turbo" in n or "lightning" in n:
        return "turbo"
    if "animagine" in n or "illustrious" in n or "noob" in n or "pony" in n:
        return "anime"
    return "sdxl"


FAMILY_DEFAULTS = {
    "flux":  dict(steps=20, cfg=1.0, sampler="euler", scheduler="simple"),
    "schnell": dict(steps=4, cfg=1.0, sampler="euler", scheduler="simple"),
    "turbo": dict(steps=8,  cfg=2.0, sampler="dpmpp_sde", scheduler="karras"),
    "anime": dict(steps=28, cfg=5.5, sampler="euler_ancestral", scheduler="normal"),
    "sdxl":  dict(steps=30, cfg=6.0, sampler="dpmpp_2m", scheduler="karras"),
}


EDITOR_ENGINES = {
    "Flux Kontext — best overall edits": "kontext",
    "Qwen Image Edit — best text removal (Apache)": "qwen",
}
EDITOR_VRAM = {"kontext": 16, "qwen": 24, "wan": 16, "wanflf": 20}

# The Clone Tool's selectable methods (label shown in the dropdown -> engine
# key). "faceswap" is the fast local identity swap; the editors re-render.
CLONE_METHODS = [
    ("Face swap — fast, best likeness", "faceswap"),
    ("Qwen editor — re-render (~28 GB)", "qwen"),
    ("Flux Kontext — re-render (~11 GB)", "kontext"),
]
KONTEXT_FILE = "flux1-dev-kontext_fp8_scaled.safetensors"
QWEN_EDIT_FILE = "qwen_image_edit_2511_fp8mixed.safetensors"
QWEN_TE_FILE = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
QWEN_VAE_FILE = "qwen_image_vae.safetensors"
FLUX_CLIP_SRC = "flux1-dev-fp8.safetensors"   # donates CLIP+VAE to Kontext


def build_kontext_graph(p):
    """Instruction edit via FLUX.1 Kontext: the loaded image(s) are the
    context, the prompt says what to change. Two multi-image modes:

    - stitch (default): images are joined side-by-side into ONE canvas the
      model sees as a single context image — right for "combine these"
      edits, but layout-preserving: the output tends to keep the panels.
    - chain (ref_mode="chain"): each image is scaled and encoded on its
      own and chained through its own ReferenceLatent, so the model sees
      N distinct context images. Required for cross-image instructions
      like the face swap — a stitched reference just gets redrawn
      side-by-side instead of swapped."""
    g = {}
    g["1"] = {"class_type": "UNETLoader",
              "inputs": {"unet_name": KONTEXT_FILE,
                         "weight_dtype": "default"}}
    g["2"] = {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": FLUX_CLIP_SRC}}
    g["4"] = {"class_type": "CLIPTextEncode",
              "inputs": {"text": p["prompt"], "clip": ["2", 1]}}
    if p.get("ref_mode") == "chain":
        cond, first_lat, nid, sid = ["4", 0], None, 10, 20
        for name in p["edit_image_names"][:4]:
            g[str(nid)] = {"class_type": "LoadImage",
                           "inputs": {"image": name}}
            g[str(sid)] = {"class_type": "FluxKontextImageScale",
                           "inputs": {"image": [str(nid), 0]}}
            g[str(sid + 1)] = {"class_type": "VAEEncode",
                               "inputs": {"pixels": [str(sid), 0],
                                          "vae": ["2", 2]}}
            g[str(sid + 2)] = {"class_type": "ReferenceLatent",
                               "inputs": {"conditioning": cond,
                                          "latent": [str(sid + 1), 0]}}
            cond = [str(sid + 2), 0]
            if first_lat is None:
                first_lat = [str(sid + 1), 0]
            nid += 1
            sid += 3
        g["33"] = {"class_type": "FluxGuidance",
                   "inputs": {"conditioning": cond,
                              "guidance": p.get("guidance") or 2.5}}
        latent_ref = first_lat
    else:
        img_ref, nid, sid = None, 10, 20
        for name in p["edit_image_names"][:4]:
            g[str(nid)] = {"class_type": "LoadImage",
                           "inputs": {"image": name}}
            if img_ref is None:
                img_ref = [str(nid), 0]
            else:
                g[str(sid)] = {"class_type": "ImageStitch",
                               "inputs": {"image1": img_ref,
                                          "image2": [str(nid), 0],
                                          "direction": "right",
                                          "match_image_size": True,
                                          "spacing_width": 0,
                                          "spacing_color": "white"}}
                img_ref = [str(sid), 0]
                sid += 1
            nid += 1
        g["30"] = {"class_type": "FluxKontextImageScale",
                   "inputs": {"image": img_ref}}
        g["31"] = {"class_type": "VAEEncode",
                   "inputs": {"pixels": ["30", 0], "vae": ["2", 2]}}
        g["32"] = {"class_type": "ReferenceLatent",
                   "inputs": {"conditioning": ["4", 0], "latent": ["31", 0]}}
        g["33"] = {"class_type": "FluxGuidance",
                   "inputs": {"conditioning": ["32", 0],
                              "guidance": p.get("guidance") or 2.5}}
        latent_ref = ["31", 0]
    g["34"] = {"class_type": "ConditioningZeroOut",
               "inputs": {"conditioning": ["4", 0]}}
    # by default the output canvas follows the (first) reference; with
    # out_size the references only condition and a fresh canvas sets
    # size. swap_denoise (< 1.0) = detail-preserving mode: sample from
    # the first image's own latent at out_size so unedited regions keep
    # its pixels. The editors' ordinary "denoise" param (Change-amount
    # slider) is deliberately NOT read here.
    den = p.get("swap_denoise") or 1.0
    if p.get("out_size"):
        ow, oh = p["out_size"]
        if den < 1.0:
            g["35"] = {"class_type": "ImageScale",
                       "inputs": {"image": ["10", 0],
                                  "upscale_method": "lanczos",
                                  "width": ow, "height": oh,
                                  "crop": "disabled"}}
            g["36"] = {"class_type": "VAEEncode",
                       "inputs": {"pixels": ["35", 0], "vae": ["2", 2]}}
            latent_ref = ["36", 0]
        else:
            g["35"] = {"class_type": "EmptySD3LatentImage",
                       "inputs": {"width": ow, "height": oh,
                                  "batch_size": 1}}
            latent_ref = ["35", 0]
    g["6"] = {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["33", 0],
                         "negative": ["34", 0], "latent_image": latent_ref,
                         "seed": p["seed"], "steps": p.get("steps") or 20,
                         "cfg": 1.0, "sampler_name": "euler",
                         "scheduler": "simple", "denoise": den}}
    g["7"] = {"class_type": "VAEDecode",
              "inputs": {"samples": ["6", 0], "vae": ["2", 2]}}
    g["8"] = {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "cbac", "images": ["7", 0]}}
    return g


def build_qwen_edit_graph(p):
    """Instruction edit via Qwen-Image-Edit (Apache 2.0) — strongest at
    removing/altering text in images."""
    g = {}
    g["1"] = {"class_type": "UNETLoader",
              "inputs": {"unet_name": QWEN_EDIT_FILE,
                         "weight_dtype": "default"}}
    g["2"] = {"class_type": "CLIPLoader",
              "inputs": {"clip_name": QWEN_TE_FILE, "type": "qwen_image",
                         "device": "default"}}
    g["3"] = {"class_type": "VAELoader",
              "inputs": {"vae_name": QWEN_VAE_FILE}}
    load_refs, nid = [], 10
    for name in p["edit_image_names"][:3]:
        g[str(nid)] = {"class_type": "LoadImage", "inputs": {"image": name}}
        load_refs.append([str(nid), 0])
        nid += 1
    enc_pos = {"clip": ["2", 0], "prompt": p["prompt"], "vae": ["3", 0]}
    enc_neg = {"clip": ["2", 0], "prompt": "", "vae": ["3", 0]}
    for i, ref in enumerate(load_refs, 1):
        enc_pos[f"image{i}"] = ref
        enc_neg[f"image{i}"] = ref
    g["20"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": enc_pos}
    g["21"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": enc_neg}
    g["30"] = {"class_type": "ImageScaleToTotalPixels",
               "inputs": {"image": load_refs[0],
                          "upscale_method": "lanczos", "megapixels": 1.0,
                          "resolution_steps": 1}}
    g["31"] = {"class_type": "VAEEncode",
               "inputs": {"pixels": ["30", 0], "vae": ["3", 0]}}
    latent_ref = ["31", 0]
    # swap_denoise (< 1.0) = detail-preserving mode: the sampler starts
    # from image 1's own latent so unedited regions keep its pixels. The
    # editors' ordinary "denoise" param is deliberately NOT read here.
    den = p.get("swap_denoise") or 1.0
    if p.get("out_size"):
        ow, oh = p["out_size"]
        if den < 1.0:
            g["35"] = {"class_type": "ImageScale",
                       "inputs": {"image": load_refs[0],
                                  "upscale_method": "lanczos",
                                  "width": ow, "height": oh,
                                  "crop": "disabled"}}
            g["36"] = {"class_type": "VAEEncode",
                       "inputs": {"pixels": ["35", 0], "vae": ["3", 0]}}
            latent_ref = ["36", 0]
        else:
            g["35"] = {"class_type": "EmptySD3LatentImage",
                       "inputs": {"width": ow, "height": oh,
                                  "batch_size": 1}}
            latent_ref = ["35", 0]
    g["6"] = {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["20", 0],
                         "negative": ["21", 0], "latent_image": latent_ref,
                         "seed": p["seed"], "steps": p.get("steps") or 20,
                         "cfg": 2.5, "sampler_name": "euler",
                         "scheduler": "simple", "denoise": den}}
    g["7"] = {"class_type": "VAEDecode",
              "inputs": {"samples": ["6", 0], "vae": ["3", 0]}}
    g["8"] = {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "cbac", "images": ["7", 0]}}
    return g


WAN_FILE = "wan2.2_ti2v_5B_fp16.safetensors"
WAN_TE_FILE = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
WAN_VAE_FILE = "wan2.2_vae.safetensors"
WAN_NEGATIVE = ("blurry, distorted, deformed, extra limbs, morphing, text, "
                "watermark, background clutter, scene change, camera "
                "movement, zoom, pan")
ANIM_SIZES = {
    "Square (704x704)": (704, 704),
    "Landscape (1280x704)": (1280, 704),
    "Portrait (704x1280)": (704, 1280),
}
ANIM_KEEP = {
    "all frames (24 fps)": 1,
    "every 2nd (12 fps)": 2,
    "every 3rd (8 fps)": 3,
    "every 4th (6 fps)": 4,
}
ANIM_LOOPS = ["Seamless (auto-cut — default)",
              "Ping-pong (perfect loop)", "Crossfade (blend ends)"]
ANIM_PRESET_HINT = "— pick a preset action —"
ANIM_PRESETS = {
    "Attacking (melee combo)":
        "attacking with a rapid melee combo, throwing alternating punches "
        "and strikes with the whole upper body, hips rotating into each "
        "blow, feet shifting stance, continuous repeating attack cycle",
    "Blocking (raise guard)":
        "raising both arms into a defensive block, bracing and flinching "
        "against impacts, body rocking back slightly with each hit "
        "absorbed, guard bobbing up and down in a repeating cycle",
    "Casting a spell":
        "casting a spell with both arms sweeping in wide circular "
        "gestures, hands weaving in front of the chest, clothes and hair "
        "flowing with the motion, body swaying, continuous repeating "
        "casting cycle",
    "Celebrating (victory)":
        "celebrating a victory, pumping both fists into the air "
        "repeatedly, bouncing on the spot, head thrown back, whole body "
        "bouncing with joy in a continuous repeating cycle",
    "Climbing":
        "climbing upward hand over hand, arms reaching up alternately, "
        "knees lifting high to find footholds, whole body pulling upward "
        "in a clear repeating climbing cycle",
    "Crouching":
        "crouching down low and rising back up, knees bending deeply, "
        "arms out for balance, weight shifting smoothly, continuous "
        "repeating crouch cycle",
    "Dancing":
        "dancing energetically, hips swaying side to side, arms waving "
        "above the head, feet stepping in rhythm, whole body grooving in "
        "a continuous repeating dance cycle",
    "Dodging":
        "dodging side to side, ducking and weaving with quick head and "
        "torso movement, feet shuffling, shoulders dipping left and "
        "right in a fast repeating cycle",
    "Dying (defeated)":
        "staggering and collapsing in defeat, clutching the chest, knees "
        "buckling, falling to the knees and slumping forward with "
        "dramatic full-body motion",
    "Falling":
        "falling through the air, arms and legs flailing, body tumbling "
        "slightly, hair and clothes whipping upward, continuous dramatic "
        "falling motion",
    "Flying":
        "flying forward with cape and clothes rippling in the wind, arms "
        "stretched ahead, body bobbing gently up and down, legs "
        "trailing, continuous smooth flying cycle",
    "Idle (breathing)":
        "standing idle in a ready stance, chest rising and falling with "
        "deep breaths, arms swaying slightly, weight shifting gently "
        "from foot to foot in a subtle repeating idle cycle",
    "Jumping":
        "jumping straight up and landing, knees bending deep before "
        "launch, arms swinging up for lift, feet leaving the ground, "
        "landing in a crouch, continuous repeating jump cycle",
    "Kicking":
        "throwing high kicks, legs snapping up alternately toward head "
        "height, arms out for balance, hips rotating into each kick, "
        "continuous repeating kicking cycle",
    "Laughing":
        "laughing hard, head thrown back, shoulders shaking up and "
        "down, one hand slapping the knee, belly heaving, whole body "
        "bouncing in a continuous repeating laugh",
    "Punching":
        "throwing powerful alternating punches, fists snapping forward "
        "one after the other, shoulders and hips rotating into each "
        "punch, feet planted in a fighting stance, continuous repeating "
        "punching cycle",
    "Running":
        "running fast with knees lifting high, arms pumping hard, feet "
        "leaving the ground mid-stride, body leaning forward, hair and "
        "clothes streaming back, continuous repeating running cycle",
    "Shooting":
        "aiming and firing a weapon, arms raised, recoil kicking the "
        "arms and shoulders back with each shot, body bracing and "
        "recovering, continuous repeating shooting cycle",
    "Slashing (sword)":
        "slashing with a sword in wide sweeping arcs, blade swinging "
        "diagonally across the body, hips and shoulders rotating into "
        "each swing, feet shifting stance, continuous repeating "
        "slashing cycle",
    "Sneaking":
        "sneaking forward on tiptoe with exaggerated slow steps, knees "
        "lifting high, body crouched low, arms raised carefully, head "
        "scanning side to side, continuous repeating sneaking cycle",
    "Stomping walk (heavy)":
        "stomping forward with heavy powerful steps, whole body "
        "shifting weight side to side with each footfall, arms swinging "
        "with momentum, shoulders rocking, continuous heavy walking "
        "cycle",
    "Taunting":
        "taunting mockingly, beckoning with one hand in a come-here "
        "gesture, shrugging shoulders, head tilting side to side, body "
        "swaying cockily in a continuous repeating cycle",
    "Walking":
        "walking forward with a steady stride, legs alternating in full "
        "steps, arms swinging naturally at the sides, whole body moving "
        "in a continuous walk cycle",
    "Walking (side view)":
        "walking in place in profile view, legs lifting and stepping in "
        "a clear repeating cycle, knees rising visibly, arms pumping "
        "back and forth, exaggerated cartoon walk cycle",
    "Waving":
        "waving hello with one arm raised high, hand sweeping side to "
        "side widely, shoulders and torso swaying along, continuous "
        "repeating waving cycle",
}


def frame_diff(a, b):
    """Mean pixel difference between two frames (0-255 scale)."""
    hst = ImageChops.difference(a.convert("RGB"),
                                b.convert("RGB")).histogram()
    return sum(hst[i % 256] * (i % 256)
               for i in range(len(hst))) / (a.width * a.height * 3)


def best_loop_cut(frames, min_len=8):
    """Cut the clip at its most similar frame pair — but only among
    segments that actually CONTAIN motion. Without the motion filter the
    cutter grabs the quietest chunk of a weak clip and the loop looks
    nearly static."""
    n = len(frames)
    if n < min_len + 3:
        return frames
    small = [f.convert("L").resize((64, 64)) for f in frames]

    def sdiff(a, b):
        hst = ImageChops.difference(a, b).histogram()
        return sum(hst[i] * i for i in range(len(hst))) / (64 * 64)

    succ = [sdiff(small[k], small[k + 1]) for k in range(n - 1)]
    clip_motion = sum(succ) / max(1, len(succ))
    best, fallback = None, None
    for i in range(0, max(1, n // 3)):
        for j in range(i + min_len, n):
            d = sdiff(small[i], small[j])
            if fallback is None or d < fallback[0]:
                fallback = (d, i, j)
            seg = succ[i:j - 1]
            seg_motion = sum(seg) / max(1, len(seg))
            if seg_motion < 0.6 * clip_motion:
                continue   # quiet segment — would loop as near-static
            if best is None or d < best[0]:
                best = (d, i, j)
    _, i, j = best if best is not None else fallback
    return frames[i:j]
ANIM_MOTION = {
    "Strong (recommended)":
        "Large, exaggerated, theatrical motion — the whole body moves "
        "dramatically and continuously through every single frame",
    "Normal":
        "Clear, continuous full-body motion throughout the clip",
    "Subtle":
        "Subtle, natural motion",
}
WAN_FLF_FILE = "wan2.1_flf2v_720p_14B_fp8_e4m3fn.safetensors"
WAN_CLIPVIS_FILE = "clip_vision_h.safetensors"
WAN21_VAE_FILE = "wan_2.1_vae.safetensors"


def build_wan_flf_graph(p):
    """Seamless loop via Wan 2.1 first-last-frame: the animation starts
    AND ends on the character's exact pose, so it loops playing forward —
    no ping-pong, no crossfade."""
    g = {}
    g["1"] = {"class_type": "UNETLoader",
              "inputs": {"unet_name": WAN_FLF_FILE,
                         "weight_dtype": "default"}}
    g["2"] = {"class_type": "CLIPLoader",
              "inputs": {"clip_name": WAN_TE_FILE, "type": "wan",
                         "device": "default"}}
    g["3"] = {"class_type": "VAELoader",
              "inputs": {"vae_name": WAN21_VAE_FILE}}
    g["4"] = {"class_type": "CLIPTextEncode",
              "inputs": {"text": p["prompt"], "clip": ["2", 0]}}
    g["5"] = {"class_type": "CLIPTextEncode",
              "inputs": {"text": p.get("negative", WAN_NEGATIVE),
                         "clip": ["2", 0]}}
    g["6"] = {"class_type": "LoadImage",
              "inputs": {"image": p["anim_image_name"]}}
    g["11"] = {"class_type": "CLIPVisionLoader",
               "inputs": {"clip_name": WAN_CLIPVIS_FILE}}
    g["12"] = {"class_type": "CLIPVisionEncode",
               "inputs": {"clip_vision": ["11", 0], "image": ["6", 0],
                          "crop": "none"}}
    g["7"] = {"class_type": "WanFirstLastFrameToVideo",
              "inputs": {"positive": ["4", 0], "negative": ["5", 0],
                         "vae": ["3", 0], "width": p["width"],
                         "height": p["height"], "length": p["length"],
                         "batch_size": 1,
                         "clip_vision_start_image": ["12", 0],
                         "clip_vision_end_image": ["12", 0],
                         "start_image": ["6", 0], "end_image": ["6", 0]}}
    g["8"] = {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["7", 0],
                         "negative": ["7", 1], "latent_image": ["7", 2],
                         "seed": p["seed"], "steps": p.get("steps") or 30,
                         "cfg": 5.5,
                         "sampler_name": "uni_pc", "scheduler": "simple",
                         "denoise": 1.0}}
    g["9"] = {"class_type": "VAEDecode",
              "inputs": {"samples": ["8", 0], "vae": ["3", 0]}}
    g["10"] = {"class_type": "SaveImage",
               "inputs": {"filename_prefix": "cbac_anim",
                          "images": ["9", 0]}}
    return g


def build_wan_graph(p):
    """Image-to-video via Wan 2.2 5B: the character image animates per
    the action prompt; every frame comes back as an image."""
    g = {}
    g["1"] = {"class_type": "UNETLoader",
              "inputs": {"unet_name": WAN_FILE, "weight_dtype": "default"}}
    g["2"] = {"class_type": "CLIPLoader",
              "inputs": {"clip_name": WAN_TE_FILE, "type": "wan",
                         "device": "default"}}
    g["3"] = {"class_type": "VAELoader",
              "inputs": {"vae_name": WAN_VAE_FILE}}
    g["4"] = {"class_type": "CLIPTextEncode",
              "inputs": {"text": p["prompt"], "clip": ["2", 0]}}
    g["5"] = {"class_type": "CLIPTextEncode",
              "inputs": {"text": p.get("negative", WAN_NEGATIVE),
                         "clip": ["2", 0]}}
    g["6"] = {"class_type": "LoadImage",
              "inputs": {"image": p["anim_image_name"]}}
    g["7"] = {"class_type": "Wan22ImageToVideoLatent",
              "inputs": {"vae": ["3", 0], "width": p["width"],
                         "height": p["height"], "length": p["length"],
                         "batch_size": 1, "start_image": ["6", 0]}}
    g["8"] = {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["4", 0],
                         "negative": ["5", 0], "latent_image": ["7", 0],
                         "seed": p["seed"], "steps": 20, "cfg": 5.0,
                         "sampler_name": "uni_pc", "scheduler": "simple",
                         "denoise": 1.0}}
    g["9"] = {"class_type": "VAEDecode",
              "inputs": {"samples": ["8", 0], "vae": ["3", 0]}}
    g["10"] = {"class_type": "SaveImage",
               "inputs": {"filename_prefix": "cbac_anim",
                          "images": ["9", 0]}}
    return g


_REMBG_DIR_CODE = (
    "import sys, pathlib; from PIL import Image; "
    "from rembg import remove, new_session; "
    "src = pathlib.Path(sys.argv[1]); dst = pathlib.Path(sys.argv[2]); "
    "s = new_session(sys.argv[3]); "
    "[remove(Image.open(f).convert('RGBA'), session=s).save(dst / f.name) "
    "for f in sorted(src.glob('*.png'))]")


def remove_background_dir(src_dir, dst_dir):
    """One rembg session for a whole folder of frames."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    (MODELS / "rembg").mkdir(parents=True, exist_ok=True)
    r = subprocess.run([str(engine_python()), "-c", _REMBG_DIR_CODE,
                        str(src_dir), str(dst_dir), "isnet-general-use"],
                       capture_output=True, text=True, timeout=1800,
                       creationflags=NO_WINDOW, env=_contained_env())
    if r.returncode != 0:
        raise RuntimeError(f"frame background removal failed: "
                           f"{r.stderr[-400:]}")


STAGE_BG = (200, 200, 205)   # the neutral staging color used for prep


def estimate_bg(img, patch=24):
    """Actual background color of a RAW (pre-cutout) frame, sampled from
    its four corners — the video model repaints the stage, so the true
    color drifts from the nominal staging gray."""
    import numpy as np
    arr = np.asarray(img.convert("RGB")).astype(np.float32)
    corners = np.concatenate([
        arr[:patch, :patch].reshape(-1, 3),
        arr[:patch, -patch:].reshape(-1, 3),
        arr[-patch:, :patch].reshape(-1, 3),
        arr[-patch:, -patch:].reshape(-1, 3)])
    return tuple(float(v) for v in corners.mean(axis=0))


def defringe(img, bg=STAGE_BG, erode=1, feather=0.5, alpha_floor=30):
    """Un-blend the background color out of semi-transparent edge pixels
    (pass the color measured from the RAW frame via estimate_bg), drop
    faint halo pixels, then tighten and smooth the silhouette."""
    import numpy as np
    arr = np.asarray(img.convert("RGBA")).astype(np.float32)
    a = arr[..., 3:4] / 255.0
    rgb = arr[..., :3]
    bg_arr = np.array(bg, dtype=np.float32)
    fg = (rgb - bg_arr * (1.0 - a)) / np.maximum(a, 1e-4)
    fg = np.clip(fg, 0, 255)
    alpha_ch = np.where(arr[..., 3] < alpha_floor, 0.0, arr[..., 3])
    out = np.concatenate([fg, alpha_ch[..., None]],
                         axis=-1).astype(np.uint8)
    res = Image.fromarray(out, "RGBA")
    alpha = res.getchannel("A")
    if erode:
        alpha = alpha.filter(ImageFilter.MinFilter(erode * 2 + 1))
    if feather:
        alpha = alpha.filter(ImageFilter.GaussianBlur(feather))
    res.putalpha(alpha)
    return res


def apply_loop(frames, mode):
    if mode.startswith("Ping-pong") and len(frames) > 2:
        return frames + frames[-2:0:-1]
    if mode.startswith("Crossfade") and len(frames) > 8:
        k = min(6, len(frames) // 4)
        out = list(frames)
        for i in range(k):
            t = (i + 1) / (k + 1)
            idx = len(frames) - k + i
            out[idx] = Image.blend(frames[idx].convert("RGBA"),
                                   frames[i].convert("RGBA"), t)
        return out
    return list(frames)


def save_gif(frames, path, fps, transparent):
    dur = max(20, int(round(1000 / fps)))
    if transparent:
        conv = []
        for f in frames:
            rgba = f.convert("RGBA")
            alpha = rgba.getchannel("A")
            p_img = rgba.convert("RGB").convert(
                "P", palette=Image.ADAPTIVE, colors=255)
            mask = alpha.point(lambda a: 255 if a <= 128 else 0)
            p_img.paste(255, mask)
            conv.append(p_img)
        conv[0].save(path, save_all=True, append_images=conv[1:],
                     duration=dur, loop=0, disposal=2, transparency=255)
    else:
        rgb = [f.convert("RGB") for f in frames]
        rgb[0].save(path, save_all=True, append_images=rgb[1:],
                    duration=dur, loop=0)


def save_video(frames, path, fps, webm=False, bg=(20, 20, 26)):
    """Encode frames to MP4 (h264) or WebM (vp9) via PyAV — self-contained
    (PyAV ships its own ffmpeg, so no external install). Video can't carry
    transparency, so RGBA frames are composited on a solid background;
    the GIF/sprite-sheet keep the alpha."""
    import av
    import numpy as np
    w, h = frames[0].size
    w -= w % 2
    h -= h % 2   # h264/vp9 need even dimensions
    container = av.open(str(path), mode="w")
    codec = "libvpx-vp9" if webm else "libx264"
    stream = container.add_stream(codec, rate=int(round(fps)))
    stream.width, stream.height = w, h
    stream.pix_fmt = "yuv420p"
    if not webm:
        stream.options = {"crf": "18", "preset": "medium"}
    try:
        for f in frames:
            flat = Image.new("RGB", (w, h), bg)
            rgba = f.convert("RGBA").resize((w, h), Image.LANCZOS)
            flat.paste(rgba, (0, 0), rgba)
            vf = av.VideoFrame.from_ndarray(np.asarray(flat), format="rgb24")
            for pkt in stream.encode(vf):
                container.mux(pkt)
        for pkt in stream.encode():
            container.mux(pkt)
    finally:
        container.close()


def save_sprite_sheet(frames, png_path, json_path, fps):
    """Pack frames into a single grid PNG (alpha preserved) with a JSON
    atlas describing the layout — drop-in for game engines/frontends."""
    import json as _json
    import math
    n = len(frames)
    fw, fh = frames[0].size
    cols = int(math.ceil(math.sqrt(n)))
    rows = int(math.ceil(n / cols))
    sheet = Image.new("RGBA", (cols * fw, rows * fh), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        sheet.paste(f.convert("RGBA"), ((i % cols) * fw, (i // cols) * fh))
    sheet.save(png_path)
    _json.dump({"frame_width": fw, "frame_height": fh, "columns": cols,
                "rows": rows, "frame_count": n, "fps": int(round(fps))},
               open(json_path, "w", encoding="utf-8"), indent=2)


def build_graph(p):
    """Build a ComfyUI prompt graph from generation params dict."""
    if p.get("edit_image_names"):
        return build_qwen_edit_graph(p) if p.get("editor") == "qwen" \
            else build_kontext_graph(p)
    fam = model_family(p["model"])
    d = FAMILY_DEFAULTS[fam]
    steps = p.get("steps") or d["steps"]
    cfg = p.get("cfg") if p.get("cfg") is not None else d["cfg"]

    g = {}
    g["1"] = {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": p["model"]}}

    # optional lora chain: model+clip pass through each loader
    model_ref, clip_ref = ["1", 0], ["1", 1]
    nid = 20
    for lora_name, strength in p.get("loras", []):
        g[str(nid)] = {"class_type": "LoraLoader",
                       "inputs": {"lora_name": lora_name,
                                  "strength_model": strength,
                                  "strength_clip": strength,
                                  "model": model_ref, "clip": clip_ref}}
        model_ref, clip_ref = [str(nid), 0], [str(nid), 1]
        nid += 1

    # anime / illustrious-family models are trained expecting clip skip 2 —
    # applying it fixes the slightly-off colour and detail they get otherwise
    if fam == "anime":
        g["19"] = {"class_type": "CLIPSetLastLayer",
                   "inputs": {"clip": clip_ref, "stop_at_clip_layer": -2}}
        clip_ref = ["19", 0]

    g["2"] = {"class_type": "CLIPTextEncode",
              "inputs": {"text": p["prompt"], "clip": clip_ref}}
    g["3"] = {"class_type": "CLIPTextEncode",
              "inputs": {"text": p.get("negative", ""), "clip": clip_ref}}

    pos_ref = ["2", 0]
    if fam in ("flux", "schnell"):
        g["4"] = {"class_type": "FluxGuidance",
                  "inputs": {"guidance": 3.5, "conditioning": pos_ref}}
        pos_ref = ["4", 0]

    # style reference via IP-Adapter (SDXL families only). Two sources can
    # now travel together, chained model→model off ONE UnifiedLoader:
    #   • image refs (style_ref_names): RAG-map example images and/or a
    #     Reference-DB person photo — applied with the basic IPAdapter node.
    #   • precomputed embeds (style_embed_names): an embeddings-only ("private")
    #     RAG map whose .ipadpt files are CLIP penultimate hidden states.
    # A single loader means one adapter + one clip_vision feed both stages, so
    # a private map and a face guide can steer the same generation without the
    # node-50 collision two loaders would cause. Either source alone builds the
    # exact graph it did before.
    style_imgs = p.get("style_ref_names")
    style_embeds = p.get("style_embed_names")
    if style_imgs or style_embeds:
        g["50"] = {"class_type": "IPAdapterUnifiedLoader",
                   "inputs": {"preset": "PLUS (high strength)",
                              "model": model_ref}}
        adapter_ref = ["50", 1]
        model_ref = ["50", 0]

        # embeds first: load each .ipadpt, concat (≤5), apply with the ADVANCED
        # IPAdapterEmbeds node. Its weight_type is WEIGHT_TYPES (linear, ease…,
        # style transfer) with NO "standard" — that lives only on the basic
        # IPAdapter node below; "linear" is the advanced-node equivalent of
        # standard uniform weighting.
        if style_embeds:
            load_ids = []
            lid = 52
            for nm in style_embeds[:5]:
                g[str(lid)] = {"class_type": "IPAdapterLoadEmbeds",
                               "inputs": {"embeds": nm}}
                load_ids.append(str(lid))
                lid += 1
            combine_inputs = {"method": "concat"}
            for idx, s in enumerate(load_ids, start=1):
                combine_inputs[f"embed{idx}"] = [s, 0]
            g["58"] = {"class_type": "IPAdapterCombineEmbeds",
                       "inputs": combine_inputs}
            g["59"] = {"class_type": "IPAdapterEmbeds",
                       "inputs": {"model": model_ref, "ipadapter": adapter_ref,
                                  "pos_embed": ["58", 0],
                                  "weight": p.get("style_weight", 0.8),
                                  "weight_type": "linear",
                                  "start_at": 0.0, "end_at": 1.0,
                                  "embeds_scaling": "V only"}}
            model_ref = ["59", 0]

        # image refs next: LoadImage (batched if several) into the basic
        # IPAdapter node, stacked on whatever the embeds stage produced.
        if style_imgs:
            img_ref, nid, bid = None, 31, 41
            for name in style_imgs[:6]:
                g[str(nid)] = {"class_type": "LoadImage",
                               "inputs": {"image": name}}
                if img_ref is None:
                    img_ref = [str(nid), 0]
                else:
                    g[str(bid)] = {"class_type": "ImageBatch",
                                   "inputs": {"image1": img_ref,
                                              "image2": [str(nid), 0]}}
                    img_ref = [str(bid), 0]
                    bid += 1
                nid += 1
            g["51"] = {"class_type": "IPAdapter",
                       "inputs": {"model": model_ref, "ipadapter": adapter_ref,
                                  "image": img_ref,
                                  "weight": p.get("style_weight", 0.8),
                                  "start_at": 0.0, "end_at": 1.0,
                                  "weight_type": p.get("ref_weight_type",
                                                       "standard")}}
            model_ref = ["51", 0]

    denoise = 1.0
    latent_ref = ["5", 0]
    if p.get("border_assets"):
        # masked generation: art is only painted in the border zone, the
        # center physically stays empty regardless of the theme
        bg_name, mask_name = p["border_assets"]
        g["10"] = {"class_type": "LoadImage", "inputs": {"image": bg_name}}
        g["5"] = {"class_type": "VAEEncode",
                  "inputs": {"pixels": ["10", 0], "vae": ["1", 2]}}
        g["12"] = {"class_type": "LoadImage", "inputs": {"image": mask_name}}
        g["13"] = {"class_type": "ImageToMask",
                   "inputs": {"image": ["12", 0], "channel": "red"}}
        g["14"] = {"class_type": "SetLatentNoiseMask",
                   "inputs": {"samples": ["5", 0], "mask": ["13", 0]}}
        latent_ref = ["14", 0]
    elif p.get("ref_image_name"):
        # img2img: reference image -> scale to canvas -> encode to latent
        g["10"] = {"class_type": "LoadImage",
                   "inputs": {"image": p["ref_image_name"]}}
        g["11"] = {"class_type": "ImageScale",
                   "inputs": {"image": ["10", 0], "width": p["width"],
                              "height": p["height"],
                              "upscale_method": "lanczos",
                              "crop": "center"}}
        g["5"] = {"class_type": "VAEEncode",
                  "inputs": {"pixels": ["11", 0], "vae": ["1", 2]}}
        denoise = p.get("denoise", 0.6)
    elif fam in ("flux", "schnell"):
        g["5"] = {"class_type": "EmptySD3LatentImage",
                  "inputs": {"width": p["width"], "height": p["height"],
                             "batch_size": 1}}
    else:
        g["5"] = {"class_type": "EmptyLatentImage",
                  "inputs": {"width": p["width"], "height": p["height"],
                             "batch_size": 1}}

    # Extra detail (FreeU): reweights the UNet skip connections for more
    # contrast and fine detail — a near-free quality bump on SDXL families
    # (not Flux/Schnell, which don't use this UNet shape).
    if p.get("freeu") and fam not in ("flux", "schnell"):
        g["18"] = {"class_type": "FreeU_V2",
                   "inputs": {"model": model_ref, "b1": 1.3, "b2": 1.4,
                              "s1": 0.9, "s2": 0.2}}
        model_ref = ["18", 0]

    g["6"] = {"class_type": "KSampler",
              "inputs": {"model": model_ref, "positive": pos_ref,
                         "negative": ["3", 0], "latent_image": latent_ref,
                         "seed": p["seed"], "steps": steps, "cfg": cfg,
                         "sampler_name": d["sampler"], "scheduler": d["scheduler"],
                         "denoise": denoise}}
    sampler_out = ["6", 0]
    # Hi-res fix: a REAL detail pass (not just the 4x model upscaler) —
    # upscale the latent and run a short second sampling at low denoise, so
    # detail is added and anatomy firms up. Skipped for img2img and border
    # jobs, where a second pass would fight the reference/mask.
    if p.get("hires") and not p.get("border_assets") \
            and not p.get("ref_image_name"):
        scale = float(p.get("hires_scale") or 1.5)
        g["60"] = {"class_type": "LatentUpscaleBy",
                   "inputs": {"samples": ["6", 0],
                              "upscale_method": "bislerp", "scale_by": scale}}
        g["61"] = {"class_type": "KSampler",
                   "inputs": {"model": model_ref, "positive": pos_ref,
                              "negative": ["3", 0], "latent_image": ["60", 0],
                              "seed": p["seed"],
                              "steps": max(10, int(steps * 0.6)), "cfg": cfg,
                              "sampler_name": d["sampler"],
                              "scheduler": d["scheduler"],
                              "denoise": float(p.get("hires_denoise") or 0.45)}}
        sampler_out = ["61", 0]
    g["7"] = {"class_type": "VAEDecode",
              "inputs": {"samples": sampler_out, "vae": ["1", 2]}}
    img_out = ["7", 0]
    # optional hi-res pass: run the decoded image through a 4x upscale model
    if p.get("upscale") and (MODELS / "upscale_models" / UPSCALE_MODEL).exists():
        g["40"] = {"class_type": "UpscaleModelLoader",
                   "inputs": {"model_name": UPSCALE_MODEL}}
        g["41"] = {"class_type": "ImageUpscaleWithModel",
                   "inputs": {"upscale_model": ["40", 0], "image": img_out}}
        img_out = ["41", 0]
    g["8"] = {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "cbac", "images": img_out}}
    return g


# --------------------------------------------------------------------------
# background removal (transparency) — runs in the engine venv so the
# packaged exe stays small and rembg's heavy deps never enter this process.
# --------------------------------------------------------------------------

_REMBG_CODE = ("import sys; from PIL import Image; "
               "from rembg import remove, new_session; "
               "img = Image.open(sys.argv[1]).convert('RGBA'); "
               "remove(img, session=new_session(sys.argv[3])).save(sys.argv[2])")


def remove_background(img):
    RAW_OUT.mkdir(parents=True, exist_ok=True)
    (MODELS / "rembg").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=RAW_OUT) as td:
        src, dst = Path(td) / "in.png", Path(td) / "out.png"
        img.save(src)
        r = subprocess.run([str(engine_python()), "-c", _REMBG_CODE, str(src),
                            str(dst), "isnet-general-use"],
                           capture_output=True, text=True, timeout=600,
                           creationflags=NO_WINDOW, env=_contained_env())
        if r.returncode != 0 or not dst.exists():
            raise RuntimeError(f"background removal failed: {r.stderr[-400:]}")
        out = Image.open(dst)
        out.load()
        return out


BORDER_SIZES = {
    "4:3  (1152x864)": (1152, 864),
    "16:9 (1280x720)": (1280, 720),
    "16:9 HD (1920x1080 — best on Flux)": (1920, 1080),
}
BORDER_NEGATIVE = ("text, letters, words, numbers, writing, typography, "
                   "logo, watermark, signature, caption, label, subtitles, "
                   "title banner, gibberish text, full scene, landscape, "
                   "scenery in the center, filled center, busy center, "
                   "background illustration in the middle, picture in the "
                   "middle, framed painting, content in the center, "
                   "full body character standing in the center, person in "
                   "the middle, large figure in the center, mascot in the "
                   "center, character blocking the center")

BORDER_CLEAN_PROMPT = (
    "Remove everything inside the frame. The entire center area must "
    "become plain solid white, completely empty, with no characters, no "
    "creatures, no objects, no scenery and no text in the middle. Keep "
    "the decorative border frame around the edges exactly as it is, "
    "unchanged. Only empty out the center.")
# Image-swap step 2: image 1 is the generated art, image 2 is the person's
# photo. Kontext keeps the scene and restyles the face to match, so a
# LoRA/RAG-built body becomes that specific person without touching the art.
# IMPORTANT: never name concrete traits (beard/glasses/etc.) in these
# prompts — with weak image grounding the words themselves leak into the
# picture (a woman's face came back as a bearded man because the prompt
# said "beard"). Only generic wording that defers entirely to the photo.
SWAP_PERSON_PROMPT = (
    "Replace the head and face of the person in the first image with the "
    "head and face of the person from the second image, so the character "
    "is unmistakably that person — the same face and the same hair, "
    "matching every visible feature of the person in the second image "
    "exactly, adding nothing that person does not have. Keep everything "
    "else from the first image exactly as it is — pose, body, clothing, "
    "framing, background, lighting, colors and art style. Match the "
    "first image's rendering style for the new face exactly: if the "
    "first image is photographic, keep the face photographic; if it is "
    "stylized art, render the face in that same style.")
# swaps follow the instruction better with guidance above the editing
# default 2.5; too high drifts the style
SWAP_GUIDANCE = 3.0
# NB: partial swap_denoise was measured and REJECTED as the fidelity fix —
# at 0.85 it preserved barely more than a full re-render yet already broke
# identity transfer. The shipped approach is swap_composite() below: full
# denoise for the swap, then restore the base's pixels wherever the swap
# didn't genuinely change anything. (The graphs keep the swap_denoise
# capability for future use.)


# --------------------------------------------------------------------------
# the local face-swap engine (insightface / inswapper) — the Clone Tool's
# default "Face swap" method. A true identity swap: detect the face in the
# freshly drawn image and replace it with the chosen person's face. Runs in
# the engine venv (like rembg), ~550 MB one-time, seconds per swap, and
# matches the selected person far more closely than a re-rendering editor.
# --------------------------------------------------------------------------
INSIGHTFACE_DIR = MODELS / "insightface"
INSWAPPER_FILE = "inswapper_128.onnx"
INSWAPPER_URL = ("https://huggingface.co/ezioruan/inswapper_128.onnx/"
                 "resolve/main/inswapper_128.onnx")
# SHA-256 of the canonical inswapper_128.onnx — the download is verified
# against this before it is kept, so a retargeted/tampered host cannot slip
# a different file onto the machine.
INSWAPPER_SHA256 = ("e4a3f08c753cb72d04e10aa0f7dbe3deebbf39567d4ead6"
                    "dce08e98aa49e16af")


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download_stream(url, dest, progress=None):
    """Stream a file to dest atomically (via a .part file), honouring the
    global CANCEL. progress(done, total) is called as it goes. A plain
    function so both the App and the Generator can use it."""
    dest = Path(dest)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                if CANCEL.is_set():
                    raise RuntimeError("cancelled")
                fh.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done, total)
    os.replace(tmp, dest)


def faceswap_ready():
    """True when the local face-swap engine can run: the swap model and the
    buffalo_l detector are on disk and install verified insightface imports
    (a .ready marker). A cheap file check — no subprocess."""
    return ((INSIGHTFACE_DIR / INSWAPPER_FILE).exists()
            and (INSIGHTFACE_DIR / "models" / "buffalo_l"
                 / "det_10g.onnx").exists()
            and (INSIGHTFACE_DIR / ".ready").exists())


_FACESWAP_CODE = r'''
import os, sys
os.environ["INSIGHTFACE_HOME"] = sys.argv[1]
import numpy as np, cv2
from insightface.app import FaceAnalysis
from insightface.model_zoo import get_model

def imread(p):
    try:
        return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None

def imwrite(p, img):
    ok, buf = cv2.imencode(os.path.splitext(p)[1] or ".png", img)
    if ok:
        buf.tofile(p)
    return ok

root, insw, base_path, out_path = sys.argv[1:5]
faces = sys.argv[5:]
gpu = ["CUDAExecutionProvider", "CPUExecutionProvider"]
cpu = ["CPUExecutionProvider"]
try:
    fa = FaceAnalysis(name="buffalo_l", root=root, providers=gpu)
    fa.prepare(ctx_id=0, det_size=(640, 640))
except Exception:
    fa = FaceAnalysis(name="buffalo_l", root=root, providers=cpu)
    fa.prepare(ctx_id=-1, det_size=(640, 640))
try:
    sw = get_model(insw, providers=gpu)
except Exception:
    sw = get_model(insw, providers=cpu)

def biggest(img):
    fs = fa.get(img)
    if not fs:
        return None
    return max(fs, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))

embs, srcf = [], None
for p in faces:
    im = imread(p)
    if im is None:
        continue
    f = biggest(im)
    if f is None:
        continue
    srcf = f
    embs.append(f.embedding)      # raw embedding; normed_embedding derives
if srcf is None or not embs:
    print("NOFACE_SOURCE")
    sys.exit(4)
# average the identity across all photos of the person, then let insightface
# recompute normed_embedding from it (that property is read-only in 2.0)
srcf.embedding = np.mean(embs, axis=0).astype(np.float32)
base = imread(base_path)
if base is None:
    print("NOBASE")
    sys.exit(5)
tfs = fa.get(base)
if not tfs:
    print("NOFACE_TARGET")
    sys.exit(6)
res = base.copy()
for tf in tfs:
    res = sw.get(res, tf, srcf, paste_back=True)
imwrite(out_path, res)
print("OK", len(tfs))
'''

_FACESWAP_INIT_CODE = (
    "import os, sys; os.environ['INSIGHTFACE_HOME'] = sys.argv[1]; "
    "from insightface.app import FaceAnalysis; "
    "fa = FaceAnalysis(name='buffalo_l', root=sys.argv[1]); "
    "fa.prepare(ctx_id=-1, det_size=(640, 640)); print('DET_OK')")


def run_face_swap(base_path, out_path, face_paths, timeout=900):
    """Swap the identity from face_paths onto base_path (writing out_path),
    in the engine venv. Returns (returncode, last_stdout_or_stderr_line)."""
    args = [str(engine_python()), "-c", _FACESWAP_CODE,
            str(INSIGHTFACE_DIR), str(INSIGHTFACE_DIR / INSWAPPER_FILE),
            str(base_path), str(out_path)] + [str(p) for p in face_paths]
    r = subprocess.run(args, capture_output=True, text=True,
                       creationflags=NO_WINDOW, env=_contained_env(),
                       timeout=timeout)
    lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
    tail = lines[-1] if lines else (r.stderr or "").strip()[-200:]
    return r.returncode, tail


SWAP_THR_CAP = 40.0        # a globally re-rendered base must not push the
SWAP_MIN_MASK = 0.015      # face out of reach; below this mask share the
#                            composite found no head — keep the raw swap


def swap_composite(base, swapped, feather=25, stats=None):
    """Merge a face-swap result back onto its base: keep `swapped` only
    where it genuinely differs (the reworked head and any truly changed
    areas), restore the base's exact pixels — reflections, lighting,
    fine detail — everywhere else. A wide morphological opening removes
    the thin ribbons caused by slight silhouette drift between the two
    renders, so outlines don't ghost.

    Two guards (v1.50): the threshold is relative to the mean change but
    CAPPED, because a photoreal base that the swap model re-renders all
    over (a RAG-guided picture, typically) raised the bar above the face
    change itself; and when the mask still covers less than SWAP_MIN_MASK
    of the picture no head was found, so the raw swap is returned rather
    than the base — the swap looked "never used" before. `stats`, when a
    dict is given, receives mean / thr / mask / used for the log.
    On any failure the raw `swapped` comes back unchanged."""
    try:
        import numpy as np
        b = np.asarray(base.convert("RGB"), dtype=np.float32)
        s = np.asarray(swapped.convert("RGB").resize(base.size),
                       dtype=np.float32)
        diff = np.abs(b - s).mean(axis=2)
        dimg = Image.fromarray(np.uint8(np.clip(diff, 0, 255)))
        diff = np.asarray(dimg.filter(ImageFilter.GaussianBlur(feather // 2)),
                          dtype=np.float32)
        mean = float(diff.mean())
        thr = min(max(16.0, 2.4 * mean), SWAP_THR_CAP)
        hard = Image.fromarray(np.uint8((diff >= thr) * 255))
        k = max(9, (min(base.size) // 32) | 1)
        hard = hard.filter(ImageFilter.MinFilter(k))
        hard = hard.filter(ImageFilter.MaxFilter(k + 4))
        mask_frac = float((np.asarray(hard) > 0).mean())
        if stats is not None:
            stats.update(mean=round(mean, 1), thr=round(thr, 1),
                         mask=round(mask_frac, 4))
        if mask_frac < SWAP_MIN_MASK:
            if stats is not None:
                stats["used"] = "raw"
            return swapped.convert("RGB").resize(base.size)
        soft = hard.filter(ImageFilter.GaussianBlur(feather))
        m = np.asarray(soft, dtype=np.float32)[..., None] / 255.0
        out = b * (1.0 - m) + s * m
        if stats is not None:
            stats["used"] = "composite"
        return Image.fromarray(np.uint8(np.clip(out, 0, 255)))
    except Exception:
        if stats is not None:
            stats["used"] = "raw-error"
        return swapped
# Qwen Image Edit is natively multi-image (image1/image2) and transfers
# identity far more reliably than Kontext's chained references — it is the
# preferred swap engine whenever its models are installed (live A/B 2026-08:
# Qwen 2/2 pairs, Kontext base-first 0/2, face-first 1/2).
# enumerate ABSTRACT attributes only (structure/tone/color/length/gender)
# — attribute names steer the transfer without being drawable objects, so
# nothing can leak into the picture the way entity nouns did
QWEN_SWAP_PROMPT = (
    "Replace the head and face of the person in image 1 with the head and "
    "face of the person from image 2, so the character in image 1 is "
    "unmistakably the person from image 2: copy their facial structure, "
    "their eyes, their skin tone, and their hair color, hair length and "
    "hairstyle exactly as they appear in image 2, adding nothing that "
    "person does not have. The character's gender and age must match the "
    "person in image 2. Keep everything else in image 1 exactly "
    "unchanged — pose, body, clothing, framing, background, lighting, "
    "colors and art style. Match image 1's rendering style for the new "
    "face exactly: if image 1 is photographic, keep the face "
    "photographic; if it is stylized art, render the face in that same "
    "style.")
def swap_face_prompt(editor, n_faces):
    """The swap instruction, adjusted when several photos of the SAME
    person are supplied (they sharpen the likeness — e.g. a multi-photo
    Actor DB or several loaded pictures)."""
    if editor == "qwen":
        if n_faces <= 1:
            return QWEN_SWAP_PROMPT
        refs = "images 2 and 3" if n_faces == 2 else "images 2, 3 and 4"
        return (QWEN_SWAP_PROMPT.replace("image 2", refs)
                + f" {refs[0].upper() + refs[1:]} are photos of the same "
                  "person — combine them for a complete, accurate "
                  "likeness.")
    if n_faces <= 1:
        return SWAP_PERSON_PROMPT
    return (SWAP_PERSON_PROMPT.replace("the second image",
                                       "the later images")
            + " The images after the first are all photos of the same "
              "person — combine them for a complete, accurate likeness.")


BORDER_SAME_MODEL = "Same as main (default)"
BORDER_LORA_FILE = "SDXL_BorderFrames_v1.safetensors"
BORDER_TRIGGER = "cbacframe"
BORDER_TEMPLATE = (
    "a highly detailed ornate decorative {theme} frame, presented as a "
    "single frame-shaped object floating at the center of a plain solid "
    "pure white background, a wide even margin of empty white space "
    "separates the ornate frame from all four edges so the frame does "
    "NOT touch the edges of the image, the entire middle is plain empty "
    "pure white with absolutely nothing in it, the frame has an "
    "irregular decorative outer silhouette with protruding ornaments and "
    "an intricate organic inner edge, elaborate corner pieces and themed "
    "cartouches built from the props, symbols and colors of {theme}, "
    "ornaments of varying depth, ornate game UI bezel, concept art "
    "quality, intricate detail, completely textless with no lettering, "
    "only the frame itself is colored and detailed, everything around it "
    "and in the center is pure flat white")



def _add_margin(img, margin_pct):
    """Shrink the (already alpha-cut) frame and center it on a fully
    transparent canvas, so the frame floats free of the image edges like
    the reference bezels instead of bleeding to the border."""
    if margin_pct <= 0:
        return img
    w, h = img.size
    scale = 1.0 - 2 * margin_pct / 100.0
    sw, sh = max(1, int(w * scale)), max(1, int(h * scale))
    small = img.resize((sw, sh), Image.LANCZOS)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.paste(small, ((w - sw) // 2, (h - sh) // 2), small)
    return canvas


def _center_hole(img, thresh=70):
    """Flood the plain central region by its own color and return
    (hole_bool_array, frac, edge_touch). A clean framed center is a large
    single void that does not reach the image edges."""
    import numpy as np
    w, h = img.size
    cx, cy = w // 2, h // 2
    seeds = [(cx, cy), (cx, int(h * 0.35)), (cx, int(h * 0.65)),
             (int(w * 0.3), cy), (int(w * 0.7), cy),
             (int(w * 0.4), int(h * 0.4)), (int(w * 0.6), int(h * 0.6))]
    fill = img.convert("RGB")
    sent = (255, 0, 255)
    for s in seeds:
        try:
            ImageDraw.floodfill(fill, s, sent, thresh=thresh)
        except Exception:
            pass
    a = np.asarray(fill)
    hole = ((a[..., 0] == 255) & (a[..., 1] == 0) & (a[..., 2] == 255))
    frac = float(hole.mean())
    edge_touch = bool(hole[0].any() or hole[-1].any()
                      or hole[:, 0].any() or hole[:, -1].any())
    return hole, frac, edge_touch


def border_center_clean(img):
    """True when the frame already has a large, clean, enclosed empty
    center (so no second Kontext clean-up pass is needed)."""
    try:
        _hole, frac, edge_touch = _center_hole(img.convert("RGBA"))
        return 0.30 < frac < 0.9 and not edge_touch
    except Exception:
        return True   # never block generation on a detection hiccup


def cut_center(img, thickness_pct, margin_pct=6):
    """Turn a frame render into a floating transparent-background bezel.

    The border LoRA draws the frame in color with a plain (white/grey)
    empty center. We flood that center region by its OWN color so the
    transparency follows the frame's REAL organic inner edge (octagon,
    arch, scalloped, …) instead of a rectangular cut. Then we add a
    transparent outer margin so the frame doesn't touch the screen edge.

    Falls back to a soft rectangle only when no clean center is found
    (e.g. the theme filled the middle), so a border is never worse than
    before."""
    import numpy as np
    img = img.convert("RGBA")
    w, h = img.size
    rect_t = max(8, int(min(w, h) * thickness_pct / 100))

    def rect_alpha():
        m = Image.new("L", (w, h), 255)
        ImageDraw.Draw(m).rectangle(
            (rect_t, rect_t, w - rect_t, h - rect_t), fill=0)
        return m.filter(ImageFilter.GaussianBlur(2))

    try:
        # generous threshold keys the plain center whatever its shade,
        # following its organic outline; must be a real central void
        hole, frac, edge_touch = _center_hole(img, 70)
        if 0.18 < frac < 0.9 and not edge_touch:
            alpha = np.where(hole, 0, 255).astype(np.uint8)
            # never eat the outermost frame pixels
            m = max(3, rect_t // 3)
            alpha[:m, :] = 255; alpha[-m:, :] = 255
            alpha[:, :m] = 255; alpha[:, -m:] = 255
            am = Image.fromarray(alpha, "L").filter(
                ImageFilter.GaussianBlur(1.2))
            img.putalpha(am)
            return _add_margin(img, margin_pct)
    except Exception:
        pass
    img.putalpha(rect_alpha())
    return _add_margin(img, margin_pct)


# --------------------------------------------------------------------------
# generation worker
# --------------------------------------------------------------------------

class ChannelQueue:
    """Relabels progress messages so each section drives its own bar."""

    def __init__(self, q, channel):
        self.q, self.channel = q, channel

    def put(self, msg):
        if msg and msg[0] == "progress":
            self.q.put((self.channel,) + tuple(msg[1:]))
        else:
            self.q.put(msg)


RAG_DUP_SIM = 0.97      # cosine above this = the same picture twice
_RAG_STOP = {"a", "an", "the", "of", "in", "on", "at", "with", "and", "or",
             "to", "is", "are", "was", "this", "that", "it", "its", "for",
             "by", "from", "as", "he", "she", "they", "his", "her", "their",
             "there", "image", "photo", "picture", "shot"}


def _caption_keywords(caption, limit=24):
    """Match-worthy words from a caption. Trainer-built maps carry
    captions/tags but no separate keyword list, so we derive one."""
    out = []
    for w in re.findall(r"[a-z0-9]+", (caption or "").lower()):
        if len(w) > 2 and w not in _RAG_STOP and w not in out:
            out.append(w)
    return out[:limit]


def _ragmap_file(path):
    """Accept the map file itself or the folder holding it."""
    p = Path(path)
    if not p.is_dir():
        return p
    for pat in ("*.ragmap.json", "index.json", "*.json"):
        hits = sorted(p.glob(pat))
        if hits:
            return hits[0]
    raise FileNotFoundError(f"no .ragmap.json or index.json in {p}")


def _read_safetensors_f32(path, key):
    """Minimal safetensors reader — 8-byte LE header length, JSON header,
    then raw tensor bytes. Returns an [n, dim] float32 array, or None if
    numpy is missing or the file isn't the 2-D float matrix we expect
    (embeddings are a bonus; the map works without them)."""
    try:
        import numpy as np
    except ImportError:
        return None
    try:
        with open(path, "rb") as f:
            hlen = int.from_bytes(f.read(8), "little")
            head = json.loads(f.read(hlen).decode("utf-8"))
            info = head.get(key) or next(
                (v for k, v in head.items() if k != "__metadata__"), None)
            if not isinstance(info, dict) or info.get("dtype") != "F32" \
                    or len(info.get("shape", [])) != 2:
                return None
            start, end = info["data_offsets"]
            f.seek(8 + hlen + start)
            buf = f.read(end - start)
        rows, dim = info["shape"]
        arr = np.frombuffer(buf, dtype="<f4")
        if arr.size != rows * dim:
            return None
        arr = arr.reshape(rows, dim)
        norm = np.linalg.norm(arr, axis=1, keepdims=True)
        return arr / np.where(norm == 0, 1, norm)   # cosine == dot product
    except Exception:
        return None


def _safetensors_metadata(path):
    """The __metadata__ dict from a safetensors header (kohya/Civitai store
    training info there), or {} on any problem."""
    try:
        with open(path, "rb") as f:
            hlen = int.from_bytes(f.read(8), "little")
            if hlen <= 0 or hlen > 20_000_000:
                return {}
            head = json.loads(f.read(hlen).decode("utf-8", "replace"))
        md = head.get("__metadata__", {})
        return md if isinstance(md, dict) else {}
    except Exception:
        return {}


_LORA_TRIGGER_CACHE = {}


def lora_trigger(name):
    """Best-effort activation keyword(s) for a LoRA, so the app can inject them
    into the prompt automatically — the user never has to type the trigger by
    hand. Looked up (in order) from:

      1. a Civitai/JSON sidecar next to the file (trainedWords / activation
         text) — the authoritative trained words when present;
      2. the safetensors metadata `modelspec.trigger_phrase`;
      3. the kohya `ss_output_name` as a last resort (short, alphabetic).

    Returns a string ('' if nothing usable — many style LoRAs need no trigger,
    and their weights apply regardless). Result cached per filename."""
    if name in _LORA_TRIGGER_CACHE:
        return _LORA_TRIGGER_CACHE[name]
    trig = ""
    try:
        path = _ensure_lora_dir() / name
        stem = path.with_suffix("")
        for side in (Path(str(stem) + ".civitai.info"),
                     stem.with_suffix(".json")):
            # bounded read: a sidecar is a few KB of JSON — refuse to
            # slurp anything unreasonable into memory
            if side.exists() and side.stat().st_size <= 5_000_000:
                try:
                    j = json.loads(side.read_text(encoding="utf-8",
                                                  errors="replace"))
                except Exception:
                    continue
                words = (j.get("trainedWords") or j.get("activation text")
                         or j.get("trigger") or [])
                if isinstance(words, str):
                    words = [words]
                words = [w.strip() for w in words
                         if isinstance(w, str) and w.strip()]
                if words:
                    trig = ", ".join(words[:4])
                    break
        if not trig and path.exists():
            md = _safetensors_metadata(path)
            tp = md.get("modelspec.trigger_phrase") or md.get("ss_trigger_words")
            if isinstance(tp, str) and tp.strip():
                trig = tp.strip()
            else:
                on = md.get("ss_output_name")
                if isinstance(on, str) and re.search(r"[A-Za-z]", on) \
                        and len(on) <= 40 and "_" not in on[:1]:
                    trig = on.strip()
    except Exception:
        trig = ""
    # cap what a file can inject into the prompt (a metadata header may
    # legally be megabytes — its trigger phrase must not be)
    trig = trig.strip()[:200]
    _LORA_TRIGGER_CACHE[name] = trig
    return trig


def load_ragmap(path, progress=None):
    """Load a RAG map paired with a trained LoRA. Two layouts are read:

      * this app's flat map — cbac-ragmap/1:
        {"schema":"cbac-ragmap/1","name":..,"lora":<file>,"trigger":<word>,
         "image_dir":"images","weight":0.8,"top_k":4,
         "entries":[{"image":"0001.png","keywords":[...],"caption":"..."}]}
      * a trainer's retrieval map — Laura-Trainer writes <name>-rag\\
        index.json + images\\ + embeddings.safetensors beside <name>.
        safetensors: {"kind":"lora-retrieval-map","images_dir":"images",
         "lora":<name, no extension>,"embeddings_file":..,"entries":
         [{"index":0,"image":"0001.png","caption":"..."}]}

    Both are normalised to the first shape. Every part is optional: a map
    whose images didn't travel still contributes captions, one whose LoRA
    isn't installed still guides with its images."""
    mp = _ragmap_file(path)
    if progress:
        progress("reading", 0, 0)
    data = json.loads(mp.read_text(encoding="utf-8"))
    base = mp.resolve().parent
    data["_file"], data["_base"] = str(mp), str(base)
    try:
        st = mp.stat()
        data["_sig"] = (st.st_mtime, st.st_size)   # "has it changed?" key
    except OSError:
        data["_sig"] = None
    data["_kind"] = "trainer" if (data.get("kind") == "lora-retrieval-map"
                                  or "images_dir" in data) else "cbac"

    # images: either spelling of the key, and tolerate it being absent or
    # pointing at a folder that didn't travel with the map
    rel = data.get("image_dir") or data.get("images_dir") or ""
    img_dir = (base / rel) if rel else base
    if not img_dir.is_dir():
        img_dir = base / "images" if (base / "images").is_dir() else base
    data["image_dir"] = str(img_dir)

    # embeddings-only maps ship precomputed IP-Adapter embeds (.ipadpt) in
    # place of viewable images: the references still guide generation, but the
    # source pictures never travel as files anyone can open. Resolve those
    # beside the map the same way images are.
    emb_rel = data.get("embeds_dir") or ""
    embeds_dir = (base / emb_rel) if emb_rel else base
    if not embeds_dir.is_dir():
        embeds_dir = base / "embeds" if (base / "embeds").is_dir() else base
    data["embeds_dir"] = str(embeds_dir)

    has_embed = False
    has_image = False
    entries = data.get("entries", [])
    total = len(entries)
    for n_i, e in enumerate(entries):
        if progress and n_i % 5000 == 0:
            progress("resolving", n_i, total)
        img_name = e.get("image", "")
        if img_name:
            ip = Path(img_name)
            p = ip if ip.is_absolute() else (img_dir / ip)
            if not p.exists() and (base / ip.name).exists():
                p = base / ip.name          # images sitting beside the map
            # only a real file counts as an image; a bare folder must not read
            # as "has image" or retrieval would try to feed a directory
            e["_path"] = str(p) if p.is_file() else ""
        else:
            e["_path"] = ""
        emb_name = e.get("embed", "")
        if emb_name:
            ep = Path(emb_name)
            ep = ep if ep.is_absolute() else (embeds_dir / ep)
            if not ep.exists() and (base / Path(emb_name).name).exists():
                ep = base / Path(emb_name).name
            e["_ipadpt"] = str(ep) if ep.is_file() else ""
        else:
            e["_ipadpt"] = ""
        has_embed = has_embed or bool(e["_ipadpt"])
        has_image = has_image or bool(e["_path"])
        if not e.get("keywords"):
            e["keywords"] = _caption_keywords(e.get("caption", ""))
        # retrieval words, once — a map can hold hundreds of thousands of
        # entries, and re-tokenising every one on every Generate click
        # stalled the UI for seconds
        e["_words"] = frozenset(sys.intern(w) for w in re.findall(
            r"[a-z0-9]+", (" ".join(e["keywords"]) + " "
                           + e.get("caption", "")).lower()))
    # embeddings-only: the builder said so, or there are embeds and no images
    # the inverted index retrieval uses instead of walking every entry
    if progress:
        progress("indexing", 0, 0)
    data["_index"] = _build_rag_index(data.get("entries", []))
    data["_embeds_only"] = (str(data.get("mode", "")).lower() == "embeddings-only"
                            or (has_embed and not has_image))

    if not data.get("name"):
        stem = base.name if mp.name.lower() == "index.json" \
            else mp.name.split(".")[0]
        data["name"] = re.sub(r"[-_]rag$", "", stem, flags=re.I) or stem

    # the paired LoRA: trainers name it without the extension, and the
    # file itself usually sits in the map's folder or just above it
    lora = str(data.get("lora") or "").strip()
    if lora:
        if not lora.lower().endswith(".safetensors"):
            lora += ".safetensors"
        data["lora"] = lora
        for cand in (base / lora, base.parent / lora):
            if cand.exists():
                data["_lora_src"] = str(cand)
                break

    # optional CLIP embeddings — nested {"embeddings": {"file":..,"key":..}}
    # or the flat embeddings_file/embeddings_key pair
    if progress:
        progress("embeddings", 0, 0)
    data["_emb"] = None
    spec = data.get("embeddings")
    if not isinstance(spec, dict):
        spec = {"file": data.get("embeddings_file"),
                "key": data.get("embeddings_key")}
    ef = spec.get("file")
    if ef and (base / ef).exists():
        m = _read_safetensors_f32(base / ef,
                                  spec.get("key") or "image_embeddings")
        if m is not None and len(m) == len(data.get("entries", [])):
            data["_emb"] = m        # row i belongs to entry i
    return data


def ragmap_lora(ragmap, installed=None):
    """The paired LoRA as it is actually installed here (tolerating case
    and a missing .safetensors), or "" when it isn't on this machine —
    the map is still usable without it."""
    want = str((ragmap or {}).get("lora") or "").strip()
    if not want:
        return ""
    stem = Path(want).stem.lower()
    for name in (list_loras() if installed is None else installed):
        if Path(name).stem.lower() == stem:
            return name
    return ""


def _build_rag_index(entries):
    """Inverted index word -> np.int32 array of entry indices, plus a
    has-reference mask, so retrieval touches only the entries that share
    a word with the prompt and ranks them in numpy. Built once, off the
    UI thread, by load_ragmap. None without numpy — retrieval then walks
    every entry as before."""
    try:
        import numpy as np
    except ImportError:
        return None
    post = {}
    has_ref = np.zeros(len(entries), dtype=bool)
    for i, e in enumerate(entries):
        if e.get("_path") or e.get("_ipadpt"):
            has_ref[i] = True
        for w in e.get("_words", ()):
            lst = post.get(w)
            if lst is None:
                post[w] = lst = []
            lst.append(i)
    vocab = {w: np.asarray(lst, dtype=np.int32) for w, lst in post.items()}
    return (vocab, has_ref, np)


def ragmap_retrieve(ragmap, prompt, k=None, require_image=True, rng=None):
    """Return the top-k entries whose keywords/caption best match the
    prompt words (falls back to the first k valid entries if nothing
    matches, so the LoRA always gets representative guidance). When the
    map ships CLIP embeddings, an entry that is a near-duplicate of one
    already picked is skipped, so the k references actually differ.
    require_image=False also considers entries whose reference is missing —
    those can still be used as caption text. A "reference" is either a real
    image file (_path) or a precomputed IP-Adapter embed (_ipadpt), so
    embeddings-only maps retrieve exactly like image maps."""
    if not ragmap:
        return []
    k = k or int(ragmap.get("top_k") or 4)
    words = set(re.findall(r"[a-z0-9]+", (prompt or "").lower()))
    entries = ragmap.get("entries", [])
    index = ragmap.get("_index")
    if index is not None:
        # score only the entries sharing a word with the prompt, rank in
        # numpy: the per-entry Python walk below took 5.5 s per Generate
        # (on the UI thread) on a 481k-entry map. Same answers: score =
        # shared-word count, ties in original order, excluded entries out.
        vocab, has_ref, np = index
        counts = np.zeros(len(entries), dtype=np.int32)
        for w in words:
            hit = vocab.get(w)
            if hit is not None:
                counts[hit] += 1
        if require_image:
            counts = np.where(has_ref, counts, -1)
        order = np.argsort(-counts, kind="stable")
        if require_image:
            order = order[counts[order] >= 0]
        cands = [(int(i), entries[i]) for i in order[:max(k * 50, 200)]]
        if rng is not None:
            cands = _rag_shuffle(cands, k, rng)
        return _rag_diverse(cands, ragmap.get("_emb"), k)
    scored = []
    for i, e in enumerate(entries):
        if require_image and not (e.get("_path") or e.get("_ipadpt")):
            continue
        ewords = e.get("_words")
        if ewords is None:          # an entry not made by load_ragmap
            text = " ".join(e.get("keywords", [])) + " " + e.get("caption", "")
            ewords = frozenset(re.findall(r"[a-z0-9]+", text.lower()))
        scored.append((len(words & ewords), i, e))
    scored.sort(key=lambda t: t[0], reverse=True)   # stable → original order on ties
    cands = [(i, e) for _s, i, e in scored]
    if rng is not None:
        cands = _rag_shuffle(cands, k, rng)
    return _rag_diverse(cands, ragmap.get("_emb"), k)


def _rag_shuffle(cands, k, rng, pool=None):
    """Reorder the strongest candidates at random, favouring better ranks,
    so the same prompt does not fetch the same references every time.

    Retrieval used to be fully deterministic: one prompt -> the same top-k
    entries -> the same reference faces -> the same person in every
    picture, only the pose changing with the seed. Now the head of the
    ranked list (the best `pool` entries) is drawn without replacement
    with weights that fall off gently with rank, and the rest follows in
    rank order for the diversity top-up. A fixed seed gives a fixed draw."""
    pool = pool or max(k * 8, 32)
    head, tail = list(cands[:pool]), cands[pool:]
    weights = [1.0 / (1.0 + i / 8.0) for i in range(len(head))]
    order = []
    while head:
        j = rng.choices(range(len(head)), weights=weights, k=1)[0]
        order.append(head.pop(j))
        weights.pop(j)
    return order + list(tail)


def _rag_diverse(cands, emb, k):
    """Take k entries off the ranked list, skipping ones that look like a
    picture already taken. Tops up in rank order if that left too few."""
    if emb is None:
        return [e for _i, e in cands[:k]]
    picked, vecs = [], []
    for i, e in cands:
        if len(picked) >= k:
            break
        v = emb[i] if i < len(emb) else None
        if v is not None and any(float(v @ r) > RAG_DUP_SIM for r in vecs):
            continue
        picked.append(e)
        if v is not None:
            vecs.append(v)
    for _i, e in cands:
        if len(picked) >= k:
            break
        if e not in picked:
            picked.append(e)
    return picked


# ---- optional prompt enhancer (Ollama) ---------------------------------
# Strictly optional and never installed by this app: if Ollama isn't
# running, the button explains what it is once and everything else works
# exactly as before. No new Python dependency — plain HTTP over requests.
ENHANCE_SYSTEM = (
    "You rewrite prompts for an AI image generator. Reply with "
    "ONE line: the rewritten prompt as comma-separated visual phrases. No "
    "preamble, no quotes, no explanation, no line breaks. Keep every "
    "subject, character and action the user named, and add only visual "
    "detail: composition, camera angle, lighting, colour, linework, "
    "materials, mood. Never add text, captions, speech balloons, logos or "
    "watermarks. Aim for 40-70 words.")
_ENHANCE_LEAD = re.compile(
    r"^\s*(sure|certainly|of course|here(?:'s| is| you go)|okay|ok|"
    r"(?:enhanced|rewritten|improved|final)?\s*prompt)\b[^:]{0,40}:\s*", re.I)

# The built-in offline enhancer needs no Ollama and no network: it keeps
# the user's words and adds composition + quality phrasing tuned to the
# target model family. It is always available, so the Enhance dropdown is
# never empty. A local Ollama model (when present) does a smarter rewrite.
BUILTIN_ENHANCER = "Built-in (offline)"
_ENHANCE_QUALITY = {
    "flux":    "sharp focus, fine detail, natural lighting, high dynamic range",
    "schnell": "sharp focus, fine detail, natural lighting",
    "turbo":   "sharp focus, crisp detail, balanced lighting",
    "anime":   "clean lineart, vibrant colours, expressive shading, "
               "detailed background",
    "sdxl":    "highly detailed, sharp focus, dramatic lighting, rich "
               "colour, intricate detail, professional composition",
}
_ENHANCE_EXTRAS = ("dynamic composition", "depth of field",
                   "volumetric lighting", "cinematic")


def _ollama_url():
    """Ollama's endpoint, honouring OLLAMA_HOST (which is often set as a
    bare host:port, without a scheme)."""
    h = (os.environ.get("OLLAMA_HOST") or "127.0.0.1:11434").strip()
    if not h.startswith(("http://", "https://")):
        h = "http://" + h
    return h.rstrip("/")


def ollama_models(timeout=2):
    """The models installed in Ollama, or [] when it isn't running. Never
    raises — an absent optional feature must not surface as an error."""
    try:
        r = requests.get(f"{_ollama_url()}/api/tags", timeout=timeout)
        r.raise_for_status()
        return sorted(m["name"] for m in r.json().get("models", [])
                      if m.get("name"))
    except Exception:
        return []


def _clean_enhanced(out, original):
    """Models like to answer in prose. Strip any reasoning block, take the
    longest line, drop the "Sure, here's..." lead-in and stray quoting.
    Returns "" if what came back is too thin to be an improvement."""
    out = re.sub(r"(?s)<think>.*?</think>", " ", out or "")
    lines = [ln.strip(" \t-*•\"'") for ln in out.splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    best = _ENHANCE_LEAD.sub("", max(lines, key=len)).strip(" \t-*•\"'")
    return best if len(best) >= max(12, len(original.strip()) // 2) else ""


def ollama_enhance(text, model, style="", timeout=120):
    """Expand a short prompt into a fuller one with a local Ollama model."""
    ask = f"Rewrite this image prompt: {text.strip()}"
    if style.strip():
        ask += f"\nStay within this house style: {style.strip()}"
    r = requests.post(f"{_ollama_url()}/api/generate", timeout=timeout,
                      json={"model": model, "prompt": ask,
                            "system": ENHANCE_SYSTEM, "stream": False,
                            "options": {"temperature": 0.8}})
    if r.status_code == 404:
        raise RuntimeError(f"Ollama has no model called '{model}'. "
                           f"Install it with:  ollama pull {model}")
    r.raise_for_status()
    return _clean_enhanced(r.json().get("response", ""), text)


def builtin_enhance(text, style="", family="sdxl"):
    """Expand a short prompt into a fuller one with NO external model:
    keep the user's words, fold in the house style, then add composition
    and quality phrasing suited to the target model family. Skips any
    phrase already present. Never adds text, logos or watermarks.
    Returns "" only when there is nothing to work with."""
    base = " ".join((text or "").split()).strip().rstrip(",")
    if not base:
        return ""
    # a light emphasis on the main subject (the first clause) nudges SDXL-family
    # models to stick to it; Flux ignores attention weights, so leave it be
    if family not in ("flux", "schnell") and "(" not in base:
        head, sep, rest = base.partition(",")
        head = head.strip()
        if head and len(head) <= 60:
            base = f"({head}:1.15)" + (sep + rest if sep else "")
    have = base.lower()
    parts = [base]
    st = (style or "").strip().rstrip(",")
    if st and st.lower() not in have:
        parts.append(st)
        have += " " + st.lower()
    quality = _ENHANCE_QUALITY.get(family, _ENHANCE_QUALITY["sdxl"])
    for phrase in list(_ENHANCE_EXTRAS) + [q.strip() for q in
                                           quality.split(",")]:
        w = phrase.strip()
        if w and w.lower() not in have:
            parts.append(w)
            have += " " + w.lower()
    return ", ".join(p for p in parts if p)


def make_collage(paths, w, h):
    """Compose the reference image(s) onto one w x h canvas — a lone ref
    is letterboxed, several tile in a grid. The editor redraws this
    canvas, so its aspect drives the output aspect."""
    canvas = Image.new("RGB", (w, h), (32, 32, 36))
    n = max(1, len(paths))
    cols = max(1, min(n, int((n * w / max(1, h)) ** 0.5 + 0.5)))
    rows = -(-n // cols)
    cw, ch = w // cols, h // rows
    for k, p in enumerate(paths):
        try:
            img = Image.open(p).convert("RGB")
        except Exception:
            continue
        img.thumbnail((cw, ch), Image.LANCZOS)
        canvas.paste(img, ((k % cols) * cw + (cw - img.width) // 2,
                           (k // cols) * ch + (ch - img.height) // 2))
    return canvas


# One shared cancel flag. /interrupt only stops the job the engine is
# running right now; a Variations run is a loop HERE that posts the next
# prompt straight afterwards, so without this the set kept going and
# "Cancel" only skipped one picture. Every worker loop checks it.
CANCEL = threading.Event()


def cancel_all():
    """Stop the current job and anything queued behind it."""
    CANCEL.set()
    for path, payload in (("/interrupt", None), ("/queue", {"clear": True})):
        try:
            requests.post(f"{ENGINE_URL}{path}", json=payload, timeout=5)
        except requests.RequestException:
            pass


class Generator:
    """Queues a prompt and streams progress/results back through a queue."""

    def __init__(self, ui_queue):
        self.q = ui_queue
        self.client_id = str(uuid.uuid4())

    def run(self, params):
        try:
            self._run(params)
        except Exception as e:
            applog.exception("generation failed")
            self.q.put(("error", f"{type(e).__name__}: {e}"))

    def _run(self, params):
        import websocket  # websocket-client
        if params.get("ref_images"):
            # instruction editing: the images are context for the prompt
            if params.get("ref_collage_size"):
                # border mode: shape the refs to the border canvas so the
                # editor's output keeps the requested aspect
                w0, h0 = params["ref_collage_size"]
                collage = make_collage(params["ref_images"], w0, h0)
                params["edit_image_names"] = [
                    self._upload_pil(collage, "cbac_border_ref.png")]
            else:
                params["edit_image_names"] = [self._upload_ref(rp)
                                              for rp in params["ref_images"]]
        # RAG map: upload the retrieved example images so build_graph can
        # feed them through IP-Adapter as visual guidance
        if params.get("rag_ref_paths"):
            names = []
            for rp in params["rag_ref_paths"]:
                try:
                    if Path(rp).exists():
                        names.append(self._upload_ref(rp))
                except Exception:
                    pass
            if names:
                params["style_ref_names"] = names
        # embeddings-only RAG map: copy the retrieved .ipadpt embeds into the
        # engine's input folder so IPAdapterLoadEmbeds can read them by name
        # (no /upload/image endpoint for non-images, and the app has no torch
        # to build them — they arrive ready-made from the map).
        if params.get("rag_embed_paths"):
            enames = []
            dest_dir = ENGINE_DIR / "input"
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            for rp in params["rag_embed_paths"]:
                try:
                    src = Path(rp)
                    if src.exists():
                        shutil.copyfile(src, dest_dir / src.name)
                        enames.append(src.name)
                except Exception:
                    pass
            if enames:
                params["style_embed_names"] = enames
        # prompt-only borders now generate the FULL frame (no restrictive
        # edge-band mask): the model/LoRA draws a complete ornate frame
        # with a plain center, and cut_center carves the center transparent
        # along the frame's real inner silhouette afterwards.
        ws = websocket.WebSocket()
        ws.connect(f"ws://{ENGINE_HOST}:{ENGINE_PORT}/ws?clientId={self.client_id}",
                   timeout=30)
        try:
            pending_swaps = []        # (base image, its params) — swapped after
            for i in range(params["batch"]):
                if CANCEL.is_set():
                    self.q.put(("status", f"Cancelled — stopped after "
                                          f"{i} of {params['batch']}."))
                    break
                p = dict(params)
                if i > 0:
                    p["seed"] = random.randrange(2**32) if params["random_seed"] \
                        else params["seed"] + i
                self.q.put(("status", f"Generating {i + 1}/{params['batch']} "
                                      f"(seed {p['seed']})…"))
                graph = build_graph(p)
                r = requests.post(f"{ENGINE_URL}/prompt",
                                  json={"prompt": graph,
                                        "client_id": self.client_id},
                                  timeout=30)
                if r.status_code == 400:
                    try:
                        j = r.json()
                        msg = j.get("error", {}).get("message",
                                                     "invalid workflow")
                        bad = ", ".join(sorted(
                            v.get("class_type", k)
                            for k, v in (j.get("node_errors") or {}).items()))
                        if bad:
                            msg += f" (nodes: {bad})"
                    except Exception:
                        msg, bad = "engine rejected the workflow", ""
                    # IP-Adapter graphs can be rejected when the engine's
                    # IPAdapter node version drifts from what the map's embeds
                    # expect. Don't fail the whole run: retry ONCE without the
                    # IP-Adapter guidance (the RAG LoRA + trigger already in the
                    # prompt still steer it) and repair the add-on in the bg.
                    if "IPAdapter" in bad and (p.get("style_embed_names")
                                               or p.get("style_ref_names")):
                        applog.log("IP-Adapter rejected (%s) — retrying "
                                   "without it" % bad)
                        self.q.put(("addon_repair", "ipadapter"))
                        self.q.put((
                            "status",
                            "RAG image guidance was rejected by the engine — "
                            "drawing with the prompt + LoRA instead, and "
                            "repairing the IP-Adapter add-on in the "
                            "background."))
                        p_fb = dict(p)
                        p_fb.pop("style_embed_names", None)
                        p_fb.pop("style_ref_names", None)
                        r = requests.post(
                            f"{ENGINE_URL}/prompt",
                            json={"prompt": build_graph(p_fb),
                                  "client_id": self.client_id}, timeout=30)
                    if r.status_code == 400:
                        raise RuntimeError(
                            f"Engine rejected the request: {msg}. If this "
                            "mentions IPAdapter, use the style/character mode "
                            "once more — the app will offer to install the "
                            "missing add-on.")
                r.raise_for_status()
                prompt_id = r.json()["prompt_id"]
                images = self._await_images(ws, prompt_id)
                if CANCEL.is_set():
                    # interrupted mid-picture: nothing usable came back,
                    # and the pictures already finished are kept
                    self.q.put(("status", f"Cancelled — stopped after "
                                          f"{i} of {params['batch']}."))
                    break
                for img_meta in images:
                    img = self._fetch_image(img_meta)
                    if params.get("border_clean") \
                            and not params.get("edit_image_names") \
                            and not border_center_clean(img):
                        img = self._clean_border_center(ws, img, p)
                    if params.get("swap_face"):
                        # clone mode: the gallery gets ONE finished image (the
                        # scene with the cloned face), not a separate faceless
                        # base. Hold the base and clone it after the batch so a
                        # heavy editor model loads at most once per batch.
                        pending_swaps.append((img, p))
                    else:
                        self.q.put(("image", img, p))
            for k, (img, p) in enumerate(pending_swaps):
                if CANCEL.is_set():
                    # cancelled before this one was cloned — keep the base so
                    # the work isn't lost
                    self.q.put(("image", img,
                                {kk: vv for kk, vv in p.items()
                                 if kk != "swap_face"}))
                    self.q.put(("status", f"Cancelled — {k} of "
                                          f"{len(pending_swaps)} cloned; kept "
                                          "the un-cloned image."))
                    break
                if len(pending_swaps) > 1:
                    self.q.put(("status", "Cloning the face into picture "
                                          f"{k + 1}/{len(pending_swaps)}…"))
                # clone at CANVAS size — the base may be 4x-upscaled
                swapped = self._swap_face_pass(
                    ws, img, params["swap_face"], seed=p["seed"],
                    out_size=(p["width"], p["height"]),
                    editor=params.get("swap_editor") or "faceswap")
                sp = dict(p)
                sp.pop("swap_face", None)
                if swapped is not None:
                    sp["transparent"] = False
                    sp["upscale"] = False
                # ONE image out: the cloned result when it worked, else the
                # base (a status already explains why the clone was skipped)
                self.q.put(("image", swapped if swapped is not None else img,
                            sp))
            self.q.put(("done", None))
        finally:
            ws.close()

    def _swap_face_pass(self, ws, base_img, face_paths, seed=0, out_size=None,
                        editor="kontext"):
        """Second pass of a gen-then-swap: put a face into the freshly drawn
        base image. face_paths = one or more photos of the SAME person
        (several sharpen the likeness — multi-photo Actor DBs plug in
        here). editor="qwen" (preferred when installed) is natively
        multi-image and transfers identity reliably; "kontext" falls back
        to chained references (a stitched pair just gets redrawn
        side-by-side instead of swapped). out_size = the canvas size; a
        4x-upscaled base is brought back down so the swap always lands at
        canvas dimensions. Returns the swapped image, or None on any
        failure (the base image is always kept regardless)."""
        if CANCEL.is_set():
            return None
        try:
            if isinstance(face_paths, str):
                face_paths = [face_paths]
            face_paths = [p for p in face_paths if Path(p).exists()]
            if not face_paths:
                self.q.put(("status", "Clone skipped — the face image no "
                                      "longer exists (deleted?); the base "
                                      "image is kept."))
                return None
            if editor == "faceswap":
                return self._face_swap_local(base_img, face_paths, seed,
                                             out_size)
            label = "Qwen" if editor == "qwen" else "Flux Kontext"
            self.q.put(("status", f"Swapping the face in ({label}) — the "
                                  "first swap can take minutes while the "
                                  "model loads…"))
            # give the swap model a clean card once per run: the base
            # model can stay resident and make a 19-28 GB load thrash
            # into the engine-stopped-responding timeout
            if not getattr(self, "_swap_freed", False):
                self._swap_freed = True
                try:
                    requests.post(f"{ENGINE_URL}/free",
                                  json={"unload_models": True,
                                        "free_memory": True}, timeout=10)
                except Exception:
                    pass
            size = tuple(out_size) if out_size \
                else (base_img.width, base_img.height)
            up = base_img.convert("RGB")
            if (up.width, up.height) != size:
                up = up.resize(size, Image.LANCZOS)
            base_name = self._upload_pil(up, "cbac_swap_base.png")
            face_names = [self._upload_ref(p) for p in face_paths]
            prompt = swap_face_prompt(editor, len(face_names))
            if editor == "qwen":
                cp = dict(prompt=prompt, editor="qwen",
                          edit_image_names=[base_name] + face_names,
                          out_size=size, seed=seed, steps=None)
                graph = build_qwen_edit_graph(cp)
            else:
                cp = dict(prompt=prompt, editor="kontext",
                          edit_image_names=[base_name] + face_names,
                          ref_mode="chain", guidance=SWAP_GUIDANCE,
                          out_size=size, seed=seed, steps=None)
                graph = build_kontext_graph(cp)
            r = requests.post(f"{ENGINE_URL}/prompt",
                              json={"prompt": graph,
                                    "client_id": self.client_id},
                              timeout=30)
            r.raise_for_status()
            # a first-time 19-28 GB model load off a hard disk plus the
            # render can far exceed the default 600s
            out = self._await_images(ws, r.json()["prompt_id"],
                                     timeout=1800)
            if out:
                # merge the swap back onto the base so reflections,
                # lighting and detail outside the reworked area keep the
                # base's actual pixels instead of a re-rendered copy
                stats = {}
                merged = swap_composite(up, self._fetch_image(out[0]),
                                        stats=stats)
                applog.log("face swap (%s): mean change %s, threshold %s, "
                           "head mask %.1f%%, used %s"
                           % (label, stats.get("mean"), stats.get("thr"),
                              100 * stats.get("mask", 0), stats.get("used")))
                return merged
        except Exception as e:
            applog.exception("face swap failed")
            self.q.put(("status", f"Face swap skipped ({e}); kept the base "
                                  "image."))
        return None

    def _face_swap_local(self, base_img, face_paths, seed=0, out_size=None):
        """The fast, local identity clone (insightface / inswapper): detect
        the face in the freshly drawn base and replace it with the chosen
        person's identity, averaged over all their photos. Returns the cloned
        PIL image, or None (base kept) on any problem."""
        try:
            size = tuple(out_size) if out_size \
                else (base_img.width, base_img.height)
            up = base_img.convert("RGB")
            if (up.width, up.height) != size:
                up = up.resize(size, Image.LANCZOS)
            RAW_OUT.mkdir(parents=True, exist_ok=True)
            base_file = RAW_OUT / f"cbac_clone_base_{seed}.png"
            out_file = RAW_OUT / f"cbac_clone_{seed}.png"
            up.save(base_file)
            self.q.put(("status", "Cloning the face onto the image…"))
            rc, tail = run_face_swap(base_file, out_file,
                                     [str(p) for p in face_paths])
            applog.log("face clone (insightface): rc=%s %s" % (rc, tail))
            if rc == 0 and out_file.exists():
                return Image.open(out_file).convert("RGB")
            if "NOFACE_TARGET" in (tail or ""):
                self.q.put(("status", "No face was found in the generated "
                            "image to clone onto — try a prompt with a clear, "
                            "front-facing person; kept the image."))
            elif "NOFACE_SOURCE" in (tail or ""):
                self.q.put(("status", "No face was found in the chosen "
                            "photo(s) — pick a clearer headshot; kept the "
                            "image."))
            else:
                self.q.put(("status", "Face clone couldn't run; kept the "
                            "image. (See app.log.)"))
        except Exception as e:
            applog.exception("local face clone failed")
            self.q.put(("status", f"Face clone skipped ({e}); kept the "
                                  "image."))
        return None

    def _clean_border_center(self, ws, img, p):
        """Second pass: the frame came out with content in the middle —
        run it through Flux Kontext to empty the center, keeping the
        frame. Returns the cleaned image, or the original on any failure
        so a border is never lost to the clean-up step."""
        if CANCEL.is_set():
            return img
        try:
            self.q.put(("status", "Cleaning the center (2nd pass, "
                                  "Flux Kontext)…"))
            name = self._upload_pil(img.convert("RGB"),
                                    "cbac_border_pre.png")
            cp = dict(p, prompt=BORDER_CLEAN_PROMPT,
                      edit_image_names=[name], editor="kontext",
                      out_size=(img.width, img.height), steps=20)
            graph = build_kontext_graph(cp)
            r = requests.post(f"{ENGINE_URL}/prompt",
                              json={"prompt": graph,
                                    "client_id": self.client_id},
                              timeout=30)
            r.raise_for_status()
            cleaned = self._await_images(ws, r.json()["prompt_id"])
            if cleaned:
                return self._fetch_image(cleaned[0])
        except Exception as e:
            self.q.put(("status", f"Center clean-up skipped ({e}); using "
                                  "the original frame."))
        return img

    def _upload_pil(self, img, name):
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        r = requests.post(f"{ENGINE_URL}/upload/image",
                          files={"image": (name, buf)},
                          data={"overwrite": "true"}, timeout=60)
        r.raise_for_status()
        j = r.json()
        return f"{j['subfolder']}/{j['name']}" if j.get("subfolder") \
            else j["name"]

    def _upload_ref(self, path):
        """Upload the reference image to the engine's input folder."""
        with open(path, "rb") as fh:
            r = requests.post(f"{ENGINE_URL}/upload/image",
                              files={"image": (Path(path).name, fh)},
                              data={"overwrite": "true"}, timeout=60)
        r.raise_for_status()
        j = r.json()
        name = j["name"]
        if j.get("subfolder"):
            name = f"{j['subfolder']}/{name}"
        return name

    def _await_images(self, ws, prompt_id, timeout=600):
        # short socket timeout so Cancel is noticed within a second rather
        # than after the whole job — recv() would otherwise block for the
        # full timeout and the cancelled run would carry on regardless
        ws.settimeout(2)
        deadline = time.time() + timeout
        loading_told = False
        loading_since = None      # a model is loading: sweep + elapsed time
        last_tick = 0.0
        while True:
            if CANCEL.is_set():
                # a sweep started for a model load must not be left running
                # when the user cancels mid-load — stop it here, not only
                # at the batch's final "done"
                if loading_since is not None:
                    self.q.put(("progress_mode", "steps"))
                return []
            if time.time() > deadline:
                raise TimeoutError("the engine stopped responding")
            if loading_since is not None and time.time() - last_tick >= 10:
                # the bar cannot know how far a model load is, so say how
                # long it has been — a bar parked at 100% read as "hung"
                last_tick = time.time()
                el = int(time.time() - loading_since)
                self.q.put(("status", "Loading the model into GPU memory — "
                                      f"{el // 60}:{el % 60:02d} elapsed. A "
                                      "big model read from a hard drive "
                                      "takes minutes on first use; the bar "
                                      "starts once it is loaded…"))
            try:
                msg = ws.recv()
            except Exception as e:
                if "timed out" in str(e).lower() or isinstance(e, TimeoutError):
                    continue          # nothing this tick; check again
                raise
            if isinstance(msg, bytes):
                continue  # binary preview frames — ignored
            data = json.loads(msg)
            t, d = data.get("type"), data.get("data", {})
            if t == "progress":
                if loading_since is not None:
                    loading_since = None
                    self.q.put(("progress_mode", "steps"))
                loading_told = True
                self.q.put(("progress", d.get("value", 0), d.get("max", 1)))
            elif t == "execution_error" and d.get("prompt_id") == prompt_id:
                raise RuntimeError(d.get("exception_message", "engine error"))
            elif t == "executing" and d.get("prompt_id") == prompt_id:
                if d.get("node") is None:
                    if loading_since is not None:
                        self.q.put(("progress_mode", "steps"))
                    break  # finished
                if not loading_told:
                    loading_told = True
                    loading_since = time.time()
                    last_tick = loading_since
                    self.q.put(("progress_mode", "loading"))
                    self.q.put(("status",
                                "Loading the model into GPU memory — the "
                                "bar starts once it's loaded (big models "
                                "take a while on first use)…"))
        hist = api_get(f"/history/{prompt_id}")[prompt_id]
        images = []
        for node_out in hist["outputs"].values():
            images.extend(node_out.get("images", []))
        return images

    def _fetch_image(self, meta):
        r = requests.get(f"{ENGINE_URL}/view",
                         params={"filename": meta["filename"],
                                 "subfolder": meta.get("subfolder", ""),
                                 "type": meta.get("type", "output")},
                         timeout=60)
        r.raise_for_status()
        return Image.open(BytesIO(r.content)).convert("RGBA")


# --------------------------------------------------------------------------
# main window
# --------------------------------------------------------------------------

class Tooltip:
    """Lightweight hover help: a borderless popup after a short delay, gone on
    leave or click. Dependency-free (a plain Toplevel) so it behaves the same
    in the frozen exe as in source."""

    _open = None   # only one tip visible at a time

    def __init__(self, widget, text, delay=450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tip = None
        self._after = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._unschedule()
        try:
            self._after = self.widget.after(self.delay, self._show)
        except Exception:
            self._after = None

    def _unschedule(self):
        if self._after:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _show(self):
        if self.tip or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 14
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except Exception:
            return
        if Tooltip._open is not None:
            try:
                Tooltip._open.destroy()
            except Exception:
                pass
        self.tip = Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        ttk.Label(self.tip, text=self.text, style="Tip.TLabel",
                  wraplength=300, justify="left").pack()
        Tooltip._open = self.tip

    def _hide(self, _e=None):
        self._unschedule()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            if Tooltip._open is self.tip:
                Tooltip._open = None
            self.tip = None


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"AI Image Generator Suite v{APP_VERSION}")
        root.geometry("1500x940")
        root.minsize(1200, 780)
        self.presets = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))["presets"]
        self.settings = self._load_settings()
        prefs = self.settings.get("prefs", {})
        apply_theme(prefs.get("theme", "dark"))
        globals()["UI_FONT"] = prefs.get("font", "Segoe UI")
        try:
            self._base_scaling = float(root.tk.call("tk", "scaling"))
        except Exception:
            self._base_scaling = 1.333
        try:
            root.tk.call("tk", "scaling",
                         self._base_scaling * float(prefs.get("ui_scale", 1.0)))
        except Exception:
            pass
        root.configure(bg=BG)
        self._style()
        self.ui_queue = queue_mod.Queue()
        self.session = []          # list of (PIL image, params, path)
        self.current = None        # index into session
        self.tagged = set()        # image paths tagged for deletion
        self._thumb_btns = []      # gallery buttons, one per session entry
        self.busy = False
        self.job_queue = []        # batch queue of pending jobs
        self._batch_active = False
        self.ragmap = None         # loaded RAG map (dict) or None
        self._sweeping = {}        # progress bars currently sweeping, by path
        self._addon_lock = threading.Lock()   # one add-on install at a time
        self.ragmap_path = None
        self._ragmap_load_gen = 0  # newest async map parse wins
        self.face_paths = []       # face-from-file source (swap), NOT ref_paths
        self.actordb_path = None   # reference DB (Actor DB / Database Builder)
        self.actordb_kind = "person"   # "person" (actordb) | "character" (chardb)
        self.actor_sel = None      # selected actor (dict) or None
        self.actor_photo_i = 0     # which of their photos the arrows chose
        self._actor_excluded = {}  # imdb_id -> set of photo indices to skip
        self._actor_base_label = ""
        self._actor_thumb = None   # Tk image ref for the small headshot
        self._ollama_models = []   # models found in a local Ollama, if any
        self._want_ollama_model = ""   # the one restored from settings
        self._auto_negative = None  # last negative set by a preset (vs typed)
        self._model_display = {}    # "name · 6.5 GB" -> raw filename
        self._pending_loras = set()
        self._persist_job = None
        self.ref_paths = []        # img2img reference images
        self.border_ref_paths = []  # border-maker reference images
        self.anim_image_path = None  # animator character image
        self.vram_gb = gpu_vram_gb()   # None = no NVIDIA GPU detected
        self._model_fits = {}      # raw name -> fits in VRAM
        self._last_fit_display = ""
        self._warned_vram = set()   # tight-fit warnings shown once each

        self._build_ui()
        self._arm_panel_wheel()
        self.left_tabs.bind("<<NotebookTabChanged>>", self._schedule_persist,
                            add="+")
        self._apply_ui_state(self.settings.get("ui", {}))
        self._refresh_models()     # disk scan — fills dropdowns before engine
        self._on_face_source()   # show the chosen face source's rows
        self._refresh_editor_state()   # initial badge colours + button gating
        self._wire_autosave()      # every change saved as it happens
        self._schedule_persist()   # baseline save right away
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        applog.tk_report(root)          # a raising callback goes to the log
        self.status_var.trace_add("write", self._log_status)
        root.after(100, self._poll_queue)
        threading.Thread(target=self._boot_engine, daemon=True).start()
        threading.Thread(target=self._check_updates_bg, daemon=True).start()
        threading.Thread(target=self._probe_ollama, daemon=True).start()
        if self.vram_gb is not None:
            threading.Thread(target=self._vram_poll, daemon=True).start()
        root.after(600, self._first_run_check)

    def _vram_poll(self):
        """Live GPU memory readings for the top-right meter."""
        while True:
            try:
                r = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used,memory.total,"
                                   "utilization.gpu",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=NO_WINDOW)
                if r.returncode == 0 and r.stdout.strip():
                    parts = [x.strip() for x in
                             r.stdout.strip().splitlines()[0].split(",")]
                    used, total = int(parts[0]), int(parts[1])
                    try:
                        util = int(parts[2])
                    except (IndexError, ValueError):
                        util = None
                    self.ui_queue.put(("vram_live", used, total, util))
            except Exception:
                pass
            time.sleep(3)

    def _first_run_check(self):
        """On a machine where Setup hasn't run (or ran engine-only),
        say so plainly and offer to launch Setup.exe."""
        runtime_missing = not (PROJECT / "python" / "python.exe").exists() \
            and not (PROJECT / "venv" / "Scripts" / "python.exe").exists()
        models_missing = not scan_models("checkpoints")
        if not (runtime_missing or models_missing):
            return
        setup_exe = PROJECT / "Setup.exe"
        what = []
        if runtime_missing:
            what.append("the AI engine")
        if models_missing:
            what.append("the models")
        msg = (f"This installation is missing {' and '.join(what)} — "
               "the app cannot create art until Setup completes the "
               "installation.\n\n")
        if setup_exe.exists():
            msg += ("Run Setup now? It downloads everything needed and "
                    "this app will find it automatically afterwards "
                    "(hit ↻ or restart).")
            if messagebox.askyesno("Setup required", msg):
                subprocess.Popen([str(setup_exe)], env=_clean_child_env())
        else:
            messagebox.showinfo(
                "Setup required",
                msg + "Setup.exe was not found next to this app — unzip "
                      "the full release and run Setup.exe first.")

    # -------------------------------------------------- ui scaffolding
    def _style(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=BG3,
                    bordercolor=BG3, lightcolor=BG3, darkcolor=BG3,
                    troughcolor=BG2, arrowcolor=FG, insertcolor=FG)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Dim.TLabel", foreground=FG_DIM)
        s.configure("Head.TLabel", foreground=ACCENT2,
                    font=(UI_FONT, 10, "bold"))
        s.configure("TButton", background=BG3, padding=6)
        s.map("TButton", background=[("active", BTN_ACTIVE)])
        s.configure("Go.TButton", background=ACCENT, foreground="white",
                    font=(UI_FONT, 12, "bold"), padding=10)
        s.configure("TNotebook", background=BG, borderwidth=0,
                    tabmargins=(6, 6, 0, 0))
        s.configure("TNotebook.Tab", background=BG3, foreground=FG_DIM,
                    padding=(16, 7), font=(UI_FONT, 10, "bold"))
        s.map("TNotebook.Tab", background=[("selected", BG2)],
              foreground=[("selected", ACCENT2)])
        s.map("Go.TButton", background=[("active", GO_ACTIVE),
                                        ("disabled", BG3)])
        s.configure("Danger.TButton", background="#c0392b",
                    foreground="white", padding=6)
        s.map("Danger.TButton", background=[("active", "#e74c3c")])
        # the in-panel Reference DB browser: green on black, terminal-style,
        # so the table reads clearly against the art panel
        s.configure("DB.Treeview", background="#000000",
                    fieldbackground="#000000", foreground="#33ff66",
                    rowheight=22, font=("Consolas", 10))
        s.map("DB.Treeview",
              background=[("selected", "#0a4d1f")],
              foreground=[("selected", "#b8ffcc")])
        s.configure("DB.Treeview.Heading", background="#0d1a0d",
                    foreground="#33ff66", font=("Consolas", 10, "bold"))
        s.map("DB.Treeview.Heading", background=[("active", "#143314")])
        s.configure("TCheckbutton", background=BG, foreground=FG)
        s.map("TCheckbutton", background=[("active", BG)])
        s.configure("TRadiobutton", background=BG, foreground=FG)
        s.map("TRadiobutton", background=[("active", BG)])
        # readonly comboboxes must show their saved value in a readable
        # colour in EVERY state (readonly, focused, hovered) — otherwise
        # the selected text renders in default colours that vanish against
        # the dark field and the box looks empty until you click it.
        s.configure("TCombobox", padding=4, foreground=FG,
                    fieldbackground=BG3, background=BG3,
                    selectbackground=BG3, selectforeground=FG)
        s.map("TCombobox",
              fieldbackground=[("readonly", BG3), ("focus", BG3)],
              foreground=[("readonly", FG), ("focus", FG),
                          ("disabled", FG_DIM)],
              selectbackground=[("readonly", BG3), ("focus", BG3)],
              selectforeground=[("readonly", FG), ("focus", FG)],
              background=[("readonly", BG3)])
        for name, col in (("Fit", ACCENT2), ("Warn", "#e74c3c"),
                          ("NoFit", "#77778a")):
            s.configure(f"{name}.TCombobox", padding=4, foreground=col,
                        fieldbackground=BG3, selectbackground=BG3,
                        selectforeground=col)
            s.map(f"{name}.TCombobox",
                  fieldbackground=[("readonly", BG3), ("focus", BG3)],
                  foreground=[("readonly", col), ("focus", col)],
                  selectbackground=[("readonly", BG3), ("focus", BG3)],
                  selectforeground=[("readonly", col), ("focus", col)])
        s.configure("Horizontal.TProgressbar", background=ACCENT2,
                    troughcolor=BG2)
        s.configure("TSpinbox", padding=4)
        s.configure("TFrame", background=BG)
        # hover tooltips + the top-right mode badges
        s.configure("Tip.TLabel", background=TIP_BG, foreground=TIP_FG,
                    padding=(8, 5), relief="solid", borderwidth=1)
        # green = the feature will apply to the next generation, red = it won't
        s.configure("Gpu.Horizontal.TProgressbar", background="#3b82f6",
                    troughcolor=BG2, bordercolor=BG3, lightcolor="#3b82f6",
                    darkcolor="#3b82f6")
        s.configure("BadgeOn.TLabel", background=ACCENT2, foreground=BADGE_FG,
                    font=(UI_FONT, 9, "bold"), padding=(9, 3))
        s.configure("BadgeOff.TLabel", background=ACCENT, foreground="white",
                    font=(UI_FONT, 9, "bold"), padding=(9, 3))

    def _rule(self, parent, row):
        """A blue divider marking where one section of the panel ends and
        the next begins. Returns the next free grid row."""
        # a Canvas, not a ttk widget: ttk styles cannot paint a plain
        # coloured bar, and Canvas honours bg directly
        Canvas(parent, height=3, bg=RULE, highlightthickness=0,
               bd=0).grid(row=row, sticky="ew", pady=(16, 2))
        return row + 1

    def _text(self, parent, height):
        return Text(parent, height=height, wrap=WORD, bg=BG3, fg=FG,
                    insertbackground=FG, relief="flat", padx=8, pady=6,
                    font=(UI_FONT, 10), undo=True)

    def _arm_wheel(self, root):
        """Put the PanelWheel tag first on a subtree — used both for the whole
        panel at startup and for widgets built later (the Using thumbnails),
        so the wheel keeps scrolling the panel over anything on it."""
        def walk(w):
            yield w
            for c in w.winfo_children():
                yield from walk(c)
        for w in walk(root):
            tags = w.bindtags()
            if tags and tags[0] != "PanelWheel":
                w.bindtags(("PanelWheel",) + tags)

    def _arm_panel_wheel(self):
        """Put the PanelWheel tag first on every widget of the panel pages,
        so the wheel over the panel scrolls the panel before the widget
        under the pointer can react."""
        for page in (self._page_gen, self._page_anim, self._page_border,
                     self._page_edit):
            self._arm_wheel(page)

    def _scroll_page(self, notebook, title):
        """One tab: a canvas with a vertical scrollbar holding a padded
        frame; returns the frame to build into. The canvas is registered
        so the wheel router can scroll it."""
        page = ttk.Frame(notebook)
        page.rowconfigure(0, weight=1)
        page.columnconfigure(0, weight=1)
        notebook.add(page, text=title)
        canvas = Canvas(page, bg=BG, highlightthickness=0, width=432)
        canvas.grid(row=0, column=0, sticky=NSEW)
        sb = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        sb.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=sb.set)
        inner = ttk.Frame(canvas, padding=12)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e, c=canvas: c.configure(
            scrollregion=c.bbox("all")))
        canvas.bind("<Configure>", lambda e, c=canvas, w=win: c.itemconfigure(
            w, width=e.width))
        inner.columnconfigure(0, weight=1)
        self._scroll_canvases.append(canvas)
        if not hasattr(self, "_page_tabs"):
            self._page_tabs = {}
        self._page_tabs[str(inner)] = page
        return inner

    def _build_ui(self):
        root = self.root
        root.columnconfigure(0, weight=0, minsize=450)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        # ---------- left column: three tabs of controls, each scrollable ----------
        # Image generation / Animation / Borders keep the long panel in
        # three short ones; the batch queue and the version row sit under
        # the tabs and stay visible whichever tab is open.
        left_wrap = ttk.Frame(root)
        left_wrap.grid(row=0, column=0, sticky=NSEW)
        left_wrap.rowconfigure(0, weight=1)
        left_wrap.columnconfigure(0, weight=1)
        # what is still starting up — engine, RAG map, update check, add-on
        # installs — listed above the tabs until everything is ready
        self._pending = {}
        self.ready_frame = ttk.Frame(left_wrap, padding=(12, 6, 12, 2))
        self.ready_frame.grid(row=0, column=0, sticky="ew")
        self.ready_frame.columnconfigure(0, weight=1)
        self.ready_bar = ttk.Progressbar(self.ready_frame, mode="indeterminate",
                                         style="Gpu.Horizontal.TProgressbar")
        self.ready_bar.grid(row=0, column=0, sticky="ew")
        self.ready_var = StringVar(value="")
        ttk.Label(self.ready_frame, textvariable=self.ready_var,
                  style="Dim.TLabel", wraplength=410,
                  justify="left").grid(row=1, column=0, sticky=W, pady=(2, 0))
        self.ready_frame.grid_remove()
        left_wrap.rowconfigure(0, weight=0)
        left_wrap.rowconfigure(1, weight=1)
        self.left_tabs = ttk.Notebook(left_wrap)
        self.left_tabs.grid(row=1, column=0, sticky=NSEW)
        self._scroll_canvases = []
        self._page_gen = self._scroll_page(self.left_tabs, "Image generation")
        self._page_anim = self._scroll_page(self.left_tabs, "Animation")
        self._page_border = self._scroll_page(self.left_tabs, "Borders")
        self._page_edit = self._scroll_page(self.left_tabs, "Edit image")
        self.left_canvas = self._scroll_canvases[0]
        self._page_bottom = ttk.Frame(left_wrap, padding=(12, 0, 12, 8))
        self._page_bottom.grid(row=2, column=0, sticky=NSEW)
        self._page_bottom.columnconfigure(0, weight=1)

        def _wheel_router(e):
            # children swallow wheel events; route them to the page whenever
            # the pointer is anywhere inside one of the tab canvases
            try:
                w = self.root.winfo_containing(e.x_root, e.y_root)
            except Exception:
                # a Combobox's open list ("popdown") is a Tk-internal
                # window the Python side cannot name — KeyError
                return None
            while w is not None:
                if w is self.lora_list:
                    return None      # its own scrollbar handles it
                if w in self._scroll_canvases:
                    w.yview_scroll(-1 if e.delta > 0 else 1, "units")
                    return "break"
                w = w.master
            return None
        self.root.bind_all("<MouseWheel>", _wheel_router, add="+")

        # over the left panel the wheel ALWAYS scrolls the panel: a
        # dropdown would change its value, a text box or the LoRA list would
        # scroll itself, because their class bindings run before "all".
        # A tag placed FIRST on every widget of the pages runs before them
        # and stops the event there (see _arm_panel_wheel, after the build).
        def _panel_wheel(e):
            try:
                w = self.root.winfo_containing(e.x_root, e.y_root)
            except Exception:
                w = None
            while w is not None:
                if w in self._scroll_canvases:
                    w.yview_scroll(-1 if e.delta > 0 else 1, "units")
                    return "break"
                w = w.master
            return None
        self.root.bind_class("PanelWheel", "<MouseWheel>", _panel_wheel)
        left = self._page_gen
        r = 0

        prompt_head = ttk.Label(left, text="PROMPT", style="Head.TLabel")
        prompt_head.grid(row=r, sticky=W); r += 1
        self._tip(prompt_head,
                  "What to draw — the subject, scene and action. Be specific. "
                  "Your chosen preset's style text is added on top at "
                  "generation (unless you're editing a loaded image).")
        self.prompt_box = self._text(left, 6)
        self.prompt_box.grid(row=r, sticky=NSEW, pady=(2, 2)); r += 1
        self._tip(self.prompt_box,
                  "The main description of the image you want. When an editor "
                  "image is loaded this becomes the edit instruction instead.")

        # optional prompt enhancer — only lights up if Ollama is running
        erow = ttk.Frame(left); erow.grid(row=r, sticky="ew", pady=(0, 8))
        r += 1
        erow.columnconfigure(1, weight=1)
        self.enhance_btn = ttk.Button(erow, text="✨ Enhance", width=11,
                                      command=self._enhance_prompt)
        self.enhance_btn.grid(row=0, column=0)
        self._tip(self.enhance_btn,
                  "Rewrite your basic prompt into a fuller, more detailed one. "
                  "Works offline with the built-in enhancer; if you run a "
                  "local Ollama model you can pick it for a smarter rewrite. "
                  "Nothing is installed or sent online.")
        self.ollama_var = StringVar(value=BUILTIN_ENHANCER)
        self.ollama_dd = ttk.Combobox(erow, textvariable=self.ollama_var,
                                      state="readonly", exportselection=False,
                                      values=[BUILTIN_ENHANCER], width=18)
        self.ollama_dd.grid(row=0, column=1, sticky="ew", padx=6)
        self._tip(self.ollama_dd,
                  "Which enhancer to use: the built-in offline one (always "
                  "available), or a local Ollama model if you have one.")
        self.undo_enhance_btn = ttk.Button(erow, text="↩", width=3,
                                           command=self._undo_enhance)
        self.undo_enhance_btn.grid(row=0, column=2)
        self.undo_enhance_btn.state(["disabled"])
        self._tip(self.undo_enhance_btn, "Undo the last Enhance and restore "
                                         "your original prompt.")
        self._prompt_undo = None

        neg_head = ttk.Label(left, text="NEGATIVE PROMPT (kept until you "
                                        "clear it)", style="Head.TLabel")
        neg_head.grid(row=r, sticky=W); r += 1
        self._tip(neg_head,
                  "What to avoid — things you don't want in the image (extra "
                  "limbs, text, watermarks…). Stays until you change it; note "
                  "Turbo models largely ignore negatives.")
        self.negative_box = self._text(left, 2)
        self.negative_box.grid(row=r, sticky=NSEW, pady=(2, 8)); r += 1

        # model row — picked FIRST; the preset list adapts to it
        model_head = ttk.Label(left, text="MODEL (pick first)",
                               style="Head.TLabel")
        model_head.grid(row=r, sticky=W); r += 1
        self._tip(model_head,
                  "The base checkpoint that draws the image. Pick it first — "
                  "the preset and LoRA lists adapt to its family (Flux, SDXL "
                  "or anime). Each entry shows its size and whether it fits "
                  "your GPU.")
        mrow = ttk.Frame(left); mrow.grid(row=r, sticky=NSEW, pady=(2, 8)); r += 1
        mrow.columnconfigure(0, weight=1)
        self.model_var = StringVar()
        self.model_dd = ttk.Combobox(mrow, textvariable=self.model_var,
                                     state="readonly", exportselection=False)
        self.model_dd.grid(row=0, column=0, sticky="ew")
        self.model_dd.bind("<<ComboboxSelected>>",
                           lambda _e: self._on_model_pick())
        self._tip(self.model_dd,
                  "Choose the base model. Green fits your GPU comfortably, red "
                  "is a tight fit (slower), grey is too big to load.")
        model_refresh = ttk.Button(mrow, text="↻", width=3,
                                   command=self._refresh_models)
        model_refresh.grid(row=0, column=1, padx=(6, 0))
        self._tip(model_refresh, "Rescan the models folder for newly added "
                                 "checkpoints and LoRAs.")
        addmodel_btn = ttk.Button(mrow, text="📁 Add model…", width=13,
                                  command=self._import_checkpoint)
        addmodel_btn.grid(row=0, column=2, padx=(6, 0))
        self._tip(addmodel_btn,
                  "Add your own .safetensors checkpoint. It is hard-linked in "
                  "(instant, no extra disk) when on the same drive, else "
                  "copied. The engine can load it without a restart.")
        self.vram_note = StringVar(
            value="Sizes shown per model — your video card RAM must be above "
                  "that number (keep ~2 GB headroom).")
        ttk.Label(left, textvariable=self.vram_note, style="Dim.TLabel",
                  wraplength=400).grid(row=r, sticky=W); r += 1

        # preset row — filtered to styles that suit the selected model
        preset_head = ttk.Label(left, text="ART STYLE PRESET (styles for the "
                                           "selected model)", style="Head.TLabel")
        preset_head.grid(row=r, sticky=W, pady=(6, 0)); r += 1
        self._tip(preset_head,
                  "A ready-made art style. Picking one fills the Style text "
                  "below, which is added to your prompt at generation. The list "
                  "is filtered to styles that suit the selected model's family.")
        prow = ttk.Frame(left); prow.grid(row=r, sticky=NSEW, pady=(2, 2)); r += 1
        prow.columnconfigure(0, weight=1)
        self.preset_var = StringVar(value="Marvel House Style")
        self.preset_dd = ttk.Combobox(prow, textvariable=self.preset_var,
                                      values=[NONE_PRESET], state="readonly", exportselection=False)
        self.preset_dd.grid(row=0, column=0, sticky="ew")
        self.preset_dd.bind("<<ComboboxSelected>>", self._on_preset)
        self._tip(self.preset_dd, "Choose an art style. It sets the Style text "
                                  "and a matching negative prompt.")
        ex_btn = ttk.Button(prow, text="Try example", width=11,
                            command=self._use_example)
        ex_btn.grid(row=0, column=1, padx=(6, 0))
        self._tip(ex_btn, "Fill the prompt with this preset's example, so you "
                          "can generate something immediately.")
        clr_all_btn = ttk.Button(prow, text="Clear", width=6,
                                 command=self._clear_all)
        clr_all_btn.grid(row=0, column=2, padx=(6, 0))
        self._tip(clr_all_btn, "Clear the prompt, negative and style text.")

        style_lbl = ttk.Label(left, text="Style text (appended to your prompt "
                                         "— editable):", style="Dim.TLabel")
        style_lbl.grid(row=r, sticky=W, pady=(6, 0)); r += 1
        self.style_box = self._text(left, 3)
        self.style_box.grid(row=r, sticky=NSEW, pady=(2, 8)); r += 1
        self._tip(self.style_box,
                  "The style wording added to your prompt (from the preset, but "
                  "fully editable). Cleared/ignored while editing a loaded "
                  "image.")

        # loras — one list, tick any number of them
        lora_head = ttk.Label(left, text="LORAS (tick to apply — match "
                                         "SDXL/Flux to the model)",
                              style="Head.TLabel")
        lora_head.grid(row=r, sticky=W); r += 1
        self._tip(lora_head,
                  "LoRAs are small style/character add-ons for the base model. "
                  "Tick any number to apply them. Match the LoRA's family "
                  "(SDXL or Flux) to your model. Each ticked LoRA's activation "
                  "keyword is added to the prompt for you automatically.")
        lframe = ttk.Frame(left); lframe.grid(row=r, sticky=NSEW, pady=2); r += 1
        lframe.columnconfigure(0, weight=1)
        self.lora_list = Listbox(lframe, selectmode="multiple",
                                 exportselection=False, height=6, bg=BG3,
                                 fg=FG, relief="flat", highlightthickness=0,
                                 selectbackground=ACCENT,
                                 selectforeground="white",
                                 activestyle="none", font=(UI_FONT, 9))
        self.lora_list.grid(row=0, column=0, sticky="ew")
        lsb = ttk.Scrollbar(lframe, orient="vertical",
                            command=self.lora_list.yview)
        self.lora_list.configure(yscrollcommand=lsb.set)
        lsb.grid(row=0, column=1, sticky="ns")
        self.lora_list.bind("<<ListboxSelect>>",
                            lambda _e: (self._schedule_persist(),
                                        self._refresh_mode_badges()))
        self._tip(self.lora_list,
                  "Click to tick/untick LoRAs (Ctrl-click for several). Ticked "
                  "ones apply to the next generation and light the green LoRA "
                  "badge; their trigger words are added automatically.")
        limprow = ttk.Frame(left); limprow.grid(row=r, sticky=NSEW,
                                                pady=(2, 0)); r += 1
        addlora_btn = ttk.Button(limprow, text="➕ Add LoRA file…",
                                 command=self._import_lora)
        addlora_btn.pack(side="left")
        self._tip(addlora_btn, "Copy a .safetensors LoRA into your LoRA folder "
                               "so it appears in the list.")
        lora_refresh = ttk.Button(limprow, text="↻", width=3,
                                  command=self._refresh_models)
        lora_refresh.pack(side="left", padx=(4, 0))
        self._tip(lora_refresh, "Rescan the LoRA folder.")
        rmlora_btn = ttk.Button(limprow, text="🗑 Remove",
                                command=self._remove_lora, style="Danger.TButton")
        rmlora_btn.pack(side="left", padx=(4, 0))
        self._tip(rmlora_btn, "Delete the ticked LoRA file(s) from your LoRA "
                              "folder (asks first). This removes the file, not "
                              "just the tick.")
        lorafld_btn = ttk.Button(limprow, text="📁 LoRA folder",
                                 command=lambda: os.startfile(
                                     str(_ensure_lora_dir())))
        lorafld_btn.pack(side="left", padx=(4, 0))
        self._tip(lorafld_btn, "Open the LoRA folder in Explorer.")
        strow = ttk.Frame(left); strow.grid(row=r, sticky=NSEW, pady=(0, 4)); r += 1
        ttk.Label(strow, text="LoRA strength",
                  style="Dim.TLabel").pack(side="left")
        self.lora_strength = DoubleVar(value=0.8)
        lora_scale = ttk.Scale(strow, from_=0.0, to=1.5,
                               variable=self.lora_strength, length=160)
        lora_scale.pack(side="left", padx=6)
        self._tip(lora_scale,
                  "How strongly the ticked LoRAs affect the image (applies to "
                  "all of them). ~0.6–0.9 is typical; too high can distort.")
        self.lora_strength_lab = ttk.Label(strow, text="0.80", width=5,
                                           style="Dim.TLabel")
        self.lora_strength_lab.pack(side="left")
        self.lora_strength.trace_add(
            "write", lambda *_a: self.lora_strength_lab.config(
                text=f"{self.lora_strength.get():.2f}"))
        # ---------- RAG map: its own section under the LoRAs ----------
        # pairs example images (or private embeddings) with a LoRA; at
        # generation the closest examples guide the picture (IP-Adapter)
        rag_head = ttk.Label(left, text="RAG MAP (example images that guide "
                                        "the picture)",
                             style="Head.TLabel", wraplength=400,
                             justify="left")
        rag_head.grid(row=r, sticky=W, pady=(10, 0)); r += 1
        self._tip(rag_head,
                  "A RAG map pairs example images (or private embeddings) "
                  "with a LoRA. For each prompt the closest examples guide "
                  "the picture through IP-Adapter on SDXL models; on Flux "
                  "their captions are added to the prompt as text. Each "
                  "Generate draws a fresh set of examples; the images of "
                  "one Generate share that set.")
        ragrow = ttk.Frame(left); ragrow.grid(row=r, sticky=NSEW, pady=(2, 0))
        r += 1
        ragrow.columnconfigure(1, weight=1)
        ragmap_btn = ttk.Button(ragrow, text="🧭 RAG map…", width=12,
                                command=self._pick_ragmap)
        ragmap_btn.grid(row=0, column=0)
        self._tip(ragmap_btn,
                  "Load a RAG map — example images paired with a LoRA. At "
                  "generation the closest examples for your prompt guide the "
                  "image via IP-Adapter (SDXL only), and the paired LoRA + "
                  "trigger are applied automatically. Lights the green RAG "
                  "badge when active.")
        self.ragmap_var = StringVar(value="none")
        ttk.Label(ragrow, textvariable=self.ragmap_var, style="Dim.TLabel",
                  wraplength=230).grid(row=0, column=1, sticky=W, padx=6)
        ragrm_btn = ttk.Button(ragrow, text="🗑 Remove", width=10,
                               command=self._clear_ragmap)
        ragrm_btn.grid(row=0, column=2)
        self._tip(ragrm_btn, "Stop using the RAG map (the map file is left "
                             "alone).")
        # progress while a map loads — a large map takes minutes, off the
        # UI thread; without this the only sign was the label's "loading…"
        self.rag_prog = ttk.Progressbar(left, mode="determinate", length=400,
                                        style="Gpu.Horizontal.TProgressbar")
        self.rag_prog.grid(row=r, sticky="ew", pady=(4, 0)); r += 1
        self.rag_prog.grid_remove()
        self.rag_prog_var = StringVar(value="")
        self.rag_prog_lab = ttk.Label(left, textvariable=self.rag_prog_var,
                                      style="Dim.TLabel", wraplength=400,
                                      justify="left")
        self.rag_prog_lab.grid(row=r, sticky=W); r += 1
        self.rag_prog_lab.grid_remove()

        # size + settings
        cs_head = ttk.Label(left, text="CANVAS & SETTINGS", style="Head.TLabel")
        cs_head.grid(row=r, sticky=W, pady=(8, 0)); r += 1
        self._tip(cs_head, "Output size and per-generation settings.")
        srow = ttk.Frame(left); srow.grid(row=r, sticky=NSEW, pady=2); r += 1
        srow.columnconfigure(0, weight=1)
        self.size_var = StringVar(value="Wide — splash (1344x768)")
        size_dd = ttk.Combobox(srow, textvariable=self.size_var,
                               state="readonly", exportselection=False,
                               values=list(SIZE_PRESETS))
        size_dd.grid(row=0, column=0, sticky="ew")
        self._tip(size_dd, "Output width × height. Wider/taller uses more VRAM "
                           "and time.")

        grow = ttk.Frame(left); grow.grid(row=r, sticky=NSEW, pady=4); r += 1
        ttk.Label(grow, text="Steps", style="Dim.TLabel").grid(row=0, column=0)
        self.steps_var = StringVar(value="auto")
        steps_dd = ttk.Combobox(grow, textvariable=self.steps_var, width=6,
                                exportselection=False,
                                values=["auto", "8", "12", "20", "28", "35", "50"])
        steps_dd.grid(row=0, column=1, padx=(4, 12))
        self._tip(steps_dd, "Sampling steps. 'auto' picks a good value per "
                            "model family. More steps = slower, sometimes "
                            "cleaner; Turbo models need very few.")
        ttk.Label(grow, text="Variations", style="Dim.TLabel").grid(row=0,
                                                                    column=2)
        self.batch_var = IntVar(value=1)
        batch_sb = ttk.Spinbox(grow, from_=1, to=10, textvariable=self.batch_var,
                               exportselection=False, width=4)
        batch_sb.grid(row=0, column=3, padx=(4, 12))
        self._tip(batch_sb, "How many images to make from this one prompt "
                            "(each with a different seed) so you can pick the "
                            "best.")
        self.transparent_var = BooleanVar(value=False)
        trans_cb = ttk.Checkbutton(grow, text="Transparent BG",
                                   variable=self.transparent_var)
        trans_cb.grid(row=0, column=4)
        self._tip(trans_cb, "Cut out the background so the subject is on "
                            "transparency (PNG). Good for stickers/sprites.")

        # ---------- QUALITY (optional extra-quality passes) ----------
        q_head = ttk.Label(left, text="QUALITY", style="Head.TLabel")
        q_head.grid(row=r, sticky=W, pady=(6, 0)); r += 1
        self._tip(q_head, "Optional extra-quality passes — each adds time and "
                          "VRAM, and all are off by default.")
        qrow = ttk.Frame(left); qrow.grid(row=r, sticky=W, pady=(2, 4)); r += 1
        self.hires_var = BooleanVar(value=False)
        hires_cb = ttk.Checkbutton(qrow, text="Hi-res fix (add detail)",
                                   variable=self.hires_var)
        hires_cb.grid(row=0, column=0, sticky=W)
        self._tip(hires_cb,
                  "A real second detail pass: the picture is upscaled in latent "
                  "space and lightly re-sampled, so texture and detail are "
                  "ADDED, not just enlarged. The biggest quality lever; it "
                  "roughly doubles the time.")
        self.hires_scale_var = StringVar(value="1.5×")
        ttk.Combobox(qrow, textvariable=self.hires_scale_var, state="readonly",
                     exportselection=False, width=4,
                     values=["1.5×", "2×"]).grid(row=0, column=1, padx=(4, 12))
        self.freeu_var = BooleanVar(value=False)
        freeu_cb = ttk.Checkbutton(qrow, text="Extra detail",
                                   variable=self.freeu_var)
        freeu_cb.grid(row=0, column=2, padx=(0, 12))
        self._tip(freeu_cb,
                  "FreeU: a near-free contrast and fine-detail boost on SDXL "
                  "models (no effect on Flux). Cheap to leave on.")
        self.upscale_var = BooleanVar(value=False)
        up_cb = ttk.Checkbutton(qrow, text="Upscale 4×",
                                variable=self.upscale_var)
        up_cb.grid(row=0, column=3)
        self._tip(up_cb, "Enlarge the finished image 4× with an upscaler model "
                         "(applied last of all). Slower and uses more VRAM.")

        seedrow = ttk.Frame(left); seedrow.grid(row=r, sticky=NSEW, pady=4); r += 1
        ttk.Label(seedrow, text="Seed", style="Dim.TLabel").grid(row=0, column=0)
        self.seed_var = StringVar(value="0")
        self.seed_entry = ttk.Entry(seedrow, textvariable=self.seed_var,
                                    exportselection=False, width=12)
        self.seed_entry.grid(row=0, column=1, padx=(4, 10))
        self._tip(self.seed_entry,
                  "The random seed. The same seed + settings reproduces the "
                  "same image. Ignored while Random is ticked.")
        self.random_seed_var = BooleanVar(value=True)
        rand_cb = ttk.Checkbutton(seedrow, text="Random",
                                  variable=self.random_seed_var)
        rand_cb.grid(row=0, column=2)
        self._tip(rand_cb, "Use a fresh random seed each generation (untick to "
                           "lock the seed above).")
        reuse_btn = ttk.Button(seedrow, text="Reuse last", width=10,
                               command=self._reuse_seed)
        reuse_btn.grid(row=0, column=3, padx=(10, 0))
        self._tip(reuse_btn, "Copy the seed of the selected image and turn off "
                             "Random, so you can tweak the prompt on the same "
                             "seed.")

        # image editor — Gemini-style instruction editing
        r = self._rule(left, r)
        # ---------- CLONE A FACE onto the generated image ----------
        fc_head = ttk.Label(left, text="CLONE TOOL (optional)",
                            style="Head.TLabel")
        fc_head.grid(row=r, sticky=W, pady=(10, 0)); r += 1
        self._tip(fc_head,
                  "Clone a specific person onto the picture your prompt makes. "
                  "Tick the box, pick the Method, choose a face from a file or "
                  "a database, and Generate: your prompt (with the model, "
                  "LoRAs and RAG map) draws the scene, then that person's face "
                  "is cloned onto the person in it. Both pictures are kept.")

        # the on/off checkbox at the TOP; unticking greys the whole section
        self.swap_rag_var = BooleanVar(value=True)
        self.swap_cb = ttk.Checkbutton(
            left, text="Clone this face onto the generated image",
            variable=self.swap_rag_var, command=self._on_clone_toggle)
        self.swap_cb.grid(row=r, sticky=W, pady=(2, 2)); r += 1
        self._tip(self.swap_cb,
                  "On: after the prompt draws the scene, the chosen face is "
                  "placed on the person in it. Off: the whole section below "
                  "is disabled and only the prompt is used.")

        self.clone_body = ttk.Frame(left)
        self.clone_body.grid(row=r, sticky=NSEW); r += 1
        self.clone_body.columnconfigure(0, weight=1)
        cb = self.clone_body
        cr = 0

        # which engine does the clone — the fast local face swap by default,
        # or a big editor that re-renders the face
        self.clone_method_var = StringVar(value=CLONE_METHODS[0][0])
        cmrow = ttk.Frame(cb); cmrow.grid(row=cr, sticky="ew", pady=(2, 2))
        cr += 1
        cmrow.columnconfigure(1, weight=1)
        ttk.Label(cmrow, text="Method", style="Dim.TLabel").grid(row=0, column=0)
        self.clone_method_dd = ttk.Combobox(
            cmrow, textvariable=self.clone_method_var, state="readonly",
            exportselection=False, values=[m[0] for m in CLONE_METHODS])
        self.clone_method_dd.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.clone_method_dd.bind("<<ComboboxSelected>>",
                                  lambda _e: self._schedule_persist())
        self._tip(self.clone_method_dd,
                  "How the chosen face is applied. 'Face swap' (recommended) "
                  "is a fast, local engine (one-time ~550 MB setup) that "
                  "closely matches the person you picked. 'Qwen' and 'Kontext' "
                  "re-render the face with the big editor models — slower, and "
                  "the likeness is looser.")

        self.face_source_var = StringVar(value="file")
        srcrow = ttk.Frame(cb); srcrow.grid(row=cr, sticky=W, pady=(2, 0)); cr += 1
        ttk.Label(srcrow, text="Source:", style="Dim.TLabel").pack(side="left")
        ttk.Radiobutton(srcrow, text="From a file", value="file",
                        variable=self.face_source_var,
                        command=self._on_face_source).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(srcrow, text="From a database", value="db",
                        variable=self.face_source_var,
                        command=self._on_face_source).pack(side="left", padx=(10, 0))

        self.face_file_row = ttk.Frame(cb)
        self.face_file_row.grid(row=cr, sticky=NSEW, pady=2); cr += 1
        self.face_file_row.columnconfigure(1, weight=1)
        ttk.Button(self.face_file_row, text="\U0001f5bc Browse\u2026", width=11,
                   command=self._pick_face).grid(row=0, column=0)
        self.face_file_var = StringVar(value="no file chosen")
        ttk.Button(self.face_file_row, text="\u2715", width=3,
                   command=self._clear_face).grid(row=0, column=2)

        self.face_db_row = ttk.Frame(cb)
        self.face_db_row.grid(row=cr, sticky=NSEW, pady=2); cr += 1
        self.face_db_row.columnconfigure(1, weight=1)
        ttk.Button(self.face_db_row, text="\U0001f4c7 Database\u2026", width=13,
                   command=self._pick_actordb).grid(row=0, column=0)
        self.actordb_var = StringVar(value="none")
        ttk.Label(self.face_db_row, textvariable=self.actordb_var,
                  style="Dim.TLabel", wraplength=180).grid(row=0, column=1,
                                                           sticky=W, padx=6)
        ttk.Button(self.face_db_row, text="\U0001f5d1", width=3,
                   command=self._clear_actordb).grid(row=0, column=2)
        _pr = ttk.Frame(self.face_db_row)
        _pr.grid(row=1, column=0, columnspan=3, sticky=NSEW, pady=(2, 0))
        _pr.columnconfigure(1, weight=1)
        ttk.Button(_pr, text="\U0001f464 Person\u2026", width=13,
                   command=self._pick_actor).grid(row=0, column=0)
        self.actor_var = StringVar(value="no person selected")
        ttk.Label(_pr, textvariable=self.actor_var, style="Dim.TLabel",
                  wraplength=150).grid(row=0, column=1, sticky=W, padx=6)
        self.actor_thumb_lab = ttk.Label(_pr)
        self.actor_thumb_lab.grid(row=0, column=2, padx=(0, 4))
        # cycle a multi-photo person's pictures and drop the ones that
        # don't fit (restricts what is sent; never deletes from the DB)
        self.photo_nav = ttk.Frame(_pr)
        self.photo_nav.grid(row=1, column=0, columnspan=3, sticky=W, pady=(2, 0))
        self.photo_prev = ttk.Button(self.photo_nav, text="\u25c0", width=3,
                                     command=lambda: self._actor_step_photo(-1))
        self.photo_prev.pack(side="left")
        self.photo_next = ttk.Button(self.photo_nav, text="\u25b6", width=3,
                                     command=lambda: self._actor_step_photo(1))
        self.photo_next.pack(side="left", padx=(2, 6))
        self.photo_excl_btn = ttk.Button(self.photo_nav, text="\u2715 Drop",
                                         width=9,
                                         command=self._actor_toggle_photo)
        self.photo_excl_btn.pack(side="left")
        self._tip(self.photo_prev, "Show the previous photo of this person.")
        self._tip(self.photo_next, "Show the next photo of this person.")
        self._tip(self.photo_excl_btn,
                  "Drop this photo from what is sent to the model (or add it "
                  "back). It stays in the database \u2014 this only restricts "
                  "which photos are used.")
        self.photo_nav.grid_remove()

        ttk.Label(cb, text="Using:", style="Dim.TLabel").grid(row=cr, sticky=W)
        cr += 1
        self.face_using = ttk.Frame(cb)
        self.face_using.grid(row=cr, sticky="ew", pady=(0, 2)); cr += 1
        self._face_thumb_imgs = []      # keep PhotoImage refs from being GC'd
        self._tip(self.face_using,
                  "The face image(s) or person that will be placed on the "
                  "generated picture — shown here as small samples.")

        self.swap_fast_var = BooleanVar(value=False)
        self.swap_use_rag_var = BooleanVar(value=True)
        self.swap_use_lora_var = BooleanVar(value=True)
        grow = ttk.Frame(cb)
        grow.grid(row=cr, sticky=W, pady=(0, 2)); cr += 1
        ttk.Label(grow, text="Guide the base with:",
                  style="Dim.TLabel").pack(side="left")
        gl = ttk.Checkbutton(grow, text="LoRA", variable=self.swap_use_lora_var,
                             command=self._refresh_editor_state)
        gl.pack(side="left", padx=(6, 0))
        gr = ttk.Checkbutton(grow, text="RAG", variable=self.swap_use_rag_var,
                             command=self._refresh_editor_state)
        gr.pack(side="left", padx=(8, 0))
        self._tip(gl, "Apply your ticked LoRAs to the picture the swap draws.")
        self._tip(gr, "Let your loaded RAG map guide the picture the swap draws.")

        self._gen_row = r      # the generation page continues here, after clone
        # ---------- EDIT A LOADED IMAGE: its own tab ----------
        left = self._page_edit
        r = 0
        ed_head = ttk.Label(left, text="EDIT A LOADED IMAGE",
                            style="Head.TLabel")
        ed_head.grid(row=r, sticky=W, pady=(10, 0)); r += 1
        self._tip(ed_head,
                  "Load an image and describe a change \u2014 the AI edits that "
                  "image. Presets, LoRAs and RAG are off here; the image and "
                  "your instruction drive the result. (Separate from the face "
                  "swap on the Image generation tab.)")
        rrow = ttk.Frame(left); rrow.grid(row=r, sticky=NSEW, pady=2); r += 1
        rrow.columnconfigure(1, weight=1)
        ttk.Button(rrow, text="\U0001f5bc Load\u2026", width=9,
                   command=self._pick_ref).grid(row=0, column=0)
        self.ref_var = StringVar(value="none \u2014 nothing loaded")
        ttk.Label(rrow, textvariable=self.ref_var, style="Dim.TLabel",
                  wraplength=150).grid(row=0, column=1, sticky=W, padx=6)
        self.editor_use_btn = ttk.Button(
            rrow, text="Use selected", width=12,
            command=self._use_selected_for_editor)
        self.editor_use_btn.grid(row=0, column=2, padx=(0, 4))
        self.editor_use_btn.state(["disabled"])
        self._tip(self.editor_use_btn,
                  "Edit the picture currently selected in the gallery.")
        ttk.Button(rrow, text="\u2715", width=3,
                   command=self._clear_ref).grid(row=0, column=3)

        irow = ttk.Frame(left); irow.grid(row=r, sticky=NSEW, pady=(6, 0)); r += 1
        irow.columnconfigure(0, weight=1)
        ttk.Label(irow, text="What should the AI change?",
                  style="Dim.TLabel").grid(row=0, column=0, sticky=W)
        self.edit_menu_btn = ttk.Menubutton(irow, text="Common edits \u25be")
        self.edit_menu_btn.grid(row=0, column=1, sticky="e")
        self.edit_menu_btn["menu"] = self._build_edit_menu(self.edit_menu_btn)
        self.edit_prompt_box = self._text(left, 3)
        self.edit_prompt_box.grid(row=r, sticky=NSEW, pady=(2, 4)); r += 1
        self.edit_prompt_box.bind("<Button-3>",
                                  lambda e: self._popup_edit_menu(e))
        self._tip(self.edit_prompt_box,
                  "Describe the change in plain words (e.g. 'remove the "
                  "watermark', 'change the background to a beach'). "
                  "Right-click for common edits.")

        self.change_var = DoubleVar(value=60)
        chrow = ttk.Frame(left); chrow.grid(row=r, sticky=NSEW, pady=(2, 0)); r += 1
        ttk.Label(chrow, text="Change amount", style="Dim.TLabel").pack(side="left")
        ttk.Scale(chrow, from_=10, to=100, variable=self.change_var,
                  length=150).pack(side="left", padx=6)
        self.change_lab = ttk.Label(chrow, text="60%", width=5, style="Dim.TLabel")
        self.change_lab.pack(side="left")
        self.change_var.trace_add("write", lambda *_a: self.change_lab.config(
            text=f"{int(self.change_var.get())}%"))

        erow = ttk.Frame(left); erow.grid(row=r, sticky=NSEW, pady=(2, 4)); r += 1
        erow.columnconfigure(1, weight=1)
        ttk.Label(erow, text="Editor", style="Dim.TLabel").grid(row=0, column=0)
        self.editor_var = StringVar()
        self._editor_display = {}
        self.editor_dd = ttk.Combobox(erow, textvariable=self.editor_var,
                                      state="readonly", exportselection=False)
        self.editor_dd.grid(row=0, column=1, padx=(4, 0), sticky="ew")
        self.editor_dd.bind("<<ComboboxSelected>>",
                            lambda _e: self._on_editor_pick())
        self._refresh_editor_list()
        self._tip(self.editor_dd,
                  "Which engine edits the image: Flux Kontext (best at "
                  "keeping identity and style) or Qwen Image Edit.")
        ttk.Button(left, text="\u26a1  Apply edit", style="Go.TButton",
                   command=lambda: self._generate(edit=True)).grid(
                       row=r, sticky="ew", pady=(4, 0))
        r += 1
        self.editor_canvas_var = BooleanVar(value=True)

        left = self._page_gen      # restore for anything after this block
        r = self._gen_row          # (set below just before this block)

        # generate
        gorow = ttk.Frame(left)        # generate
        gorow = ttk.Frame(left); gorow.grid(row=r, sticky=NSEW,
                                            pady=(12, 4)); r += 1
        gorow.columnconfigure(0, weight=1)
        self.go_btn = ttk.Button(gorow, text="⚡  GENERATE", style="Go.TButton",
                                 command=self._generate)
        self.go_btn.grid(row=0, column=0, sticky=NSEW)
        ttk.Button(gorow, text="＋Q", width=4,
                   command=lambda: self._generate(queue=True)).grid(
            row=0, column=1, padx=(4, 0))
        pbrow = ttk.Frame(left); pbrow.grid(row=r, sticky=NSEW, pady=2); r += 1
        pbrow.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(pbrow, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")
        self.pct_var = StringVar(value="")
        ttk.Label(pbrow, textvariable=self.pct_var, width=5,
                  style="Dim.TLabel").grid(row=0, column=1, padx=(6, 0))
        ttk.Button(pbrow, text="✕ Cancel", width=9,
                   command=self._cancel_generation).grid(row=0, column=2,
                                                         padx=(4, 0))
        self.status_var = StringVar(value="Starting engine…")
        ttk.Label(left, textvariable=self.status_var,
                  style="Dim.TLabel", wraplength=400).grid(row=r, sticky=W); r += 1

        # ---------- animator (old-school sprite animation) — its own tab ----------
        left = self._page_anim
        r = 0
        anim_head = ttk.Label(left, text="ANIMATOR — animate a character image "
                                         "into sprite frames & GIF",
                              style="Head.TLabel", wraplength=400,
                              justify="left")
        anim_head.grid(row=r, sticky=W, pady=(14, 0)); r += 1
        self._tip(anim_head,
                  "Turn a single character image into a short looping "
                  "animation (sprite frames + GIF, optional MP4/WebM/sprite "
                  "sheet). Pick a character image, describe the action, and "
                  "generate. Uses a video model — the first run downloads it.")
        arow = ttk.Frame(left); arow.grid(row=r, sticky=NSEW, pady=2); r += 1
        arow.columnconfigure(1, weight=1)
        animchar_btn = ttk.Button(arow, text="🖼 Character…", width=12,
                                  command=self._pick_anim_image)
        animchar_btn.grid(row=0, column=0)
        self._tip(animchar_btn, "Load the character image to animate (works "
                                "best on a clean single character).")
        self.anim_img_var = StringVar(value="none")
        ttk.Label(arow, textvariable=self.anim_img_var, style="Dim.TLabel",
                  wraplength=180).grid(row=0, column=1, sticky=W, padx=6)
        animcur_btn = ttk.Button(arow, text="Use current", width=11,
                                 command=self._use_current_for_anim)
        animcur_btn.grid(row=0, column=2)
        self._tip(animcur_btn, "Animate the image currently selected in the "
                               "gallery.")
        animclr_btn = ttk.Button(arow, text="✕", width=3,
                                 command=self._clear_anim_image)
        animclr_btn.grid(row=0, column=3, padx=(4, 0))
        self._tip(animclr_btn, "Clear the chosen character image.")
        prow = ttk.Frame(left); prow.grid(row=r, sticky=NSEW, pady=(4, 0))
        r += 1
        prow.columnconfigure(1, weight=1)
        ttk.Label(prow, text="Preset", style="Dim.TLabel").grid(row=0,
                                                                column=0)
        self.anim_preset_var = StringVar(value=ANIM_PRESET_HINT)
        pcb = ttk.Combobox(prow, textvariable=self.anim_preset_var,
                           state="readonly", exportselection=False,
                           values=list(ANIM_PRESETS), width=30)
        pcb.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        pcb.bind("<<ComboboxSelected>>", self._on_anim_preset)
        self._tip(pcb, "A ready-made action (walk, wave, attack…). Picking one "
                       "fills the Action box below; you can still edit it.")
        anim_act_lbl = ttk.Label(left, text="Action (what the character does):",
                                 style="Dim.TLabel")
        anim_act_lbl.grid(row=r, sticky=W); r += 1
        self.anim_prompt_box = self._text(left, 2)
        self.anim_prompt_box.grid(row=r, sticky="ew", pady=(2, 4)); r += 1
        self._tip(self.anim_prompt_box,
                  "Describe the motion as a repeating cycle (e.g. 'walking in "
                  "place, arms swinging'). Phrase it as a loop for a seamless "
                  "GIF.")
        a2 = ttk.Frame(left); a2.grid(row=r, sticky=NSEW, pady=2); r += 1
        ttk.Label(a2, text="Seconds", style="Dim.TLabel").grid(row=0,
                                                               column=0)
        self.anim_secs_var = IntVar(value=3)
        secs_sb = ttk.Spinbox(a2, from_=1, to=5, textvariable=self.anim_secs_var,
                              exportselection=False, width=3)
        secs_sb.grid(row=0, column=1, padx=(4, 10))
        self._tip(secs_sb, "Clip length in seconds. Longer clips give a "
                           "cleaner seamless loop (3–5s recommended).")
        ttk.Label(a2, text="Keep", style="Dim.TLabel").grid(row=0, column=2)
        self.anim_keep_var = StringVar(value=list(ANIM_KEEP)[1])
        keep_dd = ttk.Combobox(a2, textvariable=self.anim_keep_var,
                               state="readonly", exportselection=False,
                               values=list(ANIM_KEEP), width=17)
        keep_dd.grid(row=0, column=3, padx=(4, 0))
        self._tip(keep_dd, "How many of the rendered frames to keep — fewer "
                           "frames = smaller, snappier GIF.")
        a3 = ttk.Frame(left); a3.grid(row=r, sticky=NSEW, pady=2); r += 1
        ttk.Label(a3, text="Loop", style="Dim.TLabel").grid(row=0, column=0)
        self.anim_loop_var = StringVar(value=ANIM_LOOPS[0])
        loop_dd = ttk.Combobox(a3, textvariable=self.anim_loop_var,
                               state="readonly", exportselection=False,
                               values=ANIM_LOOPS, width=22)
        loop_dd.grid(row=0, column=1, padx=(4, 10))
        self._tip(loop_dd, "How the clip is made to loop: seamless auto-cut "
                           "(picks the best matching cycle), ping-pong, or "
                           "crossfade.")
        self.anim_size_var = StringVar(value=list(ANIM_SIZES)[0])
        anim_size_dd = ttk.Combobox(a3, textvariable=self.anim_size_var,
                                    state="readonly", exportselection=False,
                                    values=list(ANIM_SIZES), width=18)
        anim_size_dd.grid(row=0, column=2)
        self._tip(anim_size_dd, "Frame size of the animation.")
        a3b = ttk.Frame(left); a3b.grid(row=r, sticky=NSEW, pady=2); r += 1
        ttk.Label(a3b, text="Motion", style="Dim.TLabel").grid(row=0,
                                                               column=0)
        self.anim_motion_var = StringVar(value=list(ANIM_MOTION)[0])
        motion_dd = ttk.Combobox(a3b, textvariable=self.anim_motion_var,
                                 state="readonly", exportselection=False,
                                 values=list(ANIM_MOTION), width=22)
        motion_dd.grid(row=0, column=1, padx=(4, 0))
        self._tip(motion_dd, "How much the character moves: Subtle, Normal or "
                             "Strong (Strong = large, exaggerated motion).")
        a4 = ttk.Frame(left); a4.grid(row=r, sticky=NSEW, pady=2); r += 1
        self.anim_transparent_var = BooleanVar(value=True)
        atrans_cb = ttk.Checkbutton(a4, text="Transparent frames",
                                    variable=self.anim_transparent_var)
        atrans_cb.pack(side="left")
        self._tip(atrans_cb, "Cut out the background on each frame so the "
                             "animation is on transparency.")
        self.anim_gif_var = BooleanVar(value=True)
        agif_cb = ttk.Checkbutton(a4, text="Make GIF",
                                  variable=self.anim_gif_var)
        agif_cb.pack(side="left", padx=(12, 0))
        self._tip(agif_cb, "Also save a looping GIF alongside the frames.")
        self.anim_zip_var = BooleanVar(value=True)
        azip_cb = ttk.Checkbutton(a4, text="Zip folder",
                                  variable=self.anim_zip_var)
        azip_cb.pack(side="left", padx=(12, 0))
        self._tip(azip_cb, "Zip the frames folder for easy sharing.")
        a5 = ttk.Frame(left); a5.grid(row=r, sticky=NSEW, pady=(0, 2)); r += 1
        self.anim_sheet_var = BooleanVar(value=False)
        asheet_cb = ttk.Checkbutton(a5, text="Sprite sheet",
                                    variable=self.anim_sheet_var)
        asheet_cb.pack(side="left")
        self._tip(asheet_cb, "Also export a single sprite-sheet PNG + JSON "
                             "atlas of all frames.")
        self.anim_video_var = StringVar(value="none")
        ttk.Label(a5, text="Video", style="Dim.TLabel").pack(side="left",
                                                             padx=(12, 2))
        avid_dd = ttk.Combobox(a5, textvariable=self.anim_video_var,
                               state="readonly", exportselection=False,
                               values=["none", "MP4", "WebM"], width=7)
        avid_dd.pack(side="left")
        self._tip(avid_dd, "Also export a video file (MP4 or WebM) — opaque, "
                           "no transparency.")
        anrow = ttk.Frame(left); anrow.grid(row=r, sticky=NSEW,
                                            pady=(4, 2)); r += 1
        anrow.columnconfigure(0, weight=1)
        genanim_btn = ttk.Button(anrow, text="🎬 Generate animation",
                                 style="Go.TButton",
                                 command=self._generate_animation)
        genanim_btn.grid(row=0, column=0, sticky="ew")
        self._tip(genanim_btn, "Make the animation now from the chosen "
                               "character image and action.")
        animq_btn = ttk.Button(anrow, text="＋Q", width=4,
                               command=lambda: self._generate_animation(queue=True))
        animq_btn.grid(row=0, column=1, padx=(4, 0))
        self._tip(animq_btn, "Add this animation to the batch queue to run "
                             "after the current jobs, instead of now.")
        apb = ttk.Frame(left); apb.grid(row=r, sticky=NSEW, pady=(0, 8)); r += 1
        apb.columnconfigure(0, weight=1)
        self.anim_progress = ttk.Progressbar(apb, mode="determinate")
        self.anim_progress.grid(row=0, column=0, sticky="ew")
        self.anim_pct_var = StringVar(value="")
        ttk.Label(apb, textvariable=self.anim_pct_var, width=5,
                  style="Dim.TLabel").grid(row=0, column=1, padx=(6, 0))
        ttk.Button(apb, text="✕ Cancel", width=9,
                   command=self._cancel_generation).grid(row=0, column=2,
                                                         padx=(4, 0))

        # ---------- border maker — its own tab ----------
        left = self._page_border
        r = 0
        ttk.Label(left, text="BORDER MAKER — themed frame, transparent "
                             "center, no text", style="Head.TLabel",
                  wraplength=400, justify="left").grid(
            row=r, sticky=W, pady=(14, 0)); r += 1
        ttk.Label(left, text="Border prompt — a theme (franchise, movie, "
                             "game, material) or a full precise prompt:",
                  style="Dim.TLabel").grid(row=r, sticky=W); r += 1
        self.border_prompt_box = self._text(left, 3)
        self.border_prompt_box.grid(row=r, sticky="ew", pady=(2, 2)); r += 1
        self.border_auto_var = BooleanVar(value=True)
        ttk.Checkbutton(left, text="Add frame wording automatically "
                                   "(uncheck to use your prompt verbatim)",
                        variable=self.border_auto_var).grid(
            row=r, sticky=W, pady=(0, 4)); r += 1
        bmod = ttk.Frame(left); bmod.grid(row=r, sticky=NSEW, pady=2); r += 1
        bmod.columnconfigure(1, weight=1)
        ttk.Label(bmod, text="Model", style="Dim.TLabel").grid(row=0,
                                                               column=0)
        self.border_model_var = StringVar(value=BORDER_SAME_MODEL)
        self.border_model_dd = ttk.Combobox(bmod,
                                            textvariable=self.border_model_var,
                                            state="readonly",
                                            exportselection=False, width=34)
        self.border_model_dd.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.border_model_dd.bind("<<ComboboxSelected>>",
                                  lambda _e: self._refresh_border_styles())
        bsty = ttk.Frame(left); bsty.grid(row=r, sticky=NSEW, pady=2); r += 1
        bsty.columnconfigure(1, weight=1)
        ttk.Label(bsty, text="Style", style="Dim.TLabel").grid(row=0,
                                                               column=0)
        self.border_style_var = StringVar(value=NONE_PRESET)
        self.border_style_dd = ttk.Combobox(bsty,
                                            textvariable=self.border_style_var,
                                            state="readonly",
                                            exportselection=False, width=34)
        self.border_style_dd.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ttk.Label(left, text="LoRAs, Variations, Steps, Seed and Editor "
                             "still come from the Image generation tab.",
                  style="Dim.TLabel", wraplength=400,
                  justify="left").grid(row=r, sticky=W); r += 1
        barow = ttk.Frame(left); barow.grid(row=r, sticky=NSEW, pady=2); r += 1
        ttk.Label(barow, text="Aspect", style="Dim.TLabel").grid(row=0,
                                                                 column=0)
        self.border_aspect_var = StringVar(value=list(BORDER_SIZES)[2])
        ttk.Combobox(barow, textvariable=self.border_aspect_var,
                     state="readonly", exportselection=False,
                     values=list(BORDER_SIZES),
                     width=30).grid(row=0, column=1, padx=(4, 0), sticky=W)
        brefrow = ttk.Frame(left); brefrow.grid(row=r, sticky=NSEW, pady=2); r += 1
        brefrow.columnconfigure(1, weight=1)
        ttk.Button(brefrow, text="🖼 Refs…", width=8,
                   command=self._pick_border_refs).grid(row=0, column=0)
        self.border_ref_var = StringVar(value="none")
        ttk.Label(brefrow, textvariable=self.border_ref_var,
                  style="Dim.TLabel", wraplength=250).grid(row=0, column=1,
                                                           sticky=W, padx=6)
        ttk.Button(brefrow, text="✕", width=3,
                   command=self._clear_border_refs).grid(row=0, column=2)
        ttk.Label(left, text="Refs use the Image editor (Image generation "
                             "tab): they are "
                             "redrawn as the border (style, characters and "
                             "composition carry over).",
                  style="Dim.TLabel", wraplength=400,
                  justify="left").grid(row=r, sticky=W); r += 1
        btrow = ttk.Frame(left); btrow.grid(row=r, sticky=NSEW, pady=2); r += 1
        ttk.Label(btrow, text="Thickness", style="Dim.TLabel").pack(side="left")
        self.border_thick_var = DoubleVar(value=14)
        ttk.Scale(btrow, from_=6, to=30, variable=self.border_thick_var,
                  length=150).pack(side="left", padx=6)
        self.border_thick_lab = ttk.Label(btrow, text="14%", width=5,
                                          style="Dim.TLabel")
        self.border_thick_lab.pack(side="left")
        self.border_thick_var.trace_add(
            "write", lambda *_a: self.border_thick_lab.config(
                text=f"{int(self.border_thick_var.get())}%"))
        bvrow = ttk.Frame(left); bvrow.grid(row=r, sticky=NSEW, pady=2); r += 1
        ttk.Label(bvrow, text="Variations", style="Dim.TLabel").pack(
            side="left")
        self.border_batch_var = IntVar(value=1)
        ttk.Spinbox(bvrow, from_=1, to=10, textvariable=self.border_batch_var,
                    exportselection=False, width=4).pack(side="left",
                                                         padx=(6, 0))
        ttk.Label(bvrow, text="(makes several borders from one prompt so you "
                             "can pick the best)", style="Dim.TLabel").pack(
            side="left", padx=(8, 0))
        self.border_clean_var = BooleanVar(value=True)
        ttk.Checkbutton(left, text="Auto-clean center (2nd pass with Flux "
                                   "Kontext when a theme fills the middle)",
                        variable=self.border_clean_var).grid(
            row=r, sticky=W, pady=(2, 0)); r += 1
        borow = ttk.Frame(left); borow.grid(row=r, sticky=NSEW,
                                            pady=(4, 2)); r += 1
        borow.columnconfigure(0, weight=1)
        ttk.Button(borow, text="⚡ Generate border", style="Go.TButton",
                   command=self._generate_border).grid(row=0, column=0,
                                                       sticky="ew")
        ttk.Button(borow, text="＋Q", width=4,
                   command=lambda: self._generate_border(queue=True)).grid(
            row=0, column=1, padx=(4, 0))
        bpb = ttk.Frame(left); bpb.grid(row=r, sticky=NSEW, pady=(0, 8)); r += 1
        bpb.columnconfigure(0, weight=1)
        self.border_progress = ttk.Progressbar(bpb, mode="determinate")
        self.border_progress.grid(row=0, column=0, sticky="ew")
        self.border_pct_var = StringVar(value="")
        ttk.Label(bpb, textvariable=self.border_pct_var, width=5,
                  style="Dim.TLabel").grid(row=0, column=1, padx=(6, 0))
        ttk.Button(bpb, text="✕ Cancel", width=9,
                   command=self._cancel_generation).grid(row=0, column=2,
                                                         padx=(4, 0))

        # ---------- batch queue (under the tabs, always visible) ----------
        left = self._page_bottom
        r = 0
        self.queue_count_var = StringVar(value="Batch queue (0)")
        ttk.Label(left, textvariable=self.queue_count_var,
                  style="Head.TLabel").grid(row=r, sticky=W,
                                            pady=(14, 0)); r += 1
        ttk.Label(left, text="Use ＋Q next to any Generate button to add the "
                             "current settings as a job, then Run all.",
                  style="Dim.TLabel", wraplength=400,
                  justify="left").grid(row=r, sticky=W); r += 1
        qframe = ttk.Frame(left); qframe.grid(row=r, sticky=NSEW, pady=2)
        r += 1
        qframe.columnconfigure(0, weight=1)
        self.queue_list = Listbox(qframe, selectmode="extended", height=4,
                                  bg=BG3, fg=FG, relief="flat",
                                  highlightthickness=0, activestyle="none",
                                  exportselection=False, font=(UI_FONT, 9))
        self.queue_list.grid(row=0, column=0, sticky="ew")
        qsb = ttk.Scrollbar(qframe, orient="vertical",
                            command=self.queue_list.yview)
        self.queue_list.configure(yscrollcommand=qsb.set)
        qsb.grid(row=0, column=1, sticky="ns")
        qbtns = ttk.Frame(left); qbtns.grid(row=r, sticky=NSEW,
                                            pady=(2, 8)); r += 1
        ttk.Button(qbtns, text="▶ Run all",
                   command=self._run_queue).pack(side="left")
        ttk.Button(qbtns, text="Remove",
                   command=self._remove_queued).pack(side="left", padx=6)
        ttk.Button(qbtns, text="Clear",
                   command=self._clear_queue).pack(side="left")

        # ---------- version + updates (bottom of the panel) ----------
        vrow2 = ttk.Frame(left); vrow2.grid(row=r, sticky=NSEW,
                                            pady=(10, 4)); r += 1
        ttk.Label(vrow2, text=f"Version {APP_VERSION}",
                  style="Dim.TLabel").pack(side="left")
        self.upd_btn = ttk.Button(vrow2, text="⭯ Check for updates",
                                  command=self._check_updates_now)
        self.upd_btn.pack(side="left", padx=(10, 0))
        self._tip(self.upd_btn,
                  "Ask GitHub whether a newer release of the app exists. If "
                  "there is one, a window shows what changed and you choose "
                  "whether to install it — nothing is downloaded until you "
                  "say so. (At startup the app checks on its own and asks "
                  "before downloading: Download and install, Not now, or "
                  "Skip this version; after installing it asks again before "
                  "restarting.) This also brings back a version you "
                  "skipped.")
        self.log_btn = ttk.Button(vrow2, text="📋 Log", command=self._open_log)
        self.log_btn.pack(side="left", padx=(6, 0))
        self.settings_btn = ttk.Button(vrow2, text="⚙ Settings",
                                       command=self._open_settings)
        self.settings_btn.pack(side="left", padx=(6, 0))
        self._tip(self.settings_btn,
                  "Appearance settings: the Daylight or Dark theme and the "
                  "size of the menus and text.")
        self._tip(self.log_btn,
                  "Open app.log — every message the app showed and every "
                  "error it caught, with the time. Send it along when "
                  "reporting a problem.")

        # ---------- right column: preview + gallery ----------
        right = ttk.Frame(root, padding=(0, 12, 12, 12))
        right.grid(row=0, column=1, sticky=NSEW)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        # top-right bar: LoRA/RAG mode badges + the live VRAM meter
        vrow = ttk.Frame(right)
        vrow.grid(row=0, column=0, sticky="e", pady=(0, 6))
        self.lora_badge = ttk.Label(vrow, text="LoRA", style="BadgeOff.TLabel")
        self.lora_badge.pack(side="left", padx=(0, 5))
        self.rag_badge = ttk.Label(vrow, text="RAG", style="BadgeOff.TLabel")
        self.rag_badge.pack(side="left", padx=(0, 16))
        self._tip(self.lora_badge,
                  "LoRA status. Green when at least one LoRA is ticked and it "
                  "will apply to the next generation. Red while you are editing "
                  "a loaded image (editors ignore LoRAs), when no LoRA is "
                  "ticked, or on a model that can't use them.")
        self._tip(self.rag_badge,
                  "RAG map status. Green when a valid RAG map is loaded and "
                  "will guide the next generation on an SDXL model. Red while "
                  "editing a loaded image, when no map is loaded, or on a Flux "
                  "model (RAG image guidance needs SDXL).")
        self.gpu_var = StringVar(
            value="GPU —%" if self.vram_gb is not None else "")
        self.gpu_badge = ttk.Label(vrow, textvariable=self.gpu_var,
                                   style="Dim.TLabel")
        self.gpu_badge.pack(side="left", padx=(0, 6))
        self._tip(self.gpu_badge, "How busy the GPU is right now "
                                  "(nvidia-smi, every 3 seconds).")
        self.gpu_bar = ttk.Progressbar(vrow, mode="determinate", length=120,
                                       maximum=100,
                                       style="Gpu.Horizontal.TProgressbar")
        self.gpu_bar.pack(side="left", padx=(0, 16))
        self._tip(self.gpu_bar, "GPU busy, 0-100%.")
        self.vram_label_var = StringVar(
            value="VRAM — MB" if self.vram_gb is not None
            else "no NVIDIA GPU")
        vram_lab = ttk.Label(vrow, textvariable=self.vram_label_var,
                             style="Dim.TLabel")
        vram_lab.pack(side="left", padx=(0, 8))
        self._tip(vram_lab, "GPU memory in use / total (nvidia-smi, every "
                            "3 seconds). The engine keeps the last model "
                            "loaded, so this stays high between jobs.")
        self.vram_bar = ttk.Progressbar(vrow, mode="determinate",
                                        length=220)
        self.vram_bar.pack(side="left")

        self.canvas = Canvas(right, bg=BG2, highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky=NSEW)
        self.canvas.bind("<Configure>", lambda e: self._show_current())
        # wheel-zoom the preview: scroll to zoom at the cursor, drag to
        # pan while zoomed, double-click to reset to fit
        self._zoom = 1.0
        self._view_c = None       # zoomed view center in image coords
        self._zoom_for = None     # which gallery index the zoom belongs to
        self.canvas.bind("<MouseWheel>", self._canvas_zoom)
        self.canvas.bind("<ButtonPress-1>", self._pan_start)
        self.canvas.bind("<B1-Motion>", self._pan_move)
        self.canvas.bind("<Double-Button-1>", self._zoom_reset)
        self._tip(self.canvas,
                  "Scroll to zoom in on the picture (zooms at the cursor), "
                  "drag to pan while zoomed, double-click to fit again.")

        brow = ttk.Frame(right); brow.grid(row=2, column=0, sticky=NSEW, pady=(8, 4))
        saveas_btn = ttk.Button(brow, text="💾 Save As…", command=self._save_as)
        saveas_btn.pack(side="left")
        self._tip(saveas_btn, "Save the selected image (or GIF) somewhere of "
                              "your choosing. Every image is also auto-saved to "
                              "the output folder.")
        self.delimg_btn = ttk.Button(brow, text="🗑 Delete image",
                                     command=self._delete_current)
        self.delimg_btn.pack(side="left", padx=6)
        self._tip(self.delimg_btn,
                  "Delete the selected image file and remove it from the "
                  "gallery — or, when images are tagged, delete exactly the "
                  "tagged ones. Tag with Ctrl+click or right-click on a "
                  "thumbnail, or with the Tag button.")
        self.tag_btn = ttk.Button(brow, text="☑ Tag", command=self._tag_current)
        self.tag_btn.pack(side="left", padx=(0, 6))
        self._tip(self.tag_btn,
                  "Tag or untag the selected image for deletion. Tagged "
                  "thumbnails get a red frame; 🗑 Delete then removes only "
                  "the tagged images. Ctrl+click or right-click on a "
                  "thumbnail does the same.")
        openout_btn = ttk.Button(brow, text="📁 Open output folder",
                                 command=lambda: os.startfile(OUTPUT))
        openout_btn.pack(side="left", padx=6)
        self._tip(openout_btn, "Open the folder where generated images are "
                               "saved.")
        civ_btn = ttk.Button(brow, text="⬇ Get LoRAs (CivitAI)",
                             command=self._civitai_dialog)
        civ_btn.pack(side="left", padx=6)
        self._tip(civ_btn, "Download a LoRA from a CivitAI link (needs a free "
                           "API key). Its trained words are saved so the "
                           "trigger is applied automatically when ticked.")
        addtrain_btn = ttk.Button(brow, text="⭐ Add to training set",
                                  command=self._add_to_training)
        addtrain_btn.pack(side="left", padx=6)
        self._tip(addtrain_btn, "Save the selected image + its prompt into a "
                                "dataset folder, for training your own LoRA "
                                "later with an external trainer.")
        self.info_var = StringVar(value="")
        ttk.Label(brow, textvariable=self.info_var,
                  style="Dim.TLabel").pack(side="right")

        # gallery strip — horizontally scrollable, with its own clear button
        gwrap = ttk.Frame(right)
        gwrap.grid(row=3, column=0, sticky=NSEW, pady=(4, 0))
        gwrap.columnconfigure(0, weight=1)
        self.gallery_canvas = Canvas(gwrap, height=96, bg=BG,
                                     highlightthickness=0)
        self.gallery_canvas.grid(row=0, column=0, sticky="ew")
        self.gallery = ttk.Frame(self.gallery_canvas)
        self._gallery_win = self.gallery_canvas.create_window(
            (0, 0), window=self.gallery, anchor="nw")
        gsb = ttk.Scrollbar(gwrap, orient="horizontal",
                            command=self.gallery_canvas.xview)
        gsb.grid(row=1, column=0, sticky="ew")
        self.gallery_canvas.configure(xscrollcommand=gsb.set)
        self.gallery.bind(
            "<Configure>",
            lambda _e: self.gallery_canvas.configure(
                scrollregion=self.gallery_canvas.bbox("all")))
        self.gallery_canvas.bind(
            "<MouseWheel>",
            lambda e: self.gallery_canvas.xview_scroll(
                -1 if e.delta > 0 else 1, "units"))
        self._tip(self.gallery_canvas, "Your generated images this session. "
                  "Click a thumbnail to select it — Swap and the editor act on "
                  "the selected picture.")
        gbtns = ttk.Frame(gwrap)
        gbtns.grid(row=0, column=1, rowspan=2, sticky="s", padx=(8, 0))
        clrhist_btn = ttk.Button(gbtns, text="🗑 Clear history",
                                 command=self._clear_history)
        clrhist_btn.pack(fill="x", pady=(0, 3))
        self._tip(clrhist_btn, "Clear the gallery strip for this session. The "
                               "image files on disk are kept.")
        self.delfiles_btn = ttk.Button(gbtns, text="❌ Delete art files…",
                                       command=self._delete_history_files)
        self.delfiles_btn.pack(fill="x")
        self._tip(self.delfiles_btn,
                  "Permanently delete the generated image files from the "
                  "output folder (asks first).")

    # -------------------------------------------------- persistence
    def _get(self, box):
        return box.get("1.0", END).strip()

    def _set(self, box, text):
        box.delete("1.0", END)
        if text:
            box.insert("1.0", text)

    def _selected_loras(self):
        return [self.lora_list.get(i) for i in self.lora_list.curselection()]

    def _load_ragmap_async(self, path, on_done):
        """Parse a RAG map OFF the UI thread and hand the result to
        on_done(path, rag, err) on the UI thread.

        A map can be hundreds of MB of JSON plus a multi-GB embeddings
        file on a slow drive — a user's is 925 MB + 1.97 GB on an HDD —
        and parsing it in the UI thread froze the app ("Not responding")
        for as long as that took, twice at every launch (the restore, then
        the re-validation that followed it). Only the newest request
        counts: a later pick supersedes an earlier one still parsing."""
        self._ragmap_load_gen += 1
        gen = self._ragmap_load_gen
        try:
            mb = Path(path).stat().st_size / (1024 * 1024)
        except OSError:
            mb = 0
        self.ragmap_var.set(f"{Path(path).name} — loading…")
        self.status_var.set(f"Loading RAG map {Path(path).name}"
                            + (f" ({mb:.0f} MB)" if mb >= 1 else "")
                            + " — you can keep working.")
        self._ragmap_loading = (Path(path).name, mb)
        self._rag_progress(gen, "reading", 0, 0)

        def work():
            try:
                rag = load_ragmap(path, progress=lambda ph, d, t: self.ui_queue.put(
                    ("ragmap_progress", gen, ph, d, t)))
                self.ui_queue.put(("ragmap_loaded", gen, path, rag, None,
                                   on_done))
            except Exception as e:
                applog.exception("RAG map parse failed: " + str(path))
                self.ui_queue.put(("ragmap_loaded", gen, path, None, e,
                                   on_done))

        threading.Thread(target=work, daemon=True).start()

    def _sweep(self, bar, on, interval=45):
        """Start or stop a progress bar's sweep exactly once.

        ttk's start() schedules a NEW timer chain on every call and keeps
        only the newest id, and stop() cancels only that one — so a second
        start() left a chain nothing could reach, the bar stepped twice as
        fast, and after a batch of four it raced for ever. Starts are
        therefore guarded, and stopping also cancels any Autoincrement
        timer for this bar that Tk still holds."""
        key = str(bar)
        if on:
            if not self._sweeping.get(key):
                bar.configure(mode="indeterminate")
                bar.start(interval)
                self._sweeping[key] = True
            return
        try:
            bar.stop()
        except Exception:
            pass
        try:
            stray = 0
            for aid in self.root.tk.call("after", "info"):
                info = str(self.root.tk.call("after", "info", aid))
                if "Autoincrement" in info and key in info:
                    self.root.tk.call("after", "cancel", aid)
                    stray += 1
            if stray > 1:
                applog.log("progress bar %s: cancelled %d stacked sweep "
                           "timer(s)" % (key, stray))
        except Exception:
            pass
        bar.configure(mode="determinate")
        self._sweeping[key] = False

    # ------------------------------------------------ what is still loading
    def _set_pending(self, key, text):
        """Mark one start-up item as still loading (text) or done (None)
        and repaint the strip above the tabs."""
        if text:
            self._pending[key] = text
        else:
            self._pending.pop(key, None)
        if not hasattr(self, "ready_frame"):
            return
        if self._pending:
            self.ready_var.set("Still loading — please wait: "
                               + "; ".join(self._pending.values()))
            self.ready_frame.grid()
            self._sweep(self.ready_bar, True, 45)
        else:
            self._sweep(self.ready_bar, False)
            self.ready_frame.grid_remove()
            self.ready_var.set("")

    def _rag_progress(self, gen, phase, done, total):
        """Paint the RAG-map loading bar — for the newest load only."""
        if gen != self._ragmap_load_gen or not hasattr(self, "rag_prog"):
            return
        name, mb = getattr(self, "_ragmap_loading", ("RAG map", 0))
        size = f" ({mb:.0f} MB)" if mb >= 1 else ""
        self.rag_prog.grid()
        self.rag_prog_lab.grid()
        if phase == "resolving" and total:
            self._sweep(self.rag_prog, False)
            self.rag_prog.configure(maximum=total, value=done)
            self.rag_prog_var.set(
                f"Loading {name}: checking references {done:,} / {total:,} "
                f"({done * 100 // total}%)")
            self._set_pending("ragmap", f"RAG map {name} "
                                        f"({done * 100 // total}%)")
        else:
            self._set_pending("ragmap", f"RAG map {name} ({phase})")
            text = {"reading": f"reading and parsing the map file{size}",
                    "embeddings": "reading the embeddings",
                    "indexing": "building the word index"}.get(phase, phase)
            self._sweep(self.rag_prog, True, 45)
            self.rag_prog_var.set(f"Loading {name}: {text}…")

    def _rag_progress_done(self):
        self._set_pending("ragmap", None)
        if hasattr(self, "rag_prog"):
            self._sweep(self.rag_prog, False)
            self.rag_prog.grid_remove()
            self.rag_prog_lab.grid_remove()
            self.rag_prog_var.set("")

    def _ragmap_restored(self, path, rag, err):
        """The map remembered from last session (or one re-parsed because
        the file changed) has finished parsing."""
        if self.ragmap_path != path:
            return          # the user picked something else meanwhile
        if err is not None or not rag:
            self.ragmap = None
            self.ragmap_var.set("none")
            self.status_var.set(f"RAG map {Path(path).name} could not be "
                                f"reloaded ({err}) — pick it again with "
                                "🧭 RAG map….")
            return
        self.ragmap = rag
        self.ragmap_var.set(self._ragmap_label(rag, path))
        self._refresh_mode_badges()
        self.status_var.set(f"RAG map '{rag.get('name', '')}' ready.")

    def _pick_ragmap(self):
        """Load a RAG map — this app's .ragmap.json, or the retrieval map
        a trainer writes beside a finished LoRA (<name>-rag\\index.json).
        At generation the app retrieves the closest example images and
        feeds them as IP-Adapter visual guidance alongside the LoRA."""
        path = filedialog.askopenfilename(
            title="Load RAG map (.ragmap.json, or a trainer's index.json)",
            filetypes=[("RAG map", ("*.ragmap.json", "index.json")),
                       ("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        self._load_ragmap_async(path, self._ragmap_picked)

    def _ragmap_picked(self, path, rag, err):
        """The rest of loading a picked map, on the UI thread once the
        parse (off it) is done."""
        if err is not None:
            messagebox.showerror("RAG map",
                                 f"Could not read the RAG map: {err}")
            if self.ragmap is None:
                self.ragmap_var.set("none")
            else:
                self.ragmap_var.set(self._ragmap_label(self.ragmap,
                                                       self.ragmap_path))
            return
        entries = rag.get("entries", [])
        # a usable reference is a real image file OR a precomputed embed
        # (an embeddings-only map has no images at all, on purpose)
        n = len([e for e in entries if e.get("_path") or e.get("_ipadpt")])
        caps = len([e for e in entries if e.get("caption")])
        if not n and not caps:
            messagebox.showwarning(
                "RAG map", "This RAG map has nothing usable in it — no images "
                "resolve next to the map file, and it carries no captions.")
            return
        self.ragmap = rag
        self.ragmap_path = path
        notes = []
        # the paired LoRA: use it if installed; offer to copy it in when
        # the map was written next to it; otherwise carry on without it
        lora = ragmap_lora(rag)
        if not lora and rag.get("lora"):
            src = rag.get("_lora_src")
            if src and Path(src).exists() and messagebox.askyesno(
                    "Install the paired LoRA",
                    f"This map was trained with {Path(src).name}, which isn't "
                    "in your LoRA folder yet.\n\nCopy it in now? (Without it "
                    "the map still guides with its example images.)"):
                try:
                    shutil.copy2(src, _ensure_lora_dir() / Path(src).name)
                    self._pending_loras = getattr(
                        self, "_pending_loras", set()) | {Path(src).name}
                    self._refresh_models()
                    lora = ragmap_lora(rag)
                except OSError as e:
                    notes.append(f"couldn't copy the LoRA in ({e})")
            if not lora:
                notes.append(f"paired LoRA '{rag['lora']}' isn't installed — "
                             "add it with ➕ Add LoRA file")
        if not n:
            notes.append(f"no images travelled with this map — its {caps} "
                         "captions will be used as text instead")
        self.ragmap_var.set(self._ragmap_label(rag, path))
        if n and not self._style_support_ok():
            if messagebox.askyesno(
                    "Enable image guidance",
                    "RAG image guidance uses IP-Adapter, which isn't set up "
                    "yet (a ~1 GB one-time download).\n\nInstall it now? "
                    "(Without it, the RAG map still helps by adding its "
                    "captions to your prompt as text.)"):
                threading.Thread(target=self._install_style_support,
                                 daemon=True).start()
        if notes:
            self.status_var.set("RAG map loaded. " + "; ".join(notes))
        else:
            self.status_var.set(
                f"RAG map '{rag.get('name', '')}' loaded — its example "
                "images will guide generations on SDXL models.")
        self._refresh_mode_badges()
        self._schedule_persist()

    # ------------------------------------------------ reference database
    # Portable SQLite files the USER builds with the Actor DB Builder /
    # Database Builder tools (nothing ships with the app). Two families,
    # told apart by meta(format):
    #   cbac-actordb-*  person DBs — actor(imdb_id, first_name, last_name,
    #       birth_date, death_date, sex, headshot BLOB…); singer builds
    #       append music columns (genres, voice_type, years_active,
    #       description…) which the browser surfaces when present; extra
    #       pictures live in actor_photo(imdb_id, seq, photo) (or the
    #       older photo(imdb_id, image) shape).
    #   cbac-chardb-*   comic-character DBs — character(comicvine_id,
    #       name, real_name, character_type, first_year, appearances,
    #       deck, portrait BLOB…) + character_photo(comicvine_id, seq,
    #       photo). Rows are normalised into the person-dict shape so
    #       selection, persistence and the swap feed are format-blind.
    # The chosen entry's picture is extra context for the model: while
    # editing it joins the editor's reference images (Kontext/Qwen see
    # it); on plain generations it face-guides via IP-Adapter (SDXL
    # only). Not RAG — no retrieval.

    @staticmethod
    def _actor_age(birth, death=None):
        if not birth:
            return None
        try:
            b = [int(x) for x in str(birth).split("-")]
            while len(b) < 3:
                b.append(1)
            if death:
                d = [int(x) for x in str(death).split("-")]
                while len(d) < 3:
                    d.append(1)
                end = datetime(*d)
            else:
                end = datetime.now()
            return end.year - b[0] - ((end.month, end.day) < (b[1], b[2]))
        except Exception:
            return None

    def _load_actordb(self, path, quiet=False):
        """Open and validate a reference DB (person or character format);
        returns the row count or None. Sets self.actordb_kind."""
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
            try:
                fmt = conn.execute(
                    "SELECT value FROM meta WHERE key='format'").fetchone()
                fmt_s = str(fmt[0]) if fmt else ""
                kind = ("character" if fmt_s.startswith("cbac-chardb")
                        else "person")
                table = "character" if kind == "character" else "actor"
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            finally:
                conn.close()
        except Exception as e:
            if not quiet:
                messagebox.showerror(
                    "Reference DB", f"Could not read this database: {e}")
            return None
        if not fmt_s.startswith(("cbac-actordb", "cbac-chardb")):
            if not quiet and not messagebox.askyesno(
                    "Reference DB",
                    "This file wasn't made by Actor DB Builder — it may "
                    "still work if it has the same actor table.\n\nUse it "
                    "anyway?"):
                return None
        self.actordb_path = str(path)
        self.actordb_kind = kind
        word = "characters" if kind == "character" else "people"
        self.actordb_var.set(f"{Path(path).name} — {n} {word}")
        return n

    def _pick_actordb(self):
        path = filedialog.askopenfilename(
            title="Load reference database (made with Actor DB Builder)",
            filetypes=[("Reference DB", "*.sqlite *.db"),
                       ("All files", "*.*")])
        if not path:
            return
        n = self._load_actordb(path)
        if n is None:
            return
        self.actor_sel = None
        self.actor_var.set("no person selected")
        self.actor_thumb_lab.configure(image="")
        self._actor_thumb = None
        if n and not self._style_support_ok():
            if messagebox.askyesno(
                    "Enable image guidance",
                    "On plain generations the person's photo guides via "
                    "IP-Adapter, which isn't set up yet (a ~1 GB one-time "
                    "download).\n\nInstall it now? (Editing needs nothing "
                    "extra — the editor sees the photo directly.)"):
                threading.Thread(target=self._install_style_support,
                                 daemon=True).start()
        word = ("characters" if getattr(self, "actordb_kind", "person")
                == "character" else "people")
        self.status_var.set(f"Reference database loaded ({n} {word}) — "
                            "pick one with 👤 Person… (ℹ explains how the "
                            "photo is used).")
        self._schedule_persist()

    def _clear_actordb(self):
        self.actordb_path = None
        self.actordb_kind = "person"
        self.actor_sel = None
        self.actordb_var.set("none")
        self.actor_var.set("no person selected")
        self.actor_thumb_lab.configure(image="")
        self._actor_thumb = None
        self.status_var.set("Reference database removed.")
        self._schedule_persist()

    def _refdb_info(self):
        """The what-uses-what matrix — which extras apply in each mode."""
        messagebox.showinfo(
            "How your extras apply",
            "PLAIN GENERATION (no editor images):\n"
            "  • Style preset + ticked LoRAs — APPLIED\n"
            "  • RAG map — APPLIED (SDXL: image guidance; Flux: skipped)\n"
            "  • Reference DB person — face guidance via IP-Adapter\n"
            "    (SDXL models only)\n"
            "  • LoRAs + RAG + person combine: the LoRAs and RAG map build\n"
            "    the styled body/scene and the person face-guides it, all in\n"
            "    one pass. A private (embeds-only) map and a person now chain\n"
            "    on one IP-Adapter instead of the person replacing the map.\n"
            "  • Ticked LoRAs' trigger words are added to the prompt\n"
            "    automatically (read from the LoRA's own metadata/sidecar)\n\n"
            "IMAGE EDITING (editor image(s) loaded, swap box UNTICKED):\n"
            "  • Your prompt becomes the edit instruction\n"
            "  • Style preset, LoRAs and RAG map — NOT applied\n"
            "    (the Kontext/Qwen editors don't use them)\n"
            "  • Reference DB person — the photo is handed to the editor\n"
            "    as context, so it can draw that person into the result\n"
            "    (➡ To editor loads ALL their photos, up to the editor's\n"
            "    limit: 4 images for Kontext, 3 for Qwen)\n\n"
            "🔀 USE RAG & LoRA FOR IMAGE SWAP (ticked):\n"
            "  • Generate runs TWO steps: your prompt + preset + LoRAs +\n"
            "    RAG map draw the styled picture as usual, then the face\n"
            "    from your loaded image(s) (or the 👤 Person if nothing\n"
            "    is loaded) is applied to it — Qwen Image Edit when\n"
            "    installed, else Flux Kontext\n"
            "  • Load SEVERAL photos of the same person and they are all\n"
            "    used together for a sharper likeness\n"
            "  • Both pictures land in the gallery — the styled base and\n"
            "    the face-swapped one\n"
            "  • While ticked, loaded images are the FACE, not edit\n"
            "    targets\n\n"
            "Tip: for LoRA-styled fresh art guided by a face, select the "
            "person and generate WITHOUT editor images on an SDXL model. "
            "To restyle or stage the person directly, use ➡ To editor and "
            "write the instruction.")

    def _actor_to_editor(self):
        """Load the chosen person's photos into the editor's references —
        exactly as if they had been loaded with 🖼 Load…. EVERY picture the
        database holds for them is added, up to the editor's image limit
        (Kontext takes 4, Qwen 3), so the model sees the person from more
        than one angle."""
        if not self.actor_sel:
            messagebox.showinfo("Reference DB",
                                "Pick a person first (👤 Person…).")
            return
        paths = self._actor_ref_paths_all()
        if not paths:
            messagebox.showinfo("Reference DB",
                                "This person has no photo in the database.")
            return
        cap = 4 if self._editor_engine() == "kontext" else 3
        added, left_out = 0, 0
        for p in paths:
            if p in self.ref_paths:
                continue
            if len(self.ref_paths) >= cap:
                left_out += 1
                continue
            self.ref_paths.append(p)
            added += 1
        first = Path(self.ref_paths[0]).name
        self.ref_var.set(first if len(self.ref_paths) == 1 else
                         f"{len(self.ref_paths)} images ({first}, …)")
        note = (f"{added} photos added" if added != 1 else "Photo added")
        if left_out:
            note += (f" ({left_out} left out — this editor takes at most "
                     f"{cap} images)")
        self.status_var.set(note + " to the editor — the prompt is now "
                            "an edit instruction (presets/LoRAs/RAG are "
                            "off while editing).")
        self._refresh_editor_state()
        self._schedule_persist()

    # optional singer-build columns surfaced in the browser when present
    _PERSON_EXTRA_COLS = ("genres", "instruments", "voice_type",
                          "years_active", "description", "sitelinks")

    def _actordb_rows(self):
        conn = sqlite3.connect(f"file:{self.actordb_path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            if getattr(self, "actordb_kind", "person") == "character":
                return [{"imdb_id": str(r["comicvine_id"]),
                         "first_name": r["name"], "last_name": "",
                         "sex": r["gender"] or "",
                         "birth_date": None, "death_date": None,
                         "real_name": r["real_name"],
                         "character_type": r["character_type"],
                         "first_year": r["first_year"],
                         "appearances": r["appearances"],
                         "publisher": r["publisher"], "deck": r["deck"]}
                        for r in conn.execute(
                            "SELECT comicvine_id, name, real_name, gender, "
                            "character_type, first_year, appearances, "
                            "publisher, deck FROM character")]
            have = {r[1] for r in conn.execute("PRAGMA table_info(actor)")}
            extras = [c for c in self._PERSON_EXTRA_COLS if c in have]
            cols = ("imdb_id, first_name, last_name, birth_date, "
                    "death_date, sex" + "".join(f", {c}" for c in extras))
            return [dict(r) for r in conn.execute(
                f"SELECT {cols} FROM actor")]
        finally:
            conn.close()

    def _actor_headshot(self, imdb_id):
        conn = sqlite3.connect(f"file:{self.actordb_path}?mode=ro", uri=True, timeout=10)
        try:
            if getattr(self, "actordb_kind", "person") == "character":
                r = conn.execute(
                    "SELECT portrait FROM character WHERE comicvine_id=?",
                    (imdb_id,)).fetchone()
            else:
                r = conn.execute(
                    "SELECT headshot FROM actor WHERE imdb_id=?",
                    (imdb_id,)).fetchone()
            return r[0] if r and r[0] else None
        finally:
            conn.close()

    def _actor_photo_blobs(self, imdb_id):
        """ALL pictures of one entry as a list of image bytes: the primary
        headshot/portrait first, then every extra photo. Extra photos live
        in actor_photo(imdb_id, seq, photo) / character_photo(comicvine_id,
        seq, photo) — the shapes the DB-builder enrichment tools write —
        with the older photo(imdb_id, image) shape still read as a
        fallback."""
        blobs = []
        character = getattr(self, "actordb_kind", "person") == "character"
        try:
            conn = sqlite3.connect(f"file:{self.actordb_path}?mode=ro",
                                   uri=True, timeout=10)
            try:
                primary = self._actor_headshot(imdb_id)
                if primary:
                    blobs.append(primary)
                queries = (
                    [("SELECT photo FROM character_photo WHERE "
                      "comicvine_id=? ORDER BY seq", (imdb_id,))]
                    if character else
                    [("SELECT photo FROM actor_photo WHERE imdb_id=? "
                      "ORDER BY seq", (imdb_id,)),
                     ("SELECT image FROM photo WHERE imdb_id=? "
                      "ORDER BY rowid", (imdb_id,))])
                for sql, args in queries:
                    try:
                        extra = [r[0] for r in conn.execute(sql, args)
                                 if r[0]]
                    except sqlite3.Error:
                        extra = []
                    if extra:
                        blobs.extend(extra)
                        break
            finally:
                conn.close()
        except Exception:
            pass
        return blobs

    def _excluded_set(self):
        """The excluded photo indices for the current person (a live set)."""
        if not self.actor_sel:
            return set()
        return self._actor_excluded.setdefault(self.actor_sel["imdb_id"], set())

    def _actor_step_photo(self, delta):
        """Cycle the shown photo (wrapping) across ALL of the person's
        photos, so an excluded one can be seen and re-included."""
        if not self.actor_sel:
            return
        blobs = self._actor_photo_blobs(self.actor_sel["imdb_id"])
        if len(blobs) < 2:
            return
        self.actor_photo_i = (getattr(self, "actor_photo_i", 0) + delta) % len(blobs)
        self._refresh_actor_view()
        self._schedule_persist()

    def _actor_toggle_photo(self):
        """Drop the shown photo from the selection sent to the model, or
        add it back. Never deletes from the database."""
        if not self.actor_sel:
            return
        blobs = self._actor_photo_blobs(self.actor_sel["imdb_id"])
        if not blobs:
            return
        i = getattr(self, "actor_photo_i", 0) % len(blobs)
        ex = self._excluded_set()
        if i in ex:
            ex.discard(i)
        elif len(ex) < len(blobs) - 1:   # never drop the last remaining one
            ex.add(i)
        else:
            self.status_var.set("At least one photo must stay selected.")
            return
        self._refresh_actor_view()
        self._refresh_face_list()
        self._refresh_editor_state()
        self._schedule_persist()

    def _set_actor(self, row, photo_i=0):
        """row = dict from _actordb_rows, or None to clear the choice.
        photo_i = which of the person's pictures the browser was showing —
        that one leads everywhere it is used (see _actor_ref_paths_all)."""
        self.actor_sel = row
        self.actor_photo_i = max(0, int(photo_i or 0)) if row else 0
        if not row:
            self._actor_base_label = ""
            self.actor_var.set("no person selected")
            self.actor_thumb_lab.configure(image="")
            self._actor_thumb = None
            self._refresh_editor_state()
            self._schedule_persist()
            return
        age = self._actor_age(row.get("birth_date"), row.get("death_date"))
        bits = [b for b in (row.get("character_type"), row.get("sex"),
                            str(age) if age is not None else None) if b]
        self._actor_base_label = (
            f"{row.get('first_name', '')} {row.get('last_name', '')}".strip()
            + (f"  ({', '.join(bits)})" if bits else ""))
        self._refresh_actor_view()
        self._refresh_editor_state()
        self._schedule_persist()

    def _set_actor_photo(self, i):
        """The browser's ◀ ▶ arrows choose the picture: remember it and
        show it, so the chosen photo is the one handed to the editor,
        the swap and the IP-Adapter guidance."""
        if not self.actor_sel:
            return
        self.actor_photo_i = max(0, int(i))
        self._refresh_actor_view()
        self._schedule_persist()

    def _refresh_actor_view(self):
        """Repaint the person label + 48px thumb for the chosen photo."""
        self._actor_thumb = None
        self.actor_thumb_lab.configure(image="")
        if not self.actor_sel:
            return
        blobs = self._actor_photo_blobs(self.actor_sel["imdb_id"])
        note = ""
        shot = None
        if blobs:
            i = getattr(self, "actor_photo_i", 0) % len(blobs)
            shot = blobs[i]
            ex = self._excluded_set()
            if len(blobs) > 1:
                kept = len(blobs) - len(ex)
                note = f"  · photo {i + 1}/{len(blobs)}"
                if i in ex:
                    note += " (dropped)"
                note += f"  · {kept} sent"
        self.actor_var.set(getattr(self, "_actor_base_label", "") + note)
        if hasattr(self, "photo_nav"):
            if blobs and len(blobs) > 1:
                self.photo_nav.grid()
                dropped = (getattr(self, "actor_photo_i", 0) % len(blobs)) \
                    in self._excluded_set()
                self.photo_excl_btn.configure(
                    text="\u21ba Add back" if dropped else "\u2715 Drop")
            else:
                self.photo_nav.grid_remove()
        if shot:
            try:
                img = Image.open(BytesIO(shot))
                img.thumbnail((48, 48), Image.LANCZOS)
                self._actor_thumb = ImageTk.PhotoImage(img)
                self.actor_thumb_lab.configure(image=self._actor_thumb)
            except Exception:
                pass

    def _pick_actor(self):
        """The Reference DB browser — shown IN the art panel (the preview
        canvas hides while it is open and comes back on Close). Sortable
        green-on-black table, search, and a photo pane with ◀ ▶ arrows to
        flip through every picture the DB holds for the person."""
        if not self.actordb_path or not Path(self.actordb_path).exists():
            messagebox.showinfo("Reference DB",
                                "Load a reference database first "
                                "(📇 Reference DB…).")
            return
        try:
            rows = self._actordb_rows()
        except Exception as e:
            messagebox.showerror("Reference DB",
                                 f"Could not read the database: {e}")
            return
        by_id = {r["imdb_id"]: r for r in rows}

        self._close_db_browser(restore=False)   # never two at once
        self._stop_gif()
        dlg = self.dbview = ttk.Frame(self.canvas.master)
        self.canvas.grid_remove()
        dlg.grid(row=1, column=0, sticky=NSEW)
        srow = ttk.Frame(dlg, padding=(8, 8, 8, 0))
        srow.pack(fill="x")
        ttk.Label(srow, text="📇 REFERENCE DATABASE",
                  style="Head.TLabel").pack(side="left")
        ttk.Label(srow, text="   🔍").pack(side="left")
        search_var = StringVar()
        ttk.Entry(srow, textvariable=search_var).pack(
            side="left", fill="x", expand=True, padx=6)
        close_btn = ttk.Button(srow, text="✖ Close", width=9,
                               command=self._close_db_browser)
        close_btn.pack(side="right")
        self._tip(close_btn, "Close the browser — the art panel comes "
                             "back exactly as it was.")
        frame = ttk.Frame(dlg, padding=8)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        character = getattr(self, "actordb_kind", "person") == "character"
        if character:
            # (id, heading, width, anchor, getter, numeric-sort)
            spec = [
                ("name", "Name", 150, "w",
                 lambda r: r.get("first_name") or "", False),
                ("real", "Real name", 140, "w",
                 lambda r: r.get("real_name") or "", False),
                ("type", "Type", 70, "center",
                 lambda r: r.get("character_type") or "", False),
                ("year", "Year", 55, "center",
                 lambda r: r.get("first_year") or "", True),
                ("apps", "Apps", 60, "center",
                 lambda r: r.get("appearances") or "", True),
                ("pub", "Publisher", 90, "center",
                 lambda r: r.get("publisher") or "", False),
            ]
            state = {"col": "apps", "desc": True}
        else:
            def _age_of(r):
                a = self._actor_age(r.get("birth_date"), r.get("death_date"))
                return a if a is not None else ""
            spec = [
                ("first", "First name", 110, "w",
                 lambda r: r.get("first_name") or "", False),
                ("last", "Last name", 140, "w",
                 lambda r: r.get("last_name") or "", False),
                ("age", "Age", 45, "center", _age_of, True),
                ("sex", "Sex", 60, "center",
                 lambda r: r.get("sex") or "", False),
                ("born", "Born", 85, "center",
                 lambda r: r.get("birth_date") or "", False),
                ("died", "Died", 85, "center",
                 lambda r: r.get("death_date") or "", False),
            ]
            state = {"col": "last", "desc": False}
        cols = tuple(s[0] for s in spec)
        heads = {s[0]: s[1] for s in spec}
        getters = {s[0]: s[4] for s in spec}
        numeric = {s[0] for s in spec if s[5]}
        anchors = {s[0]: s[3] for s in spec}
        # column widths in monospace characters (spec widths are px).
        # `ratio` is each column's share of the table — the grid always
        # fills the panel, and a manual drag just changes the shares.
        widths = {s[0]: max(5, s[2] // 8) for s in spec}
        MIN_COL = 4
        _tot0 = sum(widths.values()) or 1
        ratio = {c: widths[c] / _tot0 for c in widths}

        # a Text widget draws the table: ttk's Treeview cannot render cell
        # gridlines, a monospace grid of box-drawing characters can. The
        # widget is VIRTUALIZED — only the visible window of rows is ever
        # rendered, while the scrollbar spans the whole filtered list, so
        # a 47k-row database scrolls end to end with no cap.
        table = Text(frame, bg="#000000", fg="#ffffff", bd=0,
                     insertwidth=0, highlightthickness=0, wrap="none",
                     font=("Consolas", 10), cursor="arrow",
                     exportselection=False, takefocus=0)
        table.grid(row=0, column=0, sticky=NSEW)
        table.tag_configure("head", font=("Consolas", 10, "bold"))
        table.tag_configure("selrow", background="#0a4d1f")
        view = {"rows": [], "off": 0, "shown": 0, "sel": None}
        import tkinter.font as _tkfont
        # font metrics are only an estimate — DPI scaling makes the real
        # rendered line taller; render() measures the truth via dlineinfo
        # and re-renders once, so the bottom row is never cut
        lp = {"px": max(1, _tkfont.Font(
            root=self.root, font=("Consolas", 10)).metrics("linespace")),
            "fixing": False}

        def rows_per_view():
            # data rows that fit FULLY (3 header lines, 2 lines per row);
            # "adj" is the overflow correction measured after rendering
            return max(3, (table.winfo_height() // lp["px"] - 3) // 2
                       - view.get("adj", 0))

        def on_scroll(*args):
            n = len(view["rows"])
            rpv = rows_per_view()
            if args and args[0] == "moveto":
                view["off"] = int(float(args[1]) * max(0, n - rpv))
            elif args and args[0] == "scroll":
                step = int(args[1]) * (rpv if args[2] == "pages" else 3)
                view["off"] += step
            view["off"] = max(0, min(view["off"], max(0, n - rpv)))
            render()

        sb = ttk.Scrollbar(frame, orient="vertical", command=on_scroll)
        sb.grid(row=0, column=1, sticky="ns")
        _char_px = max(1, _tkfont.Font(
            root=self.root, font=("Consolas", 10)).measure("0"))

        def col_borders():
            """Character offsets of the vertical rules, left to right."""
            b = [0, view.get("nw", 2) + 3]
            for c in cols:
                b.append(b[-1] + widths[c] + 3)
            return b

        def fit_widths():
            """Size the columns to fill the table's current width, keeping
            each column's share — so the grid spans the panel and follows
            the window as it is resized."""
            avail = table.winfo_width() // _char_px
            room = avail - view.get("nw", 2) - 4 - 3 * len(cols)
            if room < MIN_COL * len(cols):
                return
            total = sum(ratio.values()) or 1
            new = {c: max(MIN_COL, int(room * ratio[c] / total))
                   for c in cols}
            slack = room - sum(new.values())
            if slack:                     # rounding remainder → widest col
                widest = max(cols, key=lambda c: new[c])
                new[widest] = max(MIN_COL, new[widest] + slack)
            widths.update(new)

        def resize_col(c, delta):
            """Drag of a column border: take/give characters and rebalance
            the others so the grid still fills the width."""
            want = max(MIN_COL, widths[c] + delta)
            if want == widths[c]:
                return
            widths[c] = want
            tot = sum(widths.values()) or 1
            for k in cols:                # remember the new proportions
                ratio[k] = widths[k] / tot
            render()

        def fmt(c, v):
            w = widths[c]
            v = str(v)
            if len(v) > w:
                v = v[:max(1, w - 1)] + "…"
            return v.center(w) if anchors[c] == "center" else v.ljust(w)

        def rule(l, m, r, fill):
            return (l + fill * (view.get("nw", 2) + 2) + m
                    + m.join(fill * (widths[c] + 2) for c in cols) + r)

        def render():
            n = len(view["rows"])
            view["nw"] = max(2, len(str(n)))    # row-number column width
            fit_widths()                        # always span the panel
            rpv = rows_per_view()
            view["off"] = max(0, min(view["off"], max(0, n - rpv)))
            table.configure(state="normal")
            table.delete("1.0", END)
            mark = " ▼" if state["desc"] else " ▲"
            head = ("│ " + "#".rjust(view["nw"]) + " │ " + " │ ".join(
                fmt(c, heads[c] + (mark if c == state["col"] else ""))
                for c in cols) + " │")
            lines = [rule("┌", "┬", "┐", "─"), head,
                     rule("╞", "╪", "╡", "═")]
            shown = view["rows"][view["off"]:view["off"] + rpv]
            view["shown"] = len(shown)
            between = rule("├", "┼", "┤", "─")
            for i, r_ in enumerate(shown):
                lines.append(
                    "│ " + str(view["off"] + i + 1).rjust(view["nw"])
                    + " │ " + " │ ".join(
                        fmt(c, getters[c](r_)) for c in cols) + " │")
                lines.append(between)
            # always close the block with the bottom border — a dangling
            # "├─┼─┤" rule reads as a cut-off row; the scrollbar already
            # says the list continues
            closer = rule("└", "┴", "┘", "─")
            if shown:
                lines[-1] = closer
            else:
                lines.append(closer)
            table.insert("1.0", "\n".join(lines))
            table.tag_add("head", "2.0", "2.end")
            paint_sel()
            table.configure(state="disabled")
            table.yview_moveto(0)
            if n:
                sb.set(view["off"] / n, (view["off"] + len(shown)) / n)
            else:
                sb.set(0, 1)
            # trust the widget, not font math: once layout settles, check
            # whether the rendered block pokes past the viewport; if so,
            # drop whole rows and redraw — the bottom row is never left
            # cut in half. Deferred via after_idle because yview() only
            # reflects geometry after Tk has recomputed display lines.
            def _check_overflow():
                lp["fixing"] = False
                last_frac = table.yview()[1]
                if last_frac < 0.999 and view["shown"] > 1:
                    total = 3 + 2 * view["shown"]
                    hidden = int(total * (1.0 - last_frac) + 0.999)
                    view["adj"] = view.get("adj", 0) \
                        + max(1, (hidden + 1) // 2)
                    render()
            if not lp["fixing"]:
                lp["fixing"] = True
                table.after_idle(_check_overflow)

        def paint_sel():
            table.tag_remove("selrow", "1.0", END)
            if view["sel"] is None:
                return
            for i, r_ in enumerate(
                    view["rows"][view["off"]:view["off"] + view["shown"]]):
                if r_["imdb_id"] == view["sel"]:
                    ln = 4 + 2 * i
                    table.tag_add("selrow", f"{ln}.0", f"{ln}.end")
                    break

        def repopulate():
            needle = search_var.get().strip().lower()
            vr = []
            for r_ in rows:
                hay = (f"{getters[cols[0]](r_)} {getters[cols[1]](r_)}"
                       ).lower()
                if not needle or needle in hay:
                    vr.append(r_)
            col = state["col"]
            g = getters[col]

            def val(r_):
                v = g(r_)
                if col in numeric:
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        return 0
                return str(v).lower()
            vr.sort(key=val, reverse=state["desc"])
            vr.sort(key=lambda r_: str(g(r_)) == "")
            view["rows"] = vr
            view["off"] = 0
            render()

        def sort_by(col):
            if state["col"] == col:
                state["desc"] = not state["desc"]
            else:
                state["col"], state["desc"] = col, False
            repopulate()

        def select_iid(iid):
            view["sel"] = iid
            table.configure(state="normal")
            paint_sel()
            table.configure(state="disabled")
            r_ = by_id[iid]
            detail.configure(text=detail_text(r_))
            pv["blobs"] = self._actor_photo_blobs(iid)
            # reopen on the picture already in use for this person
            pv["i"] = (self.actor_photo_i
                       if (self.actor_sel
                           and self.actor_sel.get("imdb_id") == iid
                           and self.actor_photo_i < len(pv["blobs"]))
                       else 0)
            show_photo()

        def on_click(ev):
            pos = table.index(f"@{ev.x},{ev.y}")
            ln, cx = int(pos.split(".")[0]), int(pos.split(".")[1])
            if ln == 2:                       # header → sort that column
                x = 5 + view.get("nw", 2)     # skip the row-number column
                if cx < x:
                    return
                for c in cols:
                    if cx < x + widths[c] + 1:
                        sort_by(c)
                        return
                    x += widths[c] + 3
                return
            idx = ln - 4
            if idx >= 0 and idx % 2 == 0 and idx // 2 < view["shown"]:
                select_iid(
                    view["rows"][view["off"] + idx // 2]["imdb_id"])

        # drag a column border (in the header band) to resize that column;
        # a plain click still sorts / selects, decided on release
        drag = {"col": None, "x0": 0, "w0": 0}

        def near_border(cx):
            """The column whose RIGHT border sits at (or beside) char cx."""
            b = col_borders()
            for i, c in enumerate(cols):
                if abs(cx - b[i + 2]) <= 1:
                    return c
            return None

        def on_press(ev):
            pos = table.index(f"@{ev.x},{ev.y}")
            ln, cx = int(pos.split(".")[0]), int(pos.split(".")[1])
            if ln <= 3:
                c = near_border(cx)
                if c:
                    drag.update(col=c, x0=ev.x, w0=widths[c])
                    return "break"
            drag["col"] = None

        def on_drag(ev):
            if not drag["col"]:
                return
            want = max(MIN_COL,
                       drag["w0"] + (ev.x - drag["x0"]) // _char_px)
            resize_col(drag["col"], want - widths[drag["col"]])
            return "break"

        def on_release(ev):
            if drag["col"]:
                drag["col"] = None
                return "break"
            on_click(ev)

        def on_motion(ev):
            try:
                pos = table.index(f"@{ev.x},{ev.y}")
                ln, cx = int(pos.split(".")[0]), int(pos.split(".")[1])
            except Exception:
                return
            table.configure(
                cursor="sb_h_double_arrow"
                if ln <= 3 and near_border(cx) else "arrow")

        table.bind("<Button-1>", on_press)
        table.bind("<B1-Motion>", on_drag)
        table.bind("<ButtonRelease-1>", on_release)
        table.bind("<Motion>", on_motion)
        table.bind("<MouseWheel>",
                   lambda e: (on_scroll("scroll",
                                        -1 if e.delta > 0 else 1,
                                        "units"), "break")[1])
        table.bind("<Configure>",
                   lambda e: (view.update(adj=0), render()))
        search_var.trace_add("write", lambda *_a: repopulate())
        repopulate()

        side = ttk.Frame(frame, padding=(10, 0, 0, 0))
        side.grid(row=0, column=2, sticky="ns")
        # the picture lives in a FIXED box (propagation off) so photos of
        # different sizes never move the ◀ ▶ arrows or the details below
        PHOTO_BOX = (200, 240)
        pbox = ttk.Frame(side, width=PHOTO_BOX[0], height=PHOTO_BOX[1])
        pbox.pack(pady=4)
        pbox.pack_propagate(False)
        preview = ttk.Label(pbox, text="(select a person)", anchor="center",
                            justify="center",
                            wraplength=PHOTO_BOX[0] - 8)
        preview.pack(fill="both", expand=True)
        # ◀ n/m ▶ under the picture — flips through every photo the DB
        # holds for the person (multi-photo databases)
        arow = ttk.Frame(side)
        arow.pack(pady=(0, 4))
        pv = {"blobs": [], "i": 0, "img": None}

        def show_photo():
            n = len(pv["blobs"])
            counter.configure(text=f"{pv['i'] + 1} / {n}" if n else "—")
            state = ["!disabled"] if n > 1 else ["disabled"]
            prev_btn.state(state)
            next_btn.state(state)
            if not n:
                pv["img"] = None
                preview.configure(image="", text="(no photo)")
                return
            try:
                img = Image.open(BytesIO(pv["blobs"][pv["i"]]))
                img.thumbnail((190, 230), Image.LANCZOS)
                pv["img"] = ImageTk.PhotoImage(img)
                preview.configure(image=pv["img"], text="")
            except Exception:
                pv["img"] = None
                preview.configure(image="", text="(bad photo)")

        def flip(step):
            if len(pv["blobs"]) > 1:
                pv["i"] = (pv["i"] + step) % len(pv["blobs"])
                show_photo()
                # the arrows CHOOSE the picture: if this person is already
                # the one in use, the choice takes effect immediately
                if self.actor_sel \
                        and view["sel"] == self.actor_sel.get("imdb_id"):
                    self._set_actor_photo(pv["i"])

        prev_btn = ttk.Button(arow, text="◀", width=3,
                              command=lambda: flip(-1))
        prev_btn.pack(side="left")
        counter = ttk.Label(arow, text="—", width=7, anchor="center")
        counter.pack(side="left", padx=4)
        next_btn = ttk.Button(arow, text="▶", width=3,
                              command=lambda: flip(1))
        next_btn.pack(side="left")
        self._tip(prev_btn, "Previous photo of this person.")
        self._tip(next_btn, "Next photo of this person.")
        detail = ttk.Label(side, text="", justify="left", wraplength=200)
        detail.pack(anchor="w")

        def detail_text(r_):
            lines = [f"{r_.get('first_name') or ''} "
                     f"{r_.get('last_name') or ''}".strip()]
            if character:
                if r_.get("real_name"):
                    lines.append(r_["real_name"])
                facts = [str(b) for b in (r_.get("character_type"),
                                          r_.get("publisher"),
                                          r_.get("first_year")) if b]
                if facts:
                    lines.append(", ".join(facts))
                if r_.get("appearances"):
                    lines.append(f"{r_['appearances']} appearances")
                if r_.get("deck"):
                    lines.append(str(r_["deck"])[:180])
            else:
                age = self._actor_age(r_.get("birth_date"),
                                      r_.get("death_date"))
                lines.append((r_.get("sex") or "unknown")
                             + (f", {age}" if age is not None else ""))
                # singer builds carry music columns — show what exists
                if r_.get("genres"):
                    lines.append(str(r_["genres"])[:90])
                facts = [str(b) for b in (r_.get("voice_type"),
                                          r_.get("years_active")) if b]
                if facts:
                    lines.append(", ".join(facts))
                if r_.get("description"):
                    lines.append(str(r_["description"])[:160])
            return "\n".join(lines)

        def choose(_e=None):
            if view["sel"] is not None:
                # commit the person WITH the picture on screen
                self._set_actor(by_id[view["sel"]], photo_i=pv["i"])
                self._close_db_browser()
        table.bind("<Double-1>", choose)
        self._dbview_state = pv          # for tests / future hooks
        self._dbview_api = {             # widget-independent test surface
            "select": select_iid, "sort": sort_by,
            "rows": lambda: view["rows"],
            "selected": lambda: view["sel"], "table": table,
            "choose": choose, "scroll": on_scroll,
            "offset": lambda: view["off"],
            "widths": lambda: dict(widths), "resize": resize_col,
            "photo_box": pbox, "borders": col_borders}

        btns = ttk.Frame(dlg, padding=(8, 0, 8, 8))
        btns.pack(fill="x")
        ttk.Button(btns, text="✔ Use this person",
                   command=choose).pack(side="right")
        ttk.Button(btns, text="No person",
                   command=lambda: (self._set_actor(None),
                                    self._close_db_browser())).pack(
            side="right", padx=6)

    def _close_db_browser(self, restore=True):
        """Tear the in-panel DB browser down and bring the art back."""
        dv = getattr(self, "dbview", None)
        if dv is not None:
            try:
                dv.destroy()
            except Exception:
                pass
            self.dbview = None
        self._dbview_state = None
        self._dbview_api = None
        if restore:
            self.canvas.grid()
            self._show_current()

    def _actor_ref_path(self):
        """The selected actor's first photo as a real file the generation
        worker can upload — or None if it can't be produced."""
        paths = self._actor_ref_paths_all()
        return paths[0] if paths else None

    def _actor_ref_paths_all(self):
        """ALL of the selected actor's photos as real files (multi-photo
        DBs give several; classic DBs give one). The picture chosen with
        the browser's ◀ ▶ arrows comes FIRST, so every caller that takes
        one photo takes that one, and the rest follow in order."""
        if not (self.actor_sel and self.actordb_path
                and Path(self.actordb_path).exists()):
            return []
        try:
            blobs = self._actor_photo_blobs(self.actor_sel["imdb_id"])
            if not blobs:
                return []
            ex = self._actor_excluded.get(self.actor_sel["imdb_id"], set())
            sel = getattr(self, "actor_photo_i", 0) % len(blobs)
            order = [i for i in (list(range(sel, len(blobs)))
                                 + list(range(0, sel))) if i not in ex]
            if not order:                      # all dropped -> use them all
                order = list(range(len(blobs)))
            out = []
            for i in order:
                p = Path(tempfile.gettempdir()) / \
                    f"cbac_actor_{self.actor_sel['imdb_id']}_{i}.jpg"
                p.write_bytes(blobs[i])
                out.append(str(p))
            return out
        except Exception:
            return []

    def _probe_ollama(self):
        """Look for a local Ollama once at startup, off the UI thread."""
        self.ui_queue.put(("ollama", ollama_models()))

    def _on_ollama_found(self, models):
        self._ollama_models = models
        vals = [BUILTIN_ENHANCER] + list(models)
        self.ollama_dd["values"] = vals
        want = getattr(self, "_want_ollama_model", "") or ""
        if want and want in vals:
            self.ollama_var.set(want)
        elif models and not self.ollama_var.get():
            self.ollama_var.set(models[0])
        elif self.ollama_var.get() not in vals:
            self.ollama_var.set(BUILTIN_ENHANCER)

    def _enhance_prompt(self):
        """Rewrite the prompt into a fuller one. Uses the built-in offline
        enhancer by default; a local Ollama model when one is chosen. It
        always works — Ollama is never required or installed."""
        text = self._get(self.prompt_box).strip()
        if not text:
            self.status_var.set("Type a prompt first, then press ✨ Enhance.")
            return
        model = self.ollama_var.get() or BUILTIN_ENHANCER
        if model != BUILTIN_ENHANCER \
                and not getattr(self, "_ollama_models", None):
            # a remembered Ollama model but Ollama is not up now: re-probe,
            # and fall back to the built-in enhancer if it still is not there
            found = ollama_models()
            if found:
                self._on_ollama_found(found)
                model = self.ollama_var.get() or BUILTIN_ENHANCER
            else:
                model = BUILTIN_ENHANCER
        self.enhance_btn.state(["disabled"])
        label = ("the built-in enhancer" if model == BUILTIN_ENHANCER
                 else model)
        self.status_var.set(f"Enhancing the prompt with {label}…")
        style = self._get(self.style_box)
        fam = model_family(self._model_raw() or "") or "sdxl"
        threading.Thread(target=self._run_enhance,
                         args=(text, model, style, fam), daemon=True).start()

    def _run_enhance(self, text, model, style, family="sdxl"):
        try:
            if model == BUILTIN_ENHANCER:
                better = builtin_enhance(text, style, family)
            else:
                better = ollama_enhance(text, model, style)
            self.ui_queue.put(("enhanced", text, better))
        except Exception as e:
            self.ui_queue.put(("enhance_err", str(e)))

    def _undo_enhance(self):
        if self._prompt_undo is None:
            return
        self._set(self.prompt_box, self._prompt_undo)
        self._prompt_undo = None
        self.undo_enhance_btn.state(["disabled"])
        self.status_var.set("Prompt restored.")
        self._schedule_persist()

    def _clear_ragmap(self, note=""):
        """Take the RAG map out of use and forget it, so it does not come
        back on the next launch. The map file itself is left alone."""
        had = self.ragmap_path
        self.ragmap = None
        self.ragmap_path = None
        self.ragmap_var.set("none")
        if note:
            self.status_var.set(note)
        elif had:
            self.status_var.set(f"RAG map removed — {Path(had).name} is no "
                                "longer guiding generations.")
        self._refresh_mode_badges()
        self._schedule_persist()

    def _ragmap_label(self, rag, path):
        """Name + how many references are actually usable right now."""
        entries = rag.get("entries", [])
        live = sum(1 for e in entries
                   if e.get("_path") or e.get("_ipadpt"))
        missing = len(entries) - live
        kind = "private refs" if rag.get("_embeds_only") else "imgs"
        txt = f"{rag.get('name', Path(path).stem)} — {live} {kind}"
        if missing:
            txt += f" ({missing} missing)"
        lora = ragmap_lora(rag)
        return txt + (f" · LoRA {lora}" if lora else "")

    def _validate_ragmap(self):
        """Drop a RAG map that is no longer where it was: the file may have
        been moved or deleted since it was loaded. A map whose file changed
        (mtime/size) is parsed again, off the UI thread; an unchanged one
        is kept as parsed — this used to re-parse the whole map on the UI
        thread at every model refresh, which for a large map meant the
        app froze for as long as the parse took."""
        if not self.ragmap_path:
            return
        if not Path(self.ragmap_path).exists():
            name = Path(self.ragmap_path).name
            self._clear_ragmap(f"RAG map removed — {name} is no longer at "
                               "its saved location.")
            return
        if self.ragmap is None:
            return          # still parsing in the background
        try:
            st = Path(self.ragmap_path).stat()
            sig = (st.st_mtime, st.st_size)
        except OSError:
            sig = None
        if sig == self.ragmap.get("_sig"):
            return          # unchanged since it was parsed: nothing to redo
        # the file changed underneath us: parse it again, off the UI thread
        self._load_ragmap_async(self.ragmap_path, self._ragmap_restored)

    def _import_checkpoint(self):
        """Browse for a base model (.safetensors) anywhere on disk and make
        it available here — for a model you trained or downloaded yourself.

        On the same drive it is hard-linked in, which is instant and costs
        no extra space; across drives the engine cannot reach it where it
        lies, so it is copied (with the size shown first, since base models
        run to several GB)."""
        path = filedialog.askopenfilename(
            title="Add a base model",
            filetypes=[("Model (safetensors)", "*.safetensors"),
                       ("All files", "*.*")])
        if not path:
            return
        src = Path(path)
        if src.suffix.lower() != ".safetensors":
            # .ckpt/.pt are pickles: loading one can execute code
            messagebox.showerror(
                "Add a base model",
                f"{src.name} isn't a .safetensors file.\n\nOnly safetensors "
                "models are accepted — .ckpt and .pt can run code when they "
                "are loaded.")
            return
        dest_dir = MODELS / "checkpoints"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        if dest.exists():
            if dest.resolve() == src.resolve():
                self._refresh_models()
                self._select_model(src.name)
                self.status_var.set(f"{src.name} is already in your model "
                                    "folder — selected.")
                return
            if not messagebox.askyesno(
                    "Replace model",
                    f"{src.name} is already in your model folder.\n\n"
                    "Replace it?"):
                return
            try:
                dest.unlink()
            except OSError as e:
                messagebox.showerror("Add a base model",
                                     f"Could not replace it: {e}")
                return
        try:
            os.link(src, dest)       # same volume: instant, no extra space
        except OSError:
            gb = src.stat().st_size / (1024 ** 3)
            if not messagebox.askyesno(
                    "Copy model",
                    f"{src.name} is on a different drive, so it has to be "
                    f"copied into your model folder.\n\nThat's {gb:.1f} GB. "
                    "Copy it now?"):
                return
            threading.Thread(target=self._copy_checkpoint,
                             args=(src, dest), daemon=True).start()
            return
        self._refresh_models()
        self._select_model(src.name)
        self.status_var.set(f"Added {src.name} — selected and ready.")

    def _copy_checkpoint(self, src, dest):
        """Copy a multi-GB model off the UI thread, reporting progress."""
        tmp = dest.with_suffix(".part")
        try:
            total = src.stat().st_size
            done = 0
            with open(src, "rb") as fin, open(tmp, "wb") as fout:
                while True:
                    chunk = fin.read(8 << 20)
                    if not chunk:
                        break
                    fout.write(chunk)
                    done += len(chunk)
                    self.ui_queue.put(("progress", done, total))
            tmp.replace(dest)
            self.ui_queue.put(("progress", 0, 1))
            self.ui_queue.put(("model_added", dest.name))
        except Exception as e:
            tmp.unlink(missing_ok=True)
            self.ui_queue.put(("progress", 0, 1))
            self.ui_queue.put(("error", f"Could not copy the model: {e}"))

    def _select_model(self, raw):
        """Point the dropdown at a checkpoint by its filename."""
        for disp, name in self._model_display.items():
            if name == raw:
                self.model_var.set(disp)
                self._on_model_pick()
                return True
        return False

    def _remove_lora(self):
        """Take the ticked LoRA(s) out of the list by deleting the files
        from the LoRA folder — the list is a view of that folder, so there
        is nothing else to remove. Always asks first, and says plainly
        that a file is being deleted."""
        names = self._selected_loras()
        if not names:
            self.status_var.set("Tick the LoRA(s) you want to remove first.")
            return
        warn = ""
        if BORDER_LORA_FILE in names:
            warn = (f"\n\n{BORDER_LORA_FILE} is the frame LoRA the Border "
                    "maker uses. Removing it makes borders plainer until "
                    "the next update check downloads it again.")
        if not messagebox.askyesno(
                "Remove LoRA",
                "Delete these from your LoRA folder?\n\n  "
                + "\n  ".join(names)
                + "\n\nThe file(s) will be gone from disk. You can add them "
                  "back later with ➕ Add LoRA file…" + warn):
            return
        gone, failed = [], []
        lora_dir = _ensure_lora_dir()
        for name in names:
            p = lora_dir / name
            try:
                if p.exists():
                    p.unlink()
                    gone.append(name)
                else:
                    gone.append(name)      # already absent: nothing to do
            except OSError as e:
                # the engine may still hold a LoRA it loaded this session
                failed.append(f"{name} ({e.strerror or e})")
        self._pending_loras = {n for n in getattr(self, "_pending_loras",
                                                  set()) if n not in gone}
        self._refresh_models()
        parts = []
        if gone:
            parts.append(f"Removed {len(gone)}: " + ", ".join(gone))
        if failed:
            parts.append("Could not remove: " + "; ".join(failed)
                         + " — it may be in use by the engine; try again "
                           "after the next generation finishes.")
        self.status_var.set("  ·  ".join(parts))
        self._schedule_persist()

    def _import_lora(self):
        """Browse for .safetensors LoRA file(s), copy them into
        models\\loras, then refresh the list and tick the new ones."""
        paths = filedialog.askopenfilenames(
            title="Add LoRA file(s)",
            filetypes=[("LoRA (safetensors)", "*.safetensors"),
                       ("All files", "*.*")])
        if not paths:
            return
        dest_dir = _ensure_lora_dir()
        added, skipped = [], []
        for src in paths:
            src = Path(src)
            if src.suffix.lower() != ".safetensors":
                # .ckpt/.pt are pickle-based and can execute code on load
                skipped.append(f"{src.name} (only .safetensors is allowed)")
                continue
            dest = dest_dir / src.name
            if dest.exists() and dest.resolve() == src.resolve():
                added.append(src.name)   # already in the folder
                continue
            if dest.exists():
                if not messagebox.askyesno(
                        "Replace LoRA",
                        f"{src.name} is already in your LoRA folder. "
                        "Replace it?"):
                    skipped.append(f"{src.name} (kept existing)")
                    continue
            try:
                shutil.copy2(src, dest)
                added.append(src.name)
                # bring any trained-words sidecar along so the activation
                # keyword is injected automatically when the LoRA is ticked
                for ext in (".civitai.info", ".json"):
                    side = src.with_suffix(ext)
                    if side.exists():
                        try:
                            shutil.copy2(side, dest.with_suffix(ext))
                        except OSError:
                            pass
                _LORA_TRIGGER_CACHE.pop(src.name, None)   # re-read fresh
            except OSError as e:
                skipped.append(f"{src.name} ({e})")
        # make the freshly added ones tick on the next list rebuild
        self._pending_loras = getattr(self, "_pending_loras", set()) | set(added)
        self._refresh_models()
        parts = []
        if added:
            parts.append(f"Added {len(added)} LoRA(s): "
                         + ", ".join(added))
        if skipped:
            parts.append("Skipped: " + "; ".join(skipped))
        self.status_var.set("  ·  ".join(parts) or "No LoRAs added.")

    def _collect_ui_state(self):
        return {
            "prompt": self._get(self.prompt_box),
            "negative": self._get(self.negative_box),
            "style": self._get(self.style_box),
            "preset": self.preset_var.get(),
            "model": self._model_raw(),
            "loras": self._selected_loras(),
            "lora_strength": round(self.lora_strength.get(), 2),
            "ref_images": self.ref_paths,
            "editor": self._editor_engine(),
            "editor_canvas": self.editor_canvas_var.get(),
            "border_refs": self.border_ref_paths,
            "change": int(self.change_var.get()),
            "anim_image": self.anim_image_path or "",
            "anim_action": self._get(self.anim_prompt_box),
            "anim_secs": self.anim_secs_var.get(),
            "anim_keep": self.anim_keep_var.get(),
            "anim_loop": self.anim_loop_var.get(),
            "anim_size": self.anim_size_var.get(),
            "anim_transparent": self.anim_transparent_var.get(),
            "anim_motion": self.anim_motion_var.get(),
            "anim_gif": self.anim_gif_var.get(),
            "anim_zip": self.anim_zip_var.get(),
            "anim_sheet": self.anim_sheet_var.get(),
            "anim_video": self.anim_video_var.get(),
            "border_theme": self._get(self.border_prompt_box),
            "border_auto": self.border_auto_var.get(),
            "border_model": self.border_model_var.get(),
            "border_style": self.border_style_var.get(),
            "border_aspect": self.border_aspect_var.get(),
            "border_thick": int(self.border_thick_var.get()),
            "border_clean": self.border_clean_var.get(),
            "border_batch": self.border_batch_var.get(),
            "size": self.size_var.get(),
            "steps": self.steps_var.get(),
            "batch": self.batch_var.get(),
            "transparent": self.transparent_var.get(),
            "upscale": self.upscale_var.get(),
            "hires": self.hires_var.get(),
            "hires_scale": self.hires_scale_var.get(),
            "freeu": self.freeu_var.get(),
            "tab": (self.left_tabs.index("current")
                    if hasattr(self, "left_tabs") else 0),
            "ragmap_path": self.ragmap_path,
            "face_source": self.face_source_var.get(),
            "face_paths": self.face_paths,
            "clone_method": self.clone_method_var.get(),
            "actordb_path": self.actordb_path,
            "actor_imdb": (self.actor_sel or {}).get("imdb_id"),
            "actor_photo": getattr(self, "actor_photo_i", 0),
            "actor_excluded": {k: sorted(v) for k, v
                               in self._actor_excluded.items() if v},
            "ollama_model": self.ollama_var.get(),
            "seed": self.seed_var.get(),
            "random_seed": self.random_seed_var.get(),
            "swap_rag": self.swap_rag_var.get(),
            "swap_use_rag": self.swap_use_rag_var.get(),
            "swap_use_lora": self.swap_use_lora_var.get(),
            "swap_fast": self.swap_fast_var.get(),
            "auto_negative": self._auto_negative,
        }

    DEFAULT_LORAS = {"SDXL_BW_Manga.safetensors",
                     "SDXL_GraphicNovel.safetensors",
                     "SDXL_LineArt_Manga.safetensors"}

    def _apply_ui_state(self, st):
        if not st:
            # first run: comic-style starter selection
            self._pending_loras = set(self.DEFAULT_LORAS)
            self._on_preset()
            return
        try:
            self._set(self.prompt_box, st.get("prompt", ""))
            self._set(self.negative_box, st.get("negative", ""))
            self._set(self.style_box, st.get("style", ""))
            self.preset_var.set(st.get("preset", NONE_PRESET))
            if st.get("model"):
                self.model_var.set(st["model"])
            saved_loras = st.get("loras", [])
            # tolerate the old [[name, strength], ...] format
            self._pending_loras = {l[0] if isinstance(l, list) else l
                                   for l in saved_loras}
            self.lora_strength.set(st.get("lora_strength", 0.8))
            refs = st.get("ref_images") or \
                ([st["ref_image"]] if st.get("ref_image") else [])
            self.ref_paths = [p for p in refs if Path(p).exists()]
            if self.ref_paths:
                first = Path(self.ref_paths[0]).name
                self.ref_var.set(first if len(self.ref_paths) == 1 else
                                 f"{len(self.ref_paths)} images ({first}, …)")
            # editor intentionally NOT restored: Flux Kontext is always
            # the default at launch (per-session changes still allowed)
            self.editor_canvas_var.set(True)   # always canvas now
            brefs = st.get("border_refs", [])
            self.border_ref_paths = [p for p in brefs if Path(p).exists()]
            if self.border_ref_paths:
                first = Path(self.border_ref_paths[0]).name
                self.border_ref_var.set(
                    first if len(self.border_ref_paths) == 1 else
                    f"{len(self.border_ref_paths)} images ({first}, …)")
            self.change_var.set(st.get("change", 60))
            ai = st.get("anim_image", "")
            if ai and Path(ai).exists():
                self.anim_image_path = ai
                self.anim_img_var.set(Path(ai).name)
            self._set(self.anim_prompt_box, st.get("anim_action", ""))
            self.anim_secs_var.set(st.get("anim_secs", 3))
            if st.get("anim_keep") in ANIM_KEEP:
                self.anim_keep_var.set(st["anim_keep"])
            # anim_loop intentionally NOT restored: auto-cut is always
            # the default at launch (per-run changes still allowed)
            if st.get("anim_size") in ANIM_SIZES:
                self.anim_size_var.set(st["anim_size"])
            self.anim_transparent_var.set(st.get("anim_transparent", True))
            if st.get("anim_motion") in ANIM_MOTION:
                self.anim_motion_var.set(st["anim_motion"])
            self.anim_gif_var.set(st.get("anim_gif", True))
            self.anim_zip_var.set(st.get("anim_zip", True))
            self.anim_sheet_var.set(st.get("anim_sheet", False))
            if st.get("anim_video") in ("none", "MP4", "WebM"):
                self.anim_video_var.set(st["anim_video"])
            self._set(self.border_prompt_box, st.get("border_theme", ""))
            self.border_auto_var.set(st.get("border_auto", True))
            if st.get("border_model"):
                self.border_model_var.set(st["border_model"])
            if st.get("border_style"):
                self.border_style_var.set(st["border_style"])
            if st.get("border_aspect") in BORDER_SIZES:
                self.border_aspect_var.set(st["border_aspect"])
            self.border_thick_var.set(st.get("border_thick", 14))
            self.border_clean_var.set(st.get("border_clean", True))
            self.border_batch_var.set(st.get("border_batch", 1))
            if st.get("size") in SIZE_PRESETS:
                self.size_var.set(st["size"])
            self.steps_var.set(st.get("steps", "auto"))
            self.batch_var.set(st.get("batch", 1))
            self.transparent_var.set(st.get("transparent", False))
            self.upscale_var.set(st.get("upscale", False))
            self.hires_var.set(st.get("hires", False))
            self.hires_scale_var.set(st.get("hires_scale", "1.5×"))
            self.freeu_var.set(st.get("freeu", False))
            self.face_source_var.set(st.get("face_source", "file"))
            self.face_paths = [p for p in (st.get("face_paths") or [])
                               if Path(p).exists()]
            self._set_face_label()
            self.swap_rag_var.set(st.get("swap_rag", True))
            self.swap_use_rag_var.set(st.get("swap_use_rag", True))
            self.swap_use_lora_var.set(st.get("swap_use_lora", True))
            self.swap_fast_var.set(st.get("swap_fast", False))
            cm = st.get("clone_method")
            if cm in [m[0] for m in CLONE_METHODS]:
                self.clone_method_var.set(cm)
            self._on_face_source()
            # the probe runs in the background, so remember the wanted
            # model and select it once the list arrives
            self._want_ollama_model = st.get("ollama_model", "") or ""
            if self._want_ollama_model:
                self.ollama_var.set(self._want_ollama_model)
            try:
                self.left_tabs.select(int(st.get("tab", 0) or 0))
            except Exception:
                pass
            rmp = st.get("ragmap_path")
            if rmp and Path(rmp).exists():
                # parsed off the UI thread — see _load_ragmap_async; the
                # path is remembered at once so persistence keeps it
                self.ragmap_path = rmp
                self._load_ragmap_async(rmp, self._ragmap_restored)
            adb = st.get("actordb_path")
            if adb and Path(adb).exists():
                if self._load_actordb(adb, quiet=True) is not None:
                    aid = st.get("actor_imdb")
                    if aid:
                        try:
                            self._actor_excluded = {
                                k: set(v) for k, v in
                                (st.get("actor_excluded") or {}).items()}
                            row = next((r for r in self._actordb_rows()
                                        if r["imdb_id"] == aid), None)
                            if row:
                                self._set_actor(
                                    row, photo_i=st.get("actor_photo", 0))
                        except Exception:
                            pass
            self.seed_var.set(st.get("seed", "0"))
            self.random_seed_var.set(st.get("random_seed", True))
            self._auto_negative = st.get("auto_negative")
        except Exception:
            pass  # a broken settings file must never block startup

    def _persist(self):
        self.settings["ui"] = self._collect_ui_state()
        self._save_settings()

    def _wire_autosave(self):
        """Persist every change as it happens (debounced), so settings
        survive any kind of exit — including a killed process."""
        for var in (self.model_var, self.preset_var, self.size_var,
                    self.steps_var, self.seed_var, self.batch_var,
                    self.transparent_var, self.upscale_var,
                    self.hires_var, self.hires_scale_var, self.freeu_var,
                    self.random_seed_var,
                    self.lora_strength, self.change_var, self.editor_var,
                    self.editor_canvas_var,
                    self.border_auto_var, self.border_aspect_var,
                    self.border_model_var, self.border_style_var,
                    self.border_thick_var, self.border_clean_var,
                    self.border_batch_var, self.anim_secs_var,
                    self.anim_keep_var, self.anim_loop_var,
                    self.anim_size_var, self.anim_transparent_var,
                    self.anim_motion_var, self.anim_gif_var,
                    self.anim_zip_var, self.anim_sheet_var,
                    self.anim_video_var, self.ollama_var,
                    self.swap_rag_var, self.face_source_var,
                    self.swap_use_rag_var,
                    self.swap_use_lora_var, self.swap_fast_var):
            var.trace_add("write", self._schedule_persist)
        for box in (self.prompt_box, self.negative_box, self.style_box,
                    self.border_prompt_box, self.anim_prompt_box,
                    self.edit_prompt_box):
            box.bind("<KeyRelease>", self._schedule_persist)
            box.bind("<FocusOut>", self._schedule_persist)
        # a Listbox has no variable to trace, so ticking a LoRA never
        # scheduled a save — the choice survived only if some other
        # setting happened to change before the app closed
        self.lora_list.bind("<<ListboxSelect>>", self._schedule_persist,
                            add="+")

    def _schedule_persist(self, *_a):
        if self._persist_job is not None:
            self.root.after_cancel(self._persist_job)
        self._persist_job = self.root.after(700, self._persist_now)

    def _persist_now(self):
        self._persist_job = None
        try:
            self._persist()
        except Exception:
            pass

    def _on_close(self):
        """Save, then take the engine down with us.

        The engine is a separate process that keeps whatever model it last
        loaded resident — up to ~17 GB of GPU memory after a Flux job. It
        used to outlive every app session, so the card stayed occupied and
        the stale engine went on holding port 8188 against the next
        launch. An engine we did not start is left alone."""
        try:
            self._persist()
        finally:
            # hide at once; the engine shutdown (a PowerShell round trip,
            # seconds) runs off the UI thread so the window never sits
            # there "Not responding" while it closes
            try:
                self.root.withdraw()
            except Exception:
                pass

            def work():
                try:
                    if engine_ours_to_stop():
                        kill_engine()
                        ENGINE_OWNER_FILE.unlink(missing_ok=True)
                except Exception:
                    pass      # never let cleanup stop the app from closing
                self.ui_queue.put(("quit", None))

            threading.Thread(target=work, daemon=True).start()
            self.root.after(20000, self._force_quit)

    def _clear_all(self):
        self._set(self.prompt_box, "")
        self._set(self.negative_box, "")
        self._set(self.style_box, "")
        self.preset_var.set(NONE_PRESET)
        self._auto_negative = None
        self._persist()

    # -------------------------------------------------- model updates
    def _check_updates_bg(self):
        """The startup housekeeping + update check, in a worker thread. A
        failure here (no network, a broken certificate bundle) used to
        kill the thread silently; now it is logged and said."""
        self.ui_queue.put(("pending", "updates", "checking for updates"))
        try:
            self._check_updates_bg_inner()
        except Exception as e:
            applog.exception("update check failed")
            self.ui_queue.put(("status", f"Update check failed: {e}"))
        finally:
            self.ui_queue.put(("pending", "updates", None))

    def _check_updates_bg_inner(self):
        # clear the exes an earlier update renamed aside — they could not be
        # deleted while that update was running, but nothing holds them now
        try:
            self_update.sweep_old_exes()
        except Exception:
            pass
        # …and the old engines / temp folders engine updates renamed aside
        try:
            _gone, stuck = engine_files.sweep_leftovers()
            if stuck:
                self.ui_queue.put(("status", f"{len(stuck)} leftover file(s) "
                                             "from an earlier update could "
                                             "not be removed yet."))
        except Exception:
            pass
        # the exe carries the manifest it was built with — refresh a stale
        # disk copy first so the check below sees the current model list
        sync_bundled_manifest()
        # then apply any update downloaded last session while its file
        # was locked by the engine
        try:
            manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
        except Exception:
            manifest = []
        for entry in manifest:
            dest = MODELS / entry["dir"] / entry["local"]
            tmp = dest.with_suffix(".tmp")
            if tmp.exists():
                try:
                    os.replace(tmp, dest)
                except OSError:
                    pass
        # app self-update (frozen exe only) — offer before model/engine
        if getattr(sys, "frozen", False):
            app_up = self_update.startup_check(APP_VERSION)
            if app_up:
                self.ui_queue.put(("app_update", app_up))
        ups = check_model_updates()
        eng = check_engine_update()
        if ups or eng:
            self.ui_queue.put(("updates", ups, eng))

    def _check_updates_now(self):
        """The Check for updates button. Unlike the startup check this one
        ignores a remembered "skip this version", so a skipped release can
        always be reached again, and it says so when there is nothing to
        report — a button that looks like it did nothing is worse than the
        news that you are up to date."""
        self.upd_btn.state(["disabled"])
        self.status_var.set("Checking for updates…")

        def work():
            up = self_update.check(APP_VERSION, include_skipped=True)
            self.ui_queue.put(("app_update_manual", up))

        threading.Thread(target=work, daemon=True).start()

    def _show_update_window(self, upd, auto=False):
        """Open the update window. Guarded so the startup check and the
        button cannot stack two of them."""
        win = getattr(self, "_upd_win", None)
        if win is not None and win.winfo_exists():
            win.lift()
            win.focus_force()
            return
        self._upd_win = self_update.UpdateWindow(
            self.root, upd, APP_VERSION,
            {"bg": BG, "bg2": BG2, "fg": FG},
            on_relaunch=self._relaunch_after_update,
            on_status=self.status_var.set, auto=auto)

    def _relaunch_after_update(self, newexe, tag):
        """Hand over to the freshly installed exe. The engine is stopped
        first: it belongs to this install, and leaving it holding port 8188
        would make the new app think a foreign engine is squatting there.

        All of that runs OFF the UI thread — kill_engine shells out to
        PowerShell for seconds, and doing it here showed the window as
        "Not responding" in the middle of every update. The single-instance
        mutex is released before the new copy starts, and the new copy is
        told our pid so it waits for us to finish closing instead of asking
        whether we are a second copy."""
        self.status_var.set(f"Updated to {tag} — restarting…")
        try:
            self._persist()
        except Exception:
            pass

        def work():
            try:
                kill_engine()
                ENGINE_OWNER_FILE.unlink(missing_ok=True)
            except Exception:
                pass          # never let cleanup block the restart
            release_single_instance()
            try:
                subprocess.Popen([str(newexe), "--after-update",
                                  str(os.getpid()), "--clean"],
                                 cwd=str(PROJECT), env=_clean_child_env())
            except OSError as e:
                self.ui_queue.put(("status", f"Could not start the new "
                                             f"version: {e} — run "
                                             f"{newexe.name} yourself."))
                return
            self.ui_queue.put(("quit", None))

        threading.Thread(target=work, daemon=True).start()
        self.root.after(20000, self._force_quit)

    def _log_status(self, *_a):
        try:
            applog.log("status: " + self.status_var.get())
        except Exception:
            pass

    def _open_settings(self):
        """Appearance settings: theme (Dark / Daylight) and menu size."""
        prefs = self.settings.get("prefs", {})
        dlg = Toplevel(self.root)
        dlg.withdraw()
        dlg.title("Settings")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Appearance", style="Head.TLabel").grid(
            row=0, column=0, columnspan=2, sticky=W, pady=(0, 8))

        ttk.Label(frm, text="Theme").grid(row=1, column=0, sticky=W)
        theme_var = StringVar(value=prefs.get("theme", "dark"))
        trow = ttk.Frame(frm); trow.grid(row=1, column=1, sticky=W)
        ttk.Radiobutton(trow, text="Dark", value="dark",
                        variable=theme_var).pack(side="left")
        ttk.Radiobutton(trow, text="Daylight", value="light",
                        variable=theme_var).pack(side="left", padx=(10, 0))

        ttk.Label(frm, text="Menu size").grid(row=2, column=0, sticky=W,
                                              pady=(8, 0))
        SIZES = [("Small", 0.9), ("Normal", 1.0), ("Large", 1.2),
                 ("Extra large", 1.4)]
        cur = float(prefs.get("ui_scale", 1.0))
        size_var = StringVar(value=next((n for n, v in SIZES if abs(v - cur)
                                         < 0.01), "Normal"))
        ttk.Combobox(frm, textvariable=size_var, state="readonly",
                     exportselection=False,
                     values=[n for n, _v in SIZES], width=14).grid(
            row=2, column=1, sticky=W, pady=(8, 0))

        ttk.Label(frm, text="Font").grid(row=3, column=0, sticky=W,
                                         pady=(8, 0))
        font_var = StringVar(value=prefs.get("font", "Segoe UI"))
        ttk.Combobox(frm, textvariable=font_var, state="readonly",
                     exportselection=False, values=UI_FONTS, width=16).grid(
            row=3, column=1, sticky=W, pady=(8, 0))

        msg = ttk.Label(frm, text="", style="Dim.TLabel", wraplength=320,
                        justify="left")
        msg.grid(row=4, column=0, columnspan=2, sticky=W, pady=(12, 0))

        def save():
            p = self.settings.setdefault("prefs", {})
            p["theme"] = theme_var.get()
            p["ui_scale"] = dict(SIZES)[size_var.get()]
            p["font"] = font_var.get()
            self._save_settings()
            return p

        def apply_now():
            save()
            self._apply_prefs_live()
            msg.configure(text="Applied. The font and size apply fully after "
                               "Restart now.")

        def restart():
            save()
            dlg.destroy()
            self._restart_app()

        brow = ttk.Frame(frm); brow.grid(row=5, column=0, columnspan=2,
                                         sticky="ew", pady=(14, 0))
        ttk.Button(brow, text="Restart now", style="Go.TButton",
                   command=restart).pack(side="right")
        ttk.Button(brow, text="Apply", command=apply_now).pack(
            side="right", padx=(0, 8))
        ttk.Button(brow, text="Close", command=dlg.destroy).pack(
            side="right", padx=(0, 8))
        self._place_near(dlg, self.settings_btn)
        dlg.deiconify()
        try:
            dlg.grab_set()
        except Exception:
            pass

    def _apply_prefs_live(self):
        """Best-effort live apply of theme + size: rebind the palette, redo
        the ttk styles, recolour the plain tk widgets (Text/Listbox/Canvas)
        by walking the tree, and reset the scaling. A Restart makes it
        exact; this makes it close without one."""
        prefs = self.settings.get("prefs", {})
        apply_theme(prefs.get("theme", "dark"))
        globals()["UI_FONT"] = prefs.get("font", "Segoe UI")
        try:
            self.root.tk.call("tk", "scaling", getattr(self, "_base_scaling",
                              1.333) * float(prefs.get("ui_scale", 1.0)))
        except Exception:
            pass
        self._style()
        self.root.configure(bg=BG)

        import tkinter.font as _tkfont

        def refont(w):
            try:
                if w.winfo_class() in ("Text", "Listbox"):
                    f = _tkfont.Font(font=w.cget("font"))
                    w.configure(font=(UI_FONT, f.cget("size")))
            except Exception:
                pass
            for c in w.winfo_children():
                refont(c)

        refont(self.root)

        def recolour(w):
            cls = w.winfo_class()
            try:
                if cls == "Text":
                    w.configure(bg=BG3, fg=FG, insertbackground=FG)
                elif cls == "Listbox":
                    w.configure(bg=BG3, fg=FG, selectbackground=ACCENT,
                                selectforeground="white")
                elif cls == "Canvas":
                    # the section rules are RULE; everything else is BG/BG2
                    if int(w.cget("height")) == 3:
                        w.configure(bg=RULE)
                    else:
                        w.configure(bg=w.cget("bg") if False else BG)
            except Exception:
                pass
            for c in w.winfo_children():
                recolour(c)

        recolour(self.root)
        try:
            self.canvas.configure(bg=BG2)
            self.gallery_canvas.configure(bg=BG)
        except Exception:
            pass
        self._show_current()

    def _restart_app(self):
        """Relaunch the app so theme/size apply cleanly from a fresh start."""
        self.status_var.set("Restarting to apply settings…")
        try:
            self._persist()
        except Exception:
            pass

        def work():
            try:
                if engine_ours_to_stop():
                    kill_engine()
                    ENGINE_OWNER_FILE.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                release_single_instance()
            except Exception:
                pass
            try:
                subprocess.Popen([sys.executable] + sys.argv[1:],
                                 cwd=str(PROJECT),
                                 env=_clean_child_env()
                                 if "_clean_child_env" in globals() else None)
            except Exception as e:
                self.ui_queue.put(("status", f"Could not restart: {e}"))
                return
            self.ui_queue.put(("quit", None))

        threading.Thread(target=work, daemon=True).start()
        self.root.after(20000, self._force_quit)

    def _open_log(self):
        """The Log button: open app.log, or say where it is."""
        if not applog.open_log():
            self.status_var.set(f"The log is at {applog.path()}.")

    def _force_quit(self):
        """Fallback for the close / relaunch hand-over: never leave a
        hidden window running because the engine shutdown got stuck."""
        try:
            self.root.destroy()
        except Exception:
            pass

    def _download_updates(self, ups, eng=None):
        ok, locked = 0, 0
        for u in ups:
            try:
                if download_model_update(
                        u, lambda s: self.ui_queue.put(("status", s)),
                        lambda d, t: self.ui_queue.put(("progress", d, t))):
                    ok += 1
                else:
                    locked += 1
            except Exception as e:
                self.ui_queue.put(("status",
                                   f"Update failed for {u['local']}: {e}"))
        eng_ok = False
        if eng:
            try:
                self.ui_queue.put(("status", "Stopping engine for update…"))
                kill_engine()
                time.sleep(2)
                update_engine(eng["sha"],
                              lambda s: self.ui_queue.put(("status", s)))
                eng_ok = True
                self.ui_queue.put(("status", "Engine updated — restarting…"))
            except Exception as e:
                applog.exception("engine update failed")
                # the engine is as it was (engine_files renames, never
                # deletes in place) — but it was stopped above, and used to
                # stay stopped until the next launch
                self.ui_queue.put(("status", f"Engine update failed: {e} — "
                                             "restarting the engine you "
                                             "have."))
            threading.Thread(target=self._boot_engine, daemon=True).start()
        note = f"Updates done: {ok} model(s) installed"
        if locked:
            note += f", {locked} will apply on next start (file in use)"
        if eng:
            note += ", engine updated" if eng_ok else ", engine NOT updated"
        self.ui_queue.put(("status", note))
        self.ui_queue.put(("models_changed", None))

    # -------------------------------------------------- engine boot
    def _restart_engine(self):
        self.ui_queue.put(("status", "Restarting engine…"))
        kill_engine()
        time.sleep(2)
        self._boot_engine()

    def _repair_engine_dirs(self):
        """Undo the folder nesting older engine updates left behind, so
        the engine can see the user's custom nodes (and inputs) again.
        Runs before the engine starts, every launch — it is a no-op once
        the folders are flat."""
        for sub in ("custom_nodes", "input", "user"):
            try:
                n = flatten_nested_dir(ENGINE_DIR, sub,
                                       dedupe=(sub == "custom_nodes"))
            except Exception:
                n = 0
            if n:
                self.ui_queue.put((
                    "status", f"Repaired the engine's {sub} folder "
                              f"({n} item(s) restored from an older "
                              "update) — add-ons work again."))

    def _repair_engine_files(self, from_log=False):
        """Rebuild the engine when its files cannot start it — the state a
        pre-v1.36 engine update that died part-way left behind: every
        package folder emptied behind a main.py that could not import
        comfy, and no custom_nodes. Checked against the sentinel files
        before every start; checked again from engine.log after a failed
        start, because a tree can be broken in ways the list does not
        name. Once per session. Returns True only when a repair ran and
        the engine was started again from here."""
        if getattr(self, "_engine_repair_tried", False):
            return False
        if from_log:
            damaged = engine_files.log_shows_damage(PROJECT / "engine.log")
        else:
            damaged = ENGINE_DIR.is_dir() and \
                bool(engine_files.check_integrity())
        if not damaged:
            return False
        self._engine_repair_tried = True
        self.ui_queue.put(("status", "The engine's files are damaged (an "
                                     "earlier update stopped part-way) — "
                                     "rebuilding them, one moment…"))
        try:
            kill_engine()
            time.sleep(1)
            engine_files.repair_engine(
                lambda s: self.ui_queue.put(("status", s)))
        except Exception as e:
            applog.exception("engine repair failed")
            self.ui_queue.put(("error", f"Engine repair failed: {e}"))
            return False
        self.ui_queue.put(("status", "Engine files rebuilt — starting the "
                                     "engine…"))
        if from_log:
            self._boot_engine()
            return True
        return False

    def _autoheal_addons(self):
        """After the engine is up: install the add-ons the app needs but
        the model manifest cannot deliver — the IP-Adapter custom node
        that powers RAG maps and Reference-DB face guidance. Without this
        the failure only surfaced mid-generation as 'Node
        IPAdapterUnifiedLoader not found'."""
        if getattr(self, "_autoheal_tried", False):
            return
        self._autoheal_tried = True
        try:
            # 1) IP-Adapter custom node (RAG maps + person photos)
            if not self._style_support_ok():
                time.sleep(3)             # nodes may still be registering
                if not self._style_support_ok():
                    self.ui_queue.put((
                        "status", "Setting up the IP-Adapter add-on (needed "
                                  "for RAG maps and person photos) — one "
                                  "moment…"))
                    self._install_style_support()
            # 2) the local face-swap engine (Clone Tool). Set it up here — at
            # startup / right after an update — rather than waiting for the
            # user's first swap. One-time; skipped once it's ready. Runs after
            # the IP-Adapter install so they don't contend for the add-on lock.
            if not faceswap_ready():
                self.ui_queue.put((
                    "status", "Setting up the face-swap engine for the Clone "
                              "Tool (one-time ~550 MB) — you can keep working."))
                self._install_face_swap()
        except Exception:
            applog.exception("autoheal addons failed")

    def _boot_engine(self):
        self.ui_queue.put(("pending", "engine", "engine starting"))
        self._repair_engine_dirs()
        self._repair_engine_files()
        if engine_alive() and not engine_is_ours():
            # a foreign engine (another instance / leftover dev run) holds
            # the port — using it can send results to the wrong window
            self.ui_queue.put(("status", "Note: an engine was already "
                                         "running on port 8188 (another "
                                         "instance or a previous session). "
                                         "Using it — close extra windows if "
                                         "results seem to go missing."))
        if not engine_alive():
            self.ui_queue.put(("status", "Starting local engine (first start "
                                         "takes a minute)…"))
            proc = None
            try:
                proc = start_engine()
            except Exception as e:
                self.ui_queue.put(("pending", "engine", None))
                self.ui_queue.put(("error", f"Could not start engine: {e}"))
                return
            crashed = False
            for i in range(180):
                if engine_alive():
                    break
                # a first start on a hard drive is slow, but if the engine
                # PROCESS has exited it has crashed — don't sit through the
                # whole six-minute wait for something already dead
                if proc is not None and proc.poll() is not None:
                    crashed = True
                    break
                if i and i % 5 == 0:      # ~every 10s: show it is still trying
                    self.ui_queue.put((
                        "status",
                        f"Starting local engine — {i * 2}s so far. A first "
                        "start, or a fresh IP-Adapter/engine install, can "
                        "take a few minutes…"))
                time.sleep(2)
            else:
                if self._repair_engine_files(from_log=True):
                    return          # the repair started the engine again
                tail = engine_log_tail()
                self.ui_queue.put(("pending", "engine", None))
                self.ui_queue.put((
                    "error", "The engine did not come up in time — see "
                    "engine.log in the project folder."
                    + ("\n\nLast engine.log lines:\n" + tail if tail else "")))
                return
            if crashed:
                if self._repair_engine_files(from_log=True):
                    return          # the repair started a working engine
                tail = engine_log_tail()
                self.ui_queue.put(("pending", "engine", None))
                self.ui_queue.put((
                    "error", "The engine stopped while starting. This often "
                    "means a just-installed add-on (IP-Adapter) or engine "
                    "update did not load — see engine.log in the project "
                    "folder."
                    + ("\n\nLast engine.log lines:\n" + tail if tail else "")))
                return
        # a stale engine from an old session may have been started with
        # wrong model paths: if it can't see models that exist on disk,
        # restart it with our config (start_engine rewrites the yaml)
        if not getattr(self, "_engine_heal_tried", False):
            try:
                disk = set(scan_models("checkpoints"))
                known = set(_api_choices("CheckpointLoaderSimple",
                                         "ckpt_name"))
                if disk and disk - known:
                    self._engine_heal_tried = True
                    self.ui_queue.put(("status",
                                       "Engine can't see the model folder "
                                       "— restarting it with correct "
                                       "paths…"))
                    kill_engine()
                    time.sleep(2)
                    start_engine()
                    for _ in range(180):
                        if engine_alive():
                            break
                        time.sleep(2)
            except Exception:
                pass
        try:
            stats = api_get("/system_stats")
            vram = stats["devices"][0]["vram_total"] / (1024**3)
            self.ui_queue.put(("vram", vram))
        except Exception:
            pass
        self.ui_queue.put(("pending", "engine", None))
        self.ui_queue.put(("engine_ready", None))
        self._autoheal_addons()

    # -------------------------------------------------- ui handlers
    def _on_preset(self, _e=None):
        p = self._preset()
        if not p:
            self._set(self.style_box, "")
            return
        self._set(self.style_box, p["style"])
        # replace the negative only if the user hasn't customized it:
        # empty box, or box still holding a previous preset's negative
        current = self._get(self.negative_box)
        if not current or current == self._auto_negative:
            self._set(self.negative_box, p["negative"])
            self._auto_negative = p["negative"]

    def _preset(self):
        name = self.preset_var.get()
        return next((p for p in self.presets if p["name"] == name), None)

    def _refresh_preset_list(self):
        """List only presets that suit the selected model's family — but
        never clear the user's current selection: it stays even if the
        model changed; only the dropdown's option list adapts."""
        fam = model_family(self._model_raw() or "")
        want = "anime" if fam == "anime" else \
            ("flux" if fam in ("flux", "schnell") else "sdxl")
        names = [NONE_PRESET] + [p["name"] for p in self.presets
                                 if p.get("model_hint", "sdxl") == want]
        self.preset_dd["values"] = names

    def _editor_engine(self):
        return self._editor_display.get(self.editor_var.get(), "kontext")

    def _best_sdxl_model(self):
        """Best installed SDXL-family checkpoint for the border LoRA —
        Juggernaut preferred, then any non-Flux model that fits."""
        ck = list_checkpoints()
        pref = next((c for c in ck if "juggernaut" in c.lower()), None)
        if pref and self._model_fits.get(pref, "ok") != "block":
            return pref
        return next((c for c in ck
                     if model_family(c) not in ("flux", "schnell")
                     and self._model_fits.get(c, "ok") != "block"), None)

    def _border_model_raw(self):
        disp = self.border_model_var.get()
        display = getattr(self, "_model_display", {})
        if disp and disp != BORDER_SAME_MODEL and disp in display:
            return display[disp]
        return self._model_raw()

    def _border_style(self):
        return next((p for p in self.presets
                     if p["name"] == self.border_style_var.get()), None)

    def _refresh_border_styles(self):
        """Style options follow the border's model family — the current
        selection is never cleared, only the option list adapts."""
        fam = model_family(self._border_model_raw() or "")
        want = "anime" if fam == "anime" else \
            ("flux" if fam in ("flux", "schnell") else "sdxl")
        self.border_style_dd["values"] = [NONE_PRESET] + [
            p["name"] for p in self.presets
            if p.get("model_hint", "sdxl") == want]

    def _editor_tier(self, engine):
        """ok = comfortable, warn = loads but slow, block = too big."""
        if self.vram_gb is None:
            return "ok"
        req = EDITOR_VRAM.get(engine, 16)
        if req <= self.vram_gb:
            return "ok"
        if req - 4 <= self.vram_gb:
            return "warn"
        return "block"

    def _editor_fits(self, engine):
        return self._editor_tier(engine) != "block"

    def _refresh_editor_list(self):
        """Editor entries show their VRAM requirement; tight fits show
        red, ones beyond the card are greyed and blocked."""
        prev = self._editor_engine() if self._editor_display else "kontext"
        self._editor_display = {}
        for label, engine in EDITOR_ENGINES.items():
            req = EDITOR_VRAM.get(engine, 16)
            disp = f"{label} — needs ~{req} GB VRAM"
            tier = self._editor_tier(engine)
            if tier == "warn":
                disp += "  — tight fit: slow"
            elif tier == "block":
                disp += "  — exceeds GPU memory"
            self._editor_display[disp] = engine
        self.editor_dd["values"] = list(self._editor_display)
        self.editor_dd.configure(postcommand=self._color_editor_dropdown)
        target = prev if self._editor_fits(prev) else next(
            (e for e in EDITOR_ENGINES.values() if self._editor_fits(e)),
            prev)
        for disp, engine in self._editor_display.items():
            if engine == target:
                self.editor_var.set(disp)
                break

    def _color_editor_dropdown(self):
        try:
            pd = self.root.tk.call("ttk::combobox::PopdownWindow",
                                   self.editor_dd)
            lb = f"{pd}.f.l"
            for i, disp in enumerate(self.editor_dd["values"]):
                tier = self._editor_tier(
                    self._editor_display.get(disp, "kontext"))
                self.root.tk.call(lb, "itemconfigure", i, "-foreground",
                                  self.TIER_COLORS.get(tier, ACCENT2))
        except Exception:
            pass

    def _on_editor_pick(self):
        engine = self._editor_engine()
        tier = self._editor_tier(engine)
        if tier == "block":
            messagebox.showwarning(
                "Not enough GPU memory",
                f"This editor needs ~{EDITOR_VRAM.get(engine, 16)} GB of "
                f"VRAM — your card has {self.vram_gb:.1f} GB. Pick a "
                "green editor instead.")
            for disp, e in self._editor_display.items():
                if self._editor_fits(e):
                    self.editor_var.set(disp)
                    break
        elif tier == "warn" and engine not in self._warned_vram:
            self._warned_vram.add(engine)
            messagebox.showwarning(
                "Tight VRAM fit",
                f"This editor needs ~{EDITOR_VRAM.get(engine, 16)} GB — "
                f"your card has {self.vram_gb:.1f} GB. It will load and "
                "run, but expect noticeably slower edits while memory "
                "is paged.")

    def _vram_block_msg(self, raw):
        gb = self._size_gb(raw)
        messagebox.showwarning(
            "Not enough GPU memory",
            f"{raw} is {gb:.1f} GB and needs about "
            f"{gb + VRAM_HEADROOM_GB:.0f} GB of VRAM — your card has "
            f"{self.vram_gb:.1f} GB. Pick a green model instead.")

    def _on_model_pick(self):
        raw = self._model_raw()
        tier = self._model_fits.get(raw, "ok")
        if tier == "block":
            self._vram_block_msg(raw)
            if self._last_fit_display:
                self.model_var.set(self._last_fit_display)
        else:
            if tier == "warn" and raw not in self._warned_vram:
                self._warned_vram.add(raw)
                messagebox.showwarning(
                    "Tight VRAM fit",
                    f"{raw} will load and run, but it nearly fills your "
                    f"card ({self.vram_gb:.1f} GB) — expect noticeably "
                    "slower generation while memory is paged.")
            self._last_fit_display = self.model_var.get()
        self._update_model_entry_style()
        self._refresh_preset_list()
        self._refresh_mode_badges()   # RAG validity depends on the model family

    def _use_example(self):
        p = self._preset()
        if not p:
            messagebox.showinfo("Pick a preset",
                                "Choose an art style preset first.")
            return
        self._set(self.prompt_box, p["example"])
        self._on_preset()

    def _model_raw(self):
        """Raw checkpoint filename behind the decorated dropdown text."""
        return self._model_display.get(self.model_var.get(),
                                       self.model_var.get())

    @staticmethod
    def _size_gb(name):
        try:
            return (MODELS / "checkpoints" / name).stat().st_size / (1024**3)
        except OSError:
            return 0

    TIER_COLORS = {"ok": ACCENT2, "warn": "#e74c3c", "block": "#5a5a6a"}
    TIER_STYLES = {"ok": "Fit.TCombobox", "warn": "Warn.TCombobox",
                   "block": "NoFit.TCombobox"}

    def _model_tier_of(self, gb):
        """ok = comfortable, warn = loads but slow (tight fit),
        block = exceeds the card."""
        if self.vram_gb is None or not gb:
            return "ok"
        if gb + VRAM_HEADROOM_GB <= self.vram_gb:
            return "ok"
        if gb <= self.vram_gb:
            return "warn"
        return "block"

    def _color_model_dropdown(self, dd):
        """Green = fits, red = tight fit (slow), grey = too big."""
        try:
            pd = self.root.tk.call("ttk::combobox::PopdownWindow", dd)
            lb = f"{pd}.f.l"
            for i, disp in enumerate(dd["values"]):
                raw = self._model_display.get(disp, disp)
                tier = self._model_fits.get(raw, "ok")
                self.root.tk.call(lb, "itemconfigure", i, "-foreground",
                                  self.TIER_COLORS.get(tier, ACCENT2))
        except Exception:
            pass

    def _update_model_entry_style(self):
        tier = self._model_fits.get(self._model_raw(), "ok")
        self.model_dd.configure(style=self.TIER_STYLES.get(
            tier, "Fit.TCombobox"))

    def _refresh_models(self):
        ckpts = list_checkpoints()
        self._model_display = {}
        self._model_fits = {}
        for name in ckpts:
            gb = self._size_gb(name)
            tier = self._model_tier_of(gb)
            self._model_fits[name] = tier
            disp = f"{name}  ·  {gb:.1f} GB" if gb else name
            if tier == "warn":
                disp += "  — tight fit: slow"
            elif tier == "block":
                disp += "  — exceeds GPU memory"
            self._model_display[disp] = name
        self.model_dd["values"] = list(self._model_display)
        self.model_dd.configure(
            postcommand=lambda: self._color_model_dropdown(self.model_dd))
        if not ckpts:
            self.status_var.set(
                "No models installed — run Setup.exe (or drop .safetensors "
                "into models\\checkpoints), then hit ↻.")
        if self.vram_gb is None:
            self.vram_note.set(
                "⚠ No NVIDIA GPU detected — this app requires an NVIDIA "
                "card with a current driver; generation will not work.")
        else:
            n_fit = sum(1 for t in self._model_fits.values() if t == "ok")
            self.vram_note.set(
                f"Your GPU: {self.vram_gb:.1f} GB VRAM — green fits, red "
                f"runs but slow, grey too big ({n_fit}/{len(ckpts)} "
                "comfortable).")

        cur = self._model_raw()
        if cur not in ckpts:
            last = self.settings.get("last_model")
            cur = last if last in ckpts else ""
            if not cur or self._model_fits.get(cur, "ok") == "block":
                cur = next((c for c in ckpts
                            if self._model_fits.get(c) == "ok"),
                           ckpts[0] if ckpts else "")
        for disp, raw in self._model_display.items():
            if raw == cur:
                self.model_var.set(disp)
                if self._model_fits.get(raw, "ok") != "block":
                    self._last_fit_display = disp
                break
        self._update_model_entry_style()
        self._refresh_preset_list()
        self.border_model_dd["values"] = [BORDER_SAME_MODEL] + \
            list(self._model_display)
        self._refresh_border_styles()

        loras = _visible_loras(list_loras())
        keep = set(self._selected_loras()) | getattr(self, "_pending_loras",
                                                     set())
        # a LoRA that has been deleted or moved since it was ticked must
        # not linger in the saved selection, or it would be sent to the
        # engine on the next generation and fail the whole job
        ghosts = keep - set(loras)
        keep -= ghosts
        self._pending_loras = set()
        self.lora_list.delete(0, END)
        for idx, name in enumerate(loras):
            self.lora_list.insert(END, name)
            if name in keep:
                self.lora_list.selection_set(idx)
        if ghosts:
            self.status_var.set(
                f"Dropped {len(ghosts)} LoRA(s) no longer in the folder: "
                + ", ".join(sorted(ghosts)))
            self._schedule_persist()
        self._validate_ragmap()
        self._refresh_mode_badges()   # model/LoRA/map set may have changed

    def _pick_ref(self):
        paths = filedialog.askopenfilenames(filetypes=[
            ("Images", "*.png;*.jpg;*.jpeg;*.webp;*.bmp"),
            ("All files", "*.*")])
        if paths:
            self.ref_paths = list(paths)
            first = Path(self.ref_paths[0]).name
            self.ref_var.set(first if len(self.ref_paths) == 1 else
                             f"{len(self.ref_paths)} images ({first}, …)")
            self.status_var.set("Reference(s) loaded — 'Change amount' sets "
                                "how strongly the AI transforms them.")
            self._refresh_editor_state()
            self._schedule_persist()

    def _clear_ref(self):
        self.ref_paths = []
        self.ref_var.set("none — text only")
        self._refresh_editor_state()
        self._schedule_persist()

    def _pick_border_refs(self):
        paths = filedialog.askopenfilenames(filetypes=[
            ("Images", "*.png;*.jpg;*.jpeg;*.webp;*.bmp"),
            ("All files", "*.*")])
        if paths:
            self.border_ref_paths = list(paths)
            first = Path(self.border_ref_paths[0]).name
            self.border_ref_var.set(
                first if len(self.border_ref_paths) == 1 else
                f"{len(self.border_ref_paths)} images ({first}, …)")
            self._schedule_persist()

    def _clear_border_refs(self):
        self.border_ref_paths = []
        self.border_ref_var.set("none")
        self._schedule_persist()

    # -------------------------------------------------- batch queue
    def _enqueue(self, kind, label, payload):
        self.job_queue.append({"kind": kind, "label": label,
                               "payload": payload})
        self._refresh_queue()
        self.status_var.set(f"Queued: {label}  ({len(self.job_queue)} in "
                            "batch). Add more, then Run all.")

    def _refresh_queue(self):
        if not hasattr(self, "queue_list"):
            return
        self.queue_list.delete(0, END)
        for j in self.job_queue:
            self.queue_list.insert(END, f"[{j['kind']}] {j['label']}")
        self.queue_count_var.set(f"Batch queue ({len(self.job_queue)})")

    def _remove_queued(self):
        sel = list(self.queue_list.curselection())
        for i in reversed(sel):
            if 0 <= i < len(self.job_queue):
                del self.job_queue[i]
        self._refresh_queue()

    def _clear_queue(self):
        self.job_queue = []
        self._refresh_queue()

    def _run_queue(self):
        if getattr(self, "_batch_active", False):
            return
        if not self.job_queue:
            self.status_var.set("Batch queue is empty — use the ＋Q buttons "
                                "to add jobs.")
            return
        if self.busy and self._busy_guard():
            return
        self._batch_active = True
        CANCEL.clear()
        self.busy = True
        self.go_btn.state(["disabled"])
        threading.Thread(target=self._queue_worker, daemon=True).start()

    def _queue_worker(self):
        jobs = list(self.job_queue)
        total = len(jobs)
        try:
            for i, j in enumerate(jobs, 1):
                if CANCEL.is_set():
                    self.ui_queue.put(("status", f"Batch cancelled — "
                                                 f"{i - 1} of {total} done."))
                    break
                self.ui_queue.put(("status", f"Batch {i}/{total}: "
                                             f"{j['label']}…"))
                try:
                    if j["kind"] == "gen":
                        Generator(self.ui_queue).run(j["payload"])
                    elif j["kind"] == "border":
                        Generator(ChannelQueue(self.ui_queue,
                                               "border_progress")
                                  ).run(j["payload"])
                    elif j["kind"] == "anim":
                        self._run_animation(j["payload"])
                except Exception as e:
                    self.ui_queue.put(("status",
                                       f"Batch job {i} failed: {e}"))
            self.ui_queue.put(("batch_done", total))
        finally:
            self._batch_active = False

    def _busy_guard(self):
        """True = abort the new request. Offers to cancel the running job
        so a stuck/slow generation can't lock the app."""
        if getattr(self, "_batch_active", False):
            messagebox.showinfo("Batch running",
                                "A batch queue is running. Wait for it to "
                                "finish, or clear the queue first.")
            return True
        if not self.busy:
            return False
        if messagebox.askyesno(
                "Generation in progress",
                "A generation is still running (big models can take many "
                "minutes — watch the progress bar).\n\nCancel it now?"):
            cancel_all()
            self.job_queue = []
            self._refresh_queue()
            self.busy = False
            self.go_btn.state(["!disabled"])
            self.progress["value"] = 0
            self.pct_var.set("")
            self.status_var.set("Cancelled.")
        return True

    def _cancel_generation(self):
        """The ✕ Cancel next to each progress bar — stops the running job
        (image, border or animation), the rest of a Variations set, and
        anything left in the batch queue."""
        if not self.busy:
            return
        cancel_all()
        self.job_queue = []
        self._refresh_queue()
        self.busy = False
        self.go_btn.state(["!disabled"])
        for bar, var in ((self.progress, self.pct_var),
                         (self.anim_progress, self.anim_pct_var),
                         (self.border_progress, self.border_pct_var)):
            bar["value"] = 0
            var.set("")
        self.status_var.set("Cancelled.")

    def _use_selected_for_editor(self):
        if self.current is None or not self.session:
            return
        _img, _params, path = self.session[self.current]
        if str(path).lower().endswith(".gif"):
            self.status_var.set("Pick a still image for the editor — GIFs "
                                "can't be edited directly.")
            return
        self.ref_paths = [str(path)]
        self.ref_var.set("selection")
        self.status_var.set(f"Editor now works on the selected image "
                            f"({Path(path).name}) — the prompt is the "
                            "instruction.")
        self._refresh_editor_state()
        self._schedule_persist()

    # a swap sends the base + up to this many face photos (Qwen's encoder
    # takes 3 images total; Kontext chains up to 4 — 2 keeps both happy)
    SWAP_MAX_FACES = 2

    EDIT_ACTIONS = [
        ("Remove text / watermark",
         "remove all text, captions, logos and watermarks, keep everything "
         "else identical"),
        ("Plain white background",
         "replace the background with a plain solid white background, keep "
         "the subject exactly as it is"),
        ("Change the background to\u2026", "change the background to "),
        ("Change the outfit to\u2026", "change the outfit to "),
        ("Recolour to\u2026", "change the colour scheme to "),
        ("Redraw in the style of\u2026", "redraw in the style of "),
        ("Improve quality & sharpness",
         "improve the overall quality, sharpness and detail, keep the "
         "content the same"),
        ("Fix distorted hands / faces",
         "fix any distorted hands and faces, keep everything else the same"),
    ]

    def _build_edit_menu(self, parent):
        """The 'Common edits' menu: each item fills the edit instruction
        box. Items ending in a space (a template) leave the cursor for you
        to finish the sentence."""
        from tkinter import Menu
        m = Menu(parent, tearoff=0, bg=BG2, fg=FG,
                 activebackground=ACCENT, activeforeground="white")
        for label, instr in self.EDIT_ACTIONS:
            m.add_command(label=label,
                          command=lambda t=instr: self._pick_edit_action(t))
        self._edit_menu = m
        return m

    def _pick_edit_action(self, instr):
        try:
            self.left_tabs.select(self._page_tabs[str(self._page_edit)])
        except Exception:
            pass
        self._set(self.edit_prompt_box, instr)
        self.edit_prompt_box.focus_set()
        self.edit_prompt_box.mark_set("insert", END)
        if not self.ref_paths:
            self.status_var.set("Now load an image (\U0001f5bc Load\u2026 or "
                                "Use selected), then Apply edit.")
        self._schedule_persist()

    def _popup_edit_menu(self, event):
        try:
            self._edit_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._edit_menu.grab_release()
        return "break"

    def _on_clone_toggle(self, *_a):
        self._apply_clone_enabled()
        self._refresh_editor_state()
        self._schedule_persist()

    def _apply_clone_enabled(self):
        """Grey the whole Clone body when the swap is unticked."""
        if not hasattr(self, "clone_body"):
            return
        on = self.swap_rag_var.get()

        def setstate(w):
            try:
                if isinstance(w, (ttk.Button, ttk.Checkbutton, ttk.Radiobutton,
                                  ttk.Combobox, ttk.Menubutton, ttk.Scale)):
                    w.state(["!disabled"] if on else ["disabled"])
                elif w.winfo_class() == "Listbox":
                    w.configure(state=("normal" if on else "disabled"))
            except Exception:
                pass
            for c in w.winfo_children():
                setstate(c)

        setstate(self.clone_body)

    def _on_face_source(self, *_a):
        """Show the file row or the database rows for the chosen source."""
        db = self.face_source_var.get() == "db"
        if hasattr(self, "face_file_row"):
            if db:
                self.face_file_row.grid_remove()
                self.face_db_row.grid()
            else:
                self.face_db_row.grid_remove()
                self.face_file_row.grid()
        self._refresh_face_list()
        self._refresh_editor_state()
        self._schedule_persist()

    def _on_swap_guide(self, *_a):
        """One checkbox drives both RAG and LoRA guidance of the swap base."""
        self.swap_use_lora_var.set(self.swap_use_rag_var.get())
        self._refresh_editor_state()

    def _pick_face(self):
        paths = filedialog.askopenfilenames(filetypes=[
            ("Images", "*.png;*.jpg;*.jpeg;*.webp;*.bmp"),
            ("All files", "*.*")])
        if paths:
            self.face_paths = list(paths)
            self.face_source_var.set("file")
            self._on_face_source()
            self._set_face_label()
            self.status_var.set("Face image loaded — tick the swap and "
                                "Generate to put it on the picture.")
            self._refresh_editor_state()
            self._schedule_persist()

    def _clear_face(self):
        self.face_paths = []
        self._set_face_label()
        self._refresh_editor_state()
        self._schedule_persist()

    def _set_face_label(self):
        if not hasattr(self, "face_file_var"):
            return
        if not self.face_paths:
            self.face_file_var.set("no file chosen")
        else:
            first = Path(self.face_paths[0]).name
            self.face_file_var.set(first if len(self.face_paths) == 1 else
                                   f"{len(self.face_paths)} files ({first}, …)")
        self._refresh_face_list()

    def _face_thumb_image(self, src, px=56):
        """A small square-ish PhotoImage from a file path or raw image bytes;
        None on any failure. Refs are kept in self._face_thumb_imgs so Tk
        does not garbage-collect them out of their labels."""
        try:
            img = Image.open(src if isinstance(src, (str, Path))
                             else BytesIO(src)).convert("RGB")
            img.thumbnail((px, px), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._face_thumb_imgs.append(photo)
            return photo
        except Exception:
            return None

    def _refresh_face_list(self):
        """The 'Using:' strip — small thumbnail samples of the face image(s)
        for a file source, or the chosen person's kept photos, each with a
        short caption. This is what will be placed on the generated image."""
        if not hasattr(self, "face_using"):
            return
        for w in self.face_using.winfo_children():
            w.destroy()
        self._face_thumb_imgs = []
        samples = []          # (source, caption); source is a path or bytes
        note = None
        if self.face_source_var.get() == "file":
            if self.face_paths:
                for p in self.face_paths[:self.SWAP_MAX_FACES]:
                    samples.append((p, Path(p).name))
            else:
                note = "(no image chosen — Browse…)"
        else:
            if self.actor_sel:
                name = getattr(self, "_actor_base_label", "") \
                    or self.actor_var.get()
                blobs = self._actor_photo_blobs(self.actor_sel["imdb_id"])
                ex = self._excluded_set()
                kept = [i for i in range(len(blobs)) if i not in ex] \
                    or list(range(len(blobs)))
                if not blobs:
                    note = name
                else:
                    for i in kept[:self.SWAP_MAX_FACES]:
                        cap = name if len(kept) == 1 else f"photo {i + 1}"
                        samples.append((blobs[i], cap))
                    if len(kept) > self.SWAP_MAX_FACES:
                        note = f"+{len(kept) - self.SWAP_MAX_FACES} more sent"
            else:
                note = "(no person chosen — Person…)"
        col = 0
        for src, cap in samples:
            cell = ttk.Frame(self.face_using)
            cell.grid(row=0, column=col, padx=(0, 8), sticky="n"); col += 1
            img = self._face_thumb_image(src, 56)
            if img is not None:
                ttk.Label(cell, image=img).grid(row=0, column=0)
            else:
                ttk.Label(cell, text="(no preview)",
                          style="Dim.TLabel").grid(row=0, column=0)
            ttk.Label(cell, text=cap, style="Dim.TLabel", wraplength=76,
                      justify="center", font=(UI_FONT, 8)).grid(row=1, column=0)
        if note:
            ttk.Label(self.face_using, text=note, style="Dim.TLabel",
                      font=(UI_FONT, 9)).grid(row=1, column=0, columnspan=99,
                                              sticky=W, pady=(2, 0))
        # thumbnails are built after startup — keep the wheel scrolling the
        # panel when the pointer is over them
        self._arm_wheel(self.face_using)

    def _clone_method(self):
        """The engine key ('faceswap' | 'qwen' | 'kontext') for the Clone
        Tool's selected Method."""
        label = self.clone_method_var.get() if hasattr(self, "clone_method_var") \
            else ""
        for lbl, key in CLONE_METHODS:
            if lbl == label:
                return key
        return "faceswap"

    def _swap_face_source(self):
        """The face photo(s) for an image-swap run, as a list: every loaded
        editor image (several photos of the same person sharpen the
        likeness), otherwise the chosen Reference DB person's photo(s).
        Empty list if neither. A future multi-photo Actor DB only needs
        `_actor_ref_paths` to return more entries — everything downstream
        already takes the list."""
        if self.face_source_var.get() == "file":
            return list(self.face_paths[:self.SWAP_MAX_FACES])
        if self.actor_sel:
            return self._actor_ref_paths()[:self.SWAP_MAX_FACES]
        return []

    def _actor_ref_paths(self):
        """All available photos of the chosen Reference DB person —
        multi-photo databases feed every picture into the swap."""
        return self._actor_ref_paths_all()

    def _tip(self, widget, text):
        """Attach a hover tooltip and keep a reference so it isn't collected."""
        if not hasattr(self, "_tooltips"):
            self._tooltips = []
        self._tooltips.append(Tooltip(widget, text))

    def _rag_rng(self):
        """The random draw behind RAG retrieval: a fresh one on every
        Generate when the seed is random (a different person each time),
        fixed by the seed otherwise (the same references again)."""
        try:
            if not self.random_seed_var.get():
                return random.Random(int(str(self.seed_var.get()).strip()))
        except (ValueError, AttributeError):
            pass
        return random.Random()

    def _refresh_mode_badges(self):
        """Colour the top-right LoRA/RAG badges: green only when each will
        actually affect the next generation, red otherwise (editing a loaded
        image, nothing selected/loaded, or a model that can't use it)."""
        if not hasattr(self, "lora_badge"):
            return
        # in image-swap mode a loaded image is the FACE, not an edit
        # target — LoRAs/RAG still drive the base generation
        swap_mode = hasattr(self, "swap_rag_var") \
            and self.swap_rag_var.get() \
            and (bool(getattr(self, "face_paths", [])) or bool(self.actor_sel))
        editing = False   # Edit is its own tab; GENERATE never edits
        model = self._model_raw()
        fam = model_family(model) if model else ""
        sdxl = bool(fam) and fam not in ("flux", "schnell")
        lora_on = (not editing) and bool(self._selected_loras()) \
            and (not swap_mode or self.swap_use_lora_var.get())
        rag_on = (not editing) and (self.ragmap is not None) and sdxl \
            and (not swap_mode or self.swap_use_rag_var.get())
        self.lora_badge.configure(
            style="BadgeOn.TLabel" if lora_on else "BadgeOff.TLabel")
        self.rag_badge.configure(
            style="BadgeOn.TLabel" if rag_on else "BadgeOff.TLabel")
        # log WHY each badge is the colour it is, so a "why is it red?"
        # report can be answered from app.log instead of guessed
        why = []
        if not lora_on:
            why.append("LoRA red: " + ("editing a loaded image"
                       if editing else "no LoRA ticked in the list"
                       if not self._selected_loras()
                       else "swap set to skip LoRA" if swap_mode
                       and not self.swap_use_lora_var.get() else "?"))
        if not rag_on:
            why.append("RAG red: " + ("editing a loaded image" if editing
                       else "no RAG map loaded (or still loading)"
                       if self.ragmap is None
                       else f"model {fam or '?'} is not SDXL" if not sdxl
                       else "swap set to skip RAG" if swap_mode
                       and not self.swap_use_rag_var.get() else "?"))
        try:
            applog.log("badges: LoRA=%s RAG=%s%s"
                       % ("green" if lora_on else "red",
                          "green" if rag_on else "red",
                          "  (" + "; ".join(why) + ")" if why else ""))
        except Exception:
            pass

    def _refresh_editor_state(self):
        """After anything that changes the editor's inputs: enable/disable the
        editor buttons and repaint the mode badges."""
        has_gallery = bool(self.session)
        if hasattr(self, "clone_body"):
            self._apply_clone_enabled()
        if hasattr(self, "face_using"):
            self._refresh_face_list()
        if hasattr(self, "editor_use_btn"):
            self.editor_use_btn.state(
                ["!disabled"] if has_gallery else ["disabled"])
        self._refresh_mode_badges()

    def _update_editor_btn(self):
        self._refresh_editor_state()

    def _reuse_seed(self):
        if self.session:
            _, params, _ = self.session[self.current]
            self.seed_var.set(str(params["seed"]))
            self.random_seed_var.set(False)

    # -------------------------------------------------- generation
    def _generate(self, queue=False, swap_face=None, edit=False):
        if not queue and self._busy_guard():
            return
        if not engine_alive():
            messagebox.showerror("Engine", "Engine is not running yet.")
            return
        # Edit lives on its own tab now: a loaded image only drives an
        # edit when the user pressed "Apply edit" (edit=True). GENERATE
        # ignores it, so LoRA/RAG and face-swap run normally even while an
        # image is sitting on the Edit tab.
        editing0 = edit and bool(self.ref_paths)
        if edit and not self.ref_paths:
            messagebox.showinfo(
                "Edit image", "Load an image on the Edit image tab first "
                              "(\U0001f5bc Load\u2026 or Use selected), "
                              "then Apply edit.")
            return
        ref_paths = self.ref_paths if editing0 else []
        prompt = (self._get(self.edit_prompt_box) if editing0
                  else self._get(self.prompt_box))
        if not prompt:
            if editing0:
                messagebox.showinfo(
                    "Edit", "On the Edit image tab, say what the AI should "
                            "change — type it or pick from Common edits.")
            else:
                messagebox.showinfo("Prompt", "Write a prompt first — or pick "
                                              "a preset and hit 'Try example'.")
            return
        style = self._get(self.style_box)
        # 🔀 Use RAG & LoRA for image swap: two-step run — the styled base
        # generates first, then the face (loaded image, else the chosen
        # person) is applied to it. Qwen is the swap engine when installed
        # (native multi-image = reliable identity); Kontext otherwise.
        swap_editor = "faceswap"
        if isinstance(swap_face, str):
            swap_face = [swap_face]
        if swap_face is None and self.swap_rag_var.get() and not ref_paths:
            swap_face = self._swap_face_source()
            missing = [p for p in swap_face if not Path(p).exists()]
            if missing:
                # the face was a gallery image that has since been deleted:
                # say so and stop, rather than draw a base and skip the swap
                self._forget_deleted_refs({str(p) for p in missing})
                self.status_var.set(
                    "The face image "
                    + ", ".join(Path(p).name for p in missing)
                    + " no longer exists — choose another face (🖼 Browse… "
                      "or 👤 Person…). Nothing was generated.")
                return
            if not swap_face:
                self.status_var.set(
                    "The swap is ticked but no face is chosen — 🖼 Browse… "
                    "for a file or pick a 👤 Person. Generating normally.")
            else:
                # the chosen Clone method decides the engine; each is set up
                # on first use (a one-time download)
                swap_editor = self._clone_method()
                if swap_editor == "faceswap":
                    if not faceswap_ready():
                        if getattr(self, "_faceswap_installing", False):
                            self.status_var.set(
                                "The face-swap engine is still setting up — "
                                "watch the loading strip and Generate again "
                                "when it says ready.")
                            return
                        if messagebox.askyesno(
                                "Set up face swapping",
                                "The Face swap method needs a one-time "
                                "~550 MB setup (a local face-swap engine). It "
                                "then runs in seconds and closely matches the "
                                "person you chose.\n\nInstall it now?"):
                            threading.Thread(
                                target=self._install_face_swap,
                                args=("progress",), daemon=True).start()
                            self.status_var.set(
                                "Setting up the face-swap engine — watch the "
                                "loading strip and Generate again when it "
                                "says ready.")
                            return
                        self.status_var.set(
                            "Face swap needs its one-time setup — nothing was "
                            "generated. Install it when asked, or pick a "
                            "different Method.")
                        return
                else:
                    # a re-rendering editor (Qwen / Kontext)
                    miss = self._editor_missing(swap_editor)
                    if miss:
                        nm = "Qwen" if swap_editor == "qwen" else "Flux Kontext"
                        if messagebox.askyesno(
                                "Download the editor",
                                f"The {nm} editor is needed for this Clone "
                                "method and isn't installed yet.\n\nDownload "
                                "it now?"):
                            threading.Thread(
                                target=self._install_editor,
                                args=(swap_editor, miss, "progress"),
                                daemon=True).start()
                            self.status_var.set(
                                f"Downloading the {nm} editor — Generate "
                                "again when it says done.")
                            return
                        self.status_var.set(
                            "Clone skipped — that editor isn't installed. "
                            "Pick 'Face swap' or install the editor.")
                        return
                    if not self._ensure_editor_ready(swap_editor):
                        return
        # swap_face set = a gen-then-swap run: force a fresh RAG/LoRA
        # generation (the loaded image / person is the face for the later
        # Kontext pass, NOT an edit target or an IP-Adapter guide).
        if ref_paths and not swap_face:
            # a reference is a full override: the image supplies the art
            # style, so the preset's style text (and LoRAs) are not applied
            full_prompt = prompt
        else:
            full_prompt = f"{prompt}, {style}" if style else prompt
        editing = editing0
        editor = self._editor_engine()
        model = self._model_raw()
        if editing:
            if not self._ensure_editor_ready(editor):
                return
            model = f"editor:{editor}"
        else:
            if not model:
                messagebox.showerror("Model", "No model selected — hit ↻ "
                                              "or wait for downloads to "
                                              "finish.")
                return
            tier = self._model_fits.get(model, "ok")
            if tier == "block":
                self._vram_block_msg(model)
                return
            if tier == "warn":
                self.status_var.set("Running close to the VRAM limit — "
                                    "generation will be slower.")
            # the engine must actually know this model, or generation
            # 400s — a stale engine (old model paths) needs a restart
            known = _api_choices("CheckpointLoaderSimple", "ckpt_name")
            if known and model not in known and \
                    (MODELS / "checkpoints" / model).exists():
                if messagebox.askyesno(
                        "Engine restart needed",
                        "The engine was started with old settings and "
                        "can't see this model yet.\n\nRestart the engine "
                        "now? (takes ~a minute, then hit Generate again)"):
                    threading.Thread(target=self._restart_engine,
                                     daemon=True).start()
                return

        strength = round(self.lora_strength.get(), 2)
        loras = [] if editing or (swap_face
                                  and not self.swap_use_lora_var.get()) else \
            [(name, strength) for name in self._selected_loras()]

        # RAG map: retrieve the closest example images for this prompt and
        # feed them as IP-Adapter guidance (SDXL only); auto-apply the
        # paired LoRA + trigger. Falls back to caption text if IP-Adapter
        # isn't installed. Only in plain generation (not editing).
        rag_refs = []
        rag_embed_paths = []
        rag_weight = 0.8
        if self.ragmap and not editing \
                and (not swap_face or self.swap_use_rag_var.get()):
            if model_family(model) in ("flux", "schnell"):
                self.status_var.set("RAG image guidance needs an SDXL model "
                                    "(Juggernaut/DreamShaper) — skipped for "
                                    "this Flux model.")
            else:
                hits = ragmap_retrieve(self.ragmap, prompt,
                                       rng=self._rag_rng())
                rag_weight = float(self.ragmap.get("weight") or 0.8)
                lf = ragmap_lora(self.ragmap)
                if lf and lf not in [n for n, _s in loras]:
                    loras.append((lf, strength))
                trg = self.ragmap.get("trigger", "")
                if trg and trg.lower() not in full_prompt.lower():
                    full_prompt = f"{trg}, {full_prompt}"
                used_refs = False
                if hits and self._style_support_ok():
                    if self.ragmap.get("_embeds_only"):
                        # precomputed IP-Adapter embeds, no viewable images
                        rag_embed_paths = [h["_ipadpt"] for h in hits
                                           if h.get("_ipadpt")]
                        used_refs = bool(rag_embed_paths)
                    else:
                        rag_refs = [h["_path"] for h in hits if h.get("_path")]
                        used_refs = bool(rag_refs)
                if not used_refs:
                    # no IP-Adapter, or the references didn't travel with the
                    # map — use what is there, which is the captions
                    text_hits = hits or ragmap_retrieve(
                        self.ragmap, prompt, require_image=False,
                        rng=self._rag_rng())
                    caps = "; ".join(h.get("caption", "") for h in text_hits
                                     if h.get("caption"))
                    if caps:
                        full_prompt = f"{full_prompt}, {caps}"
                    self.status_var.set(
                        "RAG map applied as text "
                        + ("(this map carries no references)." if not hits else
                           "(install IP-Adapter via 🧭 RAG map… for image "
                           "guidance)."))

        # Reference DB person, plain generation: the photo face-guides via
        # IP-Adapter. Not RAG — no retrieval, the picture is passed
        # straight through. Same engine rules as any image ref: SDXL only.
        # Skipped in a gen-then-swap run: the identity comes from the crisp
        # Kontext swap, so the base is drawn without an approximate face guide.
        if self.actor_sel and not editing and not swap_face:
            if model_family(model) in ("flux", "schnell"):
                self.status_var.set("Person photo needs an SDXL model "
                                    "(Juggernaut/DreamShaper) for plain "
                                    "generations — skipped for this Flux "
                                    "model (or use ➡ To editor).")
            elif not self._style_support_ok():
                self.status_var.set("Person photo skipped — IP-Adapter "
                                    "isn't set up (reload the DB via 📇 "
                                    "Reference DB… to get the install "
                                    "offer).")
            else:
                ap = self._actor_ref_path()
                if ap:
                    rag_refs.append(ap)
                    if rag_embed_paths:
                        # both guide the same generation now: the private
                        # map's embeds and this face photo chain on one
                        # IP-Adapter (see build_graph's combined block)
                        self.status_var.set(
                            "Person photo + private RAG map are both "
                            "guiding this generation (chained IP-Adapter).")

        # Clone with Face swap: also guide the BASE with the chosen face, so
        # the scene is drawn with the person in mind; the face swap then locks
        # the exact identity. SDXL + IP-Adapter only, and skipped silently
        # otherwise (the swap alone still lands the face).
        if swap_face and swap_editor == "faceswap" and not editing \
                and model_family(model) not in ("flux", "schnell") \
                and self._style_support_ok():
            added = False
            for ap in list(swap_face)[:2]:
                if ap and ap not in rag_refs:
                    rag_refs.append(ap)
                    added = True
            if added:
                self.status_var.set("Drawing the scene with your chosen "
                                    "person in mind, then cloning the face in.")

        # Reference DB person, editing: the photo joins the loaded
        # reference images, exactly as if it had been loaded with 🖼 Load…
        edit_refs = [] if swap_face else list(ref_paths)
        if editing and self.actor_sel:
            ap = self._actor_ref_path()
            if ap and ap not in edit_refs:
                cap = 4 if editor == "kontext" else 3
                if len(edit_refs) < cap:
                    edit_refs.append(ap)
                else:
                    self.status_var.set(
                        "Person photo left out — this editor takes at "
                        f"most {cap} images and they are already loaded.")

        # auto-apply each ticked LoRA's activation keyword so the user never
        # has to type it — read from the LoRA's own sidecar/metadata and added
        # to the hidden full prompt (their typed prompt is untouched). The RAG
        # map already prepends its own trigger, so the "already present" check
        # keeps it from being doubled.
        applied_kw = []
        if not editing:
            for lname, _s in loras:
                trg = lora_trigger(lname)
                if trg and trg.lower() not in full_prompt.lower():
                    full_prompt = f"{full_prompt}, {trg}"
                    applied_kw.append(trg)
        if applied_kw:
            self.status_var.set("Applied LoRA keyword(s): "
                                + "; ".join(applied_kw))

        w, h = SIZE_PRESETS[self.size_var.get()]
        try:
            seed = int(self.seed_var.get())
        except ValueError:
            seed = 0
        if self.random_seed_var.get():
            seed = random.randrange(2**32)
            self.seed_var.set(str(seed))
        steps = None if self.steps_var.get() == "auto" else int(self.steps_var.get())

        params = dict(prompt=full_prompt, user_prompt=prompt, style=style,
                      negative=self._get(self.negative_box),
                      model=model, loras=loras, width=w, height=h, seed=seed,
                      steps=steps, cfg=None, batch=self.batch_var.get(),
                      random_seed=self.random_seed_var.get(),
                      transparent=self.transparent_var.get(),
                      upscale=self.upscale_var.get(),
                      hires=self.hires_var.get(),
                      hires_scale=(2.0 if self.hires_scale_var.get()
                                   .startswith("2") else 1.5),
                      freeu=self.freeu_var.get(),
                      preset=self.preset_var.get(),
                      ref_images=edit_refs,
                      rag_ref_paths=rag_refs, rag_embed_paths=rag_embed_paths,
                      style_weight=rag_weight,
                      editor=editor, swap_face=swap_face,
                      swap_editor=swap_editor,
                      out_size=(w, h) if editing
                      and self.editor_canvas_var.get() else None,
                      denoise=round(self.change_var.get() / 100, 2))
        if editing:
            self.status_var.set("Editing with "
                                f"{'Flux Kontext' if editor == 'kontext' else 'Qwen Image Edit'} "
                                "— the prompt is the instruction; preset "
                                "and LoRAs are ignored.")
        else:
            self.settings["last_model"] = model
        self._persist()
        if queue:
            self._enqueue("gen", (prompt or "art")[:38], params)
            return
        CANCEL.clear()
        self.busy = True
        self.go_btn.state(["disabled"])
        self.progress["value"] = 0
        gen = Generator(self.ui_queue)
        threading.Thread(target=gen.run, args=(params,), daemon=True).start()

    def _poll_queue(self):
        # the reschedule in `finally` must survive anything a handler
        # throws, otherwise one bad message would freeze the whole UI
        try:
            while True:
                msg = self.ui_queue.get_nowait()
                try:
                    self._handle_msg(msg)
                except Exception as e:
                    try:
                        self.status_var.set(f"UI error: {e}")
                    except Exception:
                        pass
        except queue_mod.Empty:
            pass
        finally:
            self.root.after(100, self._poll_queue)

    def _handle_msg(self, msg):
                kind = msg[0]
                if kind == "status":
                    self.status_var.set(msg[1])
                elif kind == "progress_mode":
                    # a model is loading: the bar cannot know how far, so it
                    # sweeps; back to a real bar once the steps begin
                    if msg[1] == "loading":
                        self._sweep(self.progress, True)
                        self.pct_var.set("loading…")
                    else:
                        self._sweep(self.progress, False)
                        self.pct_var.set("")
                elif kind == "progress":
                    _, val, mx = msg
                    if self._sweeping.get(str(self.progress)):
                        self._sweep(self.progress, False)
                    self.progress["maximum"] = mx
                    self.progress["value"] = val
                    self.pct_var.set(f"{val * 100 / max(1, mx):.0f}%")
                elif kind == "anim_progress":
                    _, val, mx = msg
                    self.anim_progress["maximum"] = mx
                    self.anim_progress["value"] = val
                    self.anim_pct_var.set(
                        f"{val * 100 / max(1, mx):.0f}%")
                elif kind == "border_progress":
                    _, val, mx = msg
                    self.border_progress["maximum"] = mx
                    self.border_progress["value"] = val
                    self.border_pct_var.set(
                        f"{val * 100 / max(1, mx):.0f}%")
                elif kind == "image":
                    _, img, params = msg
                    threading.Thread(target=self._finish_image,
                                     args=(img, params), daemon=True).start()
                elif kind == "finished_image":
                    _, img, params, path = msg
                    self.session.append((img, params, path))
                    self.current = len(self.session) - 1
                    self._show_current()
                    self._add_thumb(self.current)
                    self._update_editor_btn()
                    self.status_var.set(f"Saved  {path.name}")
                elif kind == "done":
                    self.busy = False
                    self.go_btn.state(["!disabled"])
                    self._sweep(self.progress, False)
                    for bar, var in ((self.progress, self.pct_var),
                                     (self.anim_progress,
                                      self.anim_pct_var),
                                     (self.border_progress,
                                      self.border_pct_var)):
                        bar["value"] = 0
                        var.set("")
                elif kind == "engine_ready":
                    self.status_var.set("Engine ready.")
                    self._refresh_models()
                elif kind == "addon_repair":
                    # a generation hit a broken IP-Adapter add-on — reinstall
                    # it in the background, once per session
                    if msg[1] == "ipadapter" and not getattr(
                            self, "_ipa_repair_started", False):
                        self._ipa_repair_started = True
                        threading.Thread(target=self._install_style_support,
                                         daemon=True).start()
                elif kind == "vram":
                    self.vram_gb = msg[1]
                    self._refresh_models()   # recolor with engine's number
                    self._refresh_editor_list()
                elif kind == "vram_live":
                    used, total = msg[1], msg[2]
                    util = msg[3] if len(msg) > 3 else None
                    self.vram_bar["maximum"] = total
                    self.vram_bar["value"] = used
                    self.vram_label_var.set(
                        f"VRAM {used:,} / {total:,} MB")
                    self.gpu_var.set(f"GPU {util}%" if util is not None
                                     else "GPU —%")
                    self.gpu_bar["value"] = util if util is not None else 0
                elif kind == "models_changed":
                    self._refresh_models()
                elif kind == "model_added":
                    self._refresh_models()
                    self._select_model(msg[1])
                    self.status_var.set(f"Added {msg[1]} — selected and "
                                        "ready.")
                elif kind == "ollama":
                    self._on_ollama_found(msg[1])
                elif kind == "enhanced":
                    _, before, better = msg
                    self.enhance_btn.state(["!disabled"])
                    if better:
                        self._prompt_undo = before
                        self._set(self.prompt_box, better)
                        self.undo_enhance_btn.state(["!disabled"])
                        self.status_var.set(
                            "Prompt enhanced — ↩ puts your wording back.")
                        self._schedule_persist()
                    else:
                        self.status_var.set(
                            "The model didn't return a usable prompt — your "
                            "wording is unchanged. Try a different model.")
                elif kind == "enhance_err":
                    self.enhance_btn.state(["!disabled"])
                    self.status_var.set(f"Prompt enhancer: {msg[1]}")
                elif kind == "app_update":
                    # the startup check: automatic (download now, ask
                    # before restarting) unless the user turned that off
                    self._show_update_window(
                        msg[1], auto=self_update.auto_update())
                elif kind == "app_update_manual":
                    self.upd_btn.state(["!disabled"])
                    if msg[1]:
                        self._show_update_window(msg[1])
                    else:
                        self.status_var.set(
                            f"You are up to date — v{APP_VERSION} is the "
                            "latest release.")
                elif kind == "updates":
                    ups = msg[1]
                    eng = msg[2] if len(msg) > 2 else None
                    lines = [
                        f"  •  {u['local']}   ({u['size'] / (1024**3):.1f} GB)"
                        + ("   — not installed" if u.get("missing")
                           else "   — new release")
                        for u in ups]
                    if eng:
                        lines.append("  •  ComfyUI engine — new version "
                                     "available")
                    total = sum(u["size"] for u in ups) / (1024**3)
                    if messagebox.askyesno(
                            "Updates available",
                            "Newer versions were found for:\n\n"
                            + "\n".join(lines)
                            + f"\n\nModel download: {total:.1f} GB. "
                            "Update now?\n(Updates install in the "
                            "background; the engine restarts briefly if "
                            "it is updated.)"):
                        threading.Thread(target=self._download_updates,
                                         args=(ups, eng),
                                         daemon=True).start()
                elif kind == "pending":
                    self._set_pending(msg[1], msg[2])
                elif kind == "ragmap_progress":
                    self._rag_progress(*msg[1:5])
                elif kind == "ragmap_loaded":
                    gen, path, rag, err, on_done = msg[1:6]
                    if gen == self._ragmap_load_gen:
                        self._rag_progress_done()
                        on_done(path, rag, err)
                    # an older request was superseded by a newer pick: drop
                elif kind == "quit":
                    # after this poll returns, so its reschedule is the
                    # last thing that touches the interpreter
                    self.root.after(0, self.root.destroy)
                elif kind == "batch_done":
                    self.busy = False
                    self.go_btn.state(["!disabled"])
                    self.job_queue = []
                    self._refresh_queue()
                    self.status_var.set(f"Batch complete — {msg[1]} job(s) "
                                        "finished.")
                elif kind == "error":
                    applog.error("shown: " + str(msg[1]))
                    self.busy = False
                    self._sweep(self.progress, False)
                    self.go_btn.state(["!disabled"])
                    self.status_var.set(f"Error: {msg[1]}")

    def _finish_image(self, img, params):
        """Post-process (transparency) + auto-save. Runs off the UI thread."""
        try:
            if params.get("border_cut"):
                img = cut_center(img, params["border_cut"])
            elif params["transparent"]:
                self.ui_queue.put(("status", "Removing background…"))
                img = remove_background(img)
            path = self._autosave(img, params)
            self.ui_queue.put(("finished_image", img, params, path))
        except Exception as e:
            self.ui_queue.put(("error", f"post-process: {e}"))

    def _autosave(self, img, params):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = "border_" if params.get("border_cut") else ""
        name = f"{prefix}{stamp}_seed{params['seed']}.png"
        path = OUTPUT / name
        meta = PngInfo()
        record = {k: params[k] for k in ("user_prompt", "style", "negative",
                                         "model", "loras", "width", "height",
                                         "seed", "steps", "preset",
                                         "transparent")}
        if params.get("ref_images"):
            record["ref_images"] = [Path(p).name
                                    for p in params["ref_images"]]
            record["change"] = params.get("denoise")
        meta.add_text("comic_art_creator", json.dumps(record))
        meta.add_text("parameters", params["prompt"])
        img.save(path, pnginfo=meta)
        return path

    # -------------------------------------------------- preview + gallery
    def _draw_frame(self, img):
        cw = max(self.canvas.winfo_width(), 50)
        ch = max(self.canvas.winfo_height(), 50)
        base = min(cw / img.width, ch / img.height, 1.0)
        if self._zoom <= 1.001:
            disp = img.resize((int(img.width * base), int(img.height * base)),
                              Image.LANCZOS)
            self.canvas.delete("all")
            self._tk_img = ImageTk.PhotoImage(disp)
            self.canvas.create_image(cw // 2, ch // 2, image=self._tk_img)
            return
        # zoomed: crop only the visible window, then scale — fast at any
        # zoom because the output never exceeds the canvas size
        eff = base * self._zoom
        vw, vh = cw / eff, ch / eff
        cx, cy = self._view_c or (img.width / 2, img.height / 2)
        cx = min(max(cx, vw / 2), img.width - vw / 2) \
            if vw < img.width else img.width / 2
        cy = min(max(cy, vh / 2), img.height - vh / 2) \
            if vh < img.height else img.height / 2
        self._view_c = (cx, cy)
        box = (max(0, int(cx - vw / 2)), max(0, int(cy - vh / 2)),
               min(img.width, int(cx + vw / 2) + 1),
               min(img.height, int(cy + vh / 2) + 1))
        crop = img.crop(box)
        disp = crop.resize((max(1, int(crop.width * eff)),
                            max(1, int(crop.height * eff))), Image.LANCZOS)
        self.canvas.delete("all")
        self._tk_img = ImageTk.PhotoImage(disp)
        self.canvas.create_image(cw // 2, ch // 2, image=self._tk_img)

    def _current_frame(self):
        """The image the preview shows right now (gif frame or still)."""
        frames = getattr(self, "_gif_frames", [])
        if frames:
            return frames[(self._gif_idx - 1) % len(frames)]
        if self.current is None or not self.session:
            return None
        return self.session[self.current][0]

    def _canvas_zoom(self, ev):
        img = self._current_frame()
        if img is None:
            return "break"
        cw = max(self.canvas.winfo_width(), 50)
        ch = max(self.canvas.winfo_height(), 50)
        base = min(cw / img.width, ch / img.height, 1.0)
        old = self._zoom
        new = min(8.0, max(1.0, old * (1.25 if ev.delta > 0 else 0.8)))
        if abs(new - old) < 1e-6:
            return "break"
        # keep the image point under the cursor stationary while zooming
        c = self._view_c or (img.width / 2, img.height / 2)
        dx, dy = ev.x - cw / 2, ev.y - ch / 2
        px, py = c[0] + dx / (base * old), c[1] + dy / (base * old)
        if new <= 1.001:
            self._zoom, self._view_c = 1.0, None
        else:
            self._zoom = new
            self._view_c = (px - dx / (base * new), py - dy / (base * new))
        self.canvas.configure(cursor="fleur" if self._zoom > 1 else "")
        self._draw_frame(img)
        return "break"

    def _pan_start(self, ev):
        self._pan_from = (ev.x, ev.y, self._view_c)

    def _pan_move(self, ev):
        if self._zoom <= 1.0 or not getattr(self, "_pan_from", None):
            return
        img = self._current_frame()
        if img is None:
            return
        x0, y0, c0 = self._pan_from
        c0 = c0 or (img.width / 2, img.height / 2)
        cw = max(self.canvas.winfo_width(), 50)
        ch = max(self.canvas.winfo_height(), 50)
        eff = min(cw / img.width, ch / img.height, 1.0) * self._zoom
        self._view_c = (c0[0] - (ev.x - x0) / eff,
                        c0[1] - (ev.y - y0) / eff)
        self._draw_frame(img)     # clamps the view to the image

    def _zoom_reset(self, _ev=None):
        self._zoom, self._view_c = 1.0, None
        self.canvas.configure(cursor="")
        img = self._current_frame()
        if img is not None:
            self._draw_frame(img)

    def _stop_gif(self):
        if getattr(self, "_gif_job", None):
            self.root.after_cancel(self._gif_job)
            self._gif_job = None
        self._gif_frames = []

    def _play_gif(self, path):
        try:
            from PIL import ImageSequence
            im = Image.open(path)
            frames, durs = [], []
            for fr in ImageSequence.Iterator(im):
                frames.append(fr.convert("RGBA"))
                durs.append(max(20, int(fr.info.get("duration", 80))))
        except Exception:
            return False
        if not frames:
            return False
        self._gif_frames, self._gif_durs, self._gif_idx = frames, durs, 0
        self._gif_tick()
        return True

    def _gif_tick(self):
        frames = getattr(self, "_gif_frames", [])
        if not frames:
            return
        i = self._gif_idx % len(frames)
        self._draw_frame(frames[i])
        self._gif_idx += 1
        self._gif_job = self.root.after(self._gif_durs[i], self._gif_tick)

    def _show_current(self):
        self._stop_gif()
        # switching to a different picture resets the zoom; a mere canvas
        # resize (same picture) keeps it
        if self.current != self._zoom_for:
            self._zoom, self._view_c = 1.0, None
            self.canvas.configure(cursor="")
            self._zoom_for = self.current
        self.canvas.delete("all")
        if self.current is None or not self.session:
            self.canvas.create_text(
                self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                text="Your art appears here", fill=FG_DIM,
                font=(UI_FONT, 16))
            return
        img, params, path = self.session[self.current]
        if str(path).lower().endswith(".gif") and Path(path).exists() \
                and self._play_gif(path):
            self.info_var.set(f"{params['model']}  ·  animated GIF  ·  "
                              f"seed {params['seed']}")
            return
        self._draw_frame(img)
        self.info_var.set(f"{params['model'].split('.')[0]}  ·  "
                          f"{img.width}×{img.height}  ·  seed {params['seed']}")

    def _thumb_image(self, img, tagged):
        """The gallery thumbnail; a tagged one wears a red frame and a
        corner mark so what 🗑 Delete will take is visible at a glance."""
        th = img.copy()
        th.thumbnail((84, 84))
        th = th.convert("RGB")
        if tagged:
            d = ImageDraw.Draw(th)
            w, h = th.size
            d.rectangle([0, 0, w - 1, h - 1], outline="#e74c3c", width=3)
            d.rectangle([w - 22, 0, w - 1, 18], fill="#e74c3c")
            d.text((w - 17, 2), "X", fill="white")
        return ImageTk.PhotoImage(th)

    def _add_thumb(self, idx):
        img, _p, path = self.session[idx]
        tk_th = self._thumb_image(img, str(path) in self.tagged)
        btn = ttk.Button(self.gallery, image=tk_th,
                         command=lambda i=idx: self._select(i))
        btn.image = tk_th
        btn.pack(side="left", padx=2)
        # double-click sends the image straight to the Animator
        btn.bind("<Double-Button-1>",
                 lambda _e, i=idx: self._thumb_to_animator(i))
        # Ctrl+click / right-click tag it for deletion (or untag it)
        btn.bind("<Control-Button-1>",
                 lambda _e, i=idx: (self._toggle_tag(i), "break")[1])
        btn.bind("<Button-3>", lambda _e, i=idx: self._toggle_tag(i))
        self._thumb_btns.append(btn)
        # keep the newest thumbnail in view
        self.gallery.update_idletasks()
        self.gallery_canvas.xview_moveto(1.0)

    def _rebuild_gallery(self):
        for child in self.gallery.winfo_children():
            child.destroy()
        self._thumb_btns = []
        live = {str(p) for _i, _p, p in self.session}
        self.tagged &= live          # a tag on an image no longer here is moot
        for idx in range(len(self.session)):
            self._add_thumb(idx)
        if not self.session:
            self.gallery_canvas.xview_moveto(0.0)
        self._refresh_tag_ui()

    # ------------------------------------------------ tagging for deletion
    def _toggle_tag(self, idx):
        """Tag / untag one gallery image for deletion and repaint its
        thumbnail. Tags follow the image PATH, so they survive a gallery
        rebuild and never point at the wrong picture."""
        if idx is None or not (0 <= idx < len(self.session)):
            return
        img, _p, path = self.session[idx]
        key = str(path)
        if key in self.tagged:
            self.tagged.discard(key)
        else:
            self.tagged.add(key)
        if idx < len(self._thumb_btns):
            btn = self._thumb_btns[idx]
            tk_th = self._thumb_image(img, key in self.tagged)
            btn.configure(image=tk_th)
            btn.image = tk_th
        self._refresh_tag_ui()

    def _tag_current(self):
        if self.current is None or not self.session:
            self.status_var.set("Select an image in the gallery first.")
            return
        self._toggle_tag(self.current)

    def _refresh_tag_ui(self):
        """The Delete button says what it will do."""
        if not hasattr(self, "delimg_btn"):
            return
        k = len(self.tagged)
        if k:
            self.delimg_btn.configure(text=f"🗑 Delete {k} tagged")
            self.status_var.set(
                f"{k} image{'s' if k != 1 else ''} tagged — 🗑 deletes exactly "
                "those. Ctrl+click or right-click a thumbnail to tag or untag.")
        else:
            self.delimg_btn.configure(text="🗑 Delete image")
        cur_tagged = (self.current is not None and self.session
                      and str(self.session[self.current][2]) in self.tagged)
        self.tag_btn.configure(text="☐ Untag" if cur_tagged else "☑ Tag")

    def _thumb_anchor(self, idx):
        """The widget a confirmation about image idx should sit above:
        its thumbnail when it is on screen, else the Delete button."""
        if idx is not None and 0 <= idx < len(self._thumb_btns):
            btn = self._thumb_btns[idx]
            if btn.winfo_exists() and btn.winfo_ismapped():
                return btn
        return self.delimg_btn

    def _place_near(self, dlg, anchor):
        """Put a dialog just above an anchor widget (below it when there
        is no room), kept inside the app window — never wherever Windows
        happens to drop a message box."""
        try:
            dlg.update_idletasks()
            w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
            ax, ay = anchor.winfo_rootx(), anchor.winfo_rooty()
            aw, ah = anchor.winfo_width(), anchor.winfo_height()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            x = ax + aw // 2 - w // 2
            y = ay - h - 10
            if y < ry:
                y = ay + ah + 10
            x = max(rx, min(x, rx + max(rw - w, 0)))
            y = max(ry, min(y, ry + max(rh - h, 0)))
            dlg.geometry("+%d+%d" % (x, y))
        except Exception:
            pass

    def _confirm_near(self, anchor, title, text, ok_label="Delete"):
        """A small confirmation box just above `anchor`. Enter confirms,
        Escape cancels. Returns True to go ahead."""
        from tkinter import Label as TkLabel
        dlg = Toplevel(self.root)
        dlg.withdraw()
        dlg.title(title)
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill="both", expand=True)
        TkLabel(frm, text=text, fg="#e74c3c", bg=BG,
                font=(UI_FONT, 10, "bold"), justify="left",
                wraplength=380).pack(anchor="w")
        brow = ttk.Frame(frm)
        brow.pack(fill="x", pady=(12, 0))
        result = {"ok": False}

        def ok(_e=None):
            result["ok"] = True
            dlg.destroy()

        ok_btn = ttk.Button(brow, text=ok_label, style="Danger.TButton",
                            command=ok)
        ok_btn.pack(side="right")
        ttk.Button(brow, text="Cancel",
                   command=dlg.destroy).pack(side="right", padx=(0, 8))
        dlg.bind("<Return>", ok)
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        self._place_near(dlg, anchor)
        dlg.deiconify()
        try:
            dlg.grab_set()
        except Exception:
            pass
        ok_btn.focus_set()
        self.root.wait_window(dlg)
        return result["ok"]

    def _delete_paths(self, paths):
        """Delete these session images from disk and the gallery."""
        gone, failed = [], []
        for p in paths:
            try:
                Path(p).unlink()
                gone.append(str(p))
            except FileNotFoundError:
                gone.append(str(p))
            except OSError as e:
                failed.append(f"{Path(p).name} ({e})")
        goneset = set(gone)
        self.session = [t for t in self.session if str(t[2]) not in goneset]
        self.tagged -= goneset
        # anything still pointing at a deleted picture lets go of it: the
        # editor kept a deleted gallery image as the swap's FACE, and every
        # swap after that was skipped with a one-line status nobody saw
        dropped = self._forget_deleted_refs(goneset)
        self.current = len(self.session) - 1 if self.session else None
        self._rebuild_gallery()
        self._show_current()
        self._update_editor_btn()
        note = (f"Deleted {len(gone)} image{'s' if len(gone) != 1 else ''} — "
                f"{len(self.session)} left in history.")
        if dropped:
            note += " " + dropped
        if failed:
            note += " Could not delete: " + ", ".join(failed)
        self.status_var.set(note)

    def _set_ref_label(self):
        """The editor's reference label, from ref_paths."""
        if not self.ref_paths:
            self.ref_var.set("none — text only")
            return
        first = Path(self.ref_paths[0]).name
        self.ref_var.set(first if len(self.ref_paths) == 1 else
                         f"{len(self.ref_paths)} images ({first}, …)")

    def _forget_deleted_refs(self, goneset):
        """Drop deleted files from the editor, border-maker and animator
        references, repaint their labels, and return a note saying what
        was dropped ("" when nothing was)."""
        goneset = {str(p) for p in goneset}
        notes = []
        if any(str(p) in goneset for p in self.ref_paths):
            self.ref_paths = [p for p in self.ref_paths
                              if str(p) not in goneset]
            self._set_ref_label()
            notes.append("The editor's loaded image was among them — load "
                         "another with 🖼 Load… or Use selected."
                         if not self.ref_paths else
                         "One of the editor's loaded images was among them.")
            self._refresh_editor_state()
        if any(str(p) in goneset for p in getattr(self, "face_paths", [])):
            self.face_paths = [p for p in self.face_paths
                               if str(p) not in goneset]
            self._set_face_label()
            notes.append("The chosen face image was among them.")
            self._refresh_editor_state()
        if any(str(p) in goneset for p in self.border_ref_paths):
            self.border_ref_paths = [p for p in self.border_ref_paths
                                     if str(p) not in goneset]
            if self.border_ref_paths:
                first = Path(self.border_ref_paths[0]).name
                self.border_ref_var.set(
                    first if len(self.border_ref_paths) == 1 else
                    f"{len(self.border_ref_paths)} images ({first}, …)")
            else:
                self.border_ref_var.set("none")
            notes.append("A border reference was among them.")
        if self.anim_image_path and str(self.anim_image_path) in goneset:
            self.anim_image_path = None
            self.anim_img_var.set("none")
            notes.append("The animator's character image was among them.")
        if notes:
            self._schedule_persist()
        return " ".join(notes)

    def _delete_tagged(self):
        paths = [p for _i, _p, p in self.session if str(p) in self.tagged]
        if not paths:
            return
        names = ", ".join(Path(p).name for p in paths[:6])
        if len(paths) > 6:
            names += f", … ({len(paths) - 6} more)"
        anchor_idx = next((i for i, t in enumerate(self.session)
                           if str(t[2]) in self.tagged), None)
        if self.current is not None and self.session \
                and str(self.session[self.current][2]) in self.tagged:
            anchor_idx = self.current
        if not self._confirm_near(
                self._thumb_anchor(anchor_idx), "Delete tagged images",
                f"Permanently delete {len(paths)} tagged image"
                f"{'s' if len(paths) != 1 else ''} from disk?\n{names}",
                ok_label=f"Delete {len(paths)}"):
            return
        self._delete_paths(paths)

    def _delete_current(self):
        """🗑 Delete: the tagged images when any are tagged, else the
        selected one. The confirmation opens just above the thumbnail it
        is about."""
        if self.tagged:
            self._delete_tagged()
            return
        if self.current is None or not self.session:
            self.status_var.set("Select an image in the gallery first — or "
                                "tag several with Ctrl+click / right-click.")
            return
        _img, _params, path = self.session[self.current]
        if not self._confirm_near(
                self._thumb_anchor(self.current), "Delete image",
                f"Permanently delete {Path(path).name} from disk?"):
            return
        self._delete_paths([path])

    def _clear_history(self):
        """Empty the session gallery. Files already saved in output\\ are
        untouched — this only clears the in-app history strip."""
        if not self.session:
            return
        self.session = []
        self.current = None
        self.tagged.clear()
        self._thumb_btns = []
        for child in self.gallery.winfo_children():
            child.destroy()
        self.gallery_canvas.xview_moveto(0.0)
        self._refresh_tag_ui()
        self._show_current()
        self._update_editor_btn()
        self.status_var.set("History cleared — saved images remain in the "
                            "output folder.")

    def _delete_history_files(self):
        """Permanently delete all generated art from disk, after an
        explicit red-text confirmation."""
        files = [f for f in OUTPUT.glob("*.png")] \
            + [f for f in RAW_OUT.glob("*.png")]
        if not files:
            self.status_var.set("No generated images to delete.")
            return
        mb = sum(f.stat().st_size for f in files) / (1 << 20)

        dlg = Toplevel(self.root)
        dlg.withdraw()            # placed above its button before it shows
        dlg.title("Delete generated art")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        frm = ttk.Frame(dlg, padding=18)
        frm.pack(fill="both", expand=True)
        from tkinter import Label as TkLabel
        TkLabel(frm, text="⚠  This permanently deletes "
                          f"{len(files)} generated image"
                          f"{'s' if len(files) != 1 else ''} "
                          f"({mb:.0f} MB) from the output folder.\n"
                          "This cannot be undone.",
                fg="#e74c3c", bg=BG, font=(UI_FONT, 10, "bold"),
                justify="left", wraplength=420).pack(anchor="w")
        brow = ttk.Frame(frm)
        brow.pack(fill="x", pady=(14, 0))

        def do_delete():
            n = 0
            for f in files:
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
            self._clear_history()
            dropped = self._forget_deleted_refs({str(f) for f in files})
            self.status_var.set(f"Deleted {n} image files permanently."
                                + (" " + dropped if dropped else ""))
            dlg.destroy()

        ttk.Button(brow, text="Delete permanently", style="Danger.TButton",
                   command=do_delete).pack(side="right")
        ttk.Button(brow, text="Cancel",
                   command=dlg.destroy).pack(side="right", padx=(0, 8))
        self._place_near(dlg, getattr(self, "delfiles_btn", self.root))
        dlg.deiconify()
        dlg.grab_set()

    def _select(self, idx):
        self.current = idx
        self._show_current()

    def _save_as(self):
        if self.current is None:
            return
        _img, params, src = self.session[self.current]
        is_gif = str(src).lower().endswith(".gif")
        ext = ".gif" if is_gif else ".png"
        ftypes = [("GIF animation", "*.gif")] if is_gif \
            else [("PNG image", "*.png")]
        path = filedialog.asksaveasfilename(
            defaultextension=ext, filetypes=ftypes,
            initialfile=f"comic_seed{params['seed']}{ext}")
        if path:
            shutil.copy2(src, path)
            self.status_var.set(f"Saved to {path}")

    # -------------------------------------------------- border maker
    def _generate_border(self, queue=False):
        """Generate a themed 4:3 / 16:9 border frame with a transparent
        center — for overlays, bezels and framing. No text allowed."""
        theme = self._get(self.border_prompt_box)
        if not theme:
            messagebox.showinfo("Border prompt", "Describe the border first "
                                "— a short theme ('haunted forest, gnarled "
                                "branches') or a full precise prompt.")
            return
        if not queue and self._busy_guard():
            return
        if not engine_alive():
            messagebox.showerror("Engine", "Engine is not running yet.")
            return
        w, h = BORDER_SIZES[self.border_aspect_var.get()]
        refs = list(self.border_ref_paths)
        editor = self._editor_engine()
        base = BORDER_TEMPLATE.format(theme=theme) \
            if self.border_auto_var.get() else theme
        sty = self._border_style()
        if sty:
            base += ", " + sty["style"]
        if refs:
            # references go through the image editor: it redraws them as
            # the border, carrying over style, characters and composition
            if not self._ensure_editor_ready(editor, "border_progress"):
                return
            prompt = "redraw this image as " + base
            model = f"editor:{editor}"
            loras = []
        else:
            prompt = base
            model = self._border_model_raw()
            if not model:
                messagebox.showerror("Model", "No model selected — pick "
                                              "one in the Border maker or "
                                              "the Image generation tab.")
                return
            have_border_lora = BORDER_LORA_FILE in list_loras()
            # the trained border LoRA is SDXL-only and is what makes a real
            # frame — if it's installed but the chosen model is Flux, route
            # the border to the best SDXL checkpoint so the LoRA applies
            if have_border_lora and model_family(model) in ("flux", "schnell"):
                sdxl = self._best_sdxl_model()
                if sdxl:
                    model = sdxl
                    self.status_var.set(
                        f"Border maker using {sdxl} + the trained frame "
                        "LoRA (Flux can't use it).")
            if self._model_fits.get(model, "ok") == "block":
                self._vram_block_msg(model)
                return
            strength = round(self.lora_strength.get(), 2)
            loras = [(n, strength) for n in self._selected_loras()]
            if have_border_lora and \
                    model_family(model) not in ("flux", "schnell"):
                if BORDER_LORA_FILE not in [n for n, _s in loras]:
                    loras.append((BORDER_LORA_FILE, 0.8))
                prompt = f"{BORDER_TRIGGER}, " + prompt
        seed = random.randrange(2**32) if self.random_seed_var.get() \
            else int(self.seed_var.get() or 0)
        steps = None if self.steps_var.get() == "auto" \
            else int(self.steps_var.get())
        # 2nd-pass center clean-up needs Kontext installed and fitting VRAM
        clean = bool(self.border_clean_var.get() and not refs
                     and not self._editor_missing("kontext")
                     and self._editor_tier("kontext") != "block")
        if self.border_clean_var.get() and not refs and not clean:
            self.status_var.set("Auto-clean center off: Flux Kontext isn't "
                                "installed or won't fit VRAM.")
        params = dict(
            prompt=prompt, user_prompt=theme, style="border frame",
            negative=BORDER_NEGATIVE, model=model, loras=loras,
            width=w, height=h, seed=seed, steps=steps, cfg=None,
            batch=max(1, min(10, self.border_batch_var.get())),
            random_seed=self.random_seed_var.get(),
            transparent=False, preset="border maker",
            upscale=self.upscale_var.get(),
            border_cut=int(self.border_thick_var.get()),
            border_clean=clean,
            ref_images=refs, editor=editor,
            ref_collage_size=(w, h) if refs else None,
            out_size=(w, h) if refs else None)
        if queue:
            self._enqueue("border", "border: " + theme[:30], params)
            return
        CANCEL.clear()
        self.busy = True
        self.go_btn.state(["disabled"])
        self.border_progress["value"] = 0
        gen = Generator(ChannelQueue(self.ui_queue, "border_progress"))
        threading.Thread(target=gen.run, args=(params,),
                         daemon=True).start()
        self.status_var.set("Generating border…")

    # -------------------------------------------------- image editor
    EDITOR_FILES = {
        "kontext": [("diffusion_models", KONTEXT_FILE)],
        "qwen": [("diffusion_models", QWEN_EDIT_FILE),
                 ("text_encoders", QWEN_TE_FILE),
                 ("vae", QWEN_VAE_FILE)],
        "wan": [("diffusion_models", WAN_FILE),
                ("text_encoders", WAN_TE_FILE),
                ("vae", WAN_VAE_FILE)],
        "wanflf": [("diffusion_models", WAN_FLF_FILE),
                   ("text_encoders", WAN_TE_FILE),
                   ("clip_vision", WAN_CLIPVIS_FILE),
                   ("vae", WAN21_VAE_FILE)],
    }
    EDITOR_UNETS = {"kontext": KONTEXT_FILE, "qwen": QWEN_EDIT_FILE,
                    "wan": WAN_FILE, "wanflf": WAN_FLF_FILE}

    def _editor_missing(self, engine):
        missing = [(d, f) for d, f in self.EDITOR_FILES[engine]
                   if not (MODELS / d / f).exists()]
        if engine == "kontext" and \
                not (MODELS / "checkpoints" / FLUX_CLIP_SRC).exists():
            missing.append(("checkpoints", FLUX_CLIP_SRC))
        return missing

    def _install_editor(self, engine, missing, channel="progress"):
        try:
            entries = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
        except Exception:
            entries = []
        for d, f in missing:
            e = next((x for x in entries
                      if x["dir"] == d and x["local"] == f), None)
            if not e:
                self.ui_queue.put(("error", f"no download source for {f}"))
                return
            self.ui_queue.put(("status", f"Downloading {f}…"))
            try:
                download_model_update(
                    dict(e, size=0),
                    lambda s: self.ui_queue.put(("status", s)),
                    lambda d, t: self.ui_queue.put((channel, d, t)))
            except Exception as ex:
                self.ui_queue.put(("error", f"download failed: {ex}"))
                return
        self.ui_queue.put(("status", "Editor installed — hit Generate "
                                     "again."))

    def _ensure_editor_ready(self, editor, channel="progress"):
        """True when the editor can run now; otherwise guides the user
        (VRAM block, download offer, engine restart) and returns False."""
        tier = self._editor_tier(editor)
        if tier == "block":
            messagebox.showwarning(
                "Not enough GPU memory",
                f"This editor needs about {EDITOR_VRAM.get(editor, 16)} "
                f"GB of VRAM — your card has {self.vram_gb:.1f} GB.")
            return False
        if tier == "warn":
            self.status_var.set("Running close to the VRAM limit — "
                                "expect slower processing.")
        missing = self._editor_missing(editor)
        if missing:
            gb = "12" if editor == "kontext" else "28"
            if messagebox.askyesno(
                    "Install editor",
                    f"The image editor needs {len(missing)} model "
                    f"file(s) (~{gb} GB) that aren't installed yet."
                    "\n\nDownload now? Watch the status bar; hit "
                    "Generate again when it says done."):
                threading.Thread(target=self._install_editor,
                                 args=(editor, missing, channel),
                                 daemon=True).start()
            return False
        if not self._engine_knows_editor(editor):
            self.status_var.set("Restarting engine to load the editor "
                                "files — wait for 'Engine ready.', "
                                "then Generate again.")
            threading.Thread(target=self._restart_engine,
                             daemon=True).start()
            return False
        return True

    def _engine_knows_editor(self, engine):
        try:
            unets = _api_choices("UNETLoader", "unet_name")
        except Exception:
            return True   # engine not up yet; boot flow handles that
        return self.EDITOR_UNETS.get(engine, KONTEXT_FILE) in unets

    # -------------------------------------------------- style add-on
    def _style_support_ok(self):
        """IP-Adapter node loaded in the engine AND its models on disk."""
        try:
            api_get("/object_info/IPAdapterUnifiedLoader")
            node_ok = True
        except Exception:
            node_ok = False
        return node_ok and bool(scan_models("ipadapter")) \
            and bool(scan_models("clip_vision"))

    def _install_face_swap(self, channel="progress"):
        """One-time setup of the local face-swap engine, off the UI thread:
        insightface into the engine venv, the inswapper model, and the
        buffalo_l detector. Guarded so it can't double-run. Writes a .ready
        marker only when everything is verified."""
        if not self._addon_lock.acquire(blocking=False):
            self.ui_queue.put(("status", "The face-swap engine is already "
                                         "setting up — watch the strip."))
            return
        self._faceswap_installing = True
        self.ui_queue.put(("pending", "faceswap", "setting up face swap"))
        try:
            INSIGHTFACE_DIR.mkdir(parents=True, exist_ok=True)
            self.ui_queue.put(("status", "Installing the face-swap engine…"))
            r = subprocess.run(
                [str(engine_python()), "-m", "pip", "install",
                 "insightface==2.0", "onnx"],
                capture_output=True, text=True, creationflags=NO_WINDOW,
                env=_contained_env(), timeout=1800)
            if r.returncode != 0:
                raise RuntimeError("could not install insightface: "
                                   + (r.stderr or r.stdout or "")[-300:])
            dest = INSIGHTFACE_DIR / INSWAPPER_FILE
            if not (dest.exists() and _sha256(dest) == INSWAPPER_SHA256):
                self.ui_queue.put(("status", "Downloading the face-swap model "
                                             "(~550 MB, one time)…"))
                download_stream(
                    INSWAPPER_URL, dest,
                    lambda d, t: self.ui_queue.put(
                        ("pending", "faceswap",
                         f"downloading ({d * 100 // t}%)")))
                if _sha256(dest) != INSWAPPER_SHA256:
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                    raise RuntimeError(
                        "the downloaded face-swap model failed its integrity "
                        "check (SHA-256 mismatch) — not installed.")
            self.ui_queue.put(("status", "Downloading the face detector…"))
            d = subprocess.run(
                [str(engine_python()), "-c", _FACESWAP_INIT_CODE,
                 str(INSIGHTFACE_DIR)],
                capture_output=True, text=True, creationflags=NO_WINDOW,
                env=_contained_env(), timeout=600)
            if "DET_OK" not in (d.stdout or ""):
                raise RuntimeError("the face detector did not download: "
                                   + (d.stderr or d.stdout or "")[-300:])
            (INSIGHTFACE_DIR / ".ready").write_text("ok", encoding="utf-8")
            self.ui_queue.put(("status", "Face-swap engine ready — Generate "
                                         "again to clone the face."))
        except Exception as e:
            applog.exception("face-swap setup failed")
            self.ui_queue.put(("error", f"Face-swap setup failed: {e}"))
        finally:
            self._faceswap_installing = False
            self.ui_queue.put(("pending", "faceswap", None))
            self._addon_lock.release()

    def _install_style_support(self):
        """Self-install the IP-Adapter node + models, then restart the
        engine — for installs that predate v1.3.0, and for the one an
        engine update stripped. One at a time: the boot-time heal and a
        user's click used to run this twice at once on ONE shared temp
        folder, and the second run died on mkdir."""
        if not self._addon_lock.acquire(blocking=False):
            self.ui_queue.put(("status", "IP-Adapter is already being "
                                         "installed — wait for 'Engine "
                                         "ready.'"))
            return
        self.ui_queue.put(("pending", "addons", "installing IP-Adapter"))
        try:
            node_dir = ENGINE_DIR / "custom_nodes" / "ComfyUI_IPAdapter_plus"
            if not node_dir.exists():
                self.ui_queue.put(("status", "Installing IP-Adapter node…"))
                engine_files.install_node_zip(
                    IPA_NODE_ZIP, node_dir,
                    lambda s: self.ui_queue.put(("status", s)))
            try:
                entries = json.loads(
                    MANIFEST_FILE.read_text(encoding="utf-8"))
            except Exception:
                entries = []
            for e in entries:
                if e["dir"] in ("ipadapter", "clip_vision") and \
                        not (MODELS / e["dir"] / e["local"]).exists():
                    self.ui_queue.put(("status",
                                       f"Downloading {e['local']}…"))
                    download_model_update(
                        dict(e, size=0),
                        lambda s: self.ui_queue.put(("status", s)))
            self.ui_queue.put(("status",
                               "Restarting engine with IP-Adapter…"))
            kill_engine()
            time.sleep(2)
            threading.Thread(target=self._boot_engine, daemon=True).start()
            self.ui_queue.put(("status", "IP-Adapter installed — wait for "
                                         "'Engine ready.', then Generate "
                                         "again."))
        except Exception as e:
            applog.exception("IP-Adapter install failed")
            self.ui_queue.put(("error", f"IP-Adapter install failed: {e}"))
        finally:
            self.ui_queue.put(("pending", "addons", None))
            self._addon_lock.release()

    # -------------------------------------------------- animator
    def _pick_anim_image(self):
        path = filedialog.askopenfilename(filetypes=[
            ("Images", "*.png;*.jpg;*.jpeg;*.webp"), ("All files", "*.*")])
        if path:
            self.anim_image_path = path
            self.anim_img_var.set(Path(path).name)
            self._schedule_persist()

    def _thumb_to_animator(self, idx):
        if idx >= len(self.session):
            return
        self.current = idx
        self._show_current()
        _img, _params, path = self.session[idx]
        self.anim_image_path = str(path)
        self.anim_img_var.set(Path(path).name)
        self.status_var.set(f"{Path(path).name} loaded into the Animator — "
                            "describe an action and hit 🎬.")
        self._schedule_persist()

    def _use_current_for_anim(self):
        if self.current is None or not self.session:
            self.status_var.set("Select an image in the gallery first.")
            return
        _img, _params, path = self.session[self.current]
        self.anim_image_path = str(path)
        self.anim_img_var.set(Path(path).name)
        self._schedule_persist()

    def _clear_anim_image(self):
        self.anim_image_path = None
        self.anim_img_var.set("none")
        self._schedule_persist()

    def _on_anim_preset(self, _event=None):
        txt = ANIM_PRESETS.get(self.anim_preset_var.get())
        if txt:
            self._set(self.anim_prompt_box, txt)
            self._schedule_persist()

    def _generate_animation(self, queue=False):
        if not queue and self._busy_guard():
            return
        if not engine_alive():
            messagebox.showerror("Engine", "Engine is not running yet.")
            return
        if not self.anim_image_path or \
                not Path(self.anim_image_path).exists():
            messagebox.showinfo("Character", "Load a character image first "
                                             "(🖼 Character… or Use "
                                             "current).")
            return
        action = self._get(self.anim_prompt_box)
        if not action:
            messagebox.showinfo("Action", "Describe the action first — "
                                          "e.g. 'walk cycle', 'sword "
                                          "slash', 'idle breathing, cape "
                                          "swaying'.")
            return
        if not self._ensure_editor_ready("wan", "anim_progress"):
            return
        w, h = ANIM_SIZES[self.anim_size_var.get()]
        secs = max(1, min(5, self.anim_secs_var.get()))
        base_fps = 24
        seed = random.randrange(2**32) if self.random_seed_var.get() \
            else int(self.seed_var.get() or 0)
        p = dict(image=self.anim_image_path, action=action, w=w, h=h,
                 length=base_fps * secs + 1, base_fps=base_fps,
                 keep_every=ANIM_KEEP.get(self.anim_keep_var.get(), 2),
                 loop=self.anim_loop_var.get(),
                 transparent=self.anim_transparent_var.get(),
                 gif=self.anim_gif_var.get(),
                 motion=self.anim_motion_var.get(),
                 zip=self.anim_zip_var.get(),
                 sheet=self.anim_sheet_var.get(),
                 video=self.anim_video_var.get(), seed=seed)
        if queue:
            self._enqueue("anim", "anim: " + action[:30], p)
            return
        CANCEL.clear()
        self.busy = True
        self.go_btn.state(["disabled"])
        self.progress["value"] = 0
        self._persist()
        threading.Thread(target=self._run_animation, args=(p,),
                         daemon=True).start()

    def _run_animation(self, p):
        try:
            status = lambda s: self.ui_queue.put(("status", s))
            status("Animating — uploading character…")
            gen = Generator(ChannelQueue(self.ui_queue, "anim_progress"))
            # auto-prep: isolate the character and stage it on neutral
            # gray — video models animate poorly on black voids/cutouts
            status("Preparing character (background staging)…")
            src_img = Image.open(p["image"]).convert("RGBA")
            cut = remove_background(src_img)
            stage = Image.new("RGBA", cut.size, (200, 200, 205, 255))
            stage.alpha_composite(cut)
            name = gen._upload_pil(stage.convert("RGB"),
                                   "cbac_anim_prepped.png")
            motion = ANIM_MOTION.get(p.get("motion", ""),
                                     list(ANIM_MOTION.values())[0])
            action = p["action"]
            if len(action.split()) <= 3:
                # terse actions ("walking") generate weak motion — expand
                # them into an explicit full-body cycle
                action = (f"the character is {action}, performing the "
                          "motion as a clear full-body cycle with large "
                          "limb movements repeating continuously")
            prompt = (f"{action}. {motion}. The character stays "
                      "centered in frame against a flat plain solid "
                      "background, full body always visible, locked "
                      "camera, no camera movement, no scene change.")
            gp = dict(prompt=prompt, anim_image_name=name, width=p["w"],
                      height=p["h"], length=p["length"], seed=p["seed"])
            graph = build_wan_graph(gp)
            import websocket
            ws = websocket.WebSocket()
            ws.connect(f"ws://{ENGINE_HOST}:{ENGINE_PORT}/ws"
                       f"?clientId={gen.client_id}", timeout=30)
            try:
                r = requests.post(f"{ENGINE_URL}/prompt",
                                  json={"prompt": graph,
                                        "client_id": gen.client_id},
                                  timeout=30)
                if r.status_code == 400:
                    raise RuntimeError("engine rejected the animation "
                                       "workflow — try again after the "
                                       "engine restarts")
                r.raise_for_status()
                pid = r.json()["prompt_id"]
                status(f"Animating {p['length']} frames — takes a few "
                       "minutes…")
                metas = gen._await_images(ws, pid, timeout=2400)  # live %
            finally:
                ws.close()
            if CANCEL.is_set():
                status("Cancelled — the animation was stopped.")
                return
            if not metas:
                raise RuntimeError("animation produced no frames")
            status(f"Fetching {len(metas)} frames…")
            frames = [gen._fetch_image(m) for m in metas]
            kept = frames[::p["keep_every"]]
            if p["loop"].startswith("Seamless"):
                kept = best_loop_cut(kept)

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            slug = re.sub(r"[^a-z0-9]+", "_",
                          p["action"].lower())[:30].strip("_") or "anim"
            out_dir = OUTPUT / "animations" / f"{stamp}_{slug}"
            frames_dir = out_dir / "frames"
            frames_dir.mkdir(parents=True, exist_ok=True)

            if p["transparent"]:
                status("Removing backgrounds from "
                       f"{len(kept)} frames…")
                raw_dir = out_dir / "_raw_frames"
                raw_dir.mkdir(parents=True, exist_ok=True)
                for i, f in enumerate(kept):
                    f.save(raw_dir / f"frame_{i:03d}.png")
                remove_background_dir(raw_dir, frames_dir)
                status("Cleaning frame edges (defringe)…")
                raw_files = sorted(raw_dir.glob("*.png"))
                bgs = [estimate_bg(Image.open(rf)) for rf in raw_files]
                kept = []
                for i, fp in enumerate(sorted(frames_dir.glob("*.png"))):
                    bg = bgs[i] if i < len(bgs) else STAGE_BG
                    f = defringe(Image.open(fp).convert("RGBA"), bg=bg)
                    f.save(fp)   # frames on disk get clean edges too
                    kept.append(f)
                shutil.rmtree(raw_dir, ignore_errors=True)
            else:
                for i, f in enumerate(kept):
                    f.save(frames_dir / f"frame_{i:03d}.png")

            result_path = frames_dir / "frame_000.png"
            looped = kept if p["loop"].startswith("Seamless") \
                else apply_loop(kept, p["loop"])
            fps_out = p.get("base_fps", 24) / p["keep_every"]
            extras = []
            if p.get("gif", True):
                result_path = out_dir / "animation.gif"
                save_gif(looped, result_path, fps_out, p["transparent"])
                extras.append("GIF")
            if p.get("sheet"):
                try:
                    status("Packing sprite sheet…")
                    save_sprite_sheet(kept, out_dir / "spritesheet.png",
                                      out_dir / "spritesheet.json", fps_out)
                    extras.append("sprite sheet")
                except Exception as e:
                    status(f"Sprite sheet skipped ({e}).")
            vid = (p.get("video") or "none").lower()
            if vid in ("mp4", "webm"):
                try:
                    status(f"Encoding {vid.upper()} video…")
                    vpath = out_dir / f"animation.{vid}"
                    save_video(looped, vpath, fps_out, webm=(vid == "webm"))
                    extras.append(vid.upper())
                except Exception as e:
                    status(f"{vid.upper()} export skipped ({e}).")
            if p["zip"]:
                shutil.make_archive(str(out_dir), "zip", out_dir)
            self.ui_queue.put(("finished_image", kept[0].copy(),
                              dict(model="animator", seed=p["seed"]),
                              result_path))
            status(f"Animation done: {len(kept)} frames"
                   + ("".join(f" + {e}" for e in extras))
                   + f" in {out_dir.name}"
                   + (" (+zip)" if p["zip"] else "")
                   + ". Select it in the gallery to watch it play.")
            self.ui_queue.put(("done", None))
        except Exception as e:
            self.ui_queue.put(("error", f"animation: {e}"))

    # -------------------------------------------------- training dataset
    def _add_to_training(self):
        """Copy the current image + a caption into training\\dataset —
        the raw material for training your own style LoRA (see
        TRAINING.md). The dataset is the asset that carries your style
        to any current or future model."""
        if self.current is None or not self.session:
            self.status_var.set("Generate or select an image first.")
            return
        _img, params, path = self.session[self.current]
        ds = PROJECT / "training" / "dataset"
        ds.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = ds / f"{stamp}_{path.stem}.png"
        shutil.copy2(path, dest)
        caption = params.get("prompt") or params.get("user_prompt") \
            or "comic book style artwork"
        dest.with_suffix(".txt").write_text(caption, encoding="utf-8")
        self.status_var.set(
            f"Added to training set ({len(list(ds.glob('*.png')))} images) "
            "— captions are editable .txt files; see TRAINING.md.")

    # -------------------------------------------------- civitai loras
    def _civitai_dialog(self):
        dlg = Toplevel(self.root)
        dlg.title("Download LoRA from CivitAI")
        dlg.configure(bg=BG)
        dlg.geometry("560x220")
        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        ttk.Label(frm, text="CivitAI model page URL:").grid(row=0, column=0,
                                                            sticky=W)
        url_var = StringVar()
        ttk.Entry(frm, textvariable=url_var).grid(row=0, column=1, sticky=NSEW,
                                                  padx=6, pady=4)
        ttk.Label(frm, text="API key (free, from civitai.com "
                            "account settings):").grid(row=1, column=0, sticky=W)
        key_var = StringVar(value=dpapi_decrypt(
            self.settings.get("civitai_key_enc", "")))
        ttk.Entry(frm, textvariable=key_var, show="•").grid(row=1, column=1,
                                                            sticky=NSEW, padx=6,
                                                            pady=4)
        out_var = StringVar(value="")
        ttk.Label(frm, textvariable=out_var, style="Dim.TLabel",
                  wraplength=520).grid(row=3, column=0, columnspan=2, sticky=W,
                                       pady=6)

        def go():
            self.settings["civitai_key_enc"] = dpapi_encrypt(key_var.get().strip())
            self.settings.pop("civitai_key", None)  # remove any legacy plaintext
            self._save_settings()
            threading.Thread(target=self._civitai_download,
                             args=(url_var.get().strip(), key_var.get().strip(),
                                   out_var), daemon=True).start()

        ttk.Button(frm, text="Download to models\\loras",
                   command=go).grid(row=2, column=1, sticky=E, pady=4)

    def _civitai_download(self, url, key, out_var):
        try:
            out_var.set("Resolving…")
            m = re.search(r"models/(\d+)", url)
            if not m:
                out_var.set("Could not find a model id in that URL.")
                return
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            info = requests.get(f"https://civitai.com/api/v1/models/{m.group(1)}",
                                headers=headers, timeout=30).json()
            ver = info["modelVersions"][0]
            f = next((f for f in ver["files"]
                      if f["name"].endswith(".safetensors")), None)
            if f is None:
                out_var.set("No .safetensors file in that model — refusing "
                            "other formats (pickle files can execute code).")
                return
            # strip any path components the server sends — basename only
            fname = Path(f["name"]).name
            if not fname.endswith(".safetensors"):
                out_var.set("Unexpected file name — aborted.")
                return
            out_var.set(f"Downloading {fname} ({f['sizeKB'] / 1024:.0f} MB)…")
            if not str(f["downloadUrl"]).startswith("https://"):
                out_var.set("Refusing non-HTTPS download URL — aborted.")
                return
            r = requests.get(f["downloadUrl"], headers=headers, stream=True,
                             timeout=60)
            r.raise_for_status()
            dest = (MODELS / "loras" / fname).resolve()
            if dest.parent != (MODELS / "loras").resolve():
                out_var.set("Bad destination path — aborted.")
                return
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
            # save the trained words beside the file so the app can inject the
            # activation keyword automatically when the LoRA is ticked
            words = ver.get("trainedWords") or []
            if isinstance(words, list) and words:
                try:
                    side = dest.with_suffix(".civitai.info")
                    side.write_text(json.dumps({"trainedWords": words}),
                                    encoding="utf-8")
                except OSError:
                    pass
            _LORA_TRIGGER_CACHE.pop(fname, None)   # re-read on next use
            kw = (", ".join(words[:4])) if words else ""
            out_var.set(f"Done — {fname}. Hit ↻ next to the model box to "
                        f"see it in the LoRA lists."
                        + (f" Trigger: {kw}" if kw else ""))
            self.ui_queue.put(("status", f"LoRA installed: {fname}"
                               + (f" (auto-trigger: {kw})" if kw else "")))
        except Exception as e:
            out_var.set(f"Download failed: {e}")

    # -------------------------------------------------- settings
    def _load_settings(self):
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_settings(self):
        SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2),
                                 encoding="utf-8")


def _app_icon_path():
    """The app icon on disk: the bundled copy when frozen, else the source
    one. Used for the window/taskbar icon (the exe's own icon is set at
    build time from the same file)."""
    base = Path(getattr(sys, "_MEIPASS", str(APP_DIR)))
    p = base / "icon.ico"
    return p if p.exists() else APP_DIR / "icon.ico"


def main():
    # a stable AppUserModelID so Windows groups and pins the app under its
    # own icon (separate from the exe icon and the window icon — all three
    # must be set for the icon to be consistent everywhere)
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "TheSaltTrader.AIImageGeneratorSuite")
    except Exception:
        pass
    root = Tk()
    try:
        root.iconbitmap(default=str(_app_icon_path()))
    except Exception:
        pass
    _mutex_handle, already = single_instance_handle()
    if already:
        # a copy that is closing hides its window at once and frees the
        # mutex a few seconds later (engine shutdown); a copy an update just
        # replaced is on its way out too. Wait for either — with a small
        # window that says so — before asking about a second copy.
        leaving = previous_instance_pids()
        root.title("AI Image Generator Suite")
        root.geometry("480x110")
        _wait = ttk.Label(root, text="Waiting for the previous copy to "
                                     "finish closing…", padding=(24, 38))
        _wait.pack()
        root.update()
        _mutex_handle, already = wait_for_previous_instance(
            leaving, timeout=30 if leaving else 12, tick=root.update)
        _wait.destroy()
    if already:
        from tkinter import messagebox as _mb
        if not _mb.askyesno(
                "Already running",
                "AI Image Generator Suite appears to be already "
                "running.\n\n"
                "Running a second copy can make generations and progress go "
                "to the wrong window, and both share one engine.\n\n"
                "Open another window anyway?"):
            root.destroy()
            return
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
