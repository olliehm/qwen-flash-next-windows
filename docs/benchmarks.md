# Measured results

Hardware: Strix Halo (Ryzen AI Max+ 395, gfx1151), 128 GB unified — 96 GB
carved to the iGPU, ~31.6 GB host. Windows 11, WDDM. ROCm 10.1 (TheRock
nightly SDK). Dates: 2026-09-08/09.

## Decode throughput (the headline)

| Configuration | decode t/s |
|---|---|
| Stock llamacpp-rocm b1326, no MTP (baseline) | 21.7 @500 / 21.6 @2000 / 20.3 @8000 depth |
| MTP **without** recurrent-state checkpoints (pre-PR-28118) | ~5 (net loss) |
| Fork engine, MTP n-max 4 p-min 0.75, through the full stack | **38** |

- Draft acceptance: **85–100%** (code prompts sit at the high end; p-min 0.75).
- MTP without the PR 28118 checkpoint mechanism is a net *loss* on gfx1151
  (21.4 → 5.1 t/s in our pre-trial measurements) — if you see that, your
  engine predates block 14 / PR 28118.
- Cold load of the 74 GB model: ~30 s (`--load-mode none`).
- The 38 t/s figure is measured **through the serving stack** (Lemonade plane
  behind the admission shim), not raw llama-bench.

## Fit at full context (262144)

Measured GPU-dedicated delta at load, `\GPU Adapter Memory(*)\Dedicated Usage`
counter (see `scripts/measure-fit.ps1`):

| Component | GB |
|---|---|
| target weights (UD-IQ4_XS) | 61.2 |
| MTP drafter (Q8_0) | 3.2 |
| KV @262k, f16 (incl. QSA indexer) | 8.75 |
| compute buffers | ~2 |
| **total in carve** | **74.0** |

The 26.8 GiB PLE n-gram table lands in **host mmap**, outside the carve.
Headroom in a 96 GB carve: ~22 GiB.

## Quant A/B (deterministic harness: `scripts/ab-quant.ps1`)

UD-IQ4_XS vs Q4_K_XL, byte-identical prompts, same Q8_0 drafter, same flags:

- acceptance: no measurable difference
- decode: Q4_K_XL ~5% slower
- fit headroom: ~22 GiB (IQ4_XS) vs ~6 GiB (Q4_K_XL)

→ UD-IQ4_XS unless you specifically want the quality-max fallback.

## Correctness gates passed (probe-mtp.ps1)

- multi-turn with MTP: no [#27797](https://github.com/ggml-org/llama.cpp/issues/27797)-style
  slash-flood
- decode flat across 500/2000/8000-token depth (no #27856-style cliff)
- needle retrieval at ~24k depth with MTP active
- output read and checked at every gate — on this platform, speed without
  reading the text has produced false passes before (a community port showed
  2× t/s while emitting noise past ~1–2k tokens)

## Methodology notes

- Footprints are measured deltas from the GPU dedicated-usage counter, not
  estimates; each is only valid at the context size it was measured at.
- A/B comparisons use fixed prompts (no randomness) so the only variable is
  the thing being compared.
- Depth-band probes use fresh unique prefixes per request so prompt-cache hits
  cannot flatter the numbers.
