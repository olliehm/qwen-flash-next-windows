"""
Offline tests for the shim's budget arithmetic.

Pure functions only - no plane is contacted and no memory moves, so this is safe
to run at any time, including while models are serving.

    & .venv\\Scripts\\python.exe test_budget.py
"""

from __future__ import annotations

import sys

import admission_shim as s


def view(key: str, cap: int, models: list[tuple[str, bool]]) -> s.PlaneView:
    return s.PlaneView(
        key=key,
        reachable=True,
        llm_capacity=cap,
        loaded=[
            s.LoadedModel(name=n, plane=key, busy=b, last_use=0,
                          footprint_gb=s.footprint_of(n))
            for n, b in models
        ],
    )


FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  PASS  {label}: {got}")
    else:
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
        FAILURES.append(label)


def main() -> int:
    s.load_config(force=True)
    print(f"config: budget={s.state.safe_budget_gb} GB, "
          f"slack={s.state.runtime_slack_gb} GB/model, "
          f"{len(s.state.models)} models\n")

    # --- 1. The canonical collision from the handoff -----------------------
    # A: E4B (4.4) + 27B (33.9).  B: 26B (31.9) resident, capacity 1.
    # Request the 122B (76.9) on B.  B evicts its own 26B; A must give up the 27B.
    print("1. canonical collision: request 122B on B")
    views = {
        "A": view("A", 2, [("classifier-E4B", False), ("Qwen3.8-27B-Q6-MTP", False)]),
        "B": view("B", 1, [("gemma-4-26B-Q8-MTP", False)]),
    }
    proj = s.project(views, "B", "Qwen3.5-122B-A10B-MTP-GGUF")
    # 26B is auto-evicted by plane B's own capacity=1, so it must NOT be charged.
    check("26B auto-evicted by plane B", proj["auto_evicted_by_plane"], ["gemma-4-26B-Q8-MTP"])
    check("survivors on B", proj["survivors"], [])
    # 4.4+1.5 + 33.9+1.5 + 76.9+1.5 = 119.7
    check("projected GB", proj["projected_gb"], 119.7)
    check("over budget by", proj["over_by_gb"], 19.7)

    chosen, freed = s.pick_candidates(proj["_other_pool"], proj["over_by_gb"])
    check("evicts the 27B only", [m.name for m in chosen], ["Qwen3.8-27B-Q6-MTP"])
    check("E4B kept warm", "classifier-E4B" not in [m.name for m in chosen], True)
    check("freed enough", freed >= proj["over_by_gb"], True)

    # After that eviction, re-project: 4.4+1.5 + 76.9+1.5 = 84.3
    views_after = {
        "A": view("A", 2, [("classifier-E4B", False)]),
        "B": view("B", 1, [("gemma-4-26B-Q8-MTP", False)]),
    }
    proj2 = s.project(views_after, "B", "Qwen3.5-122B-A10B-MTP-GGUF")
    check("projected after eviction", proj2["projected_gb"], 84.3)
    check("now under budget", proj2["over_by_gb"], 0.0)

    # --- 2. Naive summing would have over-rejected -------------------------
    print("\n2. target-plane auto-eviction is not double-counted")
    naive = sum(s.footprint_of(n) + s.state.runtime_slack_gb
                for n in ("classifier-E4B", "Qwen3.8-27B-Q6-MTP",
                          "gemma-4-26B-Q8-MTP", "Qwen3.5-122B-A10B-MTP-GGUF"))
    check("naive sum counts the 26B", round(naive, 1), 153.1)
    check("real projection is lower", proj["projected_gb"] < naive, True)

    # --- 3. A request that already fits changes nothing ---------------------
    print("\n3. 35B on B while A holds E4B + 27B  (the 88.3 GB observed case)")
    proj3 = s.project(views, "B", "Qwen3.6-35B-A3B-DFlash")
    # 4.4+1.5 + 33.9+1.5 + 46.6+1.5 = 89.4
    check("projected GB", proj3["projected_gb"], 89.4)
    check("fits, no eviction", proj3["over_by_gb"], 0.0)

    # --- 4. Already-loaded model is free ------------------------------------
    print("\n4. requesting a model that is already resident")
    proj4 = s.project(views, "A", "Qwen3.8-27B-Q6-MTP")
    check("flagged already_loaded", proj4["already_loaded"], True)
    # It is resident, so it must still be charged: 4.4+1.5 + 33.9+1.5 + 31.9+1.5
    check("resident model still charged", proj4["projected_gb"], 74.7)

    # --- 5. Busy models are never assumed to be auto-evicted ----------------
    print("\n5. busy resident on the target plane is charged, not assumed gone")
    busy_views = {
        "A": view("A", 2, [("classifier-E4B", False)]),
        "B": view("B", 1, [("gemma-4-26B-Q8-MTP", True)]),  # busy
    }
    proj5 = s.project(busy_views, "B", "Qwen3.5-122B-A10B-MTP-GGUF")
    check("busy 26B still counted", proj5["survivors"], ["gemma-4-26B-Q8-MTP"])
    check("nothing auto-evicted", proj5["auto_evicted_by_plane"], [])

    # --- 6. Reservations are charged ---------------------------------------
    print("\n6. an in-flight reservation is charged against the budget")
    before = s.project(views_after, "B", "Qwen3.6-35B-A3B-DFlash")["projected_gb"]
    s.state.reservations["t1"] = s.Reservation(
        req_id="t1", plane="A", model="Qwen3.8-27B-Q6-MTP", footprint_gb=33.9)
    after = s.project(views_after, "B", "Qwen3.6-35B-A3B-DFlash")["projected_gb"]
    check("reservation raises projection", round(after - before, 1), 35.4)
    s.state.reservations.clear()

    # --- 7. Plane resolution is explicit, not discovered --------------------
    print("\n7. plane resolution (both 120B-class models list on BOTH planes)")
    check("122B -> B", s.resolve_plane("Qwen3.5-122B-A10B-MTP-GGUF"), "B")
    check("gpt-oss -> B", s.resolve_plane("gpt-oss-120b-mxfp-GGUF"), "B")
    check("27B -> A", s.resolve_plane("Qwen3.8-27B-Q6-MTP"), "A")
    check("user. prefix stripped", s.resolve_plane("user.Qwen3.8-27B-Q6-MTP"), "A")
    check("unknown -> default", s.resolve_plane("no-such-model"), s.state.default_plane)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
