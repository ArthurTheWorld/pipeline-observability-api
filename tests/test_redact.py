import pytest

from obs_api.redact import contains_pii, redact


@pytest.mark.parametrize(
    "text, token",
    [
        ("duplicate key value 123.456.789-09 violates", "[CPF]"),
        ("duplicate key value 12345678909 violates", "[CPF]"),
        ("cliente 11.222.333/0001-81 não encontrado", "[CNPJ]"),
        ("cnpj 11222333000181 inválido", "[CNPJ]"),
        ("falha ao enviar para ana.souza+teste@empresa.com.br", "[EMAIL]"),
        ("ligar para (61) 99876-5432 hoje", "[TELEFONE]"),
        ("ligar para +55 61 3456-7890", "[TELEFONE]"),
        ("arquivo /home/maria/projetos/dbt/models/x.sql falhou", "[CAMINHO]"),
    ],
)
def test_redacts_each_kind_of_personal_data(text, token):
    out = redact(text)
    assert token in out
    for leaked in (
        "123.456.789-09",
        "12345678909",
        "11.222.333/0001-81",
        "11222333000181",
        "ana.souza",
        "99876",
        "3456-7890",
        "maria",
    ):
        assert leaked not in out


def test_cnpj_is_not_half_eaten_by_the_cpf_rule():
    assert redact("doc 11.222.333/0001-81") == "doc [CNPJ]"


def test_redacts_several_occurrences_in_one_message():
    out = redact("a@b.com e c@d.org, cpf 111.222.333-96 e 000.111.222-33")
    assert out == "[EMAIL] e [EMAIL], cpf [CPF] e [CPF]"


@pytest.mark.parametrize(
    "text",
    [
        "Got 1 result, configured to fail if != 0",
        "Catalog Error: Table with name orders does not exist!",
        "Database Error in model fct_orders (models/fct_orders.sql)",
        "elapsed 12.34s at 2026-10-29T10:00:00",
        "relation main.stg_orders",
    ],
)
def test_ordinary_dbt_messages_are_left_alone(text):
    assert redact(text) == text and not contains_pii(text)


def test_whitespace_is_normalised_and_none_stays_none():
    assert redact("a   b\n\n c\t d") == "a b c d"
    assert redact(None) is None and redact("") == ""


def test_truncates_after_redacting_so_a_cut_cpf_cannot_escape():
    text = "x" * 295 + " 123.456.789-09 fim"
    out = redact(text, max_chars=300)
    assert len(out) <= 300
    assert not any(ch.isdigit() for ch in out)  # nem um dígito do CPF sobrevive ao corte


def test_pii_beyond_the_truncation_point_is_never_returned():
    out = redact("ok " + "y" * 400 + " 123.456.789-09", max_chars=50)
    assert len(out) <= 50 and "789" not in out


def test_output_is_idempotent():
    once = redact("cpf 123.456.789-09 mail a@b.com")
    assert redact(once) == once


def test_huge_input_is_bounded():
    assert len(redact("a" * 10_000_000)) <= 300


@pytest.mark.parametrize("bad", [0, -1, 2001])
def test_rejects_out_of_range_limits(bad):
    with pytest.raises(ValueError):
        redact("x", max_chars=bad)


def test_contains_pii_detects_each_rule():
    assert contains_pii("x 123.456.789-09") and contains_pii("a@b.com") and contains_pii("/a/b/c/d")
    assert not contains_pii(None) and not contains_pii("limpo")
