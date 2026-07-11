"""Shadow-eval harness (v4.1 T6.1 / B6) — lands early; gates everything.

Replays a golden task pack against a CANDIDATE config (model, scaffold,
or flag) vs the CURRENT DEFAULT, reporting green-rate / tokens / latency
deltas as markdown. Required before flipping any default in the v4.1
plan — leaderboards don't transfer (SWE-bench Verified is Python-heavy
and partially contaminated); the swarm's own repo is the deciding
evidence.

Golden pack: eval/golden/*.json, each {"title", "role", "verify_cmd"} —
drawn from Forgejo history + synthetics.

    python scripts/shadow_eval.py --candidate AIDER_MODEL=ollama/gpt-oss:20b
    python scripts/shadow_eval.py --report a.json b.json   # compare saved runs
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent", "gen"))

log = logging.getLogger("shadow-eval")

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "..", "eval", "golden")


def load_golden_pack() -> list[dict]:
    tasks = []
    for path in sorted(glob.glob(os.path.join(GOLDEN_DIR, "*.json"))):
        with open(path) as f:
            tasks.append({**json.load(f), "name": os.path.basename(path)})
    return tasks


async def run_config(label: str, env_overrides: dict, pack: list[dict]) -> dict:
    """Run the golden pack under one config; returns metrics."""
    from swarm.v1 import task_pb2

    from agent.budget import ValkeyBudget
    from agent.openbao import lease_secret
    from agent.sandbox import run_sandboxed_aider

    old_env = {k: os.environ.get(k) for k in env_overrides}
    os.environ.update(env_overrides)
    try:
        secret = lease_secret("forgejo")
        results = []
        for item in pack:
            tid = str(uuid.uuid4())
            task = task_pb2.Task(
                task_id=tid,
                role=item.get("role", "backend"),
                title=item["title"],
                branch=f"feature/eval-{tid[:8]}",
                budget_tokens=200_000,
                verify_cmd=item.get("verify_cmd", ""),
            )
            start = time.monotonic()
            try:
                res = await run_sandboxed_aider(
                    task, secret, budget=ValkeyBudget("eval", tid, 200_000)
                )
                results.append(
                    {
                        "name": item["name"],
                        "green": res.first_pass_green,
                        "tokens": res.tokens_spent,
                        "latency_s": time.monotonic() - start,
                    }
                )
            except Exception as err:
                results.append(
                    {
                        "name": item["name"],
                        "green": False,
                        "tokens": 0,
                        "latency_s": time.monotonic() - start,
                        "error": str(err)[:200],
                    }
                )
        return summarize(label, results)
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def summarize(label: str, results: list[dict]) -> dict:
    n = len(results) or 1
    return {
        "label": label,
        "tasks": len(results),
        "green_rate": sum(1 for r in results if r["green"]) / n,
        "avg_tokens": sum(r["tokens"] for r in results) / n,
        "avg_latency_s": sum(r["latency_s"] for r in results) / n,
        "results": results,
    }


def markdown_report(default: dict, candidate: dict) -> str:
    """Delta report — the adopt/reject evidence (T6.1 acceptance)."""

    def delta(a: float, b: float, pct: bool = False, lower_better: bool = False) -> str:
        d = b - a
        arrow = "better" if (d < 0) == lower_better and d != 0 else (
            "worse" if d != 0 else "same")
        val = f"{d:+.1%}" if pct else f"{d:+.1f}"
        return f"{val} ({arrow})"

    lines = [
        "# Shadow-eval report",
        "",
        f"| metric | default `{default['label']}` | candidate `{candidate['label']}` | delta |",
        "|---|---|---|---|",
        f"| green rate | {default['green_rate']:.0%} | {candidate['green_rate']:.0%} "
        f"| {delta(default['green_rate'], candidate['green_rate'], pct=True)} |",
        f"| avg tokens | {default['avg_tokens']:.0f} | {candidate['avg_tokens']:.0f} "
        f"| {delta(default['avg_tokens'], candidate['avg_tokens'], lower_better=True)} |",
        f"| avg latency (s) | {default['avg_latency_s']:.1f} | {candidate['avg_latency_s']:.1f} "
        f"| {delta(default['avg_latency_s'], candidate['avg_latency_s'], lower_better=True)} |",
        "",
    ]
    verdict = (
        "ADOPT candidate (beats default on green rate without regressions)"
        if candidate["green_rate"] > default["green_rate"]
        and candidate["avg_tokens"] <= default["avg_tokens"] * 1.1
        else "KEEP default (candidate did not beat it — the plan's own threshold)"
    )
    lines.append(f"**Verdict:** {verdict}")
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    if args.report:
        with open(args.report[0]) as f:
            default = json.load(f)
        with open(args.report[1]) as f:
            candidate = json.load(f)
        print(markdown_report(default, candidate))
        return 0

    pack = load_golden_pack()
    if not pack:
        raise SystemExit(f"no golden tasks in {GOLDEN_DIR} — add *.json fixtures")
    overrides = dict(kv.split("=", 1) for kv in args.candidate)
    log.info("running %d golden tasks x 2 configs", len(pack))
    default = await run_config("default", {}, pack)
    candidate = await run_config(
        ",".join(args.candidate) or "candidate", overrides, pack
    )
    report = markdown_report(default, candidate)
    print(report)
    out = os.path.join(os.path.dirname(GOLDEN_DIR), "last_report.md")
    with open(out, "w") as f:
        f.write(report + "\n")
    for label, summary in (("default", default), ("candidate", candidate)):
        with open(os.path.join(os.path.dirname(GOLDEN_DIR), f"{label}.json"), "w") as f:
            json.dump(summary, f, indent=2)
    return 0


def main() -> None:
    logging.basicConfig(level="INFO")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--candidate", action="append", default=[],
                   metavar="KEY=VALUE", help="env override(s) for the candidate config")
    p.add_argument("--report", nargs=2, metavar=("DEFAULT.json", "CANDIDATE.json"),
                   help="render a report from two saved runs")
    sys.exit(asyncio.run(main_async(p.parse_args())))


if __name__ == "__main__":
    main()
