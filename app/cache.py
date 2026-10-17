"""Redis access."""
import json
import os

import redis

_client = None


def client():
    global _client
    if _client is None:
        url = os.environ.get("REDIS_URL") or "redis://localhost:6379/0"
        _client = redis.Redis.from_url(url, decode_responses=True)
    return _client


def get_json(key):
    raw = client().get(key)
    return json.loads(raw) if raw else None


def set_json(key, value, ttl=300):
    client().set(key, json.dumps(value, default=str), ex=ttl)


def drop(*keys):
    if keys:
        client().delete(*keys)


def drop_prefix(prefix):
    c = client()
    for k in c.scan_iter(f"{prefix}*"):
        c.delete(k)
