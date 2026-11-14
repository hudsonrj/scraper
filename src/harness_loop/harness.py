"""O arquivo de harness: a única coisa que o loop pode mudar.

Partes editáveis pelo loop: ``instrucoes``, ``regras`` e ``exemplos``.
Partes travadas: ``contrato`` (formato de saída) e ``modelo``. Uma edição que
tente mexer nelas é recusada antes de ser avaliada.
"""

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from .taxonomy import Taxonomy

MAX_RULES = 25
MAX_EXAMPLES = 12
MAX_TEXT = 600

EDIT_TYPES = (
    "adicionar_regra",
    "remover_regra",
    "reescrever_regra",
    "adicionar_exemplo",
    "remover_exemplo",
    "reescrever_instrucoes",
)


@dataclass
class Example:
    caso_id: str
    titulo: str
    codigo: str
    trecho: str = ""


@dataclass
class Harness:
    instrucoes: str
    contrato: str
    regras: List[str] = field(default_factory=list)
    exemplos: List[Example] = field(default_factory=list)
    modelo: Dict[str, Any] = field(default_factory=dict)
    versao: int = 0
    origem: str = "escrito à mão"

    # ---------- arquivo ----------
    @classmethod
    def load(cls, path: str) -> "Harness":
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        raw["exemplos"] = [Example(**e) for e in raw.get("exemplos") or []]
        raw["regras"] = list(raw.get("regras") or [])
        return cls(**raw)

    def dump(self) -> str:
        data = asdict(self)
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.dump())

    def fingerprint(self) -> str:
        editable = {
            "instrucoes": self.instrucoes,
            "regras": self.regras,
            "exemplos": [asdict(e) for e in self.exemplos],
        }
        raw = json.dumps(editable, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    # ---------- prompt ----------
    def render(self, tax: Taxonomy, title: str, category: str, content: str) -> str:
        parts = [self.instrucoes.strip(), "", "## Árvore temática", "", tax.render(), ""]
        if self.regras:
            parts += ["## Regras", ""]
            parts += [f"{i + 1}. {r}" for i, r in enumerate(self.regras)]
            parts.append("")
        if self.exemplos:
            parts += ["## Exemplos já classificados", ""]
            for e in self.exemplos:
                trecho = f" | {e.trecho}" if e.trecho else ""
                parts.append(f"- \"{e.titulo}\"{trecho} → {e.codigo}")
            parts.append("")
        parts += ["## Formato da resposta", "", self.contrato.strip(), ""]
        parts += [
            "## Notícia",
            "",
            f"Título: {title}",
            f"Categoria no site: {category or '—'}",
            f"Conteúdo: {content}",
        ]
        return "\n".join(parts)


@dataclass
class Edit:
    tipo: str
    texto: str = ""
    indice: Optional[int] = None
    caso_id: str = ""
    motivo: str = ""

    def label(self) -> str:
        if self.tipo == "adicionar_regra":
            return f"+ regra: {short(self.texto)}"
        if self.tipo == "remover_regra":
            return f"- regra {self.indice}"
        if self.tipo == "reescrever_regra":
            return f"~ regra {self.indice}: {short(self.texto)}"
        if self.tipo == "adicionar_exemplo":
            return f"+ exemplo {self.caso_id}"
        if self.tipo == "remover_exemplo":
            return f"- exemplo {self.indice}"
        return f"~ instruções: {short(self.texto)}"

    def key(self) -> str:
        return json.dumps([self.tipo, self.texto.strip().lower(), self.indice, self.caso_id])


def short(s: str, n: int = 70) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


class EditRejected(ValueError):
    pass


def apply_edit(h: Harness, e: Edit, task_cases: Dict[str, Any], heldout_titles: List[str]) -> Harness:
    """Aplica uma edição e devolve um novo harness, ou lança EditRejected.

    ``task_cases`` mapeia id → caso do lado *tarefa*. Exemplos só podem vir
    dali: um caso held-out nunca entra no harness.
    """
    if e.tipo not in EDIT_TYPES:
        raise EditRejected(f"tipo de edição desconhecido: {e.tipo!r}")
    n = copy.deepcopy(h)
    text = (e.texto or "").strip()

    if e.tipo in ("adicionar_regra", "reescrever_regra", "reescrever_instrucoes"):
        if not text:
            raise EditRejected("texto vazio")
        if len(text) > (MAX_TEXT * 3 if e.tipo == "reescrever_instrucoes" else MAX_TEXT):
            raise EditRejected("texto longo demais")
        low = text.lower()
        for t in heldout_titles:
            t = t.strip().lower()
            if len(t) >= 25 and t in low:
                raise EditRejected("o texto cita uma manchete do held-out (vazamento)")

    if e.tipo == "adicionar_regra":
        if len(n.regras) >= MAX_RULES:
            raise EditRejected(f"limite de {MAX_RULES} regras")
        if any(text.lower() == r.lower() for r in n.regras):
            raise EditRejected("regra repetida")
        n.regras.append(text)
    elif e.tipo in ("remover_regra", "reescrever_regra"):
        i = _index(e.indice, len(n.regras))
        if e.tipo == "remover_regra":
            n.regras.pop(i)
        else:
            if n.regras[i].strip() == text:
                raise EditRejected("reescrita idêntica")
            n.regras[i] = text
    elif e.tipo == "adicionar_exemplo":
        case = task_cases.get(e.caso_id)
        if case is None:
            raise EditRejected("exemplo precisa ser um caso do lado tarefa")
        if len(n.exemplos) >= MAX_EXAMPLES:
            raise EditRejected(f"limite de {MAX_EXAMPLES} exemplos")
        if any(x.caso_id == e.caso_id for x in n.exemplos):
            raise EditRejected("exemplo repetido")
        n.exemplos.append(
            Example(caso_id=case.id, titulo=case.title, codigo=case.expected, trecho=short(case.content, 160))
        )
    elif e.tipo == "remover_exemplo":
        n.exemplos.pop(_index(e.indice, len(n.exemplos)))
    elif e.tipo == "reescrever_instrucoes":
        if n.instrucoes.strip() == text:
            raise EditRejected("reescrita idêntica")
        n.instrucoes = text
    return n


def _index(i: Optional[int], size: int) -> int:
    """Índices chegam 1-based (como aparecem no prompt)."""
    if not isinstance(i, int) or not 1 <= i <= size:
        raise EditRejected(f"índice {i!r} fora do intervalo 1..{size}")
    return i - 1
