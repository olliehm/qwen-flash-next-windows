# Model, quant selection, and serving flags

## The model

[unsloth/Qwen3.8-Flash-Next-GGUF](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF)
— Qwen3.8-Flash-Next, 125B MoE (~6B active), arch `qwen4exp`, native ctx 262144.

Two files matter:

- **Target**: `Qwen3.8-Flash-Next-UD-IQ4_XS-*.gguf` (3 shards, ~61 GB) —
  we pinned revision `38bb39ee`.
- **MTP drafter sidecar**: `mtp-Qwen3.8-Flash-Next-Q8_0.gguf` (~4 GB) — the 4B
  multi-token-prediction head that ships in the official checkpoint but is
  stripped from common GGUF conversions.

## Why UD-IQ4_XS

On a 96 GB iGPU carve it is the only ≥Q4 quant that fits at full native
context:

| | UD-IQ4_XS | Q4_K_XL |
|---|---|---|
| fits 262144 ctx in carve | yes, ~22 GiB headroom | yes, ~6 GiB headroom |
| MTP draft accept | same | same (no gain) |
| decode speed | baseline | ~5% slower |

Q4_K_XL is a quality-max fallback only. `scripts/ab-quant.ps1` is the
deterministic A/B harness used for this comparison (byte-identical prompts,
same drafter, same flags — the only variable is the target quant).

A useful surprise: the model's 26.8 GiB PLE n-gram table lands in **host mmap**,
not the GPU carve, so it does not count against GPU fit (`--load-mode none`
keeps it that way). Measured GPU footprint at 262k ctx: **74.0 GB**
(61.2 weights + 3.2 draft + 8.75 KV incl. the QSA indexer + ~2 compute).
`scripts/measure-fit.ps1` reproduces this measurement load-only, without
generation.

## Serving flags (the working set)

```
-fa on --parallel 1 -ctk f16 -ctv f16 --load-mode none -b 512 -ub 512
--spec-type draft-mtp --spec-draft-n-max 4 --spec-draft-p-min 0.75
--temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0
--presence-penalty 0.0 --repeat-penalty 1.0
--n-predict 16384 --reasoning-budget 6144
--ctx-checkpoints 8 --cache-ram 3072
--chat-template-kwargs '{"enable_thinking":true,"reasoning_effort":"medium","preserve_thinking":false}'
```

Rationale for the non-obvious ones:

- `--spec-type draft-mtp --spec-draft-n-max 4 --spec-draft-p-min 0.75` — the
  MTP configuration. n-max 4 with p-min 0.75 gave 85–100% draft acceptance;
  deeper drafts did not pay for themselves on gfx1151.
- `-ctk f16 -ctv f16` — f16 KV; quantised KV was not needed for fit and MTP
  acceptance is sensitive to KV quality.
- `--load-mode none` — keeps the PLE table in host mmap (`--no-mmap` is
  deprecated; this replaces it).
- `--temp 1.0 --top-p 0.95 --top-k 20` — the model card's thinking-mode
  sampling. Do not greedy-decode a thinking model.
- `--reasoning-budget 6144` + `reasoning_effort: medium` — bounds thinking
  length for interactive use.
- `--ctx-checkpoints 8 --cache-ram 3072` — recurrent-state checkpoints, so
  multi-turn prefix reuse does not re-prefill from zero (this is the PR 28118
  mechanism; without it MTP is a net *loss* on gfx1151 — see benchmarks).

## Standalone serving (no Lemonade)

```powershell
llama-server.exe `
  -m Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf `
  -md mtp-Qwen3.8-Flash-Next-Q8_0.gguf `
  -c 262144 --host 127.0.0.1 --port 13399 --jinja `
  <flags from the working set above>
```

Then gate it before trusting it — `scripts/probe-mtp.ps1` runs
coherence-first validation (see below).

## Validate correctness, not just speed

MTP on HIP is the risky path. Two known failure modes make speed-only
benchmarking actively misleading:

- **Multi-turn slash-flood** ([llama.cpp #27797](https://github.com/ggml-org/llama.cpp/issues/27797)):
  output degrades to `//////` on multi-segment prompts. A single-turn probe is
  blind to it.
- **Coherence collapse at depth**: an earlier community gfx1151 port produced
  multilingual noise past ~1–2k tokens *while showing 2× tok/s*. A benchmark
  that does not read the output falsely passes.

`scripts/probe-mtp.ps1` therefore gates in order: single-turn coherence +
accept rate, multi-turn (slash-flood guard), decode-vs-depth bands at
500/2000/8000 tokens, and a needle retrieval at ~24k. Every gate reads the
text.
