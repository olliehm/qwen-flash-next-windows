"""
Lemonade Admission Shim v2 - cross-plane preflight evictor for Strix Halo.

Fronts BOTH Lemonade planes on one port and makes room before a large model is
allowed to load, instead of refusing the request.

    client -> shim :13304 -> plane A :13305 (dense, b1297)
                          -> plane B :13306 (MoE,   b1314)

Why this exists
---------------
The box carves 96 GB of unified memory to the iGPU. The two Lemonade processes
share that one carve but have no shared admission control: plane B's router
cannot see that plane A is holding 41.7 GB. A combined request can overflow the
carve, and on overflow WDDM spills to the ~31.6 GB of host RAM -> thrash ->
whole-system freeze. That is a documented failure mode on this machine.

Why it lives here and not in a memory watchdog
-----------------------------------------
A model that is *being loaded* does not appear in /api/v1/health at all - only
already-resident models do. There is no load-in-progress signal anywhere in the
Lemonade API. So a poller can only ever react after the memory is allocated and
the overflow has already happened. Prevention has to sit at the request layer,
which is the one place that knows a load is about to start.

The shim covers that blind spot with *reservations*: when it admits a request
that will trigger a load, it books the footprint against the budget until the
upstream response starts. That is the substitute for the missing signal - and
it only works while the shim is the sole entry point. Plane ports :13305/:13306
are deliberately left open for staged rollout, so today the guarantee is
advisory: anything talking to a plane directly bypasses it.

Run:
    & .\\start.ps1
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

# --- Configuration ---------------------------------------------------------

SHIM_HOST = "0.0.0.0"  # all interfaces - firewall accordingly, or set to 127.0.0.1
SHIM_PORT = 13304

CONFIG_PATH = Path(__file__).parent / "footprint_table.json"

DEFAULT_SAFE_BUDGET_GB = 100.0
DEFAULT_RUNTIME_SLACK_GB = 1.5
DEFAULT_BUSY_WAIT_S = 45.0
DEFAULT_BUSY_POLL_S = 2.0

# Upstream connect retry - covers the Lemonade swap window where the new
# llama-server is not yet bound to its port (CURL error 7 resilience).
CONNECT_RETRY_ATTEMPTS = 30
CONNECT_RETRY_DELAY_S = 1.0

# Forward timeout for proxied requests. Big-context flows take minutes.
UPSTREAM_TIMEOUT_S = 600.0
# Control-plane calls (/health, /unload) must be quick or they are useless.
CONTROL_TIMEOUT_S = 15.0
# An unload is followed by a settle wait so the re-check sees real numbers.
UNLOAD_SETTLE_S = 2.0

# A reservation that is never released (client vanished mid-load) must not
# wedge the budget forever.
RESERVATION_TTL_S = 300.0

# Log to a file as well as stdout: under the S4U boot task there is no console,
# so stdout goes nowhere and the shim would run blind.
LOG_PATH = Path(__file__).parent / "shim.log"
_fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
_file_handler = RotatingFileHandler(
    LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(_fmt)
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])

# httpx logs every request at INFO. Two /health reads per admission would bury
# the decision lines that actually matter in a long-running service.
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger("admission-shim")

app = FastAPI(title="Lemonade Admission Shim v2")


# --- Config model ----------------------------------------------------------

@dataclass
class ModelSpec:
    name: str
    plane: str
    footprint_gb: float
    measured_ctx: Optional[int] = None
    prefer_keep: bool = False
    notes: str = ""


@dataclass
class PlaneSpec:
    key: str
    base_url: str
    role: str = ""
    engine_pin: str = ""


@dataclass
class Reservation:
    req_id: str
    plane: str
    model: str
    footprint_gb: float
    created: float = field(default_factory=time.monotonic)


class State:
    planes: dict[str, PlaneSpec] = {}
    models: dict[str, ModelSpec] = {}          # normalised name -> spec
    safe_budget_gb: float = DEFAULT_SAFE_BUDGET_GB
    runtime_slack_gb: float = DEFAULT_RUNTIME_SLACK_GB
    busy_wait_s: float = DEFAULT_BUSY_WAIT_S
    busy_poll_s: float = DEFAULT_BUSY_POLL_S
    default_plane: str = "A"
    config_mtime: float = 0.0
    reservations: dict[str, Reservation] = {}
    ctx_drift: list[str] = []                  # populated by the startup audit


state = State()
preflight_lock = asyncio.Lock()

control_client: Optional[httpx.AsyncClient] = None
proxy_client: Optional[httpx.AsyncClient] = None


def norm(name: str) -> str:
    """Lemonade sometimes surfaces user models as 'user.<name>'."""
    if not name:
        return ""
    return name[5:] if name.startswith("user.") else name


def load_config(force: bool = False) -> None:
    """(Re)load config, but only when the file actually changed."""
    if not CONFIG_PATH.exists():
        log.warning(f"no config at {CONFIG_PATH} - running as a plain passthrough proxy")
        return
    try:
        mtime = CONFIG_PATH.stat().st_mtime
        if not force and mtime == state.config_mtime:
            return
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.error(f"config reload failed: {e} - keeping previous config")
        return

    planes: dict[str, PlaneSpec] = {}
    for key, p in (cfg.get("planes") or {}).items():
        planes[key] = PlaneSpec(
            key=key,
            base_url=str(p["base_url"]).rstrip("/"),
            role=p.get("role", ""),
            engine_pin=p.get("engine_pin", ""),
        )
    if not planes:
        log.error("config declares no planes - refusing to apply it")
        return

    models: dict[str, ModelSpec] = {}
    for m in cfg.get("models") or []:
        plane = m.get("plane")
        if plane not in planes:
            log.error(f"model {m.get('name')!r} names unknown plane {plane!r} - skipped")
            continue
        spec = ModelSpec(
            name=m["name"],
            plane=plane,
            footprint_gb=float(m["footprint_gb"]),
            measured_ctx=m.get("measured_ctx"),
            prefer_keep=bool(m.get("prefer_keep", False)),
            notes=m.get("notes", ""),
        )
        models[norm(spec.name)] = spec

    state.planes = planes
    state.models = models
    state.safe_budget_gb = float(cfg.get("safe_budget_gb", DEFAULT_SAFE_BUDGET_GB))
    state.runtime_slack_gb = float(cfg.get("runtime_slack_gb", DEFAULT_RUNTIME_SLACK_GB))
    state.busy_wait_s = float(cfg.get("busy_wait_seconds", DEFAULT_BUSY_WAIT_S))
    state.busy_poll_s = float(cfg.get("busy_poll_interval_s", DEFAULT_BUSY_POLL_S))
    state.default_plane = cfg.get("default_plane") or sorted(planes)[0]
    state.config_mtime = mtime
    log.info(
        f"config loaded: {len(planes)} planes, {len(models)} models, "
        f"budget={state.safe_budget_gb} GB, slack={state.runtime_slack_gb} GB/model, "
        f"busy_wait={state.busy_wait_s}s"
    )


# --- Ground truth from the planes ------------------------------------------

@dataclass
class LoadedModel:
    name: str
    plane: str
    busy: bool
    last_use: int
    footprint_gb: Optional[float]


@dataclass
class PlaneView:
    key: str
    reachable: bool
    llm_capacity: int = 1
    loaded: list[LoadedModel] = field(default_factory=list)
    error: str = ""


async def read_plane(plane: PlaneSpec) -> PlaneView:
    """Read one plane's /health. Never raises - unreachable is a valid answer."""
    assert control_client is not None
    try:
        r = await control_client.get(f"{plane.base_url}/api/v1/health")
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001 - any failure means "cannot see this plane"
        return PlaneView(key=plane.key, reachable=False, error=f"{type(e).__name__}: {e}")

    loaded: list[LoadedModel] = []
    for m in data.get("all_models_loaded") or []:
        if not m.get("loaded"):
            continue
        name = norm(m.get("model_name", ""))
        spec = state.models.get(name)
        loaded.append(
            LoadedModel(
                name=name,
                plane=plane.key,
                # is_busy / is_streaming are PER-MODEL here, not top-level on /health.
                busy=bool(m.get("is_busy")) or bool(m.get("is_streaming")),
                last_use=int(m.get("last_use") or 0),
                footprint_gb=spec.footprint_gb if spec else None,
            )
        )
    cap = ((data.get("max_models") or {}).get("llm")) or 1
    return PlaneView(key=plane.key, reachable=True, llm_capacity=int(cap), loaded=loaded)


async def read_all_planes() -> dict[str, PlaneView]:
    keys = list(state.planes)
    views = await asyncio.gather(*(read_plane(state.planes[k]) for k in keys))
    return dict(zip(keys, views))


# --- Reservations ----------------------------------------------------------

def sweep_reservations() -> None:
    now = time.monotonic()
    for rid, res in list(state.reservations.items()):
        if now - res.created > RESERVATION_TTL_S:
            log.warning(
                f"reservation {rid} for {res.model} expired after "
                f"{RESERVATION_TTL_S:.0f}s - releasing"
            )
            state.reservations.pop(rid, None)


def release_reservation(rid: Optional[str]) -> None:
    if rid and state.reservations.pop(rid, None):
        log.info(f"reservation released: {rid}")


# --- Budget arithmetic -----------------------------------------------------

def footprint_of(name: str) -> Optional[float]:
    spec = state.models.get(norm(name))
    return spec.footprint_gb if spec else None


def charge(models: Iterable[LoadedModel]) -> float:
    """Footprint + per-model runtime slack. Unknown models contribute nothing."""
    total = 0.0
    for m in models:
        if m.footprint_gb is not None:
            total += m.footprint_gb + state.runtime_slack_gb
    return total


def target_survivors(view: PlaneView, requested: str) -> tuple[list[LoadedModel], list[LoadedModel]]:
    """
    Split the target plane's residents into (survivors, auto-evicted).

    Lemonade evicts within a plane by itself when llm capacity is exceeded, so
    counting every resident would over-reject. Conservative choices:
      - assume the SMALLEST idle residents are the ones it drops (largest remain)
      - never assume a BUSY model gets dropped
    """
    residents = [m for m in view.loaded if m.name != norm(requested)]
    must_go = len(residents) + 1 - view.llm_capacity
    if must_go <= 0:
        return residents, []

    idle = sorted((m for m in residents if not m.busy), key=lambda m: (m.footprint_gb or 0.0))
    evicted = idle[:must_go]
    ev_names = {m.name for m in evicted}
    return [m for m in residents if m.name not in ev_names], evicted


def pick_candidates(pool: list[LoadedModel], need_gb: float) -> tuple[list[LoadedModel], float]:
    """
    Choose the fewest cross-plane models to evict to free `need_gb`.

    Order: models we would rather keep go last; within that, biggest first, so a
    single large eviction beats several small ones and a prefer_keep model stays
    warm whenever evicting one big model is enough.
    """
    def rank(m: LoadedModel) -> tuple[int, float]:
        spec = state.models.get(m.name)
        prefer_keep = spec.prefer_keep if spec else False
        return (1 if prefer_keep else 0, -(m.footprint_gb or 0.0))

    chosen: list[LoadedModel] = []
    freed = 0.0
    for m in sorted((m for m in pool if m.footprint_gb is not None), key=rank):
        if freed >= need_gb:
            break
        chosen.append(m)
        freed += m.footprint_gb + state.runtime_slack_gb
    return chosen, freed


def project(views: dict[str, PlaneView], target: str, requested: str) -> dict[str, Any]:
    """Compute projected GPU usage if `requested` were loaded on `target`."""
    tview = views[target]
    survivors, auto_evicted = target_survivors(tview, requested)
    others = [m for k, v in views.items() if k != target and v.reachable for m in v.loaded]

    req_fp = footprint_of(requested)
    already = norm(requested) in {m.name for m in tview.loaded}

    reserved = sum(r.footprint_gb + state.runtime_slack_gb for r in state.reservations.values())

    # The requested model is charged either way: if it is already resident it is
    # occupying memory right now, and if it is not it is about to. It is excluded
    # from `survivors` only so the capacity arithmetic treats it as the incoming
    # model - dropping it from the total as well would hide live memory.
    total = charge(survivors) + charge(others) + reserved
    if req_fp is not None:
        total += req_fp + state.runtime_slack_gb

    return {
        "already_loaded": already,
        "requested_footprint_gb": req_fp,
        "survivors": [m.name for m in survivors],
        "auto_evicted_by_plane": [m.name for m in auto_evicted],
        "other_plane_resident": [m.name for m in others],
        "_other_pool": others,
        "reserved_gb": round(reserved, 1),
        "projected_gb": round(total, 1),
        "budget_gb": state.safe_budget_gb,
        "over_by_gb": round(max(0.0, total - state.safe_budget_gb), 1),
    }


def public(proj: dict[str, Any]) -> dict[str, Any]:
    """Strip internal objects before a projection goes into a JSON response."""
    return {k: v for k, v in proj.items() if not k.startswith("_")}


# --- Eviction --------------------------------------------------------------

async def unload_named(plane: PlaneSpec, model_name: str) -> bool:
    """POST /api/v1/unload {"model_name": ...} - evicts one model, leaves others.

    Never Stop-Process llama-server instead: that desyncs lemond and yields 500s.
    Note an empty body {} would unload EVERYTHING, pinned models included.
    """
    assert control_client is not None
    try:
        r = await control_client.post(
            f"{plane.base_url}/api/v1/unload", json={"model_name": model_name}
        )
        ok = r.status_code < 400
        log.info(f"unload plane={plane.key} model={model_name} -> {r.status_code}")
        return ok
    except Exception as e:  # noqa: BLE001
        log.error(f"unload plane={plane.key} model={model_name} FAILED: {type(e).__name__}: {e}")
        return False


async def wait_until_idle(plane_key: str, model_name: str, deadline: float) -> bool:
    """Poll until the model reports idle, or the deadline passes."""
    while time.monotonic() < deadline:
        view = await read_plane(state.planes[plane_key])
        if not view.reachable:
            return False
        cur = next((m for m in view.loaded if m.name == model_name), None)
        if cur is None:
            return True  # gone entirely - that is 'idle' for our purposes
        if not cur.busy:
            return True
        await asyncio.sleep(state.busy_poll_s)
    return False


@dataclass
class PreflightResult:
    admit: bool
    reason: str
    detail: dict[str, Any]
    retry_after: int = 30


async def preflight(requested: str, target: str) -> PreflightResult:
    """Make room on the other plane(s) for `requested` to load on `target`."""
    sweep_reservations()
    views = await read_all_planes()

    if not views[target].reachable:
        return PreflightResult(
            False, "target_plane_unreachable",
            {"plane": target, "error": views[target].error}, retry_after=15,
        )

    proj = project(views, target, requested)

    # Already resident: no load, so nothing to make room for.
    if proj["already_loaded"]:
        return PreflightResult(True, "already_loaded", proj)

    # Unknown model: no footprint to reason about. Forward and let the plane
    # answer (usually a 404). Blocking here would break every new model.
    if proj["requested_footprint_gb"] is None:
        return PreflightResult(True, "unknown_model_passthrough", proj)

    # A plane we cannot read might be holding anything. Refuse rather than
    # guess - guessing low is what causes the freeze.
    blind = [k for k, v in views.items() if not v.reachable]
    if blind:
        return PreflightResult(
            False, "plane_unreadable",
            {**proj, "unreadable_planes": blind,
             "message": f"Cannot read plane(s) {blind}; refusing to load "
                        f"{requested} without knowing what they hold."},
            retry_after=15,
        )

    if proj["requested_footprint_gb"] + state.runtime_slack_gb > state.safe_budget_gb:
        return PreflightResult(
            False, "model_exceeds_budget_alone",
            {**proj, "message": f"'{requested}' (~{proj['requested_footprint_gb']} GB) "
                                f"exceeds the safe budget of {state.safe_budget_gb} GB "
                                f"on its own; it cannot be loaded safely."},
            retry_after=300,
        )

    if proj["over_by_gb"] <= 0:
        return PreflightResult(True, "fits", proj)

    # Over budget - evict from the OTHER planes.
    chosen, freed = pick_candidates(proj["_other_pool"], proj["over_by_gb"])
    if freed < proj["over_by_gb"]:
        return PreflightResult(
            False, "cannot_free_enough",
            {**proj, "evict_candidates": [m.name for m in chosen],
             "freeable_gb": round(freed, 1),
             "message": f"Loading '{requested}' needs {proj['over_by_gb']} GB more than "
                        f"budget allows, but only {round(freed, 1)} GB is evictable."},
            retry_after=60,
        )

    log.info(
        f"PREFLIGHT {requested}->{target}: projected {proj['projected_gb']} GB "
        f"> budget {state.safe_budget_gb} GB; evicting "
        f"{[m.name for m in chosen]} to free {round(freed, 1)} GB"
    )

    deadline = time.monotonic() + state.busy_wait_s
    for m in chosen:
        if m.busy:
            if state.busy_wait_s <= 0:
                return PreflightResult(
                    False, "candidate_busy",
                    {**proj, "busy_model": m.name,
                     "message": f"'{m.name}' on plane {m.plane} must be evicted to load "
                                f"'{requested}', but it is mid-generation."},
                    retry_after=30,
                )
            log.info(f"candidate {m.name} is busy - waiting up to {state.busy_wait_s}s")
            if not await wait_until_idle(m.plane, m.name, deadline):
                # Deliberately NOT force-evicting. A freeze-averting watchdog's 'wait then fire
                # anyway' exists to avert a freeze; here nothing is burning, so
                # killing a live generation would be pure damage.
                return PreflightResult(
                    False, "candidate_busy_timeout",
                    {**proj, "busy_model": m.name, "waited_s": state.busy_wait_s,
                     "message": f"'{m.name}' on plane {m.plane} is still generating after "
                                f"{state.busy_wait_s:.0f}s. Refusing to interrupt it; "
                                f"retry shortly."},
                    retry_after=30,
                )
        await unload_named(state.planes[m.plane], m.name)

    await asyncio.sleep(UNLOAD_SETTLE_S)

    # Re-check against reality rather than trusting the arithmetic.
    views2 = await read_all_planes()
    if not views2[target].reachable:
        return PreflightResult(False, "target_plane_unreachable",
                               {"plane": target, "error": views2[target].error}, retry_after=15)
    proj2 = project(views2, target, requested)
    if proj2["over_by_gb"] > 0:
        return PreflightResult(
            False, "still_over_after_eviction",
            {**proj2, "evicted": [m.name for m in chosen],
             "message": f"Evicted {[m.name for m in chosen]} but projected usage is still "
                        f"{proj2['projected_gb']} GB against a {state.safe_budget_gb} GB budget."},
            retry_after=60,
        )

    log.info(f"PREFLIGHT ok after eviction: projected {proj2['projected_gb']} GB")
    return PreflightResult(
        True, "fits_after_eviction", {**proj2, "evicted": [m.name for m in chosen]}
    )


# --- Plane resolution ------------------------------------------------------

def resolve_plane(model: Optional[str]) -> str:
    """
    Map a model name to its plane.

    Explicit config only. Auto-discovery from each plane's /api/v1/models cannot
    work here: Qwen3.5-122B and gpt-oss-120b are registered on BOTH planes, and
    the plane-A registrations are leftovers that must never be routed to.
    """
    spec = state.models.get(norm(model or ""))
    return spec.plane if spec else state.default_plane


def extract_model(body: bytes) -> tuple[Optional[str], bool]:
    """Pull (model, stream) out of a JSON body. Tolerant of anything unparseable."""
    if not body:
        return None, False
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, False
    if not isinstance(payload, dict):
        return None, False
    model = payload.get("model") or payload.get("model_name")
    return (model if isinstance(model, str) else None), bool(payload.get("stream"))


REQUEST_HOP_HEADERS = {"host", "content-length", "connection", "keep-alive",
                       "proxy-authenticate", "proxy-authorization", "te",
                       "trailers", "transfer-encoding", "upgrade"}
# content-encoding is dropped because httpx has already decoded the body for us.
RESPONSE_DROP_HEADERS = REQUEST_HOP_HEADERS | {"content-encoding"}


# --- Proxy -----------------------------------------------------------------

async def forward(
    method: str,
    url: str,
    headers: dict,
    body: bytes,
    is_stream: bool,
    req_id: Optional[str],
) -> Response:
    """
    Forward to a plane, retrying connect errors (Lemonade's swap window).

    For streaming, the upstream connection is established BEFORE the
    StreamingResponse is returned, so connect failures are retried here rather
    than surfacing as a corrupt half-sent SSE stream, and the real upstream
    status and content-type are passed through instead of an assumed 200.
    """
    assert proxy_client is not None
    last_err: Optional[Exception] = None

    for attempt in range(CONNECT_RETRY_ATTEMPTS):
        try:
            if is_stream:
                req = proxy_client.build_request(method, url, content=body, headers=headers)
                resp = await proxy_client.send(req, stream=True)

                async def gen():
                    try:
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                    finally:
                        await resp.aclose()
                        release_reservation(req_id)

                return StreamingResponse(
                    gen(),
                    status_code=resp.status_code,
                    headers={k: v for k, v in resp.headers.items()
                             if k.lower() not in RESPONSE_DROP_HEADERS},
                    media_type=resp.headers.get("content-type", "text/event-stream"),
                )

            r = await proxy_client.request(method, url, content=body, headers=headers)
            release_reservation(req_id)
            return Response(
                content=r.content,
                status_code=r.status_code,
                headers={k: v for k, v in r.headers.items()
                         if k.lower() not in RESPONSE_DROP_HEADERS},
            )

        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last_err = e
            log.warning(
                f"upstream connect retry {attempt + 1}/{CONNECT_RETRY_ATTEMPTS}: {type(e).__name__}"
            )
            await asyncio.sleep(CONNECT_RETRY_DELAY_S)
        except httpx.HTTPError as e:
            release_reservation(req_id)
            log.error(f"upstream error: {type(e).__name__}: {e}")
            return JSONResponse(
                status_code=502,
                content={"error": {"message": f"Upstream request failed: {type(e).__name__}: {e}",
                                   "type": "upstream_error"}},
            )

    release_reservation(req_id)
    log.error(f"connect_failed_after_retries: {last_err}")
    return JSONResponse(
        status_code=503,
        content={"error": {
            "message": (f"Lemonade plane unreachable after {CONNECT_RETRY_ATTEMPTS} attempts "
                        f"({CONNECT_RETRY_ATTEMPTS * CONNECT_RETRY_DELAY_S:.0f}s)."),
            "type": "backend_unreachable"}},
        headers={"Retry-After": "30"},
    )


def deny(result: PreflightResult, requested: str, target: str) -> JSONResponse:
    detail = public(result.detail)
    msg = detail.get("message") or f"Request denied by admission shim: {result.reason}"
    log.warning(f"DENY model={requested} plane={target} reason={result.reason}")
    return JSONResponse(
        status_code=503,
        content={"error": {
            "message": msg,
            "type": "vram_admission_denied",
            "reason": result.reason,
            "requested_model": requested,
            "target_plane": target,
            **detail,
        }},
        headers={"Retry-After": str(result.retry_after)},
    )


# --- Merged views ----------------------------------------------------------

@app.get("/api/v1/health")
@app.get("/v1/health")
async def merged_health():
    """Union of both planes, so clients see one server."""
    load_config()
    views = await read_all_planes()
    all_loaded: list[dict] = []
    for key, v in views.items():
        for m in v.loaded:
            all_loaded.append({
                "model_name": m.name, "plane": key, "loaded": True,
                "is_busy": m.busy, "footprint_gb": m.footprint_gb,
            })
    used = sum((m.footprint_gb or 0.0) + state.runtime_slack_gb
               for v in views.values() for m in v.loaded)
    return {
        "status": "ok" if all(v.reachable for v in views.values()) else "degraded",
        "shim_version": 2,
        "all_models_loaded": all_loaded,
        "planes": {k: {"reachable": v.reachable, "llm_capacity": v.llm_capacity,
                       "loaded": [m.name for m in v.loaded], "error": v.error}
                   for k, v in views.items()},
        "budget_gb": state.safe_budget_gb,
        "estimated_used_gb": round(used, 1),
        "reservations": [r.model for r in state.reservations.values()],
    }


@app.get("/api/v1/models")
@app.get("/v1/models")
async def merged_models():
    """
    Merged catalogue. A model registered on both planes is reported once, on the
    plane config says owns it - the other registration is a leftover.
    """
    load_config()
    assert control_client is not None
    out: dict[str, dict] = {}
    for key, plane in state.planes.items():
        try:
            r = await control_client.get(f"{plane.base_url}/api/v1/models")
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning(f"models listing failed for plane {key}: {type(e).__name__}: {e}")
            continue
        for m in data.get("data") or []:
            mid = m.get("id")
            if not mid:
                continue
            owner = resolve_plane(mid)
            known = norm(mid) in state.models
            # Skip a registration on a plane that config says does not own it.
            if known and owner != key:
                continue
            entry = dict(m)
            entry["_plane"] = key
            out.setdefault(mid, entry)
    return {"object": "list", "data": list(out.values())}


# --- Introspection ---------------------------------------------------------

@app.get("/_shim/status")
async def shim_status():
    load_config()
    views = await read_all_planes()
    return {
        "shim_version": 2,
        "config_path": str(CONFIG_PATH),
        "safe_budget_gb": state.safe_budget_gb,
        "runtime_slack_gb": state.runtime_slack_gb,
        "busy_wait_seconds": state.busy_wait_s,
        "default_plane": state.default_plane,
        "ctx_drift_warnings": state.ctx_drift,
        "planes": {k: {"base_url": p.base_url, "role": p.role, "engine_pin": p.engine_pin,
                       "reachable": views[k].reachable, "llm_capacity": views[k].llm_capacity,
                       "loaded": [{"name": m.name, "busy": m.busy,
                                   "footprint_gb": m.footprint_gb} for m in views[k].loaded],
                       "error": views[k].error}
                   for k, p in state.planes.items()},
        "models": {n: {"plane": s.plane, "footprint_gb": s.footprint_gb,
                       "measured_ctx": s.measured_ctx, "prefer_keep": s.prefer_keep}
                   for n, s in state.models.items()},
        "reservations": [{"id": r.req_id, "model": r.model, "plane": r.plane,
                          "footprint_gb": r.footprint_gb,
                          "age_s": round(time.monotonic() - r.created, 1)}
                         for r in state.reservations.values()],
    }


@app.get("/_shim/preflight/{model}")
async def shim_preflight_dryrun(model: str):
    """
    Dry run: what WOULD happen for this model. Reads only - evicts nothing.
    Use this to verify policy without moving any memory.
    """
    load_config()
    target = resolve_plane(model)
    views = await read_all_planes()
    if not views[target].reachable:
        return {"model": model, "target_plane": target, "error": views[target].error}
    proj = project(views, target, model)
    out: dict[str, Any] = {"model": model, "target_plane": target, **public(proj)}
    if proj["over_by_gb"] > 0 and proj["requested_footprint_gb"] is not None:
        chosen, freed = pick_candidates(proj["_other_pool"], proj["over_by_gb"])
        out["would_evict"] = [{"name": m.name, "plane": m.plane,
                               "footprint_gb": m.footprint_gb, "busy": m.busy}
                              for m in chosen]
        out["would_free_gb"] = round(freed, 1)
        out["sufficient"] = freed >= proj["over_by_gb"]
    else:
        out["would_evict"] = []
    return out


# --- Catch-all proxy -------------------------------------------------------

@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, full_path: str):
    load_config()
    body = await request.body()
    method = request.method

    model, is_stream = extract_model(body)
    if not model:
        model = request.query_params.get("model")
    target = resolve_plane(model)

    req_id: Optional[str] = None
    is_completion = "completions" in full_path

    if is_completion and model:
        # Serialise admission. Two concurrent big requests that each independently
        # decided they fit would together overflow the carve.
        async with preflight_lock:
            result = await preflight(model, target)
            if not result.admit:
                return deny(result, model, target)

            # Book the footprint until the response starts. This is what covers
            # the load window that /health cannot see (see module docstring).
            if not result.detail.get("already_loaded"):
                fp = result.detail.get("requested_footprint_gb")
                if fp is not None:
                    req_id = uuid.uuid4().hex[:8]
                    state.reservations[req_id] = Reservation(
                        req_id=req_id, plane=target, model=norm(model), footprint_gb=float(fp)
                    )
        log.info(
            f"ADMIT model={model} plane={target} reason={result.reason} "
            f"projected={result.detail.get('projected_gb')} GB stream={is_stream}"
        )

    plane = state.planes.get(target)
    if plane is None:
        return JSONResponse(status_code=500,
                            content={"error": {"message": f"no such plane {target!r}",
                                               "type": "shim_misconfigured"}})

    url = f"{plane.base_url}/{full_path}"
    if request.url.query:
        url += f"?{request.url.query}"

    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in REQUEST_HOP_HEADERS}

    return await forward(method, url, headers, body, is_stream, req_id)


# --- Lifecycle -------------------------------------------------------------

async def audit_ctx_drift() -> None:
    """
    Footprints are measured at a specific ctx_size. If a plane's live
    recipe_options.ctx_size no longer matches, the number in the table is a
    fiction - and a silently wrong footprint is worse than no footprint.
    """
    assert control_client is not None
    warnings: list[str] = []
    for key, plane in state.planes.items():
        try:
            r = await control_client.get(f"{plane.base_url}/api/v1/models")
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # noqa: BLE001
            warnings.append(f"plane {key}: could not audit ctx ({type(e).__name__})")
            continue
        for m in data.get("data") or []:
            spec = state.models.get(norm(m.get("id", "")))
            if not spec or spec.plane != key or spec.measured_ctx is None:
                continue
            live = (m.get("recipe_options") or {}).get("ctx_size")
            if live is not None and int(live) != int(spec.measured_ctx):
                warnings.append(
                    f"{spec.name} on plane {key}: footprint measured at ctx "
                    f"{spec.measured_ctx} but live ctx_size is {live} - "
                    f"footprint_gb {spec.footprint_gb} is stale, re-measure"
                )
    state.ctx_drift = warnings
    for w in warnings:
        log.warning(f"CTX DRIFT: {w}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global control_client, proxy_client
    control_client = httpx.AsyncClient(timeout=CONTROL_TIMEOUT_S)
    proxy_client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_S)

    load_config(force=True)
    log.info(f"admission-shim v2 listening on {SHIM_HOST}:{SHIM_PORT}")
    for k, p in state.planes.items():
        log.info(f"  plane {k}: {p.base_url} ({p.role}, {p.engine_pin})")

    views = await read_all_planes()
    for k, v in views.items():
        if v.reachable:
            log.info(f"  plane {k} reachable, llm_capacity={v.llm_capacity}, "
                     f"loaded={[m.name for m in v.loaded]}")
        else:
            log.error(f"  plane {k} UNREACHABLE: {v.error}")

    await audit_ctx_drift()

    yield

    for c in (control_client, proxy_client):
        if c is not None:
            await c.aclose()


# Assigned rather than passed to FastAPI(...) because `app` has to exist near the
# top of the module for the route decorators below it.
app.router.lifespan_context = lifespan


if __name__ == "__main__":
    uvicorn.run(app, host=SHIM_HOST, port=SHIM_PORT, log_level="info", access_log=False)
