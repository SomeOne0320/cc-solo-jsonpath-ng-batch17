import pytest

import jsonpath_ng.ext

parsers = pytest.mark.parametrize(
    "parse",
    (jsonpath_ng.parse, jsonpath_ng.ext.parse),
    ids=lambda function: function.__module__,
)


@pytest.mark.parametrize(
    "data, expected_serialization",
    (
        ("a..b", "(a..b)"),  # Not ideal
        # This is not an ideal serialization,
        # nor is it consistent with the precedence rules of 'a..b[c]', below.
        ("a..b.c", "(a..b.c)"),
        ("a..b[c]", "(a..b).c"),
        # A bracketed segment in the right side is parenthesized
        # so that it is not captured by the `..` expression when re-parsed.
        ("a..[0]", "(a..[0])"),
        ("a..(b[0])", "(a..(b[0]))"),
        ("a..(b[0].c)", "(a..(b[0].c))"),
        ("a..(b[1:2])", "(a..(b[1:2]))"),
    ),
)
@parsers
def test_serialization(parse, data, expected_serialization):
    """Test serialization of Descendant instances.

    Regardless of what the test inputs claim,
    the expected serialization must still parse to the same JSONPath
    that the original data parsed to.
    """

    parsed = parse(data)
    reserialized = str(parsed)

    assert reserialized == expected_serialization

    # Now that the test has succeeded, do an additional sanity check.
    assert parsed == parse(reserialized)
