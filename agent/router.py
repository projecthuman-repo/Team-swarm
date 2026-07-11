"""Optional RouteLLM tiering (v4.1 T4.4, Apache-2.0).

Proposes a model tier (9b-q4 / 35b-q4 / api) per task. NOT the default:
adopt only after it beats the baseline on the T6.1 shadow eval
(`make shadow-eval` with ROUTER_ENABLED=1 as the candidate config).

Without routellm installed, `propose_tier` falls back to a transparent
size heuristic so the comparison harness can always run both arms.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("router")

ROUTER_ENABLED = os.environ.get("ROUTER_ENABLED", "") == "1"
ROUTER_THRESHOLD = float(os.environ.get("ROUTER_THRESHOLD", "0.5"))

_controller = None


def _routellm():
    global _controller
    if _controller is None:
        try:
            from routellm.controller import Controller

            _controller = Controller(
                routers=["mf"],
                strong_model="Ornith-1.0-35B",
                weak_model="Ornith-1.0-9B",
            )
        except ImportError:
            _controller = False
    return _controller


def heuristic_tier(title: str) -> str:
    """Transparent fallback: route by task-description complexity."""
    text = (title or "").lower()
    hard_markers = ("refactor", "migrate", "concurren", "race", "architecture",
                    "security", "[swarm-hard]")
    if any(m in text for m in hard_markers) or len(text) > 2000:
        return "35b-q4"
    return "9b-q4"


def propose_tier(title: str) -> str:
    """Tier proposal for one task. '' disables routing (default subject)."""
    if not ROUTER_ENABLED:
        return ""
    controller = _routellm()
    if controller:
        try:
            routed = controller.route(prompt=title, router="mf",
                                      threshold=ROUTER_THRESHOLD)
            return "35b-q4" if "35B" in str(routed) else "9b-q4"
        except Exception as err:
            log.warning("routellm failed (%s); heuristic fallback", err)
    return heuristic_tier(title)
