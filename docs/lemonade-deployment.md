# Deploying under Lemonade (the "plane" recipe)

[Lemonade](https://github.com/lemonade-sdk/lemonade) is the natural Windows
serving layer for Strix Halo, but Flash-Next needs a fork engine, and Lemonade
has three behaviors that silently break this setup. This page is the working
recipe plus those traps.

## Why a separate instance ("plane")

Run the fork-engine model in its **own Lemonade instance** on its own port,
localhost-bound, rather than adding it to your main instance:

- the engine swap is per-instance, so your other models keep their stock engine;
- `max_loaded_models 1` makes its memory behavior predictable;
- the [admission shim](../shim/README.md) in front arbitrates the 74 GB
  footprint against your other instances with pure config — no code.

## The recipe

1. **Cache dir** (e.g. `D:\LemonadeFN\cache`) — `config.json` with:
   ```json
   { "host": "127.0.0.1", "port": 13307, "max_loaded_models": 1,
     "disable_model_filtering": true,
     "llamacpp": { "backend": "rocm", "prefer_system": true, "args": "-fa on" } }
   ```
2. **Model registration** — `user_models.json`:
   ```json
   {
     "Qwen3.8-Flash-Next-MTP": {
       "checkpoints": {
         "main":  "unsloth/Qwen3.8-Flash-Next-GGUF:Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf",
         "draft": "unsloth/Qwen3.8-Flash-Next-GGUF:mtp-Qwen3.8-Flash-Next-Q8_0.gguf"
       },
       "labels": ["custom", "mtp", "tool-calling"],
       "recipe": "llamacpp",
       "recipe_options": { "ctx_size": 262144, "llamacpp_args": "<the working flag set — see model-and-flags.md>" }
     }
   }
   ```
3. **Engine swap** — replace `cache\bin\llamacpp\rocm-nightly\` contents with
   your fork build + the runtime DLLs (see
   [build-engine-windows.md](build-engine-windows.md)), and read trap 2 below
   before you do.
4. **Start**: `lemond.exe <cache-dir> --port 13307`. Cold load of the 74 GB
   model: ~30 s.
5. **Boot task** (optional): schedule `lemond.exe` AtStartup with **S4U** logon
   so it returns headless after an unattended reboot — an Interactive trigger
   waits for a logon and can leave the server down for days.

## THE THREE TRAPS

### 1. Subfoldered HF repos make the model invisible

The unsloth repo ships quants in subfolders (`UD-IQ4_XS/`, `MTP/`). Lemonade's
downloaded-file detection only sees **snapshot-root** files, so the model
registers but is *hidden from `/v1/models`*. The tell is a debug-log line like
"N total, M downloaded" with M short.

**Fix**: hardlink the shards + drafter flat into the HF snapshot root and
reference the flat filenames in `user_models.json`:

```powershell
Get-ChildItem "$snap\UD-IQ4_XS\*.gguf","$snap\MTP\*.gguf" | ForEach-Object {
  New-Item -ItemType HardLink -Path (Join-Path $snap $_.Name) -Target $_.FullName }
```

### 2. Lemonade restores its bundled engine over yours

`lemond` pins an engine version. If `version.txt` in the engine dir does not
match the pin, it silently **restores the stock engine over your fork build**.
The symptom appears later as `unknown model architecture: 'qwen4exp'`, and the
engine dir's timestamp equals the load time.

**Fix**: make `version.txt` claim the pinned version string while the binaries
are your build, and keep the truth in a `PROVENANCE.txt` beside it. Re-check
after **every** Lemonade app upgrade — an upgrade changes the pin.

### 3. PowerShell 5.1 `-Encoding UTF8` writes a BOM

Windows PowerShell 5.1 writes UTF-8 **with BOM**, and strict JSON parsers
reject the file — the config just doesn't apply. Write configs with:

```powershell
[IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding($false)))
```

## Verifying the whole stack

- `GET :13307/api/v1/models` — the model must be listed (trap 1 check).
- First completion request — watch the plane's log for the fork engine banner,
  not a restored stock engine (trap 2 check).
- Through the shim: `GET :13304/_shim/preflight/Qwen3.8-Flash-Next-MTP` shows
  what would be evicted to admit it, without moving memory.

## When the fork retires

Upstream llama.cpp PRs **#28243** (qwen4exp MTP head) and **#28118**
(recurrent-state checkpoints) are the two that matter. Once both merge and a
llamacpp-rocm build ships past them, the stock engine can serve MTP and the
engine swap (and trap 2) disappears from this recipe. Until then, watch those
PRs after every engine upgrade.
