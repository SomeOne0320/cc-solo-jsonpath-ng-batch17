"""
Verify the type annotations of ``plan_update()`` and ``apply_update_plan()``.

``plan_update()`` accepts any JSON value as data and returns a list of
entries, each of which is a plain ``dict``.
``apply_update_plan()`` accepts a plan of the same shape.

A plan can also be passed as JSON text or wrapped in a mapping with an
``updates`` key. Those spellings are accepted for convenience.
"""

import typing as t

from jsonpath_ng import UpdatePlan
from jsonpath_ng import UpdatePlanEntry
from jsonpath_ng.parser import parse

expression = parse("$.a[*]")

# Any JSON value is accepted as data, exactly as `update()` accepts it.
expression.plan_update({"a": [1, 2]}, 3)
expression.plan_update([1, 2], 3)
expression.plan_update("text", 3)
expression.plan_update(5, 3)
expression.plan_update(None, 3)

# The plan is a list of entries.
plan = expression.plan_update({"a": [1, 2]}, 3)
t.assert_type(plan, UpdatePlan)
t.assert_type(plan, list[UpdatePlanEntry])
t.assert_type(plan[0]["path"], t.Any)
t.assert_type(plan[0]["old_value"], t.Any)
t.assert_type(plan[0]["new_value"], t.Any)

# The plan can be applied to a fresh document.
t.assert_type(expression.apply_update_plan({"a": [1, 2]}, plan), t.Any)
expression.apply_update_plan(
    {"a": [1, 2]}, '[{"path": "$[0]", "old_value": 1, "new_value": 2}]'
)
expression.apply_update_plan({"a": [1, 2]}, {"updates": plan})
expression.apply_update_plan({"a": [1, 2]}, [])

# The plan is the wrong type for an expression.
parse(plan)  # type: ignore[arg-type]
