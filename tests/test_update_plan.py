import copy
import json
from collections import UserDict

import pytest

from jsonpath_ng import parse as base_parse
from jsonpath_ng.exceptions import JSONPathError
from jsonpath_ng.exceptions import UpdatePlanConflictError
from jsonpath_ng.exceptions import UpdatePlanError
from jsonpath_ng.exceptions import UpdatePlanStaleError
from jsonpath_ng.ext.parser import parse as ext_parse

parsers = pytest.mark.parametrize(
    "parse",
    (
        pytest.param(base_parse, id="parse=jsonpath_ng.parser.parse"),
        pytest.param(ext_parse, id="parse=jsonpath_ng.ext.parser.parse"),
    ),
)


@parsers
def test_preview_lists_matches_in_order_and_leaves_data_alone(parse):
    data = {"foo": [{"baz": 1}, {"baz": 2}, {"baz": 3}]}
    original = copy.deepcopy(data)

    plan = parse("foo[*].baz").plan_update(data, 9)

    assert plan == [
        {"path": "$['foo'][0]['baz']", "old_value": 1, "new_value": 9},
        {"path": "$['foo'][1]['baz']", "old_value": 2, "new_value": 9},
        {"path": "$['foo'][2]['baz']", "old_value": 3, "new_value": 9},
    ]
    assert data == original


@parsers
def test_plan_is_json_compatible(parse):
    data = {"foo": [{"baz": 1}, {"baz": 2}]}

    plan = parse("foo[*].baz").plan_update(data, {"nested": [1, 2]})

    # The plan can cross a process boundary and come back unchanged.
    assert json.loads(json.dumps(plan)) == plan


@parsers
def test_apply_matches_direct_update(parse):
    data = {"foo": [{"baz": 1}, {"baz": 2}]}
    expression = parse("foo[*].baz")

    plan = json.loads(json.dumps(expression.plan_update(data, 9)))
    updated = expression.apply_update_plan(copy.deepcopy(data), plan)

    expected = copy.deepcopy(data)
    expression.update(expected, 9)
    assert updated == expected


@parsers
@pytest.mark.parametrize(
    "expression, data, val",
    (
        ("$", {"a": 1}, {"b": 2}),
        ("foo", {"foo": 1}, 5),
        ("$[0]", ["a", "b"], "z"),
        ("$[-1]", ["a", "b"], "z"),
        ("$[0:2]", [1, 2, 3], 0),
        ("$[*]", [1, 2, 3], 0),
        ("$.*", {"a": 1, "b": 2}, 0),
        ("$..x", {"a": {"x": 1}, "b": [{"x": 2}]}, 0),
        ("foo.bar", {"foo": {"bar": 1}}, 2),
    ),
)
def test_apply_round_trips_many_query_shapes(parse, expression, data, val):
    path = parse(expression)
    plan = json.loads(json.dumps(path.plan_update(data, val)))

    updated = path.apply_update_plan(copy.deepcopy(data), plan)

    expected = path.update(copy.deepcopy(data), copy.deepcopy(val))
    assert updated == expected


def test_duplicate_matches_are_listed_once():
    data = [1, 2]
    plan = ext_parse("$[0,0]").plan_update(data, 9)
    assert plan == [{"path": "$[0]", "old_value": 1, "new_value": 9}]


def test_union_duplicate_matches_are_listed_once():
    data = {"x": 1}
    with pytest.warns(DeprecationWarning):
        expression = ext_parse("$['x'] | $['x']")

    plan = expression.plan_update(data, 9)

    assert plan == [{"path": "$['x']", "old_value": 1, "new_value": 9}]


def test_a_field_with_dots_is_not_confused_with_nesting():
    data = {"a": {"b": 1}, "a.b": 2}
    expression = ext_parse("$['a.b']")

    plan = expression.plan_update(data, 9)
    assert plan == [{"path": "$['a.b']", "old_value": 2, "new_value": 9}]

    updated = expression.apply_update_plan(copy.deepcopy(data), plan)
    assert updated == {"a": {"b": 1}, "a.b": 9}


def test_an_array_index_is_not_confused_with_a_numeric_field_name():
    data = {"0": "member", "items": ["first", "second"]}
    expression = ext_parse("$['items'][1]")

    plan = expression.plan_update(data, "changed")
    assert plan == [
        {"path": "$['items'][1]", "old_value": "second", "new_value": "changed"}
    ]

    assert expression.apply_update_plan(copy.deepcopy(data), plan) == {
        "0": "member",
        "items": ["first", "changed"],
    }


@pytest.mark.parametrize(
    "name",
    (
        "a.b",
        "a'b",
        "a\\b",
        "a]b",
        "a[0]",
        "",
        "*",
        "true",
        "@x",
        "0",
        "é",
        "😀",
    ),
)
def test_normalized_paths_survive_escaping(name):
    data = {"outer": {name: {"deep": 1}}}
    expression = ext_parse("$['outer']..deep")

    plan = expression.plan_update(data, 0)

    assert len(plan) == 1
    entry = plan[0]
    assert entry["path"] == "$['outer'][%s]['deep']" % _quote(name)
    assert entry["old_value"] == 1
    assert entry["new_value"] == 0

    updated = expression.apply_update_plan(copy.deepcopy(data), plan)
    assert updated == {"outer": {name: {"deep": 0}}}


def _quote(name):
    escaped = (
        name.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\b", "\\b")
        .replace("\f", "\\f")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    escaped = "".join(
        f"\\u{ord(character):04x}" if ord(character) < 0x20 else character
        for character in escaped
    )
    return f"'{escaped}'"


def test_negative_indices_are_normalized():
    data = [1, 2, 3]
    plan = ext_parse("$[-1]").plan_update(data, 9)
    assert plan == [{"path": "$[2]", "old_value": 3, "new_value": 9}]


def test_overlapping_locations_are_rejected():
    data = {"x": {"y": {"x": 3}}, "other": 1}

    with pytest.raises(UpdatePlanConflictError) as exc_info:
        ext_parse("$..x").plan_update(data, 0)

    message = str(exc_info.value)
    assert "$['x']" in message
    assert "$['x']['y']['x']" in message
    # The data must still be untouched.
    assert data == {"x": {"y": {"x": 3}}, "other": 1}


def test_a_location_and_its_parent_are_rejected_in_either_order():
    data = {"a": {"b": 1}}

    with pytest.warns(DeprecationWarning):
        parent_first = ext_parse("($['a']) | ($['a']['b'])")
    with pytest.warns(DeprecationWarning):
        child_first = ext_parse("($['a']['b']) | ($['a'])")

    with pytest.raises(UpdatePlanConflictError):
        parent_first.plan_update(data, 0)

    with pytest.raises(UpdatePlanConflictError):
        child_first.plan_update(data, 0)


@pytest.mark.parametrize(
    "mutate",
    (
        pytest.param(lambda data: data["foo"][0].__setitem__("baz", 5), id="changed"),
        pytest.param(lambda data: data["foo"][0].__setitem__("baz", True), id="bool"),
        pytest.param(lambda data: data["foo"].pop(), id="removed"),
        pytest.param(lambda data: data.pop("foo"), id="parent-removed"),
        pytest.param(
            lambda data: data.__setitem__("foo", [1, 2]), id="parent-replaced"
        ),
    ),
)
def test_apply_writes_nothing_when_a_location_is_stale(mutate):
    data = {"foo": [{"baz": 1}, {"baz": 2}]}
    expression = ext_parse("foo[*].baz")
    plan = json.loads(json.dumps(expression.plan_update(data, 9)))

    changed = copy.deepcopy(data)
    mutate(changed)
    before = copy.deepcopy(changed)

    with pytest.raises(UpdatePlanStaleError):
        expression.apply_update_plan(changed, plan)

    assert changed == before


def test_apply_writes_nothing_when_only_a_later_location_is_stale():
    data = [1, 2, 3]
    expression = ext_parse("$[*]")
    plan = json.loads(json.dumps(expression.plan_update(data, 0)))

    changed = [1, 2, 99]
    with pytest.raises(UpdatePlanStaleError):
        expression.apply_update_plan(changed, plan)

    assert changed == [1, 2, 99]


def test_apply_accepts_an_unchanged_value_of_another_numeric_type():
    data = {"a": 1}
    expression = base_parse("$['a']")
    plan = json.loads(json.dumps(expression.plan_update(data, 2)))

    # JSON does not distinguish 1 from 1.0, so neither may a plan.
    assert expression.apply_update_plan({"a": 1.0}, plan) == {"a": 2}


def test_apply_rejects_boolean_for_number():
    data = {"a": 1}
    expression = base_parse("$['a']")
    plan = json.loads(json.dumps(expression.plan_update(data, 2)))

    with pytest.raises(UpdatePlanStaleError):
        expression.apply_update_plan({"a": True}, plan)


def test_a_list_value_is_consumed_one_element_per_position():
    data = ["a", "b", "c"]
    expression = ext_parse("$[0, 1]")

    plan = expression.plan_update(data, ["x", "y"])
    assert [entry["new_value"] for entry in plan] == ["x", "y"]

    assert expression.apply_update_plan(copy.deepcopy(data), plan) == ["x", "y", "c"]


def test_a_list_value_with_too_few_elements_is_rejected():
    with pytest.raises(UpdatePlanError):
        ext_parse("$[0, 1]").plan_update(["a", "b", "c"], ["x"])


def test_a_list_value_with_extra_elements_is_rejected():
    with pytest.raises(UpdatePlanError):
        ext_parse("$[0, 1]").plan_update(["a", "b", "c"], ["x", "y", "z"])


def test_a_list_value_is_assigned_whole_to_object_members():
    data = {"a": 1, "b": 2}
    expression = ext_parse("$.*")

    plan = expression.plan_update(data, ["x", "y"])
    assert [entry["new_value"] for entry in plan] == [["x", "y"], ["x", "y"]]


def test_apply_accepts_a_json_string_plan():
    data = {"foo": [{"baz": 1}, {"baz": 2}]}
    expression = ext_parse("foo[*].baz")
    plan = expression.plan_update(data, 9)

    updated = expression.apply_update_plan(copy.deepcopy(data), json.dumps(plan))
    assert updated == {"foo": [{"baz": 9}, {"baz": 9}]}


def test_apply_accepts_a_bare_list_of_entries():
    data = {"a": 1}
    plan = [{"path": "$['a']", "old_value": 1, "new_value": 2}]

    assert base_parse("$").apply_update_plan(data, plan) == {"a": 2}


def test_apply_accepts_a_userdict():
    data = UserDict({"a": 1})
    expression = base_parse("a")
    plan = expression.plan_update(data, 2)

    updated = expression.apply_update_plan(UserDict({"a": 1}), plan)
    assert updated == {"a": 2}


def test_update_callbacks_are_supported():
    data = {"foo": [{"baz": 1}, {"baz": 2}]}
    expression = ext_parse("foo[*].baz")

    def double(value, parent, key):
        return value * 2

    plan = expression.plan_update(data, double)
    assert [entry["new_value"] for entry in plan] == [2, 4]
    assert data == {"foo": [{"baz": 1}, {"baz": 2}]}

    assert expression.apply_update_plan(copy.deepcopy(data), plan) == {
        "foo": [{"baz": 2}, {"baz": 4}]
    }


def test_in_place_update_callbacks_are_supported():
    data = {"foo": [{"baz": 1}, {"baz": 2}]}
    expression = ext_parse("foo[*].baz")

    def bump(value, parent, key):
        parent[key] = value + 10
        # Deliberately return None, as an in-place callback does.

    plan = expression.plan_update(data, bump)
    assert [entry["new_value"] for entry in plan] == [11, 12]

    assert expression.apply_update_plan(copy.deepcopy(data), plan) == {
        "foo": [{"baz": 11}, {"baz": 12}]
    }


def test_root_update_callback_is_rejected():
    with pytest.raises(UpdatePlanError):
        base_parse("$").plan_update({"a": 1}, lambda value, parent, key: {"b": 2})


def test_no_matches_produces_an_empty_plan():
    plan = ext_parse("$['missing']").plan_update({"a": 1}, 2)
    assert plan == []

    data = {"a": 1}
    assert ext_parse("$['missing']").apply_update_plan(data, plan) == {"a": 1}


@pytest.mark.parametrize(
    "path",
    (
        "`len`",
        "`keys`",
        "`path`",
    ),
)
def test_computed_matches_cannot_be_planned(path):
    with pytest.raises(UpdatePlanError):
        ext_parse(path).plan_update({"a": 1}, 2)


def test_a_multi_index_match_cannot_be_planned():
    # An `Index` node with several indices can never be a single location.
    from jsonpath_ng.jsonpath import DatumInContext
    from jsonpath_ng.jsonpath import Index
    from jsonpath_ng.jsonpath import JSONPath

    class MultiIndexPath(JSONPath):
        def find(self, data):
            return [DatumInContext(data, path=Index(0, 1), context=None)]

    with pytest.raises(UpdatePlanError):
        MultiIndexPath().plan_update([1, 2], 0)


def test_a_multi_field_match_cannot_be_planned():
    from jsonpath_ng.jsonpath import DatumInContext
    from jsonpath_ng.jsonpath import Fields
    from jsonpath_ng.jsonpath import JSONPath

    class MultiFieldPath(JSONPath):
        def find(self, data):
            return [DatumInContext(data, path=Fields("a", "b"), context=None)]

    with pytest.raises(UpdatePlanError):
        MultiFieldPath().plan_update({"a": 1, "b": 2}, 0)


def test_auto_ids_cannot_be_planned():
    import jsonpath_ng.jsonpath

    jsonpath_ng.jsonpath.auto_id_field = "id"
    try:
        expression = ext_parse("$.id")
        with pytest.raises(UpdatePlanError):
            expression.plan_update({"a": {"id": "x"}}, 2)
    finally:
        jsonpath_ng.jsonpath.auto_id_field = None


def test_hand_written_plans_with_overlaps_are_rejected_before_writing():
    data = {"a": {"b": 1}}
    plan = [
        {"path": "$['a']", "old_value": {"b": 1}, "new_value": 0},
        {"path": "$['a']['b']", "old_value": 1, "new_value": 2},
    ]

    with pytest.raises(UpdatePlanConflictError):
        base_parse("$").apply_update_plan(data, plan)

    assert data == {"a": {"b": 1}}


def test_a_hand_written_plan_with_repeated_entries_is_rejected():
    data = {"a": 1}
    plan = [
        {"path": "$['a']", "old_value": 1, "new_value": 2},
        {"path": "$['a']", "old_value": 1, "new_value": 3},
    ]

    with pytest.raises(UpdatePlanConflictError):
        base_parse("$").apply_update_plan(data, plan)

    assert data == {"a": 1}


def test_repeated_identical_entries_are_merged():
    data = {"a": 1}
    plan = [
        {"path": "$['a']", "old_value": 1, "new_value": 2},
        {"path": "$['a']", "old_value": 1, "new_value": 2},
    ]

    assert base_parse("$").apply_update_plan(data, plan) == {"a": 2}


def test_a_hand_written_plan_with_a_non_normalized_index_is_accepted():
    data = [1, 2]
    plan = [{"path": "$[-1]", "old_value": 2, "new_value": 0}]

    assert base_parse("$").apply_update_plan(data, plan) == [1, 0]


def test_a_hand_written_plan_with_a_dotted_path_is_accepted():
    data = {"a": {"b": 1}}
    plan = [{"path": "a.b", "old_value": 1, "new_value": 0}]

    assert base_parse("$").apply_update_plan(data, plan) == {"a": {"b": 0}}


@pytest.mark.parametrize(
    "plan",
    (
        pytest.param({"nope": []}, id="missing-list"),
        pytest.param({"updates": "nope"}, id="list-not-a-list"),
        pytest.param({"updates": ["nope"]}, id="entry-not-a-mapping"),
        pytest.param({"updates": [{"path": "$[0]"}]}, id="missing-values"),
        pytest.param(
            {"updates": [{"old_value": 1, "new_value": 2}]}, id="missing-path"
        ),
        pytest.param(
            {"updates": [{"path": 1, "old_value": 1, "new_value": 2}]},
            id="path-not-a-string",
        ),
        pytest.param(
            {"updates": [{"path": "$[", "old_value": 1, "new_value": 2}]},
            id="unparseable-path",
        ),
        pytest.param(
            {"updates": [{"path": "$['a'],$['b']", "old_value": 1, "new_value": 2}]},
            id="ambiguous-path",
        ),
        pytest.param(42, id="not-a-plan"),
        pytest.param("{not json", id="invalid-json-text"),
    ),
)
def test_malformed_plans_are_rejected(plan):
    with pytest.raises(UpdatePlanError):
        base_parse("$").apply_update_plan({"a": 1}, plan)


def test_a_path_into_a_missing_location_is_stale():
    data = {"a": 1}
    plan = [{"path": "$['b']", "old_value": 1, "new_value": 2}]

    with pytest.raises(UpdatePlanStaleError) as exc_info:
        base_parse("$").apply_update_plan(data, plan)

    assert exc_info.value.path == "$['b']"


def test_apply_returns_the_root_replacement():
    data = {"a": 1}
    expression = base_parse("$")
    plan = json.loads(json.dumps(expression.plan_update(data, {"b": 2})))

    assert expression.apply_update_plan(data, plan) == {"b": 2}
    # The original document object is left alone.
    assert data == {"a": 1}


def test_apply_rolls_back_if_a_write_fails():
    class Hostile(dict):
        refuse = False

        def __setitem__(self, key, value):
            if self.refuse:
                raise RuntimeError("no")
            super().__setitem__(key, value)

    data = Hostile({"a": 1})
    plan = [{"path": "$['a']", "old_value": 1, "new_value": 2}]
    data.refuse = True

    with pytest.raises(RuntimeError):
        base_parse("$").apply_update_plan(data, plan)

    assert dict(data) == {"a": 1}


def test_errors_are_jsonpath_errors():
    assert issubclass(UpdatePlanError, JSONPathError)
    assert issubclass(UpdatePlanConflictError, UpdatePlanError)
    assert issubclass(UpdatePlanStaleError, UpdatePlanError)


def test_the_plan_does_not_alias_the_original_values():
    data = {"a": {"nested": [1]}}

    plan = base_parse("$['a']").plan_update(data, {"nested": [2]})
    plan[0]["old_value"]["nested"].append(3)
    plan[0]["new_value"]["nested"].append(4)

    assert data == {"a": {"nested": [1]}}


@parsers
def test_direct_update_is_still_available(parse):
    data = {"foo": [{"baz": 1}, {"baz": 2}]}

    assert parse("foo[*].baz").update(data, 3) == {"foo": [{"baz": 3}, {"baz": 3}]}
