"""Redação de dados pessoais (LGPD) em texto livre.

Mensagens de erro de banco e de testes do dbt podem conter VALORES de dados: um CPF numa
violação de unicidade, um e-mail num erro de conversão. Antes de qualquer texto ser armazenado
ou devolvido pela API, ele passa por `redact`. Preferimos redigir de MENOS para MAIS: um número
qualquer que pareça um CPF é substituído mesmo que não seja um (falso positivo é barato; vazamento não).

A ordem importa: CNPJ (14 dígitos) antes de CPF (11) antes de telefone (10-11), senão um padrão
mais curto comeria parte do mais longo.
"""

from __future__ import annotations

import re

_RULES = [
    ("CNPJ", re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\d)")),
    ("CPF", re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)")),
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")),
    ("TELEFONE", re.compile(r"(?<![\d.])(?:\+?55[\s-]?)?\(?\d{2}\)?[\s-]?9?\d{4}[\s-]?\d{4}(?!\d)")),
    # caminho absoluto com 3+ segmentos: vaza estrutura de diretórios e usuário da máquina
    ("CAMINHO", re.compile(r"(?<![\w.])(?:/[\w.\-]+){3,}/?")),
]

MAX_INPUT_CHARS = 5_000  # limita o custo e o tamanho do que é analisado
HARD_MAX_OUTPUT = 2_000  # a redação roda ANTES de truncar: um CPF cortado no meio escaparia


def redact(text: str | None, max_chars: int = 300) -> str | None:
    """Remove dados pessoais, normaliza espaços e trunca. `None` continua `None`."""
    if text is None:
        return None
    if not 1 <= max_chars <= HARD_MAX_OUTPUT:
        raise ValueError(f"max_chars deve estar entre 1 e {HARD_MAX_OUTPUT}")
    cleaned = " ".join(str(text)[:MAX_INPUT_CHARS].split())
    for label, pattern in _RULES:
        cleaned = pattern.sub(f"[{label}]", cleaned)
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 1].rstrip() + "…"
    return cleaned


def contains_pii(text: str | None) -> bool:
    """Verdadeiro se `redact` mudaria algo além de espaços/truncamento (usado em testes e varreduras)."""
    if not text:
        return False
    sample = " ".join(str(text)[:MAX_INPUT_CHARS].split())
    return any(pattern.search(sample) for _, pattern in _RULES)
