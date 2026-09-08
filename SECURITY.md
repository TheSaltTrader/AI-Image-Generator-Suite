# Security review — AI Image Generator Suite (formerly Comic Book Art Creator) v1.0.0

Scope: `app\comic_art_creator.py`, `setup.ps1`, launcher scripts, and the
runtime architecture (Tkinter frontend + headless ComfyUI engine on
loopback). Review performed 2026-08-06 against the OWASP Top 10 (2021),
interpreted for a single-user desktop application.

## Threat model

Local desktop app, single user, no server component. Network traffic is:
(a) loopback HTTP/WebSocket to the local engine, (b) HTTPS downloads from
huggingface.co and civitai.com. Sensitive data held: the user's CivitAI API
key. Primary attack surfaces: downloaded model files, the CivitAI API
responses, and the local engine port.

## OWASP Top 10 assessment

| # | Category | Status | Notes |
|---|----------|--------|-------|
| A01 | Broken Access Control | ✅ Pass (accepted risk noted) | Engine bound to `127.0.0.1` only — never reachable from the network. Like all ComfyUI installs it has no auth, so any *local* process could drive it. Accepted for a personal machine. |
| A02 | Cryptographic Failures | ✅ Fixed in review | CivitAI API key was plaintext in `settings.json`; now encrypted at rest with Windows DPAPI (user-scoped `CryptProtectData`), masked in the UI. All remote calls are HTTPS with certificate verification (requests defaults; no `verify=False` anywhere). |
| A03 | Injection | ✅ Pass (hardened in review) | No `shell=True`; all subprocesses use fixed argument lists. Path-traversal fix: CivitAI-supplied filenames are reduced to a basename, must end in `.safetensors`, and the resolved destination is verified to stay inside `models\loras`. Prompts travel only as JSON to the local engine. |
| A04 | Insecure Design | ✅ Pass | **safetensors-only policy**: `.ckpt`/pickle formats (which can execute code on load) are neither listed, offered, nor downloaded — enforced in the folder scanner and the CivitAI downloader. No auto-update/remote-code path exists. |
| A05 | Security Misconfiguration | ✅ Pass | Engine launched with explicit flags (loopback listen, fixed port, scratch output dir, no auto-launch UI). No debug endpoints exposed. Logs are local files with no secrets. |
| A06 | Vulnerable Components | ⚠️ Ongoing duty | Fresh installs pull current ComfyUI/torch/rembg. Re-run `setup.ps1` occasionally to update the engine stack. The exe is unsigned — SmartScreen will show "unrecognized app" on other machines (More info → Run anyway, or unblock the file). |
| A07 | Auth Failures | ✅ Pass | Only credential is the optional CivitAI key: masked entry field, DPAPI at rest, sent solely to `civitai.com` in an `Authorization` header over HTTPS. |
| A08 | Software & Data Integrity | ⚠️ Partial | Downloads ride HTTPS from fixed, reputable hosts (HuggingFace, CivitAI + its CDN, python.org, bootstrap.pypa.io, github.com, download.pytorch.org; non-HTTPS download URLs are refused). No SHA-256 pinning of model files yet — reasonable next step for the setup script. safetensors is a data-only format, so a tampered model can at worst produce bad images, not code execution. |
| A09 | Logging Failures | ✅ Pass | `engine.log` holds engine stdout only; the API key is never logged or echoed. PNG metadata embeds prompts/settings by design (user-visible feature). |
| A10 | SSRF | ✅ Pass | API hosts are hard-coded; the CivitAI model id is regex-extracted digits; the returned `downloadUrl` must be HTTPS. The app never fetches user-typed URLs directly. |

## Residual risks (accepted, documented)

1. **Unauthenticated local engine** — inherent to ComfyUI; loopback-only.
2. **No model checksums** — HTTPS + trusted hosts only; add hash pinning if
   supply-chain hardening becomes a priority.
3. **Unsigned executable** — expect SmartScreen on machines that download
   the release; code-signing would remove this.
4. **Community models/LoRAs are untrusted content** — safetensors keeps this
   at the image level, not the code level.

## Secure-coding notes for contributors

- Never add `shell=True`, `pickle.load` on downloaded data, or `.ckpt`
  support.
- Any new download source must be HTTPS with a fixed host allow-list, and
  filenames must be sanitized to basenames before writing.
- Secrets go through `dpapi_encrypt`/`dpapi_decrypt` — never plaintext JSON.
- Keep the engine on `127.0.0.1`; never expose `--listen 0.0.0.0`.


## Delta review — v1.24.0 (2026-08-12)

Scope: everything added since the original audit (trigger auto-injection,
gen-then-swap / image-swap checkbox, chained Kontext references, sidecar
handling).

- **LoRA sidecar / metadata parsing** (`lora_trigger`,
  `_safetensors_metadata`): all reads are bounded — safetensors header cap
  20 MB, sidecar JSON cap 5 MB, and the extracted trigger phrase is capped
  at 200 chars before it can reach the prompt (a hostile "LoRA" shipped
  with a megabyte trigger cannot balloon requests). Parsing is
  `json.loads` inside try/except; values are only ever joined into prompt
  TEXT for the loopback engine — no path, shell or query construction.
  File paths come from the app's own LoRA-folder scan, never from user
  strings (imports still sanitize to basename, safetensors-only).
- **CivitAI trained-words sidecar write**: JSON-encoded via `json.dumps`,
  written next to the already-sanitized basename inside `models\loras`.
  No new hosts; the existing HTTPS + fixed-host rules are unchanged.
- **Swap pipeline**: uploads and renders go exclusively to the loopback
  engine (`127.0.0.1:8188`); the new `ref_mode`/`guidance` values are
  internal constants, never user-supplied strings.
- **UI/persistence**: the image-swap checkbox persists as a boolean in
  `settings.json`; no secrets, no new files outside the app folder, no
  new listeners, no new dependencies.

Verdict: no new attack surface beyond bounded local-file parsing, which is
now explicitly capped. A01/A03/A05/A08/A10 postures unchanged.
