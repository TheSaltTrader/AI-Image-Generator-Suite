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
