"""Call sites that moved from the Redis client to ``CacheBackend`` keep their
claim semantics on both backends. Each test runs once per backend."""

from uuid import uuid4

import pytest

from ee.onyx.utils import license as license_utils
from onyx.cache import factory as cache_factory
from onyx.cache.interface import CacheBackend
from onyx.utils import audit


def test_audit_dedup_claims_the_window_once(
    cache: CacheBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache_factory, "get_cache_backend", lambda **_: cache)
    key = f"call_site_{uuid4().hex}"

    assert audit.should_emit(key, 60, None) is True
    assert audit.should_emit(key, 60, None) is False
    assert cache.get(f"audit:{key}") == b"1"
    assert 0 < cache.ttl(f"audit:{key}") <= 60


def test_license_claim_cooldown_holds_until_cleared(
    cache: CacheBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(license_utils, "get_cache_backend", lambda: cache)
    license_utils.clear_claim_cooldown()

    try:
        assert license_utils.claim_cooldown_is_active() is False
        assert license_utils.claim_cooldown_is_active() is True
        license_utils.clear_claim_cooldown()
        assert license_utils.claim_cooldown_is_active() is False
    finally:
        license_utils.clear_claim_cooldown()
