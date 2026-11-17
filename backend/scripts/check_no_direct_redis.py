#!/usr/bin/env python3
"""Fail when application code adds a direct use of Redis.

Onyx can run with PostgreSQL as its cache (`CACHE_BACKEND=postgres`), so cache
and short-lived state must go through `CacheBackend`. This check counts the
direct uses of Redis in each file and compares the counts with a committed
baseline. A count that rises, or a file that is not in the baseline, fails.

A direct use is one of:
  * an import of the `redis` package, including its types and exceptions
  * an import or a call of one of this repo's Redis client accessors
  * a Lua script call: `register_script`, `script_load`, `evalsha`, `eval`

Usage, from the repo root:
    python3 backend/scripts/check_no_direct_redis.py                  # check the tree
    python3 backend/scripts/check_no_direct_redis.py --list           # print each use
    python3 backend/scripts/check_no_direct_redis.py --write-baseline # after a migration
"""

import argparse
import ast
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

ALLOW_MARKER = "no-direct-redis: allow"
_BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = _BACKEND_DIR.parent
BASELINE_PATH = _BACKEND_DIR / "scripts" / "no_direct_redis_baseline.txt"

# Application code. Tests and operator scripts may talk to Redis.
SCANNED_DIRS = (
    "backend/onyx",
    "backend/ee",
    "backend/model_server",
    "backend/shared_configs",
)

# Path prefixes where Redis code is correct, each with its reason.
ALLOWED_LOCATIONS = {
    # The Redis client, the connection pool and the Redis-backed objects.
    "backend/onyx/redis/": "the Redis package",
    # The CacheBackend interface and its Redis implementation.
    "backend/onyx/cache/": "the CacheBackend implementations",
    # Reads the Celery broker queues. The broker is not cache state.
    "backend/onyx/background/celery/celery_redis.py": "Celery broker access",
}

# Names in `onyx.redis` that hand out a Redis client or a Redis-only lock.
_ONYX_REDIS_MODULE = "onyx.redis"
_CLIENT_ACCESSORS = frozenset(
    {
        "get_redis_client",
        "get_redis_replica_client",
        "get_shared_redis_client",
        "get_shared_redis_replica_client",
        "get_raw_redis_client",
        "get_raw_redis_replica_client",
        "get_async_redis_connection",
        "redis_pool",
        "RedisPool",
        "TenantRedisClient",
        "TenantRedisPipeline",
        "redis_shared_lock",
    }
)
_LUA_METHODS = frozenset({"register_script", "script_load", "evalsha"})
# `eval(script, numkeys, ...)` in redis-py, `eval(script, keys=, args=)` on
# TenantRedisClient. Any other call shape is another library's `eval`.
_LUA_EVAL_MIN_ARGS = 2
_LUA_EVAL_KEYWORDS = frozenset({"keys", "args", "numkeys"})
_SOURCE_TOKENS = ("redis", "eval", "script")

_BASELINE_HEADER = """\
# Direct uses of Redis in application code, as `<count> <path>` lines.
# The rule is in backend/AGENTS.md ("Key Rules"): use CacheBackend.
# Do not edit by hand, and do not raise a count. After you move code to
# CacheBackend, lower the counts with:
#   python3 backend/scripts/check_no_direct_redis.py --write-baseline
"""

_FIX_MESSAGE = f"""\
Use `get_cache_backend()` / `get_shared_cache_backend()` from
`backend/onyx/cache/factory.py`. If `CacheBackend` does not have the operation,
add the operation to `CacheBackend` with both Redis and PostgreSQL implementations.
Direct Redis code breaks deployments that run with `CACHE_BACKEND=postgres`.
A deliberate exception carries `# {ALLOW_MARKER} <reason>` on the line."""


class Use(NamedTuple):
    line: int
    what: str


def _is_redis_package(module: str) -> bool:
    return module == "redis" or module.startswith("redis.")


def _is_onyx_redis(module: str) -> bool:
    return module == _ONYX_REDIS_MODULE or module.startswith(_ONYX_REDIS_MODULE + ".")


def _is_lua_eval(call: ast.Call) -> bool:
    return len(call.args) >= _LUA_EVAL_MIN_ARGS or any(
        keyword.arg in _LUA_EVAL_KEYWORDS for keyword in call.keywords
    )


def _imports(tree: ast.AST) -> Iterator[tuple[int, str, str | None, str]]:
    """Each imported name as (line, module, name in module, local name)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                yield alias.lineno, alias.name, None, local
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                yield alias.lineno, node.module, alias.name, alias.asname or alias.name


def find_direct_redis_uses(source: str) -> list[Use]:
    """Direct uses of Redis in one file, without the lines that carry the marker."""
    tree = ast.parse(source)
    lines = source.splitlines()
    uses: list[Use] = []
    # Local names bound to an accessor, so an `as` alias is still counted.
    accessor_locals: dict[str, str] = {}

    for line, module, name, local in _imports(tree):
        if _is_redis_package(module):
            imported = f"{module}.{name}" if name else module
            uses.append(Use(line, f"import of `{imported}`"))
        elif _is_onyx_redis(module) and name in _CLIENT_ACCESSORS:
            accessor_locals[local] = name
            uses.append(Use(line, f"import of `{name}`"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in accessor_locals:
                uses.append(Use(node.lineno, f"call of `{accessor_locals[func.id]}`"))
            continue
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr in _CLIENT_ACCESSORS:
            uses.append(Use(node.lineno, f"call of `{func.attr}`"))
        elif func.attr in _LUA_METHODS or (func.attr == "eval" and _is_lua_eval(node)):
            uses.append(Use(node.lineno, f"Lua script call `{func.attr}`"))

    return sorted(use for use in uses if ALLOW_MARKER not in lines[use.line - 1])


def allowed_reason(relative_path: str) -> str | None:
    for prefix, reason in ALLOWED_LOCATIONS.items():
        if relative_path.startswith(prefix):
            return reason
    return None


def scan_tree(repo_root: Path) -> dict[str, list[Use]]:
    """Direct uses by repo-relative path, for every file that has one."""
    found: dict[str, list[Use]] = {}
    for scanned in SCANNED_DIRS:
        for path in sorted((repo_root / scanned).rglob("*.py")):
            relative = path.relative_to(repo_root).as_posix()
            if allowed_reason(relative):
                continue
            source = path.read_text()
            # Every direct use contains one of these, so skip the parse without them.
            if not any(token in source for token in _SOURCE_TOKENS):
                continue
            uses = find_direct_redis_uses(source)
            if uses:
                found[relative] = uses
    return found


def read_baseline(path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        count, relative = line.split(maxsplit=1)
        counts[relative] = int(count)
    return counts


def format_baseline(found: dict[str, list[Use]]) -> str:
    rows = [f"{len(uses)} {relative}" for relative, uses in sorted(found.items())]
    return _BASELINE_HEADER + "\n".join(rows) + "\n"


def compare(
    found: dict[str, list[Use]], baseline: dict[str, int]
) -> tuple[list[str], list[str]]:
    """Failures (a count above the baseline) and stale baseline rows (below it)."""
    failures: list[str] = []
    stale: list[str] = []
    for relative, uses in sorted(found.items()):
        allowed = baseline.get(relative, 0)
        if len(uses) > allowed:
            failures.append(
                f"{relative}: {len(uses)} direct Redis uses, baseline allows {allowed}"
            )
            failures.extend(f"  {relative}:{use.line}: {use.what}" for use in uses)
    for relative, allowed in sorted(baseline.items()):
        current = len(found.get(relative, []))
        if current < allowed:
            stale.append(f"{relative}: baseline {allowed}, now {current}")
    return failures, stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--write-baseline",
        action="store_true",
        help="record the current counts as the baseline",
    )
    group.add_argument(
        "--list", action="store_true", help="print each direct use and exit"
    )
    args = parser.parse_args(argv)

    found = scan_tree(REPO_ROOT)
    if args.write_baseline:
        BASELINE_PATH.write_text(format_baseline(found))
        total = sum(len(uses) for uses in found.values())
        print(f"Wrote {total} direct Redis uses in {len(found)} files.")
        return 0
    if args.list:
        for relative, uses in found.items():
            for use in uses:
                print(f"{relative}:{use.line}: {use.what}")
        return 0

    failures, stale = compare(found, read_baseline(BASELINE_PATH))
    if failures:
        print("New direct use of Redis:")
        print("\n".join(failures))
        print(_FIX_MESSAGE)
        return 1
    if stale:
        print("The direct Redis baseline is out of date. Lower it with:")
        print("  python3 backend/scripts/check_no_direct_redis.py --write-baseline")
        print("\n".join(f"  {row}" for row in stale))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
