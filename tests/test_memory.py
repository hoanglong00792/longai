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
