"""Tests for the per-client token bucket; the clock is always injected."""
from denoise_service.limits import TokenBucket


def test_bucket_allows_burst_then_refuses():
    b = TokenBucket(rate_per_minute=120, burst=3)
    assert [b.allow("a", 0.0)[0] for _ in range(3)] == [True, True, True]
    ok, wait = b.allow("a", 0.0)
    assert ok is False
    assert wait == 0.5  # one token at 2 tokens/second


def test_bucket_refills_over_time():
    b = TokenBucket(rate_per_minute=60, burst=2)
    b.allow("a", 0.0)
    b.allow("a", 0.0)
    assert b.allow("a", 0.5)[0] is False
    assert b.allow("a", 1.0)[0] is True
    assert b.allow("a", 1.0)[0] is False
    # long idle refills only up to the burst capacity
    assert [b.allow("a", 100.0)[0] for _ in range(3)] == [True, True, False]


def test_bucket_keys_are_independent():
    b = TokenBucket(rate_per_minute=60, burst=1)
    assert b.allow("a", 0.0)[0] is True
    assert b.allow("a", 0.0)[0] is False
    assert b.allow("b", 0.0)[0] is True


def test_bucket_evicts_idle_keys():
    b = TokenBucket(rate_per_minute=60, burst=5)
    for i in range(10000):
        b.allow("client-%d" % i, 0.0)
    assert len(b) > 0
    # one request much later triggers a sweep; every earlier client has long since refilled
    b.allow("late", 10000.0)
    assert len(b) <= 2


def test_eviction_does_not_grant_extra_tokens():
    b = TokenBucket(rate_per_minute=60, burst=2)
    b.allow("a", 0.0)
    b.allow("a", 0.0)
    b.allow("x", 1000.0)  # sweep: "a" is full again, safe to forget
    assert [b.allow("a", 1000.0)[0] for _ in range(3)] == [True, True, False]


def test_bucket_rejects_nonpositive_settings():
    import pytest
    with pytest.raises(ValueError):
        TokenBucket(rate_per_minute=0, burst=1)
    with pytest.raises(ValueError):
        TokenBucket(rate_per_minute=1, burst=0)
