"""The HTTP surface (Lane D).

`api/` contains **no business logic**. Routes marshal, validate, persist and
paginate; every number they return was computed by `core/` or `scorer/`. If
something here starts computing a net, a fee or a rate, it belongs in `core/`.

The dependency arrow points one way: `api/` imports `core/`, never the reverse.
`tests/test_boundaries.py::test_core_has_no_web_dependency` enforces the other
direction, `core/store/` included.
"""
