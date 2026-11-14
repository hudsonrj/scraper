"""Roda o modelo congelado com um harness e dá nota em um conjunto de casos."""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Optional

from .exam import Case
from .harness import Harness
from .models import ChatModel
from .taxonomy import Taxonomy, level, prefix

CONTENT_CHARS = 1500
_CODE = re.compile(r"\b\d{2}(?:\.\d{2}){0,2}\b")


@dataclass
class CaseResult:
    case_id: str
    expected: str
    predicted: Optional[str]
    credit: float
    valid: bool
    error: str = ""


class IncompleteEval(RuntimeError):
    """Alguma chamada falhou por rede ou limite de taxa: a nota não é confiável."""


@dataclass
class EvalResult:
    score: float
    l1_accuracy: float
    invalid: int
    results: List[CaseResult] = field(default_factory=list)

    @property
    def failures(self) -> List[CaseResult]:
        return [r for r in self.results if r.credit < 1.0]


def parse_prediction(raw: str, tax: Taxonomy) -> (Optional[str], bool, str):
    """Lê o primeiro tema da resposta. Retorna (código, válido, erro)."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            return None, False, "resposta não é JSON"
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None, False, "resposta não é JSON"
    temas = data.get("temas") if isinstance(data, dict) else None
    if not isinstance(temas, list) or not temas:
        return None, False, "campo 'temas' ausente ou vazio"
    first = temas[0]
    if isinstance(first, dict):
        first = first.get("codigo") or first.get("code") or ""
    m = _CODE.search(str(first))
    code = m.group(0) if m else None
    if not tax.exists(code):
        return code, False, f"código {code!r} não existe na árvore"
    if level(code) != 3:
        return code, False, f"código {code} não é de nível 3"
    return code, True, ""


def credit(expected: str, predicted: Optional[str]) -> float:
    """Crédito hierárquico: acerta até o nível que a prova rotulou.

    Esperado 05.02.01 e previsto 05.02.07 → 2/3. Esperado só "05" → 1.0 se o
    nível 1 bate.
    """
    if not predicted:
        return 0.0
    depth = level(expected)
    hit = 0
    for lv in range(1, depth + 1):
        if prefix(predicted, lv) == prefix(expected, lv):
            hit = lv
        else:
            break
    return hit / depth


def evaluate(
    h: Harness, model: ChatModel, tax: Taxonomy, cases: List[Case], workers: int = 8
) -> EvalResult:
    temperature = float(h.modelo.get("temperatura", 0.0))
    tree = Harness.render_prefix(tax)

    def one(c: Case) -> CaseResult:
        prompt = h.render(c.title, c.category, (c.content or "")[:CONTENT_CHARS])
        try:
            raw = model.complete(prompt, temperature=temperature, json_mode=True, prefix=tree)
        except Exception as e:
            if e.__class__.__name__ == "BudgetExceeded":
                raise
            # erro de infraestrutura não é erro do classificador: fica marcado à parte
            return CaseResult(c.id, c.expected, None, 0.0, False, f"erro na chamada: {e}")
        code, valid, err = parse_prediction(raw, tax)
        return CaseResult(c.id, c.expected, code, credit(c.expected, code) if valid else 0.0, valid, err)

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(one, cases))
    else:
        results = [one(c) for c in cases]
    broken = [r for r in results if r.error.startswith("erro na chamada")]
    if broken:
        raise IncompleteEval(
            f"{len(broken)} de {len(results)} chamadas falharam (ex.: {broken[0].error[:160]}). "
            "Reduza --workers ou espere o limite de taxa da API."
        )
    n = len(results) or 1
    return EvalResult(
        score=sum(r.credit for r in results) / n,
        l1_accuracy=sum(1 for r in results if r.predicted and prefix(r.predicted, 1) == prefix(r.expected, 1)) / n,
        invalid=sum(1 for r in results if not r.valid),
        results=results,
    )
