"""
Build and apply atomic update plans for parsed JSONPath expressions.

An update plan is plain JSON-compatible data. It records, in match order, the
locations that a query matches together with the value found there and the
value that should replace it. Locations are identified with RFC 9535
normalized paths, so an array index cannot be mistaken for an object member
whose name happens to be a number, and a member name that contains a period is
never mistaken for a nested path.

Applying a plan is optimistic: every location is checked first, and if any
location is missing or holds a different value, then nothing is written.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import typing
from collections.abc import Mapping
from collections.abc import MutableMapping
from collections.abc import Sequence

from jsonpath_ng.exceptions import UpdatePlanConflictError
from jsonpath_ng.exceptions import UpdatePlanError
from jsonpath_ng.exceptions import UpdatePlanStaleError

if typing.TYPE_CHECKING:
    from jsonpath_ng.jsonpath import DatumInContext
    from jsonpath_ng.jsonpath import JSONPath

# The categories of value that a normalized path segment can select.
SegmentKind: typing.TypeAlias = typing.Literal["field", "index"]
UpdatePlanEntry: typing.TypeAlias = dict[str, typing.Any]
# A plan is an ordered list of entries, and is itself plain JSON data.
UpdatePlan: typing.TypeAlias = list[UpdatePlanEntry]


@dataclasses.dataclass(frozen=True)
class _Segment:
    """One normalized path segment: an object member or an array index."""

    kind: SegmentKind
    key: str | int


_Location: typing.TypeAlias = tuple[_Segment, ...]


def _field(name: str) -> _Segment:
    return _Segment("field", name)


def _index(index: int) -> _Segment:
    return _Segment("index", index)


def _escape_name(name: str) -> str:
    """Escape a member name as an RFC 9535 normalized-path name selector."""

    parts: list[str] = []
    for character in name:
        if character == "'":
            parts.append("\\'")
        elif character == "\\":
            parts.append("\\\\")
        elif character == "\b":
            parts.append("\\b")
        elif character == "\f":
            parts.append("\\f")
        elif character == "\n":
            parts.append("\\n")
        elif character == "\r":
            parts.append("\\r")
        elif character == "\t":
            parts.append("\\t")
        elif ord(character) < 0x20:
            parts.append(f"\\u{ord(character):04x}")
        else:
            parts.append(character)
    return "".join(parts)


def _format_path(location: _Location) -> str:
    """Format *location* as an RFC 9535 normalized path."""

    parts = ["$"]
    for segment in location:
        if segment.kind == "field":
            name = typing.cast(str, segment.key)
            parts.append(f"['{_escape_name(name)}']")
        else:
            parts.append(f"[{typing.cast(int, segment.key)}]")
    return "".join(parts)


def _parse_path(path: str) -> _Location:
    """Parse an RFC 9535 normalized path.

    This is deliberately strict: normalized paths are the only path syntax
    that an update plan needs to guarantee a unique, exact location.
    """

    if not path.startswith("$"):
        raise UpdatePlanError(
            f"The update plan path {path!r} is not a normalized JSONPath; "
            "it must begin with '$'."
        )

    segments: list[_Segment] = []
    position = 1
    while position < len(path):
        if path[position] != "[":
            raise UpdatePlanError(
                f"The update plan path {path!r} is not a normalized JSONPath "
                f"near position {position}."
            )
        position += 1

        if position < len(path) and path[position] == "'":
            position += 1
            characters: list[str] = []
            while position < len(path) and path[position] != "'":
                character = path[position]
                if character != "\\":
                    characters.append(character)
                    position += 1
                    continue

                position += 1
                if position >= len(path):
                    raise UpdatePlanError(
                        f"The update plan path {path!r} ends in an escape."
                    )
                escape = path[position]
                position += 1
                if escape == "u":
                    digits = path[position : position + 4]
                    if len(digits) != 4 or any(
                        digit not in "0123456789abcdefABCDEF" for digit in digits
                    ):
                        raise UpdatePlanError(
                            f"The update plan path {path!r} has an invalid "
                            "unicode escape."
                        )
                    characters.append(chr(int(digits, 16)))
                    position += 4
                    continue

                escapes = {
                    "b": "\b",
                    "f": "\f",
                    "n": "\n",
                    "r": "\r",
                    "t": "\t",
                    "'": "'",
                    "\\": "\\",
                }
                if escape not in escapes:
                    raise UpdatePlanError(
                        f"The update plan path {path!r} has an invalid escape "
                        f"\\{escape}."
                    )
                characters.append(escapes[escape])

            if position >= len(path) or path[position] != "'":
                raise UpdatePlanError(
                    f"The update plan path {path!r} has an unterminated name."
                )
            position += 1
            if position >= len(path) or path[position] != "]":
                raise UpdatePlanError(
                    f"The update plan path {path!r} is missing a closing ']'."
                )
            position += 1
            segments.append(_field("".join(characters)))
            continue

        start = position
        while position < len(path) and path[position].isdigit():
            position += 1
        digits = path[start:position]
        if not digits:
            raise UpdatePlanError(
                f"The update plan path {path!r} is missing a selector "
                f"near position {start}."
            )
        if len(digits) > 1 and digits[0] == "0":
            raise UpdatePlanError(
                f"The update plan path {path!r} has a non-normalized index "
                f"{digits!r}."
            )
        if position >= len(path) or path[position] != "]":
            raise UpdatePlanError(
                f"The update plan path {path!r} is missing a closing ']'."
            )
        position += 1
        segments.append(_index(int(digits)))

    return tuple(segments)


def _flatten_path(node: typing.Any, segments: list[_Segment]) -> None:
    """Convert the path stored on a datum into normalized path segments."""

    # Local imports avoid a circular import at module load time.
    from jsonpath_ng.jsonpath import Child
    from jsonpath_ng.jsonpath import Fields
    from jsonpath_ng.jsonpath import Index
    from jsonpath_ng.jsonpath import Root
    from jsonpath_ng.jsonpath import This

    if isinstance(node, (Root, This)):
        return
    if isinstance(node, Child):
        _flatten_path(node.left, segments)
        _flatten_path(node.right, segments)
        return
    if isinstance(node, Fields):
        if len(node.fields) != 1:
            raise UpdatePlanError(
                "The query matched a location that cannot be identified "
                f"exactly (fields {node.fields!r})."
            )
        field = node.fields[0]
        if not isinstance(field, str):
            raise UpdatePlanError(
                "The query matched an object member whose name is not a string "
                f"({field!r}); JSON member names are strings."
            )
        segments.append(_field(field))
        return
    if isinstance(node, Index):
        if len(node.indices) != 1:
            raise UpdatePlanError(
                "The query matched a location that cannot be identified "
                f"exactly (indices {node.indices!r})."
            )
        index: typing.Any = node.indices[0]
        if isinstance(index, bool) or not isinstance(index, int):
            raise UpdatePlanError(
                f"The query matched an array index that is not an integer "
                f"({index!r})."
            )
        segments.append(_index(index))
        return
    raise UpdatePlanError(
        "The query matched a location that cannot be represented "
        f"as a normalized path ({node!r})."
    )


def _location_of(datum: DatumInContext, data: typing.Any) -> _Location:
    """Return the location of *datum* within *data*."""

    from jsonpath_ng.jsonpath import AutoIdForDatum

    if isinstance(datum, AutoIdForDatum):
        raise UpdatePlanError(
            "The query matched an automatically generated id, "
            "which is not a location in the document."
        )

    segments: list[_Segment] = []
    _flatten_path(datum.full_path, segments)

    if not segments and datum.value is not data:
        raise UpdatePlanError(
            "The query matched a value that was computed rather than found "
            "in the document; it cannot be represented as a location."
        )
    return tuple(segments)


def _resolve(
    data: typing.Any,
    location: _Location,
    path: str | None = None,
) -> tuple[typing.Any, typing.Any, _Location, typing.Any]:
    """Resolve *location* in *data*.

    Returns ``(parent, key, normalized_location, value)``. The normalized
    location replaces negative array indices with their actual positions.

    Raises `UpdatePlanStaleError` when *path* is given (during application),
    and `UpdatePlanError` otherwise (during preview).
    """

    def fail(reason: str) -> typing.NoReturn:
        if path is None:
            raise UpdatePlanError(f"Cannot resolve the matched location: {reason}")
        raise UpdatePlanStaleError(
            f"The update plan is stale: {path} {reason}.", path=path
        )

    current = data
    parent: typing.Any = None
    key: typing.Any = None
    normalized: list[_Segment] = []

    for segment in location:
        if segment.kind == "field":
            name = typing.cast(str, segment.key)
            if isinstance(current, (list, tuple)):
                fail("no longer selects an object (found an array)")
            try:
                next_value = current[name]
            except (KeyError, IndexError, TypeError):
                fail(f"no longer exists (missing member {name!r})")
            parent, key = current, name
            current = next_value
            normalized.append(_field(name))
            continue

        index = typing.cast(int, segment.key)
        if not isinstance(current, list):
            fail("no longer selects an array")
        if index < 0:
            index += len(current)
        if not 0 <= index < len(current):
            fail(f"no longer exists (index {index} is out of range)")
        parent, key = current, index
        current = current[index]
        normalized.append(_index(index))

    return parent, key, tuple(normalized), current


def _json_equal(left: typing.Any, right: typing.Any) -> bool:
    """Compare two JSON values, keeping booleans distinct from numbers."""

    pairs = [(left, right)]
    while pairs:
        left, right = pairs.pop()
        if isinstance(left, bool) or isinstance(right, bool):
            if left is not right:
                return False
        elif isinstance(left, Mapping) and isinstance(right, Mapping):
            if left.keys() != right.keys():
                return False
            pairs.extend((value, right[key]) for key, value in left.items())
        elif isinstance(left, Sequence) and isinstance(right, Sequence):
            if isinstance(left, (str, bytes)) or isinstance(right, (str, bytes)):
                if left != right:
                    return False
                continue
            if len(left) != len(right):
                return False
            pairs.extend(zip(left, right))
        elif isinstance(left, (Mapping, Sequence)) or isinstance(
            right, (Mapping, Sequence)
        ):
            return False
        elif left != right:
            return False
    return True


def _conflicts(
    locations: Sequence[_Location],
) -> None:
    """Raise if any location is an ancestor of another location."""

    if len(locations) < 2:
        return

    known = set(locations)
    for location in locations:
        # Start at 0 so that the root, which is an ancestor of every location,
        # is detected as well.
        for length in range(len(location)):
            ancestor = location[:length]
            if ancestor in known:
                raise UpdatePlanConflictError(
                    "The update plan has a conflict: "
                    f"{_format_path(ancestor)} and {_format_path(location)} "
                    "both match, but one contains the other. "
                    "Updating both could silently overwrite one of them.",
                    paths=(_format_path(ancestor), _format_path(location)),
                )


def _consumes_list(datum: DatumInContext) -> bool:
    """Whether `update()` would consume a list value one element at a time.

    `Index.update()` assigns successive elements of a list value,
    while `Fields.update()` assigns the whole list to each member.
    The leaf of a match's path identifies which applied.
    """

    from jsonpath_ng.jsonpath import Index

    return isinstance(datum.path, Index)


def build_plan(expression: JSONPath, data: typing.Any, val: typing.Any) -> UpdatePlan:
    """Create a JSON-compatible update plan for *expression* against *data*.

    *data* itself is never modified.
    """

    matches: typing.Any = expression.find(data)
    if not isinstance(matches, list):
        # Some ``find()`` implementations return a bare datum.
        matches = [matches]

    # Resolve every location against the document. This both validates that
    # the match can be replayed directly, and normalizes negative indices.
    # Duplicate locations keep their first occurrence.
    resolved: list[tuple[_Location, typing.Any, bool]] = []
    seen: set[_Location] = set()
    for datum in matches:
        location = _location_of(datum, data)
        _, _, normalized, value = _resolve(data, location)
        if normalized in seen:
            continue
        seen.add(normalized)
        resolved.append((normalized, value, _consumes_list(datum)))

    unique_locations = [item[0] for item in resolved]
    _conflicts(unique_locations)

    # A callback sees the document as it will be when all earlier entries in
    # the plan have been applied, just as `update()` updates in place.
    work = copy.deepcopy(data) if callable(val) else None
    list_values = isinstance(val, list)

    entries: list[UpdatePlanEntry] = []
    value_index = 0
    for location, old_value, consumes_list in resolved:
        if callable(val):
            assert work is not None
            if not location:
                raise UpdatePlanError(
                    "An update callback cannot replace the root of the document, "
                    "because the result could not be stored in a JSON plan."
                )
            parent, key, _, value = _resolve(work, location)
            result = val(value, parent, key)
            if result is not None:
                new_value = result
                parent[key] = new_value
            else:
                # The callback may have updated the parent in place.
                new_value = parent[key]
        elif list_values and consumes_list:
            # Match `Index.update()`, which assigns one element per match.
            if value_index >= len(val):
                raise UpdatePlanError(
                    "The update value is a list with "
                    f"{len(val)} elements, but the query matched more than "
                    f"{len(val)} array positions. `update()` requires one "
                    "value per match."
                )
            new_value = val[value_index]
            value_index += 1
        else:
            new_value = val
        entries.append(
            {
                "path": _format_path(location),
                "old_value": copy.deepcopy(old_value),
                "new_value": copy.deepcopy(new_value),
            }
        )

    if list_values and value_index not in (0, len(val)):
        raise UpdatePlanError(
            "The update value is a list with "
            f"{len(val)} elements, but the query matched {value_index} "
            "array positions. `update()` would silently ignore the extra "
            "elements; supply exactly as many values as there are matches."
        )

    return entries


def _plan_entries(plan: typing.Any) -> list[typing.Mapping[str, typing.Any]]:
    """Extract and validate the entries of an update plan."""

    if isinstance(plan, (str, bytes, bytearray)):
        try:
            plan = json.loads(plan)
        except (TypeError, ValueError) as exc:
            raise UpdatePlanError("The update plan is not valid JSON.") from exc

    if isinstance(plan, Mapping):
        for name in ("updates", "entries", "changes"):
            if name in plan:
                entries = plan[name]
                break
        else:
            raise UpdatePlanError(
                "The update plan has no 'updates' list, so there is nothing "
                "to apply."
            )
    elif isinstance(plan, (list, tuple)):
        # A plan is an ordered list of entries, so a bare list is also valid.
        entries = plan
    else:
        raise UpdatePlanError(
            "An update plan must be a mapping or a list of update entries."
        )

    if not isinstance(entries, (list, tuple)):
        raise UpdatePlanError("The update plan's list of updates is invalid.")

    for entry in entries:
        if not isinstance(entry, Mapping):
            raise UpdatePlanError(
                "Every update plan entry must be a mapping with 'path', "
                "'old_value', and 'new_value' keys."
            )
    return typing.cast(list[typing.Mapping[str, typing.Any]], list(entries))


# Accepted spellings for the values in a plan entry. `plan_update()` emits
# the first name in each list; the others make hand-written plans convenient
# without changing the meaning of a plan.
_OLD_KEYS = ("old_value", "old", "original", "before")
_NEW_KEYS = ("new_value", "new", "updated", "after")


def _entry_value(
    entry: typing.Mapping[str, typing.Any], names: Sequence[str]
) -> typing.Any:
    for name in names:
        if name in entry:
            return entry[name]
    raise UpdatePlanError(
        "An update plan entry is missing one of the required keys: "
        + ", ".join(repr(name) for name in names)
        + "."
    )


def _plan_location(
    entry: typing.Mapping[str, typing.Any], data: typing.Any
) -> tuple[_Location, str]:
    """Return the location that *entry* selects in *data*."""

    path = _entry_value(entry, ("path",))
    if not isinstance(path, str):
        raise UpdatePlanError(f"The update plan path {path!r} is not a string.")

    try:
        return _parse_path(path), path
    except UpdatePlanError as error:
        normalized_error = error

    # Accept any other JSONPath spelling, including the paths that
    # ``str(datum.full_path)`` produces. It is resolved against the document
    # that is about to be updated, so it must identify exactly one location.
    # A plan that this library produced only ever uses normalized paths.
    from jsonpath_ng.exceptions import JsonPathParserError
    from jsonpath_ng.parser import parse

    try:
        matches: typing.Any = parse(path).find(data)
    except (JsonPathParserError, ValueError) as exc:
        raise UpdatePlanError(
            f"The update plan path {path!r} is not a valid JSONPath: "
            f"{normalized_error}"
        ) from exc

    if not isinstance(matches, list):
        matches = [matches]
    if len(matches) != 1:
        raise UpdatePlanError(
            f"The update plan path {path!r} must identify exactly one "
            f"location, but it matched {len(matches)}."
        )
    return _location_of(matches[0], data), path


def apply_plan(expression: JSONPath, data: typing.Any, plan: typing.Any) -> typing.Any:
    """Apply *plan* to *data* if every recorded location is unchanged.

    If any location is missing or holds a different value, no location is
    written and `UpdatePlanStaleError` is raised.
    """

    entries = _plan_entries(plan)

    prepared: list[tuple[_Location, typing.Any, str]] = []
    for entry in entries:
        location, path = _plan_location(entry, data)
        prepared.append((location, entry, path))

    # Reject overlapping locations before touching the document, so a
    # hand-written plan cannot make one entry silently replace another.
    unique: list[_Location] = []
    unique_entries: list[tuple[_Location, typing.Any, str]] = []
    seen: dict[_Location, typing.Any] = {}
    for location, entry, path in prepared:
        old_value = _entry_value(entry, _OLD_KEYS)
        new_value = _entry_value(entry, _NEW_KEYS)
        if location in seen:
            previous = seen[location]
            if not (
                _json_equal(_entry_value(previous, _OLD_KEYS), old_value)
                and _json_equal(_entry_value(previous, _NEW_KEYS), new_value)
            ):
                raise UpdatePlanConflictError(
                    "The update plan has a conflict: "
                    f"{path} appears more than once with different values. "
                    "Updating both could silently overwrite one of them.",
                    paths=(path, path),
                )
            continue
        seen[location] = entry
        unique.append(location)
        unique_entries.append((location, entry, path))

    _conflicts(unique)

    # Validate every entry against the document before writing anything.
    writes: list[tuple[typing.Any, typing.Any, typing.Any]] = []
    root_value: typing.Any = _UNSET
    for location, entry, path in unique_entries:
        old_value = _entry_value(entry, _OLD_KEYS)
        new_value = _entry_value(entry, _NEW_KEYS)

        parent, key, _, current = _resolve(data, location, path=path)
        if not _json_equal(current, old_value):
            raise UpdatePlanStaleError(
                f"The update plan is stale: the value at {path} has changed.",
                path=path,
                expected=old_value,
                actual=current,
            )

        if parent is None:
            root_value = copy.deepcopy(new_value)
        elif not isinstance(parent, (list, MutableMapping)):
            # The location exists but cannot be written, for example because
            # the document uses tuples instead of lists.
            raise UpdatePlanError(
                f"The update plan cannot write to {path}: "
                f"its parent ({type(parent).__name__}) is not mutable."
            )
        else:
            writes.append((parent, key, copy.deepcopy(new_value)))

    if root_value is not _UNSET:
        # A root replacement cannot coexist with another location because
        # the root is an ancestor of every location (checked above).
        return root_value

    # The plan has been fully validated; now write it. Keep enough state to
    # restore the document if a mapping-like object rejects a write.
    undo: list[tuple[typing.Any, typing.Any, typing.Any]] = []
    try:
        for parent, key, new_value in writes:
            undo.append((parent, key, parent[key]))
            parent[key] = new_value
    except Exception:
        for parent, key, old_value in reversed(undo):
            parent[key] = old_value
        raise

    return data


# A sentinel that distinguishes "the plan does not replace the root" from a
# legitimate replacement value of ``None`` or any other JSON value.
_UNSET = object()
