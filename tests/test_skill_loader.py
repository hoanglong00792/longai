# tests/test_skill_loader.py
"""I12 — read-only skill loader: list and load (8KB cap)."""
import json
import os

import pytest

from longai_mcps.skill_loader.server import (
    _list_skills_impl,
    _load_skill_impl,
)


@pytest.fixture
def fake_repos(tmp_path, monkeypatch):
    """Build three sibling skill repos with one skill each."""
    shared = tmp_path / "shared" / "skills" / "alpha"
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Test alpha skill\naccess: shared\n---\n# Alpha\nBody of alpha."
    )
    personal = tmp_path / "personal" / "skills" / "beta"
    personal.mkdir(parents=True)
    (personal / "SKILL.md").write_text(
        "---\nname: beta\ndescription: Test beta skill\naccess: personal\n---\n# Beta\n" + ("X" * 10000)
    )
    work = tmp_path / "work" / "skills" / "gamma"
    work.mkdir(parents=True)
    (work / "SKILL.md").write_text(
        "---\nname: gamma\ndescription: Test gamma skill\n---\n# Gamma\nShort body."
    )
    monkeypatch.setenv("LONGAI_SKILLS_SHARED", str(tmp_path / "shared"))
    monkeypatch.setenv("LONGAI_SKILLS_PERSONAL", str(tmp_path / "personal"))
    monkeypatch.setenv("LONGAI_SKILLS_WORK", str(tmp_path / "work"))
    return tmp_path


def test_list_skills_returns_all(fake_repos):
    out = _list_skills_impl(query=None, access=None)
    names = {s["name"] for s in out["skills"]}
    assert names == {"alpha", "beta", "gamma"}


def test_list_skills_query_filter(fake_repos):
    out = _list_skills_impl(query="alpha", access=None)
    names = {s["name"] for s in out["skills"]}
    assert names == {"alpha"}


def test_list_skills_access_filter(fake_repos):
    out = _list_skills_impl(query=None, access="personal")
    names = {s["name"] for s in out["skills"]}
    assert names == {"beta"}


def test_load_skill_returns_body(fake_repos):
    out = _load_skill_impl("alpha")
    assert "Body of alpha" in out["body"]


def test_load_skill_strips_frontmatter(fake_repos):
    out = _load_skill_impl("alpha")
    assert "---" not in out["body"][:5]
    assert "name: alpha" not in out["body"]


def test_load_skill_capped_at_8kb(fake_repos):
    out = _load_skill_impl("beta")
    assert len(out["body"]) <= 8500  # cap + truncation marker headroom
    assert "[...skill body truncated" in out["body"]


def test_unknown_skill_returns_error(fake_repos):
    out = _load_skill_impl("nonexistent")
    assert "error" in out


# ── Complexity field (PR C — feeds Loop's tier-bump logic) ─────────────


@pytest.fixture
def repos_with_complexity(tmp_path, monkeypatch):
    """Skills declaring various complexity values in frontmatter."""
    shared = tmp_path / "shared" / "skills"
    for name, complexity_line in [
        ("light", "complexity: S\n"),
        ("middle", "complexity: M\n"),
        ("heavy", "complexity: L\n"),
        ("noisy", "complexity: l\n"),         # lowercase — should normalize to L
        ("invalid", "complexity: HUGE\n"),    # invalid — should default to M
        ("nodecl", ""),                        # absent — should default to M
    ]:
        d = shared / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: skill {name}\n"
            f"access: shared\n{complexity_line}---\n# {name}\nBody."
        )
    monkeypatch.setenv("LONGAI_SKILLS_SHARED", str(tmp_path / "shared"))
    monkeypatch.delenv("LONGAI_SKILLS_PERSONAL", raising=False)
    monkeypatch.delenv("LONGAI_SKILLS_WORK", raising=False)
    return tmp_path


def test_load_skill_returns_complexity_when_set(repos_with_complexity):
    assert _load_skill_impl("light")["complexity"] == "S"
    assert _load_skill_impl("middle")["complexity"] == "M"
    assert _load_skill_impl("heavy")["complexity"] == "L"


def test_load_skill_normalizes_lowercase_complexity(repos_with_complexity):
    assert _load_skill_impl("noisy")["complexity"] == "L"


def test_load_skill_defaults_complexity_to_m(repos_with_complexity):
    """Skills without `complexity:` or with invalid values default to M."""
    assert _load_skill_impl("nodecl")["complexity"] == "M"
    assert _load_skill_impl("invalid")["complexity"] == "M"


def test_list_skills_includes_complexity(repos_with_complexity):
    out = _list_skills_impl(query=None, access=None)
    by_name = {s["name"]: s["complexity"] for s in out["skills"]}
    assert by_name["light"] == "S"
    assert by_name["heavy"] == "L"
    assert by_name["nodecl"] == "M"
