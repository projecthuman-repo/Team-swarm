"""v4.1 T4.1 — capacity probe tier math and spill detection (mocked NVML)."""

from agent.capacity import compute_tiers, detect_ollama_spill, serveable_tiers


def test_gpu_less_host_registers_cpu_only_plus_api():
    assert compute_tiers(0, spill=False) == ["cpu-only", "api"]


def test_12gb_serves_9b_q4_only():
    tiers = compute_tiers(12_000, spill=False)
    assert "9b-q4" in tiers and "35b-q4" not in tiers and "api" in tiers


def test_24gb_serves_35b_with_kv_headroom():
    tiers = compute_tiers(24_000, spill=False)
    assert {"9b-q4", "9b-bf16", "35b-q4"} <= set(tiers)


def test_20gb_does_not_advertise_35b():
    # 35B needs ~24GB free including KV-cache headroom
    assert "35b-q4" not in compute_tiers(20_000, spill=False)


def test_spill_demotes_to_cpu_only():
    assert compute_tiers(24_000, spill=True) == ["cpu-only", "api"]


def test_detect_spill_from_ollama_ps():
    header = "NAME  ID  SIZE  PROCESSOR  UNTIL\n"
    assert detect_ollama_spill(header + "m1 x 8GB 100% GPU 5m") is False
    assert detect_ollama_spill(header + "m1 x 25GB 22%/78% CPU/GPU 5m") is True
    assert detect_ollama_spill(header + "m1 x 8GB 100% CPU 5m") is True


def test_serveable_tiers_keeps_default_subject():
    caps = {"can_serve": ["9b-q4", "35b-q4", "api"]}
    tiers = serveable_tiers(caps)
    assert tiers[0] == "default"  # backward-compatible v4.0 subject
    assert "api" not in tiers  # api is a routing target, not a queue
