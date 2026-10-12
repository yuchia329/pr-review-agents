import pytest

from pr_verification.model import (
    ModelRequest,
    PricedModel,
    PricedResponse,
    ScriptedModel,
    ScriptExhausted,
)


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


def test_scripted_model_says_each_request_cost_its_price_and_shares_one_script_with_complete() -> (
    None
):
    model = ScriptedModel(["first", "second", "third"], dollars_per_request=0.25)
    one, two, three = (ModelRequest("List the Claims.", data) for data in ("one", "two", "three"))

    assert isinstance(model, PricedModel)
    assert model.complete_priced(one) == PricedResponse("first", 0.25)
    assert model.complete(two) == "second"
    assert model.complete_priced(three) == PricedResponse("third", 0.25)
    assert model.requests == [one, two, three]


def test_scripted_model_given_no_price_says_a_request_cost_nothing() -> None:
    model = ScriptedModel(["only"])

    priced = model.complete_priced(ModelRequest(instructions="List the Claims.", data="one"))

    assert priced == PricedResponse("only", 0.0)


def test_scripted_model_asked_for_a_priced_response_beyond_its_script_says_so() -> None:
    model = ScriptedModel(["only"], dollars_per_request=0.25)
    model.complete(ModelRequest(instructions="List the Claims.", data="one"))

    with pytest.raises(ScriptExhausted, match="1 prepared"):
        model.complete_priced(ModelRequest(instructions="List the Claims.", data="two"))
