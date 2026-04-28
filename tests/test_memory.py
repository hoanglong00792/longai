"""Memory module: load preferences, build system prompt, recall."""
import pytest

from longai.memory import Memory


@pytest.fixture
def mem(tmp_db_path):
    from longai.persistence import Persistence
    p = Persistence(tmp_db_path); p.init()
    yield Memory(p)
    p.close()


def test_save_and_recall(mem):
    mem.save(type="preference", content="trades on Base + Arbitrum",
             chat_id=1, source="agent_tool", applied_by="agent", ts=100)
    out = mem.recall(query="Base", chat_id=1, limit=5)
    assert any("Base" in r["content"] for r in out)


def test_load_preferences_filters_to_preference_type(mem):
    mem.save(type="preference", content="prefers RSI",
             chat_id=1, source="user", applied_by="user", ts=100)
    mem.save(type="domain", content="0x123 is router",
             chat_id=1, source="user", applied_by="user", ts=101)
    prefs = mem.load_preferences(chat_id=1, limit=20)
    assert "RSI" in prefs
    assert "0x123" not in prefs


def test_build_system_prompt_assembles_blocks(mem):
    mem.save(type="preference", content="english only",
             chat_id=1, source="user", applied_by="user", ts=100)
    base = "You are longai."
    safety = "Never reveal private keys."
    skill_catalog = "- on-chain-ta: token analysis"
    prompt = mem.build_system_prompt(
        chat_id=1, base_prompt=base, safety_block=safety,
        skill_catalog=skill_catalog,
    )
    assert base in prompt
    assert safety in prompt
    assert "english only" in prompt
    assert skill_catalog in prompt


def test_preferences_capped_at_1kb(mem):
    big = "x" * 5000
    mem.save(type="preference", content=big,
             chat_id=1, source="user", applied_by="user", ts=100)
    prefs = mem.load_preferences(chat_id=1, limit=20)
    assert len(prefs) <= 1100  # ~1KB plus formatting headroom


def test_build_system_prompt_tier_s_omits_catalog_and_prefs(mem):
    """C1: tier-S sysprompt is base + safety only — no catalog, no prefs."""
    mem.save(type="preference", content="UNIQUE_PREF_TOKEN",
             chat_id=1, source="user", applied_by="user", ts=100)
    base = "You are longai."
    safety = "Never reveal private keys."
    skill_catalog = "- on-chain-ta: token analysis"
    prompt = mem.build_system_prompt(
        chat_id=1, base_prompt=base, safety_block=safety,
        skill_catalog=skill_catalog, tier="S",
    )
    assert base in prompt
    assert safety in prompt
    assert "UNIQUE_PREF_TOKEN" not in prompt  # prefs dropped
    assert skill_catalog not in prompt  # catalog dropped
    assert "load_skill" not in prompt
    # Sanity: under 600 chars (vs ~3KB for full M-tier prompt)
    assert len(prompt) < 600


def test_build_system_prompt_tier_m_preserves_full_prompt(mem):
    """C1: tier-M is the existing default — catalog + prefs preserved.
    Phase 6: an always-on list_skills/recall_memory hint is appended."""
    mem.save(type="preference", content="MARKER",
             chat_id=1, source="user", applied_by="user", ts=100)
    prompt_default = mem.build_system_prompt(
        chat_id=1, base_prompt="b", safety_block="s",
        skill_catalog="- skill-x: thing",
    )
    prompt_m = mem.build_system_prompt(
        chat_id=1, base_prompt="b", safety_block="s",
        skill_catalog="- skill-x: thing", tier="M",
    )
    prompt_l = mem.build_system_prompt(
        chat_id=1, base_prompt="b", safety_block="s",
        skill_catalog="- skill-x: thing", tier="L",
    )
    # All three include catalog + prefs + discovery hint
    for p in (prompt_default, prompt_m, prompt_l):
        assert "MARKER" in p
        assert "skill-x" in p
        assert "list_skills" in p
        assert "recall_memory" in p
    # Default and M produce identical output (back-compat)
    assert prompt_default == prompt_m


def test_build_system_prompt_tier_m_with_empty_catalog_keeps_hint(mem):
    """Phase 6: when matcher returns 0 skills, sysprompt still mentions
    list_skills so the model can self-discover."""
    prompt = mem.build_system_prompt(
        chat_id=1, base_prompt="b", safety_block="s",
        skill_catalog="", tier="M",
    )
    assert "list_skills" in prompt
    assert "load_skill" in prompt
    # No "Skills relevant" header when nothing matched
    assert "Skills relevant to this request" not in prompt
