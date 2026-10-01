from careerops.llm import extract_json


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('<think>hmm {x}</think> Sure: {"b": [1,2]} done') == {"b": [1, 2]}
