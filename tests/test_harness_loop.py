"""Testes do loop de harness com modelos falsos (sem rede, sem chave de API)."""

import json
import re

import pytest

from src.harness_loop.editor import build_prompt, parse_edits
from src.harness_loop.exam import Case, by_split, report, split_for
from src.harness_loop.harness import Edit, EditRejected, Harness, apply_edit
from src.harness_loop.loop import better, run_loop
from src.harness_loop.models import BudgetExceeded, CallCounter, FunctionModel
from src.harness_loop.runner import credit, evaluate, parse_prediction
from src.harness_loop.taxonomy import Taxonomy

TAX = Taxonomy.load()


def l3_under(l1: str) -> str:
    return sorted(c for c in TAX.labels if c.startswith(l1 + ".") and len(c) == 8)[0]


SAUDE, AMBIENTE, EDUCACAO, DEFAULT = l3_under("03"), l3_under("05"), l3_under("02"), l3_under("20")
WORDS = {"vacina": SAUDE, "floresta": AMBIENTE, "escola": EDUCACAO}


def make_cases():
    """Ids escolhidos para cair 4 de cada tema em cada lado."""
    cases, want = [], {(w, s): 4 for w in WORDS for s in ("tarefa", "heldout")}
    i = 0
    while any(want.values()):
        i += 1
        for w, code in WORDS.items():
            cid = f"{w}-{i}"
            s = split_for(cid)
            if want[(w, s)]:
                want[(w, s)] -= 1
                cases.append(Case(id=cid, title=f"Notícia {i} sobre {w} no município {i}",
                                  content=f"Texto sobre {w}.", expected=code, status="humano"))
    return cases


def harness0():
    return Harness(instrucoes="Classifique a notícia.", contrato='{"temas": ["XX.YY.ZZ"]}', regras=[], exemplos=[])


def fake_big(cases):
    expected = {c.title: c.expected for c in cases}
    broken = sorted(c.title for c in cases if c.split == "heldout")[0]

    def fn(prompt: str) -> str:
        rules = re.findall(r"palavra '(\w+)' indica (\d\d\.\d\d\.\d\d)", prompt)
        title = re.search(r"Título: (.*)", prompt).group(1)
        if "QUEBRA" in prompt.split("## Notícia")[0]:
            # trapaça: acerta tudo, mas quebra o formato em um caso
            return "lixo" if title == broken else json.dumps({"temas": [expected[title]]})
        for word, code in rules:
            if word in title:
                return json.dumps({"temas": [code]})
        return json.dumps({"temas": [DEFAULT]})

    return FunctionModel(fn, "big")


def fake_editor(cases):
    held = [c for c in cases if c.split == "heldout"]

    def fn(prompt: str) -> str:
        edits = [{"tipo": "adicionar_regra", "texto": f"palavra '{w}' indica {c}"} for w, c in WORDS.items()]
        edits += [
            {"tipo": "adicionar_regra", "texto": "QUEBRA"},
            {"tipo": "adicionar_exemplo", "caso_id": held[0].id},
            {"tipo": "adicionar_regra", "texto": f"Se for '{held[1].title}' use {SAUDE}"},
            {"tipo": "remover_regra", "indice": 99},
            {"tipo": "mudar_contrato", "texto": "x"},
        ]
        return json.dumps({"edicoes": edits})

    return FunctionModel(fn, "editor")


# ---------------- unidades ----------------

def test_taxonomy_loads_three_levels():
    assert len([c for c in TAX.labels if len(c) == 2]) == 25
    assert TAX.exists(SAUDE) and not TAX.exists("99.99.99")


@pytest.mark.parametrize("exp,pred,want", [
    ("05.02.01", "05.02.01", 1.0),
    ("05.02.01", "05.02.07", 2 / 3),
    ("05.02.01", "05.01.01", 1 / 3),
    ("05.02.01", "03.01.01", 0.0),
    ("05", "05.03.01", 1.0),
    ("05", None, 0.0),
])
def test_hierarchical_credit(exp, pred, want):
    assert credit(exp, pred) == pytest.approx(want)


def test_parse_prediction_enforces_contract():
    assert parse_prediction(json.dumps({"temas": [SAUDE]}), TAX) == (SAUDE, True, "")
    assert parse_prediction(json.dumps({"temas": [{"codigo": SAUDE}]}), TAX)[1]
    assert not parse_prediction("não sei", TAX)[1]
    assert not parse_prediction(json.dumps({"temas": ["03"]}), TAX)[1]  # nível 1 não vale
    assert not parse_prediction(json.dumps({"temas": ["99.99.99"]}), TAX)[1]


def test_split_is_stable():
    assert split_for("abc") == split_for("abc")
    assert {split_for(str(i)) for i in range(50)} == {"tarefa", "heldout"}


def test_examples_only_from_task_side_and_no_leaks():
    cases = make_cases()
    task = {c.id: c for c in by_split(cases, "tarefa")}
    held = by_split(cases, "heldout")
    h = harness0()
    with pytest.raises(EditRejected):
        apply_edit(h, Edit("adicionar_exemplo", caso_id=held[0].id), task, [c.title for c in held])
    with pytest.raises(EditRejected):
        apply_edit(h, Edit("adicionar_regra", texto=f"Ver '{held[0].title}'"), task, [c.title for c in held])
    with pytest.raises(EditRejected):
        apply_edit(h, Edit("mudar_contrato", texto="livre"), task, [])
    n = apply_edit(h, Edit("adicionar_exemplo", caso_id=next(iter(task))), task, [])
    assert len(n.exemplos) == 1 and h.exemplos == []  # o original não muda


def test_editor_parser_ignores_garbage():
    raw = '{"edicoes": [{"tipo": "adicionar_regra", "texto": "a"}, {"tipo": "xpto"}, "lixo", {"tipo": "remover_regra", "indice": "2"}]}'
    edits = parse_edits(raw)
    assert [e.tipo for e in edits] == ["adicionar_regra", "remover_regra"]
    assert edits[1].indice == 2


def test_editor_never_sees_heldout():
    cases = make_cases()
    big = fake_big(cases)
    task = by_split(cases, "tarefa")
    res = evaluate(harness0(), big, TAX, task, workers=1)
    prompt = build_prompt(harness0(), res.failures, {c.id: c for c in task}, TAX, 8)
    for c in by_split(cases, "heldout"):
        assert c.title not in prompt


def test_exam_report_blocks_small_exams():
    rep = report(make_cases()[:6], TAX)
    assert rep.blocking


def test_better_uses_task_only_as_tiebreak():
    assert better(0.6, 0.1, 0.5, 0.9)
    assert better(0.5, 0.6, 0.5, 0.5)
    assert not better(0.5, 0.5, 0.5, 0.5)
    assert not better(0.4, 1.0, 0.5, 0.0)


def test_min_gain_ignores_noise():
    assert not better(0.51, 0.9, 0.50, 0.5, min_gain=0.02)
    assert better(0.53, 0.1, 0.50, 0.5, min_gain=0.02)


def test_network_errors_are_not_scored_as_model_errors():
    from src.harness_loop.runner import IncompleteEval

    def boom(prompt):
        raise ConnectionError("429 rate limit")

    with pytest.raises(IncompleteEval):
        evaluate(harness0(), FunctionModel(boom), TAX, make_cases()[:3], workers=1)


def test_budget_stops_calls():
    counter = CallCounter(limit=1)
    counter.tick(cached=False)
    with pytest.raises(BudgetExceeded):
        counter.tick(cached=False)


# ---------------- o loop inteiro ----------------

def test_loop_climbs_and_rejects_unsafe_edits(tmp_path):
    cases = make_cases()
    res = run_loop(harness0(), cases, fake_big(cases), fake_editor(cases), TAX,
                   rounds=5, n_candidates=8, workers=1, out_dir=str(tmp_path))

    assert res.v0_heldout == 0.0
    assert res.rounds[-1].heldout == pytest.approx(1.0)
    # sobe uma regra por rodada
    assert [round(r.heldout, 2) for r in res.rounds[:3]] == [0.33, 0.67, 1.0]
    assert "QUEBRA" not in res.final.regras
    assert res.final.exemplos == []
    assert res.final.versao == 3

    reasons = " ".join(c.rejected for r in res.rounds for c in r.candidates)
    assert "porteira" in reasons
    assert "lado tarefa" in reasons
    assert "vazamento" in reasons
    assert "fora do intervalo" in reasons
    # "mudar_contrato" nem chega aqui: o parser descarta tipos desconhecidos
    assert all(c.edit.tipo != "mudar_contrato" for r in res.rounds for c in r.candidates)

    for f in ("v0.yaml", "v3.yaml", "final.yaml", "history.jsonl", "report.md"):
        assert (tmp_path / f).exists()
    assert Harness.load(str(tmp_path / "final.yaml")).regras == res.final.regras
