"""A prova: casos rotulados, divisão tarefa/held-out e checagens de qualidade.

Cada linha de ``cases.jsonl`` é um caso::

    {"id": "...", "title": "...", "category": "...", "content": "...",
     "expected": "05.02.01",          # código esperado (nível 1, 2 ou 3)
     "split": "tarefa" | "heldout",   # fixado na criação, nunca muda
     "status": "humano" | "provisorio" | "descartado",
     "source": "cogfy" | "manual" | "tests", "note": "..."}

``status = provisorio`` quer dizer que o rótulo veio do Cogfy e ainda não foi
revisado por uma pessoa. A prova só vale tanto quanto os rótulos ``humano``.
"""

import hashlib
import json
import os
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Iterable, List, Optional

from .taxonomy import Taxonomy, level, prefix

SPLITS = ("tarefa", "heldout")
STATUSES = ("humano", "provisorio", "descartado")


@dataclass
class Case:
    id: str
    title: str
    expected: str
    content: str = ""
    category: str = ""
    split: str = ""
    status: str = "provisorio"
    source: str = "manual"
    note: str = ""
    url: str = ""
    published_at: str = ""
    agency: str = ""

    def __post_init__(self):
        if not self.split:
            self.split = split_for(self.id)


def split_for(case_id: str) -> str:
    """Divisão determinística: o mesmo id cai sempre do mesmo lado."""
    h = int(hashlib.sha256(case_id.encode("utf-8")).hexdigest(), 16)
    return SPLITS[h % 2]


def load_cases(path: str) -> List[Case]:
    cases = []
    if not os.path.exists(path):
        return cases
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{n}: JSON inválido ({e})") from e
            known = {k: v for k, v in data.items() if k in Case.__dataclass_fields__}
            cases.append(Case(**known))
    return cases


def save_cases(path: str, cases: Iterable[Case]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def usable(cases: List[Case], allow_provisional: bool) -> List[Case]:
    ok = {"humano", "provisorio"} if allow_provisional else {"humano"}
    return [c for c in cases if c.status in ok]


def by_split(cases: List[Case], split: str) -> List[Case]:
    return [c for c in cases if c.split == split]


@dataclass
class Check:
    ok: bool
    text: str


@dataclass
class ExamReport:
    total: int
    humano: int
    provisorio: int
    descartado: int
    tarefa: int
    heldout: int
    checks: List[Check] = field(default_factory=list)

    @property
    def blocking(self) -> List[Check]:
        return [c for c in self.checks if not c.ok and c.text.startswith("BLOQUEIA")]


def report(
    cases: List[Case], tax: Taxonomy, allow_provisional: bool = False
) -> ExamReport:
    st = Counter(c.status for c in cases)
    use = usable(cases, allow_provisional)
    held = by_split(use, "heldout")
    task = by_split(use, "tarefa")
    r = ExamReport(
        total=len(cases),
        humano=st["humano"],
        provisorio=st["provisorio"],
        descartado=st["descartado"],
        tarefa=len(task),
        heldout=len(held),
    )

    bad = [c.id for c in cases if c.status != "descartado" and not tax.exists(c.expected)]
    r.checks.append(
        Check(
            not bad,
            "Todos os códigos esperados existem na árvore"
            if not bad
            else f"BLOQUEIA: {len(bad)} caso(s) com código fora da árvore: {', '.join(bad[:5])}",
        )
    )
    r.checks.append(
        Check(
            len(held) >= 30,
            f"{len(held)} perguntas held-out"
            + ("" if len(held) >= 30 else " (abaixo de 30, cada acerto vale mais de 3 pontos e a nota fica ruidosa)"),
        )
    )
    if len(held) < 10 or len(task) < 10:
        r.checks.append(
            Check(False, "BLOQUEIA: são necessários pelo menos 10 casos em cada lado (tarefa e held-out)")
        )
    if not allow_provisional and st["provisorio"]:
        r.checks.append(
            Check(
                True,
                f"{st['provisorio']} caso(s) provisórios ignorados até alguém revisar (comando review)",
            )
        )
    if allow_provisional and st["provisorio"]:
        r.checks.append(
            Check(
                False,
                f"{st['provisorio']} caso(s) usam o rótulo do Cogfy sem revisão: o loop vai aprender a imitar o Cogfy, inclusive os erros dele",
            )
        )
    l1 = Counter(prefix(c.expected, 1) for c in held)
    if l1:
        top, n = l1.most_common(1)[0]
        share = n / len(held)
        r.checks.append(
            Check(
                share <= 0.25,
                f"Tema mais frequente no held-out: {tax.label(top)} com {share:.0%}"
                + ("" if share <= 0.25 else " (o loop vai otimizar sobretudo esse tema)"),
            )
        )
        r.checks.append(
            Check(
                len(l1) >= 10,
                f"{len(l1)} dos 25 temas de nível 1 aparecem no held-out",
            )
        )
    deep = sum(1 for c in held if level(c.expected) == 3)
    if held:
        r.checks.append(
            Check(
                True,
                f"{deep} de {len(held)} casos held-out têm rótulo de nível 3; os demais valem só até o nível rotulado",
            )
        )
    titles = Counter(c.title.strip().lower() for c in use)
    dups = [t for t, n in titles.items() if n > 1]
    r.checks.append(
        Check(
            not dups,
            "Nenhuma manchete repetida"
            if not dups
            else f"{len(dups)} manchete(s) repetida(s): podem vazar da tarefa para o held-out",
        )
    )
    return r


def find(cases: List[Case], case_id: str) -> Optional[Case]:
    for c in cases:
        if c.id == case_id:
            return c
    return None
