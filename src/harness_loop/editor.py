"""O editor pequeno: lê as falhas do lado tarefa e propõe edições ao harness.

O editor nunca vê casos held-out. Isso é garantido pela assinatura de
``propose``: ela só recebe falhas e casos do lado tarefa.
"""

import json
import logging
import re
from typing import Dict, List

from .exam import Case
from .harness import EDIT_TYPES, Edit, Harness, short
from .models import ChatModel
from .runner import CaseResult
from .taxonomy import Taxonomy

MAX_FAILURES_SHOWN = 25

PROMPT = """Você melhora o harness de um classificador de notícias do governo federal.
O classificador é um modelo grande que NÃO pode ser alterado. Você só pode editar
o texto ao redor dele: instruções, regras e exemplos.

Abaixo estão o harness atual e as notícias que ele classificou errado. Proponha
exatamente {n} edições DIFERENTES entre si. Cada edição deve ser pequena e atacar
um padrão de erro que se repete, não um caso isolado. Regras genéricas que valem
para muitas notícias são melhores que regras sobre uma manchete específica.
Nunca copie manchetes para dentro de regras.

## Harness atual

### Instruções
{instrucoes}

### Regras (numeradas a partir de 1)
{regras}

### Exemplos (numerados a partir de 1)
{exemplos}

## Erros (esperado → previsto)
{falhas}

## Tipos de edição permitidos
- {{"tipo": "adicionar_regra", "texto": "..."}}
- {{"tipo": "reescrever_regra", "indice": 2, "texto": "..."}}
- {{"tipo": "remover_regra", "indice": 3}}
- {{"tipo": "adicionar_exemplo", "caso_id": "<um id da lista de erros>"}}
- {{"tipo": "remover_exemplo", "indice": 1}}
- {{"tipo": "reescrever_instrucoes", "texto": "..."}}

Inclua em cada edição um campo "motivo" curto. Responda só com JSON no formato:
{{"edicoes": [ ... {n} itens ... ]}}
"""


def build_prompt(h: Harness, failures: List[CaseResult], cases: Dict[str, Case], tax: Taxonomy, n: int) -> str:
    regras = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(h.regras)) or "(nenhuma)"
    exemplos = (
        "\n".join(f"{i + 1}. \"{e.titulo}\" → {e.codigo}" for i, e in enumerate(h.exemplos)) or "(nenhum)"
    )
    linhas = []
    for f in failures[:MAX_FAILURES_SHOWN]:
        c = cases[f.case_id]
        prev = tax.label(f.predicted) if f.valid else f"resposta inválida ({f.error})"
        linhas.append(
            f"- id={c.id} | \"{c.title}\" | {short(c.content, 220)}\n"
            f"  esperado: {tax.label(c.expected)} | previsto: {prev}"
        )
    if len(failures) > MAX_FAILURES_SHOWN:
        linhas.append(f"(... e mais {len(failures) - MAX_FAILURES_SHOWN} erros)")
    return PROMPT.format(
        n=n,
        instrucoes=h.instrucoes.strip(),
        regras=regras,
        exemplos=exemplos,
        falhas="\n".join(linhas),
    )


def parse_edits(raw: str) -> List[Edit]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    items = data.get("edicoes") if isinstance(data, dict) else data
    out = []
    for it in items or []:
        if not isinstance(it, dict) or it.get("tipo") not in EDIT_TYPES:
            continue
        idx = it.get("indice")
        try:
            idx = int(idx) if idx is not None else None
        except (TypeError, ValueError):
            idx = None
        out.append(
            Edit(
                tipo=it["tipo"],
                texto=str(it.get("texto") or ""),
                indice=idx,
                caso_id=str(it.get("caso_id") or ""),
                motivo=str(it.get("motivo") or ""),
            )
        )
    return out


def propose(
    editor: ChatModel,
    h: Harness,
    failures: List[CaseResult],
    task_cases: Dict[str, Case],
    tax: Taxonomy,
    n: int = 8,
    attempts: int = 3,
    temperature: float = 0.8,
) -> List[Edit]:
    """Pede edições ao editor até juntar ``n`` diferentes (ou esgotar tentativas)."""
    edits: List[Edit] = []
    seen = set()
    for attempt in range(attempts):
        missing = n - len(edits)
        if missing <= 0:
            break
        prompt = build_prompt(h, failures, task_cases, tax, missing)
        if attempt:
            prompt += f"\n(Tentativa {attempt + 1}: não repita estas edições: {[e.label() for e in edits]})"
        try:
            raw = editor.complete(prompt, temperature=temperature, json_mode=True)
        except Exception as e:
            if e.__class__.__name__ == "BudgetExceeded":
                raise
            logging.warning("Editor falhou: %s", e)
            continue
        for e in parse_edits(raw):
            if e.key() not in seen:
                seen.add(e.key())
                edits.append(e)
    return edits[:n]
