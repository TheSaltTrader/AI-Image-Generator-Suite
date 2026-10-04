# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('C:\\users\\renoi\\claudecode\\Comic_book__art_creator\\app\\models_manifest.json', '.'), ('C:\\users\\renoi\\claudecode\\Comic_book__art_creator\\app\\icon.ico', '.')]
datas += [('C:\\users\\renoi\\claudecode\\Comic_book__art_creator\\app\\fonts', 'fonts')]   # bundled OFL fonts + LICENSES.txt
binaries = []
hiddenimports = []
tmp_ret = collect_all('av')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# Decals pipeline deps (PDF read + vectorize) — bundle so the Decals tab runs
# in-process without a separate install
for _pkg in ('pymupdf', 'vtracer'):
    try:
        _r = collect_all(_pkg)
        datas += _r[0]; binaries += _r[1]; hiddenimports += _r[2]
    except Exception:
        pass
hiddenimports += ['pymupdf', 'fitz', 'vtracer']
# Vision redraw (v2.16): the official Anthropic SDK (httpx2/pydantic come
# along) + fontTools for text -> outlines in the SVGs.
# fontTools: NOT collect_all — that dragged fontTools.misc.symfont -> sympy
# and, through the shared dev venv, torch/transformers/onnxruntime into a
# 3 GB exe. ttLib loads its table modules by name at runtime, so those are
# collected explicitly; everything else is found by the import analysis.
from PyInstaller.utils.hooks import collect_submodules
try:
    _r = collect_all('anthropic')
    datas += _r[0]; binaries += _r[1]; hiddenimports += _r[2]
except Exception:
    pass
hiddenimports += ['anthropic']
hiddenimports += collect_submodules('fontTools.ttLib')
hiddenimports += ['fontTools', 'fontTools.pens.svgPathPen',
                  'fontTools.pens.basePen', 'fontTools.misc.textTools']


a = Analysis(
    ['C:\\users\\renoi\\claudecode\\Comic_book__art_creator\\app\\comic_art_creator.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # the dev venv doubles as the ENGINE runtime (torch & co.); none of this
    # belongs in the app exe — the engine runs in its own process
    excludes=['torch', 'torchaudio', 'torchvision', 'functorch', 'einops',
              'transformers', 'huggingface_hub', 'safetensors', 'cv2',
              'scipy', 'sympy', 'mpmath', 'networkx', 'onnxruntime',
              'tokenizers', 'triton', 'accelerate', 'peft', 'matplotlib',
              'IPython', 'pandas', 'sklearn', 'fontTools.misc.symfont'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AIImageGeneratorSuite',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='C:\\users\\renoi\\claudecode\\Comic_book__art_creator\\app\\version_app.txt',
    icon=['C:\\users\\renoi\\claudecode\\Comic_book__art_creator\\app\\icon.ico'],
)
