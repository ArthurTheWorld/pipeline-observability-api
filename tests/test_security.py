import json
import subprocess
import sys

import pytest

from obs_api.config import ApiKey, Settings
from obs_api.security import KEY_PREFIX, Principal, RateLimiter, authenticate, generate_key, hash_key


def settings_with(*pairs):
    return Settings(api_keys={n: ApiKey(n, hash_key(k), r) for n, k, r in pairs})


def test_generated_keys_are_long_prefixed_and_unique():
    keys = {generate_key() for _ in range(50)}
    assert len(keys) == 50 and all(k.startswith(KEY_PREFIX) and len(k) >= 40 for k in keys)


def test_only_the_hash_matters_never_the_plain_key():
    key = generate_key()
    assert len(hash_key(key)) == 64 and key not in hash_key(key)


def test_authenticate_returns_the_right_principal():
    s = settings_with(("a", "key-a", "viewer"), ("b", "key-b", "admin"))
    assert authenticate(s, "key-b") == Principal("b", "admin")
    assert authenticate(s, "key-a") == Principal("a", "viewer")


@pytest.mark.parametrize("presented", [None, "", "wrong", "key-a ", " key-a", "KEY-A", "x" * 500])
def test_wrong_or_malformed_keys_are_rejected(presented):
    assert authenticate(settings_with(("a", "key-a", "viewer")), presented) is None


def test_roles_are_ordered():
    viewer, admin = Principal("v", "viewer"), Principal("a", "admin")
    assert viewer.allows("viewer") and not viewer.allows("admin")
    assert admin.allows("viewer") and admin.allows("admin")


def test_rate_limiter_blocks_after_the_limit_and_recovers():
    now = [0.0]
    limiter = RateLimiter(3, clock=lambda: now[0])
    assert [limiter.check("k") for _ in range(3)] == [None, None, None]
    wait = limiter.check("k")
    assert wait is not None and 1 <= wait <= 61
    now[0] = 61.0
    assert limiter.check("k") is None


def test_rate_limit_is_per_identity():
    limiter = RateLimiter(1, clock=lambda: 0.0)
    assert limiter.check("a") is None and limiter.check("a") is not None
    assert limiter.check("b") is None


def test_cli_new_key_prints_a_key_whose_hash_is_in_the_snippet():
    out = subprocess.run(
        [sys.executable, "-m", "obs_api", "new-key", "--name", "grafana", "--role", "viewer"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    key = next(line.strip() for line in out.splitlines() if line.strip().startswith(KEY_PREFIX))
    snippet = json.loads(out.strip().splitlines()[-1])
    assert snippet["grafana"] == {"sha256": hash_key(key), "role": "viewer"}
    assert key not in json.dumps(snippet)  # o trecho de configuração NÃO contém a chave


def test_cli_rejects_an_invalid_role():
    r = subprocess.run(
        [sys.executable, "-m", "obs_api", "new-key", "--name", "x", "--role", "root"], capture_output=True, text=True
    )
    assert r.returncode != 0
