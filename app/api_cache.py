"""Answers from paid services, remembered.

Every Anthropic request (the exact model, prompt and image) and every
Recraft picture is hashed; the answer is kept on disk under that hash. The
same request again — a rerun of a sheet, the same decal printed twice, a
redraw after a small tweak that left a decal's crop unchanged — is answered
from the disk for free, and its cost counts as $0. A changed crop, prompt
or model is a new request and is paid for as usual.

The cache holds only answers (text, SVG) under hashes: no image and no key
is stored. Delete the folder to start over.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

_lock = threading.Lock()
_DIR: Path | None = None
STATS = {"hits": 0, "misses": 0}


def set_dir(path) -> None:
    """Where answers are kept (None switches the cache off)."""
    global _DIR
    _DIR = Path(path) if path else None
    if _DIR is not None:
        _DIR.mkdir(parents=True, exist_ok=True)


def get_dir():
    return _DIR


def _key(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _path(kind: str, key: str):
    if _DIR is None:
        return None
    return _DIR / kind / key[:2] / f"{key}.json"


def load(kind: str, key: str):
    p = _path(kind, key)
    if p is None or not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save(kind: str, key: str, value) -> None:
    p = _path(kind, key)
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp%d" % os.getpid())
        with _lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(value, f)
            os.replace(tmp, p)
    except Exception:
        pass


def bytes_key(data: bytes, *extra) -> str:
    h = hashlib.sha256(data)
    for e in extra:
        h.update(repr(e).encode("utf-8"))
    return h.hexdigest()


# ------------------------------------------------------------ Anthropic
def _usage_dict(u):
    return {k: int(getattr(u, k, 0) or 0) for k in
            ("input_tokens", "output_tokens", "cache_read_input_tokens",
             "cache_creation_input_tokens")}


def _to_dict(resp):
    content = []
    for b in getattr(resp, "content", []) or []:
        if getattr(b, "type", "") == "text":
            content.append({"type": "text", "text": b.text})
    return {"content": content,
            "stop_reason": getattr(resp, "stop_reason", None),
            "model": getattr(resp, "model", None)}


def _from_dict(d):
    """A response rebuilt from the disk: same text, zero usage (free)."""
    blocks = [SimpleNamespace(type="text", text=b["text"]) for b in d["content"]]
    zero = SimpleNamespace(input_tokens=0, output_tokens=0,
                           cache_read_input_tokens=0,
                           cache_creation_input_tokens=0)
    return SimpleNamespace(content=blocks, stop_reason=d.get("stop_reason"),
                           model=d.get("model"), usage=zero, _cached=True)


def request_key(kw: dict) -> str:
    """The hash of one messages.create request (everything that shapes the
    answer; the timeout does not)."""
    k = {x: kw[x] for x in sorted(kw) if x not in ("timeout", "extra_headers")}
    return _key(k)


class _Endpoint:
    """client.messages or client.beta.messages, with answers remembered."""

    def __init__(self, owner, beta):
        self._owner = owner
        self._beta = beta

    def create(self, **kw):
        return self._owner._create(kw, self._beta)

    def __getattr__(self, name):          # batches, count_tokens, …
        src = self._owner._inner.beta if self._beta else self._owner._inner
        return getattr(src.messages, name)


class CachingClient:
    """Wraps an Anthropic client (or the Ollama stand-in): messages.create
    and beta.messages.create answer from the disk when the very same
    request was answered before. A refusal or an error is never remembered.
    (v2.25.0 routed beta requests to messages.create: every vision redraw
    failed with "unexpected keyword argument 'betas'".)"""

    def __init__(self, inner):
        self._inner = inner
        self.messages = _Endpoint(self, beta=False)
        self.beta = type("_Beta", (), {})()
        self.beta.messages = _Endpoint(self, beta=True)

    def __getattr__(self, name):          # anything else: the real client
        return getattr(self._inner, name)

    def create(self, **kw):               # old call style
        return self._create(kw, "betas" in kw)

    def _create(self, kw, beta):
        # the plain request hash: a beta request carries its own "betas"
        # field, and the stored answers / batch mode keep matching
        key = request_key(kw)
        got = load("anthropic", key)
        if got is not None:
            STATS["hits"] += 1
            return _from_dict(got)
        if beta:
            resp = self._inner.beta.messages.create(**kw)
        else:
            resp = self._inner.messages.create(**kw)
        STATS["misses"] += 1
        if getattr(resp, "stop_reason", None) != "refusal":
            save("anthropic", key, _to_dict(resp))
        return resp


def wrap(client):
    """The client, caching when a cache folder is set."""
    if client is None or _DIR is None or isinstance(client, CachingClient):
        return client
    return CachingClient(client)


def preload(kind: str, key: str, value) -> None:
    """Store an answer obtained another way (a batch) for later requests."""
    save(kind, key, value)
