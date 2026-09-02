"""Lane A's tests are a package so `tests/generator/test_cli.py` and the frozen
`tests/test_cli.py` do not collide on module basename under pytest's default
import mode. Without it, collection aborts with an import-file-mismatch error.
"""
