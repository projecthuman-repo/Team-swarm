"""Optional role-modeling upgrade: CrewAI (MIT) — runbook §18.

Adopt when you prefer declarative crews over the bespoke per-role workers,
accepting CrewAI's weaker built-in observability. This module models the
four swarm roles as a CrewAI crew running against the same local
OpenAI-compatible endpoint (Ollama/vLLM) — useful for triage/planning
passes where roles collaborate on one task before it enters the bus.

    pip install -r requirements-orchestration.txt
    python -m agent.crew_roles "add a /healthz endpoint to the API"
"""

from __future__ import annotations

import os
import sys

MODEL = os.environ.get("AIDER_MODEL", "ollama/maxwell1500/ornith-9b:Q4_K_M")
OLLAMA_API_BASE = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")


def _read(path: str) -> str:
    root = os.path.join(os.path.dirname(__file__), "..")
    try:
        with open(os.path.join(root, path)) as f:
            return f.read()
    except OSError:
        return ""


def build_crew(task_title: str):
    try:
        from crewai import Agent, Crew, Process, Task
        from crewai.llm import LLM
    except ImportError as err:
        raise SystemExit(
            "CrewAI is an optional upgrade: "
            "pip install -r requirements-orchestration.txt"
        ) from err

    llm = LLM(model=MODEL, base_url=OLLAMA_API_BASE)
    guardrails = _read("guardrails.md")

    def role_agent(role: str, goal: str) -> Agent:
        return Agent(
            role=role,
            goal=goal,
            backstory=f"{_read('identity.md')}\n\nHard rules:\n{guardrails}",
            llm=llm,
            allow_delegation=False,
            verbose=True,
        )

    triage = role_agent(
        "triage",
        "Turn the inbound request into a well-scoped, testable task spec "
        "with the right role label.",
    )
    backend = role_agent(
        "backend", "Plan the server-side implementation for the spec."
    )
    frontend = role_agent(
        "frontend", "Plan any UI changes the spec needs."
    )
    reviewer = role_agent(
        "review",
        "Review the combined plan; flag risks, missing tests, and diff-size "
        "concerns before any code is written.",
    )
    # v4.1 T2.3 — independent verifier template: reviews ONLY the diff +
    # task spec (via SharedContext), never the author's conversation, so
    # author biases don't leak into verification.
    verifier = role_agent(
        "verifier",
        "Independently verify a finished diff against its task spec with "
        "no knowledge of how it was produced. Flag any seeded defect, "
        "missing test, or scope creep. End with VERDICT: PASS or BLOCK.",
    )
    _ = verifier  # exposed for swarm-hard fan-out (agent/verifier.py runs it)

    tasks = [
        Task(
            description=f"Scope this request into a task spec: {task_title}",
            expected_output="A concise task spec with acceptance criteria "
            "and a role:<name> label.",
            agent=triage,
        ),
        Task(
            description="Plan the backend work for the spec.",
            expected_output="Ordered implementation steps with test plan.",
            agent=backend,
        ),
        Task(
            description="Plan the frontend work for the spec (or state 'none').",
            expected_output="Ordered implementation steps or 'none needed'.",
            agent=frontend,
        ),
        Task(
            description="Review the plans against the guardrails and CI gates.",
            expected_output="Approval or a list of required changes.",
            agent=reviewer,
        ),
    ]
    return Crew(
        agents=[triage, backend, frontend, reviewer],
        tasks=tasks,
        process=Process.sequential,
    )


def main() -> None:
    title = " ".join(sys.argv[1:]) or "demo: add a /healthz endpoint"
    result = build_crew(title).kickoff()
    print(result)


if __name__ == "__main__":
    main()
