"""vector_redraw without the network: the SVG ask (fake client), outline
conversion, the scan check, the key store and the cost estimate.

Run: venv\\Scripts\\python.exe app\\vector_redraw_test.py
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vector_redraw as vr  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (("  " + str(detail)) if detail and not cond else ""))


# ---- a synthetic decal: red rounded panel with a white bar, on transparency
W, H = 150, 60
_a = np.zeros((H, W, 4), np.uint8)
_a[8:52, 6:110] = (224, 66, 49, 255)
_a[22:38, 20:96] = (255, 255, 255, 255)
_a[10:50, 118:146] = (224, 66, 49, 255)
crop = Image.fromarray(_a, "RGBA")
pal = np.array([(224, 66, 49), (255, 255, 255)], np.uint8)

GOOD = ('<svg xmlns="http://www.w3.org/2000/svg" width="0.5in" height="0.2in" '
        'viewBox="0 0 150 60"><rect x="6" y="8" width="104" height="44" fill="#e04231"/>'
        '<rect x="20" y="22" width="76" height="16" fill="#ffffff"/>'
        '<rect x="118" y="10" width="28" height="40" fill="#e04231"/></svg>')
BLUE = GOOD.replace("#e04231", "#1040c0")
TINY = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 150 60">'
        '<rect x="60" y="25" width="10" height="10" fill="#e04231"/></svg>')
TEXT = ('<svg xmlns="http://www.w3.org/2000/svg" width="0.5in" height="0.2in" '
        'viewBox="0 0 150 60"><rect x="6" y="8" width="104" height="44" fill="#e04231"/>'
        '<text x="58" y="46" font-family="Arial, Helvetica, sans-serif" font-weight="bold" '
        'font-size="36" fill="#ffffff" text-anchor="middle" textLength="90" '
        'lengthAdjust="spacingAndGlyphs">FUEL</text>'
        '<polygon points="118,10 147,31 118,52" fill="#e04231"/></svg>')

print("models + cost")
check("Opus 5.5 is the default, Sonnet 5.5 the cheaper option",
      vr.DEFAULT_MODEL == "claude-opus-5-5"
      and any(m == "claude-sonnet-5-5" for _l, m in vr.MODELS))


class _U:
    input_tokens = 1500
    output_tokens = 1200
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


check("cost estimate uses the published per-million prices",
      abs(vr.estimate_cost("claude-opus-5-5", _U()) - 0.030) < 1e-6
      and abs(vr.estimate_cost("claude-sonnet-5-5", _U()) - 0.015) < 1e-6)

print("the scan check")
ok, iou, col = vr.check_against_scan(GOOD, crop)
check("a faithful drawing passes", ok, (iou, col))
check("…with a high silhouette overlap", iou > 0.8, iou)
ok_b, _i, col_b = vr.check_against_scan(BLUE, crop)
check("the same shapes in the wrong colour fail", not ok_b, (ok_b, col_b))
ok_t, iou_t, _c = vr.check_against_scan(TINY, crop)
check("a drawing that misses most of the decal fails", not ok_t, iou_t)
check("unparseable SVG is rejected, not raised",
      vr.check_against_scan("<svg", crop)[0] is False)

print("text to outlines")
font = vr._find_font("Arial")
check("a Windows bold font is found", font is not None and font.exists(), font)
outlined = vr.text_to_paths(TEXT)
check("<text> is replaced by glyph paths",
      "<text" not in outlined and outlined.count("<path") >= 4)
r_text = vr.render_svg(TEXT, W)
r_path = vr.render_svg(outlined, W)
a1 = np.asarray(r_text)[..., :3].astype(int)
a2 = np.asarray(r_path)[..., :3].astype(int)
# the white word must land in the same band of the red panel either way
band_text = (np.asarray(r_text)[22:38, 20:96, :3].min(2) > 200).mean()
band_path = (np.asarray(r_path)[22:38, 20:96, :3].min(2) > 200).mean()
check("the outlined word sits where the text was (white in the word band)",
      band_path > 0.25 and abs(band_path - band_text) < 0.35,
      (round(band_text, 2), round(band_path, 2)))
check("outlining keeps the drawing faithful to the scan",
      vr.check_against_scan(outlined, crop)[0])
check("a rotated/anchored <text> converts without error",
      "<text" not in vr.text_to_paths(
          '<svg viewBox="0 0 100 100"><text transform="translate(50 50) rotate(90)" '
          'font-size="20" text-anchor="middle" dominant-baseline="central" '
          'fill="#000">B11</text></svg>'))
check("SVG without text passes through unchanged", vr.text_to_paths(GOOD) == GOOD)

print("sharp zoom from the SVG")
import tempfile  # noqa: E402
_td = tempfile.mkdtemp()
_svgp = Path(_td) / "d.svg"
_svgp.write_text(GOOD, encoding="utf-8")
# the preview raster is 150 px wide (1:1 with the viewBox); zoom 4x on the
# white bar's region must come back crisp: pure white inside, pure red beside
_reg = vr.render_svg_region(_svgp, 150, (10, 15, 60, 45), 4.0, backing=(0, 0, 0))
check("a zoomed region renders at the zoom scale",
      abs(_reg.width - 200) <= 2 and abs(_reg.height - 120) <= 2, _reg.size)
# box x 10..60 → the bar (x 20..96, y 22..38) starts 10 px in → 40 px at 4x
_px_bar = _reg.getpixel((100, 60))       # inside the white bar
_px_red = _reg.getpixel((20, 60))        # red panel left of the bar
check("…with exact colours (no resampling blur)",
      _px_bar == (255, 255, 255) and _px_red == (224, 66, 49), (_px_bar, _px_red))
check("a region outside the drawing is just the backing",
      vr.render_svg_region(_svgp, 150, (0, 0, 4, 4), 2.0, backing=(0, 0, 0)).getpixel((1, 1)) == (0, 0, 0))

print("the ask (fake client)")
_calls = []


class _Resp:
    stop_reason = "end_turn"
    model = "claude-sonnet-5-5"       # the model that served (as the API reports)
    usage = _U()
    _request_id = "req_test"

    class _B:
        type = "text"
        text = ("Here you go:\n```svg\n<svg viewBox=\"0 0 150 60\" width=\"0.5in\" "
                "height=\"0.2in\"><rect x=\"6\" y=\"8\" width=\"104\" height=\"44\" "
                "fill=\"#e04231\"/></svg>\n```")
    content = [_B()]


class _Msgs:
    def create(self, **kw):
        _calls.append(kw)
        return _Resp()


class _Beta:
    messages = _Msgs()


class _Client:
    beta = _Beta()
    messages = _Msgs()


res = vr.draw_decal(crop, 0.5, 0.2, pal, hint="a fuel label", model="claude-sonnet-5-5",
                    client=_Client())
kw = _calls[-1]
check("the ask carries the image, the printed size and the palette",
      kw["model"] == "claude-sonnet-5-5"
      and kw["messages"][0]["content"][0]["type"] == "image"
      and "0.500 in wide" in kw["messages"][0]["content"][1]["text"]
      and "#e04231" in kw["messages"][0]["content"][1]["text"]
      and "fuel label" in kw["messages"][0]["content"][1]["text"])
check("the ask uses adaptive thinking at high effort (no budget_tokens)",
      "thinking" not in kw and kw["output_config"]["effort"] == "high")
check("the refusal fallback is on by default",
      kw.get("fallbacks") == "default"
      and "server-side-fallback-2026-07-01" in kw.get("betas", []))
check("the SVG is extracted from the reply and given an xmlns",
      res["svg"].startswith("<svg") and "xmlns=" in res["svg"][:80]
      and res["svg"].endswith("</svg>"))
check("the cost rides along", abs(res["cost_usd"] - 0.015) < 1e-6
      and res["request_id"] == "req_test")


class _Refuse(_Resp):
    stop_reason = "refusal"


class _RClient(_Client):
    class beta:
        class messages:
            @staticmethod
            def create(**kw):
                return _Refuse()


try:
    vr.draw_decal(crop, 0.5, 0.2, pal, client=_RClient())
    check("a refusal raises (so the caller falls back)", False)
except RuntimeError as e:
    check("a refusal raises (so the caller falls back)", "declined" in str(e))

print("orientation ask")


class _OrientResp(_Resp):
    class _B:
        type = "text"
        text = "270"
    content = [_B()]


class _OrientClient:
    class messages:
        @staticmethod
        def create(**kw):
            _calls.append(kw)
            return _OrientResp()


_calls.clear()
check("ask_orientation returns the model's rotation as an int",
      vr.ask_orientation(crop, client=_OrientClient()) == 270
      and _calls[-1]["messages"][0]["content"][0]["type"] == "image"
      and _calls[-1]["output_config"]["effort"] == "low")

print("text sweep")
import json as _json
from PIL import ImageDraw as _ID, ImageFont as _IF
import decals as _dec

# a two-line red caption as a scan would give it: letters on transparent
_tcrop = Image.new("RGBA", (420, 160), (0, 0, 0, 0))
_tdraw = _ID.Draw(_tcrop)
_fnt = _IF.truetype(str(vr._find_font("Arial Bold")), 48)
_tdraw.text((20, 15), "DANGER", font=_fnt, fill=(206, 22, 30, 255))
_tdraw.text((20, 85), "JET BLAST", font=_fnt, fill=(206, 22, 30, 255))
_geom = _dec.text_geometry(_tcrop)
check("text_geometry sees two rows of lettering",
      _geom["text"] and len(_geom["lines"]) == 2 and _geom["share"] > 0.9
      and _geom["lines"][0][1] < _geom["lines"][1][1], (_geom["text"], len(_geom["lines"]), _geom["share"]))
_blob = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
_ID.Draw(_blob).ellipse([20, 20, 180, 180], fill=(10, 10, 10, 255))
check("…and a solid graphic is not text", not _dec.text_geometry(_blob)["text"])
# the GI JOE banner: a logo word plus a solid bar — not lettering
_ban = Image.new("RGBA", (600, 120), (0, 0, 0, 0))
_bd = _ID.Draw(_ban)
_bd.text((10, 25), "GI JOE", font=_IF.truetype(str(vr._find_font("Arial Black")), 60), fill=(255, 255, 255, 255))
_bd.rectangle([260, 30, 590, 95], fill=(255, 255, 255, 255))
check("a logo with a solid banner bar is not taken for lettering", not _dec.text_geometry(_ban)["text"])
# a condensed word in a narrow box is fitted to the box, not clipped
_cond = {"lines": [{"text": "DANGER!", "colour": "#cc1020", "weight": "bold", "italic": False}],
         "align": "center"}
_svgc = vr.typeset_lines(_cond, [(5, 10, 135, 50)], 140, 60, 0.47, 0.2, palette=[(206, 22, 30)])
_rc = np.asarray(vr.render_svg(_svgc, 140))
_cols = np.nonzero(_rc[..., 3].max(0) > 0)[0]
check("a condensed word is squeezed to the box the scan shows (no clipping)",
      _cols.size and _cols.min() >= 3 and _cols.max() <= 137, (_cols.min() if _cols.size else None, _cols.max() if _cols.size else None))
_read = {"lines": [{"text": "DANGER", "colour": "#cc1020", "weight": "bold", "italic": False},
                   {"text": "JET BLAST", "colour": "#cc1020", "weight": "bold", "italic": False}],
         "align": "left"}
_svg = vr.typeset_lines(_read, _geom["lines"], 420, 160, 1.4, 0.533, palette=[(206, 22, 30)])
check("typeset_lines sets the words as outlines in the scan's colour",
      _svg is not None and "<text" not in _svg and "#ce161e" in _svg and _svg.count("<path") >= 14, (_svg or "")[:200])
_okc, _iou, _col = vr.check_against_scan(_svg, _tcrop, min_iou=0.4)
check("…and the typeset version overlaps the scan", _okc and _iou > 0.6, (_iou, _col))
check("a row/line mismatch is refused (None)",
      vr.typeset_lines({"lines": _read["lines"][:1], "align": "left"}, _geom["lines"], 420, 160, 1.4, 0.533) is None)
check("_find_font knows the regular and italic faces",
      vr._find_font("Arial Regular") is not None and vr._find_font("Arial Regular").name.lower() == "arial.ttf"
      and vr._find_font("Arial Bold Italic").name.lower() == "arialbi.ttf"
      and vr._find_font("Arial Bold").name.lower() == "arialbd.ttf"
      and vr._find_font("Arial Black").name.lower() == "ariblk.ttf")


class _ReadResp(_Resp):
    class _B:
        type = "text"
        text = _json.dumps(_read)
    content = [_B()]


class _YesResp(_Resp):
    class _B:
        type = "text"
        text = "YES"
    content = [_B()]


class _NoResp(_Resp):
    class _B:
        type = "text"
        text = "NO"
    content = [_B()]


_VERIFY = {"answer": "YES"}


class _ReadClient:
    class messages:
        @staticmethod
        def create(**kw):
            _calls.append(kw)
            if kw.get("system") == vr.VERIFY_SYSTEM:
                return _YesResp() if _VERIFY["answer"] == "YES" else _NoResp()
            return _ReadResp()


_calls.clear()
_st = {}
_tfn = vr.make_text_fn(_ReadClient(), "claude-opus-5-5", 300, stats=_st)
_got = _tfn(_tcrop, [(206, 22, 30)], 1.4, 0.533, _geom)
_read_kw = [c for c in _calls if c.get("system") != vr.VERIFY_SYSTEM][-1]
check("make_text_fn reads, sets, checks and renders a lettering decal",
      _got is not None and "<path" in _got[0] and _got[1].size[0] == 420 and _st["text"] == 1
      and _read_kw["output_config"]["effort"] == "medium" and _read_kw["max_tokens"] == 1200, (_st, _read_kw.get("max_tokens")))
check("…the SVG carries the printed size", 'width="1.4000in"' in _got[0])
check("…and its spelling was checked by a second look (scan beside type)",
      any(c.get("system") == vr.VERIFY_SYSTEM for c in _calls) and _st["calls"] == 2, _st)
_VERIFY["answer"] = "NO"
_st2 = {}
_got2 = vr.make_text_fn(_ReadClient(), "claude-opus-5-5", 300, stats=_st2)(
    _tcrop, [(206, 22, 30)], 1.4, 0.533, _geom)
_VERIFY["answer"] = "YES"
check("a typeset word the second look says is misspelt is NOT used (AWAY → ARMY)",
      _got2 is None and _st2["text"] == 0 and _st2["text_fallback"] == 1, _st2)


class _UnsureReadResp(_Resp):
    class _B:
        type = "text"
        text = "UNSURE"
    content = [_B()]


class _UnsureReadClient:
    class messages:
        @staticmethod
        def create(**kw):
            return _UnsureReadResp()


_st2 = {}
check("an UNSURE reading falls through (None) and is counted",
      vr.make_text_fn(_UnsureReadClient(), "m", 300, stats=_st2)(_tcrop, None, 1.4, 0.533, _geom) is None
      and _st2["unsure"] == 1 and _st2["text_fallback"] == 1 and _st2["text"] == 0)


class _LimitReadClient:
    class messages:
        @staticmethod
        def create(**kw):
            raise RuntimeError("Error code: 429 - {'message': 'Your usage limit has been reached'}")


try:
    vr.make_text_fn(_LimitReadClient(), "m", 300, stats={})(_tcrop, None, 1.4, 0.533, _geom)
    _stopped = False
except RuntimeError as _e:
    _stopped = "usage limit" in str(_e).lower() or "stopped the calls" in str(_e).lower()
check("a usage-limit error stops the text sweep too", _stopped)

print("transparent background")
_bg_svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">'
           '<rect x="0" y="0" width="200" height="100" fill="#aab9c3"/>'
           '<rect width="100%" height="100%" fill="#ffffff"></rect>'
           '<rect x="20" y="20" width="60" height="60" fill="#cc1020"/></svg>')
_nobg = vr.strip_background(_bg_svg, 200, 100)
check("full-canvas backdrops are removed, the decal's own shapes stay",
      _nobg.count("<rect") == 1 and 'fill="#cc1020"' in _nobg, _nobg)
_clear = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
_clear.paste((200, 20, 30, 255), (20, 20, 80, 80))
_panel = Image.new("RGBA", (100, 100), (20, 20, 20, 255))
check("a decal with clear corners is stripped; a filled panel is not",
      vr._corners_clear(_clear) and not vr._corners_clear(_panel))

print("local models (Ollama)")
import requests as _rq
_posted = []


class _OResp:
    status_code = 200

    def __init__(self, text):
        self._t = text

    def json(self):
        return {"message": {"content": self._t}, "prompt_eval_count": 900, "eval_count": 120}


_orig_post = _rq.post
try:
    _rq.post = lambda url, json=None, timeout=None, **k: (_posted.append((url, json)), _OResp(_json.dumps(_read)))[1]
    _ov = vr.OllamaVision(base_url="http://127.0.0.1:11434")
    _rr = vr.read_text_decal(_tcrop, 2, palette=[(206, 22, 30)], model="ollama:qwen3-vl:8b", client=_ov)
    _u, _b = _posted[-1]
    check("the Ollama client sends the same ask: model tag, system, the image, no thinking",
          _u.endswith("/api/chat") and _b["model"] == "qwen3-vl:8b" and _b["messages"][0]["role"] == "system"
          and _b["messages"][1].get("images") and _b["think"] is False
          and _rr["lines"][0]["text"] == "DANGER" and _rr["cost_usd"] == 0.0, _b.get("model"))
    _rq.post = lambda url, json=None, timeout=None, **k: (_posted.append((url, json)), _OResp(
        '<svg viewBox="0 0 100 100" width="1in" height="1in"><rect x="20" y="20" width="60" height="60" fill="#c8141e"/></svg>'))[1]
    _st3 = {}
    _vfn = vr.make_vector_fn(_ov, "ollama:qwen3-vl:8b", 300, stats=_st3)
    _gotv = _vfn(_clear, [(200, 20, 30)], 1.0, 1.0)
    check("a local model draws through the same vector_fn, at no cost",
          _gotv is not None and _st3["ok"] == 1 and _st3["cost"] == 0.0, _st3)

    class _Missing:
        status_code = 404

        def json(self):
            return {"error": "model 'qwen3-vl:32b' not found"}
        text = "not found"
    _rq.post = lambda url, json=None, timeout=None, **k: _Missing()
    try:
        vr.make_vector_fn(vr.OllamaVision(), "ollama:qwen3-vl:32b", 300, stats={})(_clear, None, 1.0, 1.0)
        _stop = ""
    except RuntimeError as _e:
        _stop = str(_e)
    check("a model that is not pulled stops the run and says how to get it",
          "not installed" in _stop and "Pull" in _stop, _stop)
finally:
    _rq.post = _orig_post
check("a local model costs nothing", vr.estimate_cost("ollama:gemma3:27b", None) == 0.0)

print("cancel is immediate")
import threading as _th, time as _tm
_flag = _th.Event()


class _SlowClient:
    class messages:
        @staticmethod
        def create(**kw):
            _tm.sleep(5)
            return _OrientResp()

    beta = messages


_th.Timer(0.3, _flag.set).start()
_t0 = _tm.time()
_st4 = {}
_got4 = vr.make_vector_fn(_SlowClient(), "claude-opus-5-5", 300, stats=_st4,
                          cancelled=_flag.is_set)(_clear, None, 1.0, 1.0)
_dt = _tm.time() - _t0
check("Cancel abandons a model call in flight at once (well under its 5 s)",
      _got4 is None and _dt < 1.0, round(_dt, 2))
_flag.clear()
check("call_cancellable passes a result and an error through",
      vr.call_cancellable(lambda: 7, _flag.is_set) == 7)
try:
    vr.call_cancellable(lambda: (_ for _ in ()).throw(ValueError("x")), _flag.is_set)
    _raised2 = False
except ValueError:
    _raised2 = True
check("…and re-raises the call's own error", _raised2)

print("pull progress")
import json as _j2


class _PullResp:
    status_code = 200

    def __init__(self, lines):
        self._l = lines

    def raise_for_status(self):
        pass

    def iter_lines(self):
        for x in self._l:
            yield _j2.dumps(x).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_lines = [{"status": "pulling manifest"},
          {"status": "pulling a", "digest": "sha:a", "total": 20_000_000_000, "completed": 5_000_000_000},
          {"status": "pulling b", "digest": "sha:b", "total": 1_000_000_000, "completed": 0},
          {"status": "pulling a", "digest": "sha:a", "total": 20_000_000_000, "completed": 20_000_000_000},
          {"status": "pulling b", "digest": "sha:b", "total": 1_000_000_000, "completed": 1_000_000_000},
          {"status": "success"}]
_seen = []
_orig_post2 = _rq.post
try:
    _rq.post = lambda url, json=None, stream=False, timeout=None, **k: _PullResp(_lines)
    vr.ollama_pull("qwen3-vl:32b", on_bytes=lambda d, t, s: _seen.append((d, t)))
finally:
    _rq.post = _orig_post2
check("pull progress sums every layer of the download",
      _seen[0] == (5_000_000_000, 20_000_000_000) and _seen[-1] == (21_000_000_000, 21_000_000_000)
      and _seen[1] == (5_000_000_000, 21_000_000_000), _seen)
_eta = vr.pull_eta_text(7_800_000_000, 21_000_000_000, 100.0, 3_600_000_000)
check("the ETA reads percent, GB, speed and time left",
      _eta.startswith("37% · 7.8 / 21.0 GB · 42 MB/s · about 5 min left"), _eta)
check("…and says it is measuring before it knows the speed",
      "measuring" in vr.pull_eta_text(0, 21_000_000_000, 0.1))

print("repeated decals")
# a sheet: the same red label 4 times (one turned, one mirrored), a different one
_sh = Image.new("RGBA", (900, 300), (0, 0, 0, 0))
_lab = Image.new("RGBA", (160, 60), (0, 0, 0, 0))
_ld = _ID.Draw(_lab)
_ld.rectangle([0, 0, 159, 59], fill=(200, 20, 30, 255))
_ld.polygon([(10, 10), (60, 30), (10, 50)], fill=(255, 255, 255, 255))
_ld.rectangle([80, 15, 150, 25], fill=(255, 255, 255, 255))
_sh.alpha_composite(_lab, (20, 20))
_sh.alpha_composite(_lab, (220, 20))
_sh.alpha_composite(_lab.rotate(180), (420, 20))
_sh.alpha_composite(_lab.transpose(Image.FLIP_LEFT_RIGHT), (620, 20))
_other = Image.new("RGBA", (160, 60), (0, 0, 0, 0))
_ID.Draw(_other).ellipse([0, 0, 159, 59], fill=(20, 40, 160, 255))
_sh.alpha_composite(_other, (20, 200))
_boxes = _dec.segment_decals(_sh, gap=6)
_groups = _dec.find_copies(_sh, _boxes)
_hows = sorted(h for g in _groups for _i, h in g)
check("copies are found — straight, turned and mirrored — and the different decal is not",
      len(_groups) == 1 and len(_groups[0]) == 4 and _hows == ["", "", "mirror", "turn"], (_groups, len(_boxes)))
_st5 = {}
_out5 = _dec.redraw_sheet(_sh, None, native_dpi=300, target_dpi=150, gap=6, stats=_st5)
_srcs = [it["source"] for it in _out5["items"]]
check("the best copy is placed on the others (the 4th reused); the different decal drawn itself",
      len(_out5["items"]) == 5 and _st5["copy_groups"] == 1 and _st5["copies"] >= 1 and "copy" in _srcs, (_st5, _srcs))
_by = {it["box"][:2]: it for it in _out5["items"]}
_tr = np.asarray(_out5["rgba"])
# the turned copy's triangle points the other way: white near its right end, red near its left end
_k = 150 / 300.0
_y = int(round(50 * _k))
check("a turned copy is drawn turned (its arrow points the other way)",
      _tr[_y, int(round((420 + 160 - 30) * _k)), :3].min() > 200
      and _tr[_y, int(round((420 + 20) * _k)), 0] > 150, (_tr[_y, int(round((420 + 160 - 30) * _k))].tolist(), _tr[_y, int(round(440 * _k))].tolist()))
_st6 = {}
_out6 = _dec.redraw_sheet(_sh, None, native_dpi=300, target_dpi=150, gap=6, stats=_st6, reuse_copies=False)
check("…and with the switch off every decal is drawn on its own",
      _st6["copies"] == 0 and all(it["source"] != "copy" for it in _out6["items"]))

print("the key store")
t = "AIImageGeneratorSuite/_test_vr"
check("write/read/delete round trip in the Credential Manager",
      vr.cred_write(t, "secret-test-123") and vr.cred_read(t) == "secret-test-123"
      and vr.cred_write(t, "") and vr.cred_read(t) is None)
# make_vector_fn: the closure the app hands to redraw_sheet
_st = {}
_vf = vr.make_vector_fn(_Client(), "claude-sonnet-5-5", 300, hint="label",
                        stats=_st)
_got = _vf(crop, pal, 0.5, 0.2)
check("make_vector_fn returns (svg, raster) for a drawing that checks out",
      _got is not None and _got[0].startswith("<svg")
      and 'width="0.5000in"' in _got[0] and _got[1].size[0] == 150,
      (None if _got is None else (_got[0][:80], _got[1].size)))
check("…and books the call and its cost",
      _st["calls"] == 1 and _st["ok"] == 1 and abs(_st["cost"] - 0.015) < 1e-6, _st)
check("a cancelled run draws nothing",
      vr.make_vector_fn(_Client(), "claude-sonnet-5-5", 300,
                        cancelled=lambda: True)(crop, pal, 0.5, 0.2) is None)


class _Boom:
    class beta:
        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError("network down")
    messages = beta.messages


class _UnsureResp(_Resp):
    class _B:
        type = "text"
        text = "UNSURE"
    content = [_B()]


class _UnsureClient(_Client):
    class beta:
        class messages:
            @staticmethod
            def create(**kw):
                return _UnsureResp()


_st3 = {}
check("an UNSURE answer falls back to the trace and is counted as such",
      vr.make_vector_fn(_UnsureClient(), "claude-sonnet-5-5", 300, stats=_st3)(crop, pal, 0.5, 0.2) is None
      and _st3["unsure"] == 1 and _st3["fallback"] == 1 and _st3["ok"] == 0, _st3)
check("the ask tells the model to say UNSURE rather than guess",
      "UNSURE" in vr.SYSTEM and "do NOT guess" in vr.SYSTEM)
class _Capped:
    class beta:
        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError("Error code: 400 - {'type': 'error', 'error': "
                                   "{'type': 'invalid_request_error', 'message': "
                                   "'You have reached your specified API usage "
                                   "limits. You will regain access on 2026-11-01 "
                                   "at 00:00 UTC.'}}")
    messages = beta.messages


try:
    vr.make_vector_fn(_Capped(), "claude-sonnet-5-5", 300)(crop, pal, 0.5, 0.2)
    check("a usage-limit error stops the run with a clear message", False)
except RuntimeError as e:
    check("a usage-limit error stops the run with a clear message",
          "usage limits" in str(e) and "console.anthropic.com" in str(e), str(e))
_st2 = {}
check("a failed call falls back (None) and is counted",
      vr.make_vector_fn(_Boom(), "claude-sonnet-5-5", 300, stats=_st2)(crop, pal, 0.5, 0.2) is None
      and _st2["fallback"] == 1)
check("a missing target reads as None", vr.cred_read("AIImageGeneratorSuite/_nope") is None)

print()
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED: " + f)
sys.exit(1 if FAIL else 0)
