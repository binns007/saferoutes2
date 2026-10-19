"""Tiny cache-backed login-failure limiter (per email + client IP)."""
from django.core.cache import cache

MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60


def _key(email, ip):
    return f"login-fail:{email.lower()}:{ip}"


def is_blocked(email, ip):
    return cache.get(_key(email, ip), 0) >= MAX_FAILURES


def record_failure(email, ip):
    key = _key(email, ip)
    cache.add(key, 0, WINDOW_SECONDS)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, WINDOW_SECONDS)


def clear(email, ip):
    cache.delete(_key(email, ip))
