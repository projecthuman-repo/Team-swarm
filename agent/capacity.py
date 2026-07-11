"""Hardware capacity probe (v4.1 T4.1) — capacity-aware inference routing.

On boot each node measures free VRAM (nvidia-ml-py), RAM/CPU (psutil) and
writes a node_caps row. `can_serve` tiers are computed with KV-cache
headroom so a node never advertises a model it would spill:

    tier      weights est.   +KV-cache headroom -> required free VRAM
    9b-q4     ~6 GB          ~8 GB
    9b-bf16   ~19 GB         ~22 GB
    35b-q4    ~20 GB         ~24 GB
    api       always serveable (remote endpoint, no local VRAM)

Ollama CPU spill (5-30x slower) is detected by parsing `ollama ps`; a
spilling node demotes its advertised GPU tiers to cpu-only + api.

Deps are in requirements-elevation.txt (opt-in); without them the node
registers cpu-only + api, which is correct fail-safe behavior.
"""

from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger("capacity")

# Required free VRAM (MB) per tier, including KV-cache headroom at the
# contexts the swarm actually runs (T4.5 tier math).
TIER_VRAM_MB = {
    "9b-q4": 8_000,
    "9b-bf16": 22_000,
    "35b-q4": 24_000,
}
DEFAULT_TIERS = ["default"]  # v4.0 subject compatibility (T4.2)


def probe_gpu() -> int:
    """Free VRAM in MB across the best single GPU; 0 if none/no driver."""
    try:
        import pynvml  # nvidia-ml-py

        pynvml.nvmlInit()
        best = 0
        for i in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            best = max(best, info.free // (1024 * 1024))
        pynvml.nvmlShutdown()
        return best
    except Exception:  # no GPU, no driver, or pynvml not installed
        return 0


def probe_host() -> tuple[int, int]:
    """(total RAM MB, cpu count); degrades gracefully without psutil."""
    try:
        import psutil

        return psutil.virtual_memory().total // (1024 * 1024), psutil.cpu_count() or 1
    except ImportError:
        return 0, os.cpu_count() or 1


def detect_ollama_spill(ps_output: str | None = None) -> bool:
    """True when `ollama ps` shows a model partially on CPU (spill).

    `ollama ps` prints a PROCESSOR column like "100% GPU", "22%/78% CPU/GPU",
    or "100% CPU" — anything mentioning CPU means spill (5-30x slower).
    """
    if ps_output is None:
        try:
            ps_output = subprocess.run(
                ["ollama", "ps"], capture_output=True, text=True, timeout=10
            ).stdout
        except (FileNotFoundError, subprocess.SubprocessError):
            return False
    for line in ps_output.splitlines()[1:]:  # skip header
        if "CPU" in line.upper():
            return True
    return False


def compute_tiers(free_vram_mb: int, spill: bool) -> list[str]:
    """can_serve tiers from VRAM + spill state. api is always serveable."""
    if spill or free_vram_mb <= 0:
        return ["cpu-only", "api"]
    tiers = [t for t, need in TIER_VRAM_MB.items() if free_vram_mb >= need]
    return (tiers or ["cpu-only"]) + ["api"]


def serveable_tiers(caps: dict) -> list[str]:
    """Subscription tiers for WORKER_TIERS=auto: default + capable tiers."""
    return DEFAULT_TIERS + [
        t for t in caps.get("can_serve", []) if t in TIER_VRAM_MB
    ]


async def register_node(pool, node_id: str) -> dict:
    """Probe and upsert this node's node_caps row; returns the caps dict."""
    free_vram = probe_gpu()
    ram_mb, cpus = probe_host()
    spill = detect_ollama_spill()
    tiers = compute_tiers(free_vram, spill)
    caps = {
        "node_id": node_id,
        "free_vram_mb": free_vram,
        "total_ram_mb": ram_mb,
        "cpu_count": cpus,
        "can_serve": tiers,
        "ollama_spill": spill,
    }
    try:
        await pool.execute(
            """
            INSERT INTO node_caps(node_id, free_vram_mb, total_ram_mb,
                                  cpu_count, can_serve, ollama_spill, updated_at)
            VALUES($1, $2, $3, $4, $5, $6, now())
            ON CONFLICT (node_id) DO UPDATE SET
              free_vram_mb=$2, total_ram_mb=$3, cpu_count=$4,
              can_serve=$5, ollama_spill=$6, updated_at=now()
            """,
            node_id,
            free_vram,
            ram_mb,
            cpus,
            tiers,
            spill,
        )
    except Exception as err:  # missing migration 004 must not kill the worker
        log.warning("node_caps registration skipped: %s", err)
    log.info("node %s capacity: %s", node_id, caps)
    return caps
