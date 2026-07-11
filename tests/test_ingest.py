"""Ingest logic: role-label parsing and dedupe key format (runbook §11A)."""

from ingest.forgejo_issue_sync import role_of


def issue(labels):
    return {"labels": [{"name": name} for name in labels]}


def test_role_from_label():
    assert role_of(issue(["swarm-ready", "role:backend"])) == "backend"
    assert role_of(issue(["role:review"])) == "review"


def test_default_role_is_triage():
    assert role_of(issue(["swarm-ready"])) == "triage"
    assert role_of(issue([])) == "triage"


def test_unknown_role_falls_back_to_triage():
    assert role_of(issue(["role:cfo"])) == "triage"
