# Qwen3.8-Flash-Next on Windows (Strix Halo)

Qwen3.8-Flash-Next — 125B MoE, ~6B active, arch `qwen4exp` — running on
**Windows** on Strix Halo (Ryzen AI Max+ 395, gfx1151, 128 GB unified) with
working **MTP speculative decoding**:

> **38 t/s decode** through a full serving stack · 85–100% draft accept ·
> 262144 native context · 74 GB GPU footprint · ~30 s cold load

The Strix Halo community has good Linux coverage for this model
([discussion #27950](https://github.com/ggml-org/llama.cpp/discussions/27950)).
Windows is different enough — MSVC-ABI HIP builds, WDDM spill behavior,
Lemonade's engine pinning, PowerShell encoding — that we kept notes. This repo
is those notes, plus the tooling we run in production.

## What's here

| | |
|---|---|
| [docs/build-engine-windows.md](docs/build-engine-windows.md) | Building the patched llama.cpp for gfx1151/HIP on Windows: toolchain, cmake flags, runtime-DLL packaging, and how to verify with a stripped PATH |
| [docs/model-and-flags.md](docs/model-and-flags.md) | Quant selection (why UD-IQ4_XS), the full working flag set with rationale, standalone serving, correctness-first validation |
| [docs/lemonade-deployment.md](docs/lemonade-deployment.md) | Serving under Lemonade, and **the three traps** that silently break it |
| [docs/benchmarks.md](docs/benchmarks.md) | All measured numbers and methodology |
| [shim/](shim/README.md) | **Admission shim** — a FastAPI preflight proxy that lets multiple Lemonade instances share one unified-memory carve without freezing the box. Model-agnostic; useful beyond this model |
| [scripts/](scripts/) | The validation harnesses: MTP coherence gates, load-only footprint measurement, deterministic quant A/B |

## What this is not

- **Not our model support.** The `qwen4exp` + MTP engine code is
  [Stew Forster's rdna-boosts patch set](https://github.com/stew675/llama-cpp-rdna-boosts)
  (14 blocks, all authored by him), building on upstream PRs
  [#27836](https://github.com/ggml-org/llama.cpp/pull/27836), #28243 and
  #28118 (unmerged at time of writing). This repo is the Windows build,
  deployment, and serving layer around that work.
- **No binaries, no weights.** You build the engine from source and pull the
  GGUFs from [unsloth/Qwen3.8-Flash-Next-GGUF](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF).

## Quickstart

1. Build the engine — [docs/build-engine-windows.md](docs/build-engine-windows.md)
2. Download the model + MTP sidecar, pick flags — [docs/model-and-flags.md](docs/model-and-flags.md)
3. Serve standalone, gate it with `scripts/probe-mtp.ps1`
4. Optional: deploy under Lemonade behind the shim — [docs/lemonade-deployment.md](docs/lemonade-deployment.md)

## Upstream status (read before investing)

- **Merged**: `qwen4exp` base arch (PR 27742, 2026-08-27); the gfx1151 decode
  cliff fix (#27856, closed 2026-09-07). The base model runs on stock engines.
- **Unmerged** (the reason the fork engine is needed): PR **#28243** (MTP/NextN
  draft head) and PR **#28118** (recurrent-state checkpoints — without which
  MTP is a net loss on gfx1151).
- When both merge and a llamacpp-rocm build ships past them, the engine-build
  half of this repo becomes history; the Lemonade recipe, shim, and
  measurement tooling remain.

## Tested on

Corsair AXB35-02 (Strix Halo / Ryzen AI Max+ 395, Radeon 8060S gfx1151),
128 GB board with a 96 GB iGPU carve, Windows 11, ROCm 10.1 TheRock nightly
SDK, Lemonade 11.x. Your carve split changes the fit arithmetic — re-measure,
don't copy our footprints (`scripts/measure-fit.ps1`).

## Credits

- **Stew Forster** ([stew675](https://github.com/stew675)) — the entire
  rdna-boosts engine patch set, including qwen4exp MTP support.
- The authors of upstream PRs #27836 / #28243 / #28118 and the participants in
  [discussion #27950](https://github.com/ggml-org/llama.cpp/discussions/27950).
- [unsloth](https://huggingface.co/unsloth) — the Dynamic quants and the MTP
  sidecar GGUF.
- The [llama.cpp](https://github.com/ggml-org/llama.cpp) and
  [Lemonade](https://github.com/lemonade-sdk/lemonade) projects.

## License

MIT — see [LICENSE](LICENSE). Applies to the shim, scripts, and docs in this
repo; the engine source and model weights carry their own licenses.
