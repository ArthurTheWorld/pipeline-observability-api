"""Autenticação por chave de API, papéis e limite de requisições.

A chave NUNCA é guardada: só o SHA-256 dela. Chaves são geradas com 256 bits de entropia, então um
hash simples (sem sal, sem custo) é adequado; a proteção vem da entropia, não da lentidão do hash
(diferente de senhas escolhidas por pessoas).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass

from .config import ApiKey, Settings

ROLE_RANK = {"viewer": 1, "admin": 2}
KEY_PREFIX = "obs_"


@dataclass(frozen=True)
class Principal:
    name: str
    role: str

    def allows(self, role: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def authenticate(settings: Settings, presented: str | None) -> Principal | None:
    """Compara em tempo constante e percorre TODAS as chaves, sem parar na primeira que casa:
    o tempo de resposta não revela qual (ou se alguma) chave estava quase certa."""
    if not presented or len(presented) > 200:
        return None
    digest = hash_key(presented)
    found: ApiKey | None = None
    for key in settings.api_keys.values():
        if hmac.compare_digest(digest, key.sha256):
            found = key
    return Principal(found.name, found.role) if found else None


class RateLimiter:
    """Janela deslizante de 60 s por identidade (nome da chave, ou o cliente quando anônimo)."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic):
        self.per_minute, self.clock = per_minute, clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, identity: str) -> int | None:
        """None se permitido; senão, segundos até poder tentar de novo."""
        now = self.clock()
        hits = self._hits[identity]
        while hits and now - hits[0] >= 60:
            hits.popleft()
        if len(hits) >= self.per_minute:
            return max(1, int(60 - (now - hits[0])) + 1)
        hits.append(now)
        if len(self._hits) > 10_000:  # evita crescer sem limite sob varredura de IPs
            self._hits = defaultdict(deque, {k: v for k, v in self._hits.items() if v and now - v[-1] < 60})
        return None
