"""Sugere o tema de casos sem rótulo usando um modelo mais forte que o congelado.

A sugestão entra como ``provisorio``: ajuda a pessoa que revisa, mas não conta
para a nota até alguém confirmar em /revisar.
"""

from concurrent.futures import ThreadPoolExecutor
from typing import List

from .exam import Case
from .harness import Harness
from .models import ChatModel
from .runner import CONTENT_CHARS, parse_prediction
from .taxonomy import Taxonomy


def suggest_labels(cases: List[Case], h: Harness, model: ChatModel, tax: Taxonomy, workers: int = 4) -> int:
    todo = [c for c in cases if c.status == "sem_rotulo"]
    tree = Harness.render_prefix(tax)

    def one(c: Case):
        prompt = h.render(c.title, c.category, (c.content or "")[:CONTENT_CHARS], c.agency)
        try:
            raw = model.complete(prompt, temperature=0.0, json_mode=True, prefix=tree)
        except Exception as e:  # sem sugestão: a pessoa rotula do zero
            c.note = f"sem sugestão ({str(e)[:80]})"
            return 0
        code, valid, _ = parse_prediction(raw, tax)
        if valid:
            c.expected, c.status, c.note = code, "provisorio", f"sugestão de {model.name}"
            return 1
        return 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return sum(ex.map(one, todo))
