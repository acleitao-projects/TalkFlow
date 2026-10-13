from talkflow.features.snippets.matcher import expand
from talkflow.features.snippets.store import Snippet


def _snip(trigger, expansion):
    return Snippet(id="x", trigger=trigger, expansion=expansion, created_at="", updated_at="")


def test_basic_expansion():
    out = expand("send my email signature please", [_snip("email signature", "Best, Alex")])
    assert out == "send my Best, Alex please"


def test_longest_match_wins_on_overlap():
    snippets = [_snip("my email", "SHORT"), _snip("my email signature", "LONG")]
    out = expand("insert my email signature now", snippets)
    assert "LONG" in out
    assert "SHORT" not in out


def test_no_match_passthrough():
    out = expand("nothing to expand here", [_snip("foobar", "X")])
    assert out == "nothing to expand here"


def test_empty_inputs():
    assert expand("", [_snip("a", "b")]) == ""
    assert expand("hello", []) == "hello"
