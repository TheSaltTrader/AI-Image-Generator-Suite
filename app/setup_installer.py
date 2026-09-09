r"""AI Image Generator Suite - self-contained setup (Setup.exe).

Bootstraps everything on a bare 64-bit Windows 10/11 machine - no Python,
no git, no anything pre-installed:
  1. downloads the official embeddable Python runtime into <project>\python
     (the same approach ComfyUI's portable builds use)
  2. downloads the ComfyUI engine as a zip
  3. installs PyTorch cu128 + engine requirements into the bundled runtime
  4. downloads / updates the starter models listed in
     app\models_manifest.json, always against their LATEST releases

Everything - including temp files and caches - stays inside the folder
Setup.exe runs from.

Also runnable headless:  Setup.exe --cli [--skip-models]
(progress is written to setup.log next to the exe).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

import requests

if getattr(sys, "frozen", False):
    PROJECT = Path(sys.executable).resolve().parent
else:
    PROJECT = Path(__file__).resolve().parent.parent

MANIFEST = PROJECT / "app" / "models_manifest.json"
TMP_DIR = PROJECT / "_setup_tmp"   # keep even transient files in-folder


def _safe_extractall(zf, dest):
    """Extract a zip, refusing any member that would land outside dest — the
    zip-slip guard the runtime extractors already have, here too."""
    dest = Path(dest).resolve()
    for m in zf.namelist():
        if not str((dest / m).resolve()).startswith(str(dest)):
            raise RuntimeError("unsafe path in archive: " + m)
    zf.extractall(dest)
PIP_ENV = dict(os.environ, PIP_NO_CACHE_DIR="1")  # no %LOCALAPPDATA% cache
COMFY_ZIP = ("https://github.com/comfyanonymous/ComfyUI/"
             "archive/refs/heads/master.zip")
PY_EMBED_URL = ("https://www.python.org/ftp/python/3.12.10/"
                "python-3.12.10-embed-amd64.zip")
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

BG, BG2, FG, DIM, OK = "#17171c", "#2a2a38", "#e8e8f0", "#9a9ab0", "#4ecca3"


# ==========================================================================
# install core - UI-agnostic; callbacks: log(str), status(str),
# progress(done, total)
# ==========================================================================

def engine_installed():
    return ((PROJECT / "ComfyUI" / "main.py").exists()
            and (PROJECT / "python" / "python.exe").exists())


def models_needed():
    """Manifest entries that are missing OR outdated vs the latest release
    on HuggingFace - the exact same check the app uses at startup, so Setup
    and the app always agree on what 'installed' means.

    Offline fallback: if HuggingFace is unreachable, existing files count
    as current (never block an install on a network hiccup)."""
    try:
        entries = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return []
    needed, repo_cache = [], {}
    for e in entries:
        local = PROJECT / "models" / e["dir"] / e["local"]
        if e.get("url"):   # direct-URL entry (our own trained LoRA)
            size = 0
            try:
                h = requests.head(e["url"], timeout=20, allow_redirects=True)
                size = int(h.headers.get("content-length", 0))
            except requests.RequestException:
                pass
            if not local.exists():
                needed.append(dict(e, size=size))
            elif size and local.stat().st_size != size:
                needed.append(dict(e, size=size))
            continue
        repo = e["repo"]
        if repo not in repo_cache:
            try:
                r = requests.get(
                    f"https://huggingface.co/api/models/{repo}?blobs=true",
                    timeout=20)
                repo_cache[repo] = r.json().get("siblings", []) if r.ok else []
            except requests.RequestException:
                repo_cache[repo] = []
        sib = next((s for s in repo_cache[repo]
                    if s.get("rfilename") == e["remote_file"]), None)
        size = sib.get("size") if sib else None
        if not local.exists():
            needed.append(dict(e, size=size or 0))
        elif size and local.stat().st_size != size:
            needed.append(dict(e, size=size))
    return needed


def run_install(skip_models, log, status, progress):
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _run_install(skip_models, log, status, progress)
        progress(1000, 1000)
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)


def _run_install(skip_models, log, status, progress):
    # ---- overall progress plan: everything reports into one bar ----
    needed = [] if skip_models else models_needed()
    pydir = PROJECT / "python"
    pyexe = pydir / "python.exe"
    runtime_missing = not pyexe.exists()
    engine_missing = not (PROJECT / "ComfyUI" / "main.py").exists()
    RUNTIME_EST = (6 << 30) if runtime_missing else (256 << 20)
    total_bytes = (sum(e.get("size") or 0 for e in needed)
                   + RUNTIME_EST + ((60 << 20) if engine_missing else 0))
    state = {"done": 0}

    def adv(nbytes):
        state["done"] = min(total_bytes, state["done"] + nbytes)
        progress(int(state["done"] * 1000 / max(1, total_bytes)), 1000)

    # ---- 1: bundled python runtime ----
    if pyexe.exists():
        log("Bundled Python runtime already present.")
    else:
        status("Downloading Python runtime...")
        log("Downloading embedded Python 3.12 runtime...")
        with tempfile.TemporaryDirectory(dir=TMP_DIR) as td:
            z = Path(td) / "py.zip"
            _download(PY_EMBED_URL, z, "python runtime", status, adv)
            pydir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(z) as zf:
                _safe_extractall(zf, pydir)
        log("Python runtime installed (self-contained, nothing touched "
            "outside this folder).")

    # enable site-packages in the embedded runtime, and put the engine on
    # the path (embedded pythons ignore cwd/script dir by design)
    pth = next(pydir.glob("python3*._pth"))
    want = ("python312.zip\n.\nLib\\site-packages\n..\\ComfyUI\n"
            "import site\n")
    if pth.read_text(encoding="utf-8") != want:
        pth.write_text(want, encoding="utf-8")

    # pip
    r = subprocess.run([str(pyexe), "-m", "pip", "--version"],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        status("Installing pip...")
        log("Installing pip into the bundled runtime...")
        with tempfile.TemporaryDirectory(dir=TMP_DIR) as td:
            gp = Path(td) / "get-pip.py"
            _download(GET_PIP_URL, gp, "pip installer", status, adv)
            _exec([str(pyexe), str(gp), "--no-warn-script-location"])
    log("pip ready.")

    # ---- 2: engine ----
    engine = PROJECT / "ComfyUI"
    if (engine / "main.py").exists():
        log("Engine already present - skipping download.")
    else:
        status("Downloading engine...")
        log("Downloading ComfyUI engine...")
        with tempfile.TemporaryDirectory(dir=TMP_DIR) as td:
            zpath = Path(td) / "comfy.zip"
            _download(COMFY_ZIP, zpath, "engine", status, adv)
            log("Extracting engine...")
            with zipfile.ZipFile(zpath) as z:
                _safe_extractall(z, td)
            inner = next(Path(td).glob("ComfyUI-*"))
            shutil.move(str(inner), str(engine))
        log("Engine installed.")
        try:  # record the installed engine version for the app's update check
            r = requests.get("https://api.github.com/repos/comfyanonymous/"
                             "ComfyUI/commits/master", timeout=20)
            if r.ok:
                (PROJECT / "engine_version.json").write_text(
                    json.dumps({"sha": r.json()["sha"]}), encoding="utf-8")
        except requests.RequestException:
            pass

    # ---- 3: packages ----
    status("Installing PyTorch + engine packages (several GB - the slow "
           "part)...")
    pip_budget = RUNTIME_EST - (20 << 20)
    _pip(pyexe, ["install", "--upgrade", "pip"], log,
         adv, pip_budget // 20)
    _pip(pyexe, ["install", "torch", "torchvision", "torchaudio",
                 "--index-url", "https://download.pytorch.org/whl/cu128"],
         log, adv, pip_budget // 2)
    _pip(pyexe, ["install", "-r", str(engine / "requirements.txt")], log,
         adv, pip_budget // 4)
    _pip(pyexe, ["install", "rembg", "onnxruntime", "websocket-client",
                 "requests"], log, adv, pip_budget // 5)
    r = subprocess.run([str(pyexe), "-c",
                        "import torch; print(torch.cuda.get_device_name(0) "
                        "if torch.cuda.is_available() else 'NOGPU')"],
                       capture_output=True, text=True, timeout=180,
                       creationflags=NO_WINDOW)
    gpu = (r.stdout or "").strip()
    if "NOGPU" in gpu or r.returncode != 0:
        log("WARNING: CUDA not available - make sure this machine has an "
            "NVIDIA GPU and a current driver. The app will not generate "
            "without it.")
    else:
        log(f"GPU OK: {gpu}")

    # ---- 3b: IP-Adapter custom node (style references) ----
    node_dir = engine / "custom_nodes" / "ComfyUI_IPAdapter_plus"
    if not node_dir.exists():
        status("Installing IP-Adapter node...")
        with tempfile.TemporaryDirectory(dir=TMP_DIR) as td:
            z = Path(td) / "ipa.zip"
            _download("https://github.com/cubiq/ComfyUI_IPAdapter_plus/"
                      "archive/refs/heads/main.zip", z, "IP-Adapter node",
                      status, progress)
            with zipfile.ZipFile(z) as zf:
                _safe_extractall(zf, td)
            inner = next(Path(td).glob("ComfyUI_IPAdapter_plus-*"))
            shutil.move(str(inner), str(node_dir))
        log("IP-Adapter node installed.")

    # ---- 4: folders + models ----
    for d in ("models/checkpoints", "models/loras", "models/vae",
              "models/upscale_models", "models/rembg", "models/ipadapter",
              "models/clip_vision", "models/diffusion_models",
              "models/text_encoders", "output/_raw"):
        (PROJECT / d).mkdir(parents=True, exist_ok=True)
    if skip_models:
        log("Model pack skipped (engine-only install).")
        return
    status("Checking models against their latest releases...")
    needed = models_needed()
    if not needed:
        log("All models present and up to date with their latest releases.")
        return
    for i, e in enumerate(needed, 1):
        dest = PROJECT / "models" / e["dir"] / e["local"]
        why = "outdated" if dest.exists() else "missing"
        status(f"Downloading model {i}/{len(needed)}: {e['local']} ({why})")
        url = e.get("url") or (
            f"https://huggingface.co/{e['repo']}/resolve/main/"
            f"{e['remote_file']}")
        _download(url, dest, f"[{i}/{len(needed)}] {e['local']}", status,
                  adv)
        log(f"[{i}/{len(needed)}] downloaded {e['local']} ({why})")
    log("Model pack complete - everything matches the latest releases.")


def _download(url, dest, label, status, adv):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=60,
                      allow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 22):
                fh.write(chunk)
                done += len(chunk)
                adv(len(chunk))
                if total and done % (1 << 27) < (1 << 22):  # ~128 MB
                    status(f"{label}: {done / (1 << 30):.1f}/"
                           f"{total / (1 << 30):.1f} GB")
    os.replace(tmp, dest)


def _exec(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True,
                       creationflags=NO_WINDOW, env=PIP_ENV)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, cmd))}\n{r.stderr[-600:]}")


def _pip(pyexe, args, log, adv=None, budget=0):
    proc = subprocess.Popen([str(pyexe), "-m", "pip"] + args + ["--no-color"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, creationflags=NO_WINDOW, env=PIP_ENV)
    last, spent, step = "", 0, max(1, budget // 40)
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            last = line
            if line.startswith(("Collecting", "Installing", "Successfully",
                                "Downloading")):
                log("  " + line[:110])
                if adv and spent < budget:
                    adv(step)
                    spent += step
    proc.wait()
    if adv and spent < budget:
        adv(budget - spent)   # close out this phase's share of the bar
    if proc.returncode != 0:
        raise RuntimeError(f"pip {' '.join(args[:2])} failed: {last}")


# ==========================================================================
# GUI
# ==========================================================================

class SetupApp:
    def __init__(self, root):
        from tkinter import ttk, Text, StringVar, BooleanVar
        self.root = root
        root.title("AI Image Generator Suite - Setup")
        root.geometry("720x520")
        root.configure(bg=BG)
        root.minsize(600, 420)
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=BG2,
                    bordercolor=BG2, troughcolor=BG2, arrowcolor=FG)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("TButton", background=BG2, padding=8)
        s.configure("TCheckbutton", background=BG, foreground=FG)
        s.map("TCheckbutton", background=[("active", BG)])
        s.configure("Horizontal.TProgressbar", background=OK, troughcolor=BG2)

        frm = ttk.Frame(root, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(2, weight=1)

        self.intro_var = StringVar(
            value="Fully self-contained install - no Python, git or "
                  "anything else required on this PC,\nand nothing is "
                  "written outside this folder. Calculating download "
                  "sizes…\nNeeds 64-bit Windows 10/11 and an NVIDIA GPU.")
        ttk.Label(frm, textvariable=self.intro_var,
                  justify="left").grid(row=0, sticky="w")

        row = ttk.Frame(frm)
        row.grid(row=1, sticky="ew", pady=10)
        self.skip_models = BooleanVar(value=False)
        ttk.Checkbutton(row, text="Engine only (skip the model pack)",
                        variable=self.skip_models).pack(side="left")
        self.go_btn = ttk.Button(row, text="Install", command=self.start)
        self.go_btn.pack(side="right")

        self.log_box = Text(frm, bg=BG2, fg=FG, relief="flat", padx=8,
                            pady=8, font=("Consolas", 9), state="disabled",
                            wrap="word")
        self.log_box.grid(row=2, sticky="nsew")

        pfrm = ttk.Frame(frm)
        pfrm.grid(row=3, sticky="ew", pady=(8, 2))
        pfrm.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(pfrm, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")
        self.pct_var = StringVar(value="0%")
        ttk.Label(pfrm, textvariable=self.pct_var, width=5).grid(
            row=0, column=1, padx=(8, 0))
        self.status = StringVar(value="Ready.")
        ttk.Label(frm, textvariable=self.status, foreground=DIM).grid(
            row=4, sticky="w")

        self.running = False
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # check current state against the LATEST releases in the background;
        # gray out only when truly nothing is left to do
        self.status.set("Checking installation against latest releases...")
        threading.Thread(target=self._startup_check, daemon=True).start()

    def _startup_check(self):
        engine_ok = engine_installed()
        needed = models_needed()
        # recalculate real totals from the manifest + what's on disk
        try:
            entries = json.loads(MANIFEST.read_text(encoding="utf-8"))
            installed_gb = sum(
                (PROJECT / "models" / e["dir"] / e["local"]).stat().st_size
                for e in entries
                if (PROJECT / "models" / e["dir"] / e["local"]).exists()
            ) / (1 << 30)
            dl_gb = sum(e.get("size") or 0 for e in needed) / (1 << 30)
            total_gb = installed_gb + dl_gb
            runtime_gb = 0 if engine_ok else 9
            self.root.after(0, lambda: self.intro_var.set(
                "Fully self-contained install - no Python, git or anything "
                "else required on this PC,\nand nothing is written outside "
                f"this folder. Model pack: {total_gb:.0f} GB total, "
                f"{dl_gb:.0f} GB still to download"
                + (f" (+ ~{runtime_gb} GB engine & runtime)"
                   if runtime_gb else "")
                + f".\nEstimated disk use when complete: "
                f"~{total_gb + 9:.0f} GB. Needs 64-bit Windows 10/11 and "
                "an NVIDIA GPU."))
        except Exception:
            pass
        if not engine_ok:
            needed = None

        def _apply():
            if self.running:
                return
            if engine_ok and not needed:
                self.go_btn.configure(text="Installed")
                self.go_btn.state(["disabled"])
                self.status.set("Already installed and up to date - "
                                "nothing to do.")
                self.log("Everything is installed and matches the latest "
                         "releases.\nLaunch AIImageGeneratorSuite.exe to "
                         "create art.")
            elif engine_ok:
                gb = sum(e["size"] for e in needed) / (1 << 30)
                self.status.set(f"{len(needed)} model(s) missing or "
                                f"outdated ({gb:.1f} GB) - click Install "
                                "to fetch just those.")
                for e in needed:
                    state = "outdated" if (PROJECT / "models" / e["dir"]
                                           / e["local"]).exists() \
                        else "missing"
                    self.log(f"  needs download: {e['local']} ({state})")
            else:
                self.status.set("Ready.")
        self.root.after(0, _apply)

    def log(self, text):
        from tkinter import END
        def _do():
            self.log_box.configure(state="normal")
            self.log_box.insert(END, text + "\n")
            self.log_box.see(END)
            self.log_box.configure(state="disabled")
        self.root.after(0, _do)

    def set_status(self, text):
        self.root.after(0, lambda: self.status.set(text))

    def set_progress(self, value, maximum=100):
        def _do():
            self.progress.configure(mode="determinate", maximum=maximum)
            self.progress["value"] = value
            self.pct_var.set(f"{value * 100 / max(1, maximum):.0f}%")
        self.root.after(0, _do)

    def on_close(self):
        from tkinter import messagebox
        if self.running and not messagebox.askyesno(
                "Setup running", "Setup is still working - really quit?"):
            return
        self.root.destroy()

    def start(self):
        if self.running:
            return
        self.running = True
        self.go_btn.state(["disabled"])
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        from tkinter import messagebox
        ok = False
        try:
            run_install(self.skip_models.get(), self.log, self.set_status,
                        self.set_progress)
            ok = True
            self.log("\nAll done! Launch AIImageGeneratorSuite.exe")
            self.set_status("Setup complete.")
            self.root.after(0, lambda: messagebox.showinfo(
                "Setup complete",
                "Everything is installed.\n\nLaunch AIImageGeneratorSuite.exe "
                "to start creating."))
        except Exception as e:
            self.log(f"\nFAILED: {e}")
            self.set_status("Setup failed - see log above.")
            self.root.after(0, lambda: messagebox.showerror("Setup failed",
                                                            str(e)))
        finally:
            self.running = False

            def _finish():
                if ok:
                    # stay grayed out - installation is done, never redo it
                    self.go_btn.configure(text="Installed")
                    self.go_btn.state(["disabled"])
                else:
                    self.go_btn.state(["!disabled"])
            self.root.after(0, _finish)


# ==========================================================================
# CLI mode (Setup.exe --cli [--skip-models]) - logs to setup.log
# ==========================================================================

def run_cli():
    logf = open(PROJECT / "setup.log", "w", encoding="utf-8", buffering=1)

    def log(text):
        logf.write(text + "\n")
        if sys.stdout:
            print(text)

    def status(text):
        log("[status] " + text)

    def progress(done, total):
        pass

    try:
        if engine_installed() and not models_needed():
            log("Already installed and up to date - nothing to do.")
            log("SETUP OK")
            return
        run_install("--skip-models" in sys.argv, log, status, progress)
        log("SETUP OK")
    except Exception as e:
        log(f"SETUP FAILED: {e}")
        sys.exit(1)
    finally:
        logf.close()


def main():
    if "--cli" in sys.argv:
        run_cli()
        return
    from tkinter import Tk
    root = Tk()
    SetupApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
