"""Phase 6 — skill_matcher: prompt → relevant skill subset."""
from longai.skill_matcher import _tokens, match_skills


# ── tokenization ────────────────────────────────────────────────────────

def test_tokens_drops_stopwords_and_short_words():
    assert _tokens("the cat is on the mat") == {"cat", "mat"}


def test_tokens_keeps_domain_words():
    """Domain words like 'swap', 'buy', 'send' must NOT be stripped —
    they're exactly the trigger keywords skills index by."""
    out = _tokens("swap ETH to USDC and buy on base")
    assert "swap" in out
    assert "buy" in out
    assert "base" in out
    assert "eth" in out
    assert "usdc" in out


def test_tokens_lowercases():
    assert _tokens("SWAP ETH") == {"swap", "eth"}


def test_tokens_strips_punctuation():
    assert _tokens("swap, ETH-to-USDC!") == {"swap", "eth", "usdc"}


def test_tokens_empty_input():
    assert _tokens("") == set()
    assert _tokens(None) == set()  # type: ignore[arg-type]


# ── matching ────────────────────────────────────────────────────────────

_FAKE_SKILLS = [
    {"name": "swap-build", "description": "Build a swap transaction. Use when asked to swap tokens, trade crypto, exchange ETH USDC."},
    {"name": "swap-execute", "description": "Execute a previously built swap transaction on-chain."},
    {"name": "limit-order", "description": "Create a gasless limit order at a target price for a token swap."},
    {"name": "on-chain-ta", "description": "On-chain token analysis using a contract address."},
    {"name": "long-writing", "description": "Use when writing blog posts, essays, long-form content on behalf of Long Mai."},
    {"name": "judge-hung", "description": "Trần Văn Hùng — Vietnamese real estate tycoon evaluating arguments."},
    {"name": "fortress-analyst", "description": "Senior macro-economist providing portfolio strategy advice."},
]


def test_match_skills_swap_prompt_returns_swap_skills():
    """A multi-token swap prompt should match the swap skills (≥2 overlap)
    and exclude unrelated ones. limit-order has only 'swap' in common
    (1 token) — correctly below min_score=2."""
    out = match_skills("swap ETH to USDC tokens trade", _FAKE_SKILLS)
    names = {s["name"] for s in out}
    assert "swap-build" in names  # name + desc share swap, tokens, trade
    # Unrelated skills filtered out
    assert "long-writing" not in names
    assert "judge-hung" not in names
    assert "fortress-analyst" not in names


def test_match_skills_greeting_returns_empty():
    """Tier-S smoke prompt: 'hi' has no usable tokens after filtering."""
    assert match_skills("hi", _FAKE_SKILLS) == []
    assert match_skills("hello there", _FAKE_SKILLS) == []


def test_match_skills_below_threshold_excluded():
    """A prompt sharing only 1 token shouldn't match (min_score=2 default)."""
    # "swap" only — swap-execute has only "swap" matching
    out = match_skills("swap", _FAKE_SKILLS)
    # All swap-flavored skills include "swap"; need 2+ overlapping
    # tokens. Single-token prompt → 1 match max → filtered out.
    assert out == []


def test_match_skills_min_score_one_recall_more():
    out = match_skills("swap", _FAKE_SKILLS, min_score=1)
    names = {s["name"] for s in out}
    assert "swap-build" in names
    assert "swap-execute" in names


def test_match_skills_top_n_caps_results():
    """Out of many matches, return at most top_n."""
    skills = [
        {"name": f"swap-{i}",
         "description": "swap transaction execute exchange tokens trade"}
        for i in range(20)
    ]
    out = match_skills(
        "swap transaction execute exchange tokens trade", skills, top_n=5,
    )
    assert len(out) == 5


def test_match_skills_ties_preserve_config_order():
    """Two skills with identical scores → original order wins."""
    skills = [
        {"name": "first", "description": "alpha bravo charlie"},
        {"name": "second", "description": "alpha bravo charlie"},
    ]
    out = match_skills("alpha bravo", skills)
    assert [s["name"] for s in out] == ["first", "second"]


def test_match_skills_empty_skills_input():
    assert match_skills("anything goes here", []) == []


def test_match_skills_token_score_descending():
    """Higher-overlap matches come first."""
    skills = [
        {"name": "weak", "description": "swap tokens"},
        {"name": "strong",
         "description": "swap tokens trade execute exchange transaction"},
    ]
    out = match_skills(
        "swap tokens trade execute exchange transaction", skills,
    )
    assert out[0]["name"] == "strong"
    assert out[1]["name"] == "weak"


def test_match_skills_punctuation_in_prompt_doesnt_break_match():
    out = match_skills("swap, ETH/USDC tokens!!!", _FAKE_SKILLS)
    names = {s["name"] for s in out}
    assert "swap-build" in names  # matched on "swap" + "tokens"
