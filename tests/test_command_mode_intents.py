from talkflow.features.command_mode.intents import Intent, classify, extract_search_query


def test_classify_search():
    assert classify("search for the nearest coffee shop") == Intent.SEARCH
    assert classify("look up the weather in Lisbon") == Intent.SEARCH
    assert classify("Google the capital of Peru") == Intent.SEARCH


def test_classify_edit_default():
    assert classify("make this more concise") == Intent.EDIT
    assert classify("draft a reply saying I'll be there at noon") == Intent.EDIT


def test_extract_search_query_strips_trigger_phrase():
    assert extract_search_query("search for the nearest coffee shop") == "the nearest coffee shop"
    assert extract_search_query("look up the weather in Lisbon") == "the weather in Lisbon"
