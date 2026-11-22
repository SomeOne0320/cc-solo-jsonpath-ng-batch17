Changed
-----

*   Unify how paths and the queries inside filters are serialized to text.

    Indices, slices, and filters now follow the previous segment directly,
    with no ``.`` between them, both in whole paths and in filter queries:
    ``$.a[0]`` is now written as ``$.a[0]`` instead of ``$.a.[0]``,
    matching how it has always been written inside a filter.
    A bracketed segment on the right side of ``..`` is parenthesized
    so that it is not captured by the ``..`` expression when re-parsed:
    ``$..(a[0])`` is now written as ``($..(a[0]))``.

