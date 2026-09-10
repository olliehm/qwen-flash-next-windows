# Lemonade Admission Shim

Cross-plane preflight evictor in front of multiple Lemonade server instances
("planes") sharing one unified-memory GPU carve.

## The problem it solves

Running more than one Lemonade instance against the same iGPU carve (e.g. a
dense plane pinned to an old engine, a MoE plane on a newer one, and a plane on
a custom-built engine) means no single router sees total GPU memory. A combined
request can overflow the carve. On overflow, WDDM spills GPU allocations to
host RAM — and on a Strix Halo box with a 96 GB carve, host RAM is ~31 GB, so
spill → thrash → whole-system freeze. That is a documented failure mode on the
machine this was built for, not a theoretical one.

Worse, the Lemonade API has **no load-in-progress signal**: a model being
loaded does not appear in `/api/v1/health` at all — only already-resident
models do. A memory-watching poller can only ever react *after* the allocation
has happened. Prevention has to sit at the request layer, the one place that
knows a load is about to start.

## What it does

Fronts all planes on **`:13304`**:

```
client -> shim :13304
            resolve model -> plane          (explicit, from config)
            PREFLIGHT (completion requests only)
              1. read /health on ALL planes
              2. project: survivors + other-plane residents + reservations + requested
              3. fits -> forward
              4. else evict from the OTHER plane(s), biggest-first, prefer_keep last
              5. candidate busy -> wait up to busy_wait_seconds, then REFUSE (never kill)
              6. re-check against reality; still over -> 503 + Retry-After
            forward to the resolved plane (streaming passthrough intact)
```

When it admits a request that will trigger a load, it books the footprint as a
**reservation** against the budget until the upstream response starts — the
substitute for the missing load-in-progress signal. It also merges
`/api/v1/models` and `/api/v1/health` across planes, so clients see one server.

## Files

- `admission_shim.py` — FastAPI proxy, preflight, eviction, reservations
- `footprint_table.example.json` — planes, budget, measured footprints; copy to
  `footprint_table.json` and edit. Live-reloaded on mtime change — config
  edits need no restart
- `test_budget.py` — offline arithmetic tests; contacts no plane, moves no memory
- `start.ps1` — launcher (`-Setup` creates/repairs the venv)
- `register-boot-task.ps1` — run elevated to install the boot task (S4U, headless)
- `requirements.txt` — pinned deps (Python 3.12)

## Run

```powershell
Copy-Item footprint_table.example.json footprint_table.json   # then edit
.\start.ps1 -Setup    # first run: create venv + start
.\start.ps1           # subsequent runs
.\.venv\Scripts\python.exe test_budget.py   # offline checks (uses footprint_table.json)
```

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /_shim/status` | config, all planes, footprints, open reservations, ctx-drift warnings |
| `GET /_shim/preflight/{model}` | **dry run** — what *would* happen. Reads only, evicts nothing |
| `GET /api/v1/health` | merged across planes, with estimated GB used |
| `GET /api/v1/models` | merged catalogue, deduped to the owning plane |
| everything else | proxied to the resolved plane |

## Configuration notes

- **Footprints are measured deltas**, not `weights + KV + cache` estimates.
  Load the model alone and read the `\GPU Adapter Memory(*)\Dedicated Usage`
  performance counter before/after. Estimates were tried first and were wrong
  enough to matter; measured deltas verified within 0.5 GB of the live counter.
- **`measured_ctx` keeps footprints honest.** A footprint is only valid at the
  ctx it was measured at. The shim audits every model's live
  `recipe_options.ctx_size` at startup and logs `CTX DRIFT` when they disagree —
  a silently wrong footprint is worse than no footprint.
- **Plane ownership is explicit config, not discovery.** A model can be
  registered on two planes (leftover registrations happen); auto-discovery
  cannot know which one is real.

## Design decisions

- **Busy candidates are never force-evicted.** If an eviction candidate is
  mid-generation, the shim waits up to `busy_wait_seconds`, then refuses with
  503. Nothing is burning; killing a live generation would be pure damage.
- **Target-plane residents are not double-counted.** Lemonade evicts within a
  plane by itself when `llm` capacity is exceeded. The shim assumes the
  *smallest idle* residents are dropped, and never assumes a busy model is.
- **Eviction prefers one big unload over several small ones**, with
  `prefer_keep` models last.
- **An unreadable plane means refuse.** It might be holding anything; guessing
  low is exactly what causes the freeze.
- **Admission is serialised.** Two concurrent large requests that each
  independently decided they fit would together overflow the carve.

## Lemonade API gotchas (hard-won)

- Never `Stop-Process` a plane's `llama-server` to free memory — it desyncs
  lemond and produces 500s. Unload via `POST /api/v1/unload {"model_name": ...}`.
- An empty unload body `{}` evicts **everything, including pinned models**.
  `pin` does not survive unload-all.
- `is_busy` / `is_streaming` are **per-model inside `all_models_loaded`**, not
  top-level on `/health`.
- Lemonade sometimes surfaces user models as `user.<name>` — normalise before
  comparing names.

## Known gaps

- The busy-path (wait-then-refuse) is unit-tested but has not been exercised
  against a real mid-generation eviction candidate.
- If an external watchdog reloads a model between the shim's re-check and the
  upstream load completing, the projection is stale. Reservations cover the
  shim's own loads only.
- Unknown models pass through unguarded (by design — otherwise every new model
  breaks). Add a footprint row to bring a model under management.
- The guarantee is only as strong as the topology: anything that talks to a
  plane's port directly bypasses the shim. Bind planes to `127.0.0.1` and
  expose only the shim.
