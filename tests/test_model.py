import pytest

from pr_verification.model import ModelRequest, ScriptedModel, ScriptExhausted


def test_scripted_model_replays_its_responses_in_order_and_records_each_request() -> None:
    model = ScriptedModel(["first", "second"])
    one = ModelRequest(instructions="List the Claims.", data="one")
    two = ModelRequest(instructions="List the Claims.", data="two")

    assert [model.complete(one), model.complete(two)] == ["first", "second"]
    assert model.requests == [one, two]


def test_scripted_model_asked_for_more_than_was_prepared_says_so() -> None:
    model = ScriptedModel(["only"])
    model.complete(ModelRequest(instructions="List the Claims.", data="one"))

    with pytest.raises(ScriptExhausted, match="1 prepared"):
        model.complete(ModelRequest(instructions="List the Claims.", data="two"))
