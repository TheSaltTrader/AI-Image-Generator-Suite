"""Tests for the self-updater. Run: venv\\Scripts\\python.exe app\\self_update_test.py

Covers the parts that decide whether a user's install survives an update:
version comparison, the exe version-resource read that gates the swap, the
skip-this-version memory, the stale-exe sweep, the zip path guard, and the
roll-back when the swap fails halfway. The window is built for real (on a
withdrawn root) so a typo in a widget line fails here, not on the user's
machine.
"""

import io
import json
import os
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import self_update as su

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + ("  " + detail
                                                       if detail and not cond
                                                       else ""))


def pump(root, cond, timeout=5.0):
    """Run the REAL Tk event loop until cond() holds (or timeout). The
    window's worker thread reports back through after(), which needs a
    running loop exactly as in the app — an update() polling loop makes
    every cross-thread after() raise 'main thread is not in main loop'."""
    deadline = time.time() + timeout

    def tick():
        if cond() or time.time() > deadline:
            root.quit()
        else:
            root.after(20, tick)

    root.after(0, tick)
    root.mainloop()


# --------------------------------------------------------------- versions
print("version comparison")
check("v-prefixed tag parses",
      su.version_tuple("v1.35.0") == (1, 35, 0, 0))
check("4-part version parses",
      su.version_tuple("1.35.0.0") == (1, 35, 0, 0))
check("a 3-part tag equals the 4-part exe resource for it",
      su.version_tuple("1.34.0") == su.version_tuple("1.34.0.0"))
check("newer beats older",
      su.version_tuple("v1.35.0") > su.version_tuple("1.34.0"))
check("10 sorts above 9 (not string order)",
      su.version_tuple("v1.10.0") > su.version_tuple("v1.9.0"))
check("garbage tag sorts lowest",
      su.version_tuple("nightly") == (0, 0, 0, 0))
check("garbage is never an upgrade",
      su.version_tuple("nightly") <= su.version_tuple("1.34.0"))
check("equal version is not an upgrade",
      not (su.version_tuple("v1.34.0") > su.version_tuple("1.34.0")))

# ------------------------------------------------------------ exe version
print("exe version resource")
proj = Path(__file__).resolve().parent.parent
live = proj / "ComicArtCreator.exe"
if live.exists():
    got = su.exe_version(live)
    check("reads the shipped exe's FileVersion", got is not None, str(got))
    if got:
        check("shipped exe reports a 1.x version", got[0] == 1, str(got))
        print("       (installed exe reports %s)"
              % ".".join(str(n) for n in got))
else:
    print("  skip  no ComicArtCreator.exe next to the project")

with tempfile.TemporaryDirectory() as td:
    junk = Path(td) / "notanexe.exe"
    junk.write_bytes(b"this is not a PE file at all" * 100)
    check("a non-exe is rejected", su.exe_version(junk) is None)
    check("a missing file is rejected",
          su.exe_version(Path(td) / "nope.exe") is None)

# ------------------------------------------------------------- skip state
print("skip this version")
with tempfile.TemporaryDirectory() as td:
    su.configure(td, td)
    check("nothing skipped to start", su.skipped_version() is None)
    su.skip_version("v1.35.0")
    check("skip is remembered", su.skipped_version() == "v1.35.0")
    check("skip survives a reread",
          json.loads((Path(td) / "update_state.json")
                     .read_text(encoding="utf-8"))["skipped"] == "v1.35.0")
    su.clear_skip()
    check("skip clears", su.skipped_version() is None)

    # a corrupt state file must not take the app down
    (Path(td) / "update_state.json").write_text("{ broken",
                                                encoding="utf-8")
    check("corrupt state file reads as empty",
          su.skipped_version() is None)

# --------------------------------------------------------------- sweeping
print("stale exe sweep")
with tempfile.TemporaryDirectory() as td:
    su.configure(td, td)
    p = Path(td)
    (p / "ComicArtCreator_old_123.exe").write_bytes(b"x")
    (p / "Setup_old_123.exe").write_bytes(b"x")
    (p / "ComicArtCreator.exe").write_bytes(b"x")     # the live one
    (p / "keepme_old_1.txt").write_text("x")
    n = su.sweep_old_exes()
    check("both stale exes swept", n == 2, "swept %d" % n)
    check("the live exe is untouched", (p / "ComicArtCreator.exe").exists())
    check("unrelated files untouched", (p / "keepme_old_1.txt").exists())
    check("sweeping again is a no-op", su.sweep_old_exes() == 0)

# ------------------------------------------------------------- zip guard
print("release zip handling")


class _FakeResp:
    """Stands in for requests' streaming response."""

    def __init__(self, data):
        self._data = data
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, n):
        for i in range(0, len(self._data), n):
            yield self._data[i:i + n]


def _serve(data):
    """Patch requests.get so the download path runs with no network."""
    su.requests.get = lambda *a, **k: _FakeResp(data)


_real_get = su.requests.get

with tempfile.TemporaryDirectory() as td:
    su.configure(td, td)
    tmp = Path(td) / "work"
    tmp.mkdir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../escaped.txt", "pwned")
    _serve(buf.getvalue())
    upd = su.Update("v9.9.9", "http://x/a.zip", 0, "", "")
    try:
        su._download_and_unpack(upd, tmp, lambda s: None,
                                lambda d, t: None, threading.Event())
        check("zip path traversal is refused", False, "no error raised")
    except RuntimeError as e:
        check("zip path traversal is refused", "bad path" in str(e))
    check("nothing escaped the temp folder",
          not (Path(td) / "escaped.txt").exists()
          and not (Path(td).parent / "escaped.txt").exists())

# a mid-download cancel must stop, not finish
with tempfile.TemporaryDirectory() as td:
    su.configure(td, td)
    tmp = Path(td) / "work"
    tmp.mkdir()
    _serve(b"x" * (4 << 20))
    ev = threading.Event()
    ev.set()
    try:
        su._download_and_unpack(su.Update("v9", "u", 0, "", ""), tmp,
                                lambda s: None, lambda d, t: None, ev)
        check("cancel stops the download", False, "download completed")
    except su._Cancelled:
        check("cancel stops the download", True)

# --------------------------------------------- verify gate + roll-back
print("verify gate and roll-back")


def _zip_with(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


with tempfile.TemporaryDirectory() as td:
    su.configure(td, td)
    p = Path(td)
    (p / "ComicArtCreator.exe").write_bytes(b"ORIGINAL-APP")
    (p / "Setup.exe").write_bytes(b"ORIGINAL-SETUP")

    # a zip with no app exe at all
    _serve(_zip_with({"README.md": "hi"}))
    try:
        su.apply_update(su.Update("v9.9.9", "u", 0, "", ""), "1.34.0",
                        lambda s: None, lambda d, t: None,
                        threading.Event())
        check("zip without the app exe is refused", False)
    except RuntimeError as e:
        check("zip without the app exe is refused",
              "has no ComicArtCreator.exe" in str(e))

    # a zip whose "exe" is not a real program — the truncated-download case
    _serve(_zip_with({"ComicArtCreator.exe": "not a PE" * 50}))
    try:
        su.apply_update(su.Update("v9.9.9", "u", 0, "", ""), "1.34.0",
                        lambda s: None, lambda d, t: None,
                        threading.Event())
        check("a damaged exe is refused", False)
    except RuntimeError as e:
        check("a damaged exe is refused", "not a valid Windows" in str(e))

    check("the original app survived a refused update",
          (p / "ComicArtCreator.exe").read_bytes() == b"ORIGINAL-APP")
    check("the original setup survived a refused update",
          (p / "Setup.exe").read_bytes() == b"ORIGINAL-SETUP")
    check("no leftover temp folder", not list(p.glob("_app_upd_tmp*")))

# the mis-tagged release: a real exe, but not actually newer
if live.exists() and su.exe_version(live):
    running = ".".join(str(n) for n in su.exe_version(live)[:3])
    with tempfile.TemporaryDirectory() as td:
        su.configure(td, td)
        p = Path(td)
        (p / "ComicArtCreator.exe").write_bytes(b"ORIGINAL-APP")
        _serve(_zip_with({"ComicArtCreator.exe": live.read_bytes()}))
        try:
            # claim the release is newer while shipping the same build
            su.apply_update(su.Update("v99.0.0", "u", 0, "", ""), running,
                            lambda s: None, lambda d, t: None,
                            threading.Event())
            check("a mis-tagged release is refused", False)
        except RuntimeError as e:
            check("a mis-tagged release is refused",
                  "mis-tagged" in str(e), str(e))
        check("original kept after a mis-tagged release",
              (p / "ComicArtCreator.exe").read_bytes() == b"ORIGINAL-APP")

# a genuine update: real exe, older claimed running version -> swap happens
if live.exists() and su.exe_version(live):
    with tempfile.TemporaryDirectory() as td:
        su.configure(td, td)
        p = Path(td)
        (p / "ComicArtCreator.exe").write_bytes(b"ORIGINAL-APP")
        (p / "CHANGELOG.md").write_text("old changelog", encoding="utf-8")
        (p / "app").mkdir()
        (p / "app" / "presets.json").write_text('{"mine": 1}',
                                                encoding="utf-8")
        (p / "app" / "models_manifest.json").write_text('[{"old": 1}]',
                                                        encoding="utf-8")
        _serve(_zip_with({
            "ComicArtCreator.exe": live.read_bytes(),
            "CHANGELOG.md": "new changelog",
            "app/presets.json": '{"theirs": 2}',
            "app/models_manifest.json": '[{"new": 1}]',
        }))
        out = su.apply_update(su.Update("v99.0.0", "u", 0, "", ""), "0.0.1",
                              lambda s: None, lambda d, t: None,
                              threading.Event())
        check("the new exe was installed",
              (p / "ComicArtCreator.exe").read_bytes() == live.read_bytes())
        check("returns the exe to relaunch", out == p / "ComicArtCreator.exe")
        check("the old exe was renamed aside, not deleted",
              any(f.name.startswith("ComicArtCreator_old_")
                  for f in p.glob("*.exe")))
        check("shipped docs were refreshed",
              (p / "CHANGELOG.md").read_text(encoding="utf-8")
              == "new changelog")
        check("the model list was refreshed",
              json.loads((p / "app" / "models_manifest.json")
                         .read_text(encoding="utf-8")) == [{"new": 1}])
        check("the user's presets.json was left alone",
              json.loads((p / "app" / "presets.json")
                         .read_text(encoding="utf-8")) == {"mine": 1})
        check("the swap cleaned up its temp folder",
              not list(p.glob("_app_upd_tmp*")))

# a broken models_manifest.json in the release must not overwrite a good one
if live.exists() and su.exe_version(live):
    with tempfile.TemporaryDirectory() as td:
        su.configure(td, td)
        p = Path(td)
        (p / "ComicArtCreator.exe").write_bytes(b"ORIGINAL-APP")
        (p / "app").mkdir()
        (p / "app" / "models_manifest.json").write_text('[{"good": 1}]',
                                                        encoding="utf-8")
        _serve(_zip_with({"ComicArtCreator.exe": live.read_bytes(),
                          "app/models_manifest.json": "{ not json"}))
        su.apply_update(su.Update("v99.0.0", "u", 0, "", ""), "0.0.1",
                        lambda s: None, lambda d, t: None,
                        threading.Event())
        check("a broken model list is not copied over a good one",
              json.loads((p / "app" / "models_manifest.json")
                         .read_text(encoding="utf-8")) == [{"good": 1}])

su.requests.get = _real_get

# ------------------------------------------------------------ check() paths
print("update check")


class _JsonResp:
    def __init__(self, payload, ok=True):
        self._p = payload
        self.ok = ok

    def json(self):
        return self._p


def _api(payload, ok=True):
    su.requests.get = lambda *a, **k: _JsonResp(payload, ok)


ASSET = [{"name": "ComicBookArtCreator_v1.35.0.zip",
          "browser_download_url": "http://x/a.zip", "size": 75 * 1024 * 1024}]

with tempfile.TemporaryDirectory() as td:
    su.configure(td, td)
    _api({"tag_name": "v1.35.0", "assets": ASSET, "body": "* fixed things",
          "published_at": "2026-08-20T10:00:00Z"})
    up = su.check("1.34.0")
    check("a newer release is offered", up is not None)
    check("tag carried through", up and up.tag == "v1.35.0")
    check("size carried through", up and round(up.size_mb) == 75)
    check("publish date trimmed to a day",
          up and up.published == "2026-08-20")

    _api({"tag_name": "v1.34.0", "assets": ASSET})
    check("the same version is not offered", su.check("1.34.0") is None)

    _api({"tag_name": "v1.33.0", "assets": ASSET})
    check("an older release is not offered", su.check("1.34.0") is None)

    _api({"tag_name": "v1.35.0", "assets": []})
    check("a release with no zip asset is not offered",
          su.check("1.34.0") is None)

    _api({"tag_name": "v1.35.0", "assets": ASSET}, ok=False)
    check("a failed API call is not offered", su.check("1.34.0") is None)

    def _boom(*a, **k):
        raise OSError("no network")

    su.requests.get = _boom
    check("no network is survived", su.check("1.34.0") is None)

    # skip, then confirm the button can still reach it
    _api({"tag_name": "v1.35.0", "assets": ASSET})
    su.skip_version("v1.35.0")
    check("a skipped version is hidden from the startup check",
          su.check("1.34.0") is None)
    check("the button still reaches a skipped version",
          su.check("1.34.0", include_skipped=True) is not None)
    _api({"tag_name": "v1.36.0", "assets": ASSET})
    check("a newer release than the skipped one is offered again",
          su.check("1.34.0") is not None)

su.requests.get = _real_get

# ---------------------------------------------------------- release notes
print("release notes rendering")
check("headings lose their hashes",
      "What's new" in su._plain_text("## What's new"))
check("hash is gone", "#" not in su._plain_text("## What's new"))
check("list markers become bullets",
      su._plain_text("- fixed a thing").startswith("•"))
check("bold markers stripped",
      su._plain_text("**bold**") == "bold")
check("inline code stripped",
      su._plain_text("run `setup.bat`") == "run setup.bat")
check("links keep their text only",
      su._plain_text("see [the docs](http://x)") == "see the docs")
check("empty notes get a readable stand-in",
      "No release notes" in su._plain_text(""))
check("None notes get a readable stand-in",
      "No release notes" in su._plain_text(None))
# real release bodies wrap, so a bold span often straddles a newline —
# stripping line by line left stray ** on screen
check("bold wrapped across a line still strips",
      "**" not in su._plain_text("failed with **Node 'X'\nnot found** here"))
check("wrapped bold keeps its words",
      "Node 'X'" in su._plain_text("failed with **Node 'X'\nnot found**"))
check("italic strips", su._plain_text("moved *inside* the folder")
      == "moved inside the folder")
check("italic wrapped across a line strips",
      "*" not in su._plain_text("moved *inside the\nnew folder* again"))
check("underscore bold strips", su._plain_text("__strong__") == "strong")
check("an unclosed marker does not eat the rest",
      su._plain_text("**oops\n\nnext paragraph").endswith("next paragraph"))
check("a bare asterisk is left alone",
      "2 * 3" in su._plain_text("2 * 3 = 6"))
check("numbered lists keep their numbers",
      su._plain_text("1. first").startswith("1."))
check("a horizontal rule becomes a line",
      "─" in su._plain_text("---"))
check("block quotes lose their marker",
      su._plain_text("> quoted") == "quoted")
check("a heading needs a space after the hashes",
      su._plain_text("#hashtag") == "#hashtag")

# ---------------------------------------------------------------- the window
print("update window")
try:
    from tkinter import Tk, ttk
    root = Tk()
    root.withdraw()
    s = ttk.Style()
    s.theme_use("clam")
    s.configure("Head.TLabel", foreground="#4ecca3")
    s.configure("Dim.TLabel", foreground="#9a9ab0")
    s.configure("Go.TButton", background="#e94560")
    with tempfile.TemporaryDirectory() as td:
        su.configure(td, td)
        upd = su.Update("v1.35.0", "http://x/a.zip", 75 * 1024 * 1024,
                        "## What's new\n- one thing\n- another thing",
                        "2026-08-20")
        relaunched = []
        statuses = []
        w = su.UpdateWindow(root, upd, "1.34.0",
                            {"bg": "#17171c", "bg2": "#20202a",
                             "fg": "#e8e8f0"},
                            on_relaunch=lambda e, t: relaunched.append(t),
                            on_status=statuses.append)
        root.update()
        check("window builds", w.winfo_exists())
        check("all three buttons present",
              all(b.winfo_exists() for b in (w.update_btn, w.skip_btn,
                                             w.cont_btn)))
        check("notes are read-only",
              str(w.notes.cget("state")) == "disabled")
        check("notes rendered the bullets",
              "•" in w.notes.get("1.0", "end"))

        # Skip: remembered, window closes, nothing downloaded
        w._skip()
        root.update()
        check("Skip remembers the version",
              su.skipped_version() == "v1.35.0")
        check("Skip closes the window", not w.winfo_exists())
        check("Skip says so in the status bar",
              statuses and "skipped" in statuses[-1])
        check("Skip downloaded nothing", not relaunched)

        # Continue: closes, changes nothing
        su.clear_skip()
        statuses.clear()
        w2 = su.UpdateWindow(root, upd, "1.34.0",
                             {"bg": "#17171c", "bg2": "#20202a",
                              "fg": "#e8e8f0"},
                             on_relaunch=lambda e, t: relaunched.append(t),
                             on_status=statuses.append)
        root.update()
        w2._continue()
        root.update()
        check("Continue closes the window", not w2.winfo_exists())
        check("Continue does not remember a skip",
              su.skipped_version() is None)
        check("Continue downloaded nothing", not relaunched)

        # a failed update leaves the user able to carry on
        w3 = su.UpdateWindow(root, upd, "1.34.0",
                             {"bg": "#17171c", "bg2": "#20202a",
                              "fg": "#e8e8f0"},
                             on_relaunch=lambda e, t: relaunched.append(t),
                             on_status=statuses.append)
        root.update()
        w3._failed("the download was damaged")
        root.update()
        check("a failed update leaves the window open", w3.winfo_exists())
        check("a failed update re-enables Update now",
              "disabled" not in w3.update_btn.state())
        check("a failed update explains itself",
              "damaged" in w3.msg_var.get())
        check("a failed update points at the releases page",
              "github.com" in w3.msg_var.get())
        w3._close()
        check("closing twice is safe", w3._close() is None)

        # ---- automatic mode: downloads on open, restarts on approval ----
        print("automatic mode")
        check("automatic is the default", su.auto_update())
        su.set_auto_update(False)
        check("automatic can be turned off", not su.auto_update())
        su.set_auto_update(True)
        check("…and back on", su.auto_update())

        # the launch-time check ignores a skip only when automatic is on
        _api({"tag_name": "v1.35.0", "assets": ASSET})
        su.skip_version("v1.35.0")
        check("automatic: a skipped version is still taken at startup",
              su.startup_check("1.34.0") is not None)
        su.set_auto_update(False)
        check("manual: a skipped version stays skipped at startup",
              su.startup_check("1.34.0") is None)
        su.set_auto_update(True)
        su.clear_skip()
        su.requests.get = _real_get

        applied = []
        real_apply = su.apply_update

        def fake_apply(upd_, cur, status, progress, cancel):
            applied.append(upd_.tag)
            status("Installing…")
            progress(10, 10)
            return Path(td) / "ComicArtCreator.exe"

        su.apply_update = fake_apply
        relaunched.clear()
        statuses.clear()
        w4 = su.UpdateWindow(root, upd, "1.34.0",
                             {"bg": "#17171c", "bg2": "#20202a",
                              "fg": "#e8e8f0"},
                             on_relaunch=lambda e, t: relaunched.append(t),
                             on_status=statuses.append, auto=True)
        pump(root, lambda: w4._newexe is not None)
        check("automatic: the download starts on its own", applied == ["v1.35.0"])
        check("automatic: the window says it is updating",
              "Updating" in w4.title())
        check("automatic: no Skip button is offered",
              not w4.skip_btn.winfo_manager() == "pack")
        check("automatic: installed, but NOT restarted on its own",
              w4._newexe is not None and not relaunched)
        check("automatic: the primary button is Restart now, enabled",
              w4.update_btn.cget("text") == "Restart now"
              and "disabled" not in w4.update_btn.state())
        check("automatic: the other button is Later",
              w4.cont_btn.cget("text") == "Later")
        check("automatic: the message says a restart is the user's call",
              "Restart now" in w4.msg_var.get() and "Later" in w4.msg_var.get())
        check("automatic: the status bar says restart when ready",
              statuses and "restart" in statuses[-1].lower())
        check("automatic: the checkbox reflects the setting", w4.auto_var.get())
        w4.auto_var.set(False)
        w4._toggle_auto()
        check("automatic: the checkbox turns the setting off", not su.auto_update())
        w4.auto_var.set(True)
        w4._toggle_auto()
        check("automatic: …and on again", su.auto_update())
        # Later: closes, nothing relaunched, told it starts next time
        w4._continue()
        root.update()
        check("Later closes the window without restarting",
              not w4.winfo_exists() and not relaunched)
        check("Later says the new version starts next time",
              statuses and "next time" in statuses[-1])

        # Restart now: the approval
        applied.clear()
        w5 = su.UpdateWindow(root, upd, "1.34.0",
                             {"bg": "#17171c", "bg2": "#20202a",
                              "fg": "#e8e8f0"},
                             on_relaunch=lambda e, t: relaunched.append(t),
                             on_status=statuses.append, auto=True)
        pump(root, lambda: w5._newexe is not None)
        check("Restart now is inert until the install is done — it is done",
              w5._newexe is not None)
        w5._restart()
        root.update()
        check("Restart now hands over to the new exe", relaunched == ["v1.35.0"])
        w5._close()

        # a failed automatic update offers Retry, never restarts
        relaunched.clear()

        def bad_apply(*a, **k):
            raise RuntimeError("the download was damaged")

        su.apply_update = bad_apply
        w6 = su.UpdateWindow(root, upd, "1.34.0",
                             {"bg": "#17171c", "bg2": "#20202a",
                              "fg": "#e8e8f0"},
                             on_relaunch=lambda e, t: relaunched.append(t),
                             on_status=statuses.append, auto=True)
        pump(root, lambda: not w6._busy)
        check("automatic: a failed update offers Retry",
              w6.update_btn.cget("text") == "Retry"
              and "disabled" not in w6.update_btn.state())
        check("automatic: a failed update never restarts", not relaunched)
        check("automatic: a failed update explains itself",
              "damaged" in w6.msg_var.get())
        w6._close()
        su.apply_update = real_apply

        # the manual window is unchanged by all this
        w7 = su.UpdateWindow(root, upd, "1.34.0",
                             {"bg": "#17171c", "bg2": "#20202a",
                              "fg": "#e8e8f0"},
                             on_relaunch=lambda e, t: relaunched.append(t),
                             on_status=statuses.append)
        root.update()
        check("manual: Update now / Skip / Continue as before",
              w7.update_btn.cget("text") == "Update now"
              and w7.skip_btn.winfo_manager() == "pack"
              and w7.cont_btn.cget("text") == "Continue")
        check("manual: nothing downloads on open", not w7._busy)
        w7._close()
    root.destroy()
except Exception as e:
    check("window smoke test", False, repr(e))

print()
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  FAILED: " + f)
sys.exit(1 if FAIL else 0)
