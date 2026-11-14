"""O loop: falhas → 8 edições → nota held-out → a melhor fica.

Regras de aceitação de uma edição (todas precisam valer):

1. A nota held-out sobe; empate só passa se a nota no lado tarefa subir.
2. A quantidade de respostas inválidas no held-out não aumenta (porteira de
   segurança: um harness que quebra o formato de saída nunca é promovido,
   por melhor que seja a média).

O resultado de uma execução é uma pasta com cada versão do harness, o histórico
e um relatório. Nenhuma versão vira produção sozinha: alguém roda ``promote``.
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional

from .editor import propose
from .exam import Case, by_split
from .harness import Edit, EditRejected, Harness, apply_edit
from .models import BudgetExceeded, ChatModel
from .runner import EvalResult, IncompleteEval, evaluate
from .taxonomy import Taxonomy

log = logging.getLogger("harness_loop")


@dataclass
class Candidate:
    edit: Edit
    heldout: Optional[float] = None
    tarefa: Optional[float] = None
    invalid: Optional[int] = None
    rejected: str = ""


@dataclass
class Round:
    n: int
    failures: int
    candidates: List[Candidate] = field(default_factory=list)
    best: Optional[int] = None
    kept: bool = False
    heldout: float = 0.0
    tarefa: float = 0.0
    seconds: float = 0.0


@dataclass
class RunResult:
    v0_heldout: float
    v0_tarefa: float
    v0_invalid: int
    rounds: List[Round] = field(default_factory=list)
    final: Optional[Harness] = None
    stopped: str = ""


def better(cand_h: float, cand_t: float, cur_h: float, cur_t: float, min_gain: float = 0.0, eps: float = 1e-9) -> bool:
    """Sobe o held-out pelo menos ``min_gain``; ou empata no held-out e sobe na tarefa.

    ``min_gain`` protege contra ruído: com 96 perguntas, uma única notícia vale ~1 ponto.
    """
    gain = cand_h - cur_h
    if (gain >= min_gain - eps) if min_gain > 0 else (gain > eps):
        return True
    return abs(cand_h - cur_h) <= eps and cand_t > cur_t + eps


def run_loop(
    h0: Harness,
    cases: List[Case],
    big: ChatModel,
    editor: ChatModel,
    tax: Taxonomy,
    rounds: int = 4,
    n_candidates: int = 8,
    workers: int = 8,
    out_dir: Optional[str] = None,
    on_round: Optional[Callable[[Round], None]] = None,
    min_gain: float = 0.0,
) -> RunResult:
    task = by_split(cases, "tarefa")
    held = by_split(cases, "heldout")
    task_by_id: Dict[str, Case] = {c.id: c for c in task}
    held_titles = [c.title for c in held]

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        h0.save(os.path.join(out_dir, "v0.yaml"))

    cur = h0
    cur_task: EvalResult = evaluate(cur, big, tax, task, workers)
    cur_held: EvalResult = evaluate(cur, big, tax, held, workers)
    res = RunResult(cur_held.score, cur_task.score, cur_held.invalid)
    log.info(
        "v0: held-out %.1f%% · tarefa %.1f%% · inválidas %d",
        100 * cur_held.score, 100 * cur_task.score, cur_held.invalid,
    )

    try:
        for r in range(1, rounds + 1):
            t0 = time.time()
            failures = cur_task.failures
            rd = Round(n=r, failures=len(failures))
            if not failures:
                res.stopped = "o harness já acerta todo o lado tarefa"
                break

            edits = propose(editor, cur, failures, task_by_id, tax, n=n_candidates)
            evaluated = []
            for e in edits:
                c = Candidate(edit=e)
                try:
                    nh = apply_edit(cur, e, task_by_id, held_titles)
                except EditRejected as err:
                    c.rejected = str(err)
                    rd.candidates.append(c)
                    continue
                try:
                    he = evaluate(nh, big, tax, held, workers)
                except IncompleteEval as err:
                    c.rejected = f"medição incompleta: {err}"
                    rd.candidates.append(c)
                    continue
                c.heldout, c.invalid = he.score, he.invalid
                rd.candidates.append(c)
                evaluated.append((len(rd.candidates) - 1, nh, he))

            # porteira: quem aumenta as respostas inválidas sai antes da disputa
            safe = []
            for idx, nh, he in evaluated:
                if he.invalid > cur_held.invalid:
                    rd.candidates[idx].rejected = (
                        f"porteira: {he.invalid} respostas inválidas contra {cur_held.invalid} da versão atual"
                    )
                else:
                    safe.append((idx, nh, he))

            # desempate pelo lado tarefa só entre os empatados no topo do held-out
            best_tuple = None
            if safe:
                top = max(he.score for _, _, he in safe)
                tied = [x for x in safe if abs(x[2].score - top) <= 1e-9]
                for idx, nh, he in tied:
                    try:
                        te = evaluate(nh, big, tax, task, workers)
                    except IncompleteEval as err:
                        rd.candidates[idx].rejected = f"medição incompleta no lado tarefa: {err}"
                        continue
                    rd.candidates[idx].tarefa = te.score
                    if best_tuple is None or te.score > best_tuple[3].score:
                        best_tuple = (idx, nh, he, te)
            if safe and best_tuple is not None:
                idx, nh, he, te = best_tuple
                rd.best = idx
                if better(he.score, te.score, cur_held.score, cur_task.score, min_gain):
                    rd.kept = True
                    nh.versao = cur.versao + 1
                    nh.origem = f"loop, rodada {r}: {rd.candidates[idx].edit.label()}"
                    cur, cur_held, cur_task = nh, he, te

            rd.heldout, rd.tarefa = cur_held.score, cur_task.score
            rd.seconds = time.time() - t0
            res.rounds.append(rd)
            if out_dir:
                cur.save(os.path.join(out_dir, f"v{cur.versao}.yaml"))
                with open(os.path.join(out_dir, "history.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(rd), ensure_ascii=False) + "\n")
            log.info(
                "rodada %d: %d falhas, %d candidatas, %s → held-out %.1f%%",
                r, rd.failures, len(rd.candidates),
                "mantida " + rd.candidates[rd.best].edit.label() if rd.kept else "nenhuma venceu",
                100 * rd.heldout,
            )
            if on_round:
                on_round(rd)
        else:
            res.stopped = f"{rounds} rodadas concluídas"
    except (BudgetExceeded, IncompleteEval) as e:
        res.stopped = f"parado: {e}"
        log.warning(res.stopped)

    res.final = cur
    if out_dir:
        cur.save(os.path.join(out_dir, "final.yaml"))
        with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
            f.write(render_report(res, h0, len(task), len(held)))
    return res


def render_report(res: RunResult, h0: Harness, n_task: int, n_held: int) -> str:
    final = res.final or h0
    last_h = res.rounds[-1].heldout if res.rounds else res.v0_heldout
    last_t = res.rounds[-1].tarefa if res.rounds else res.v0_tarefa
    lines = [
        "# Execução do loop de harness",
        "",
        f"- Casos: {n_task} no lado tarefa, {n_held} held-out",
        f"- Held-out: {res.v0_heldout:.1%} (v0) → {last_h:.1%} (v{final.versao})",
        f"- Tarefa: {res.v0_tarefa:.1%} (v0) → {last_t:.1%}",
        f"- Respostas inválidas no held-out na v0: {res.v0_invalid}",
        f"- Fim: {res.stopped or '—'}",
        "",
        "| rodada | falhas | melhor edição | held-out | resultado |",
        "|---|---|---|---|---|",
    ]
    for rd in res.rounds:
        if rd.best is None:
            why = "; ".join(sorted({c.rejected.split(":")[0] for c in rd.candidates if c.rejected})) or "o editor não propôs edições válidas"
            lines.append(f"| {rd.n} | {rd.failures} | — | — | nenhuma candidata passou ({why}) |")
            continue
        c = rd.candidates[rd.best]
        status = "mantida" if rd.kept else (c.rejected or "não superou a versão atual")
        lines.append(
            f"| {rd.n} | {rd.failures} | {c.edit.label().replace('|', '/')} | {c.heldout:.1%} | {status} |"
        )
    lines += [
        "",
        "## Próximo passo",
        "",
        "Revise `final.yaml`. Se fizer sentido, promova com:",
        "",
        "```bash",
        "poetry run python src/run_harness_loop.py promote <esta-pasta>/final.yaml",
        "```",
    ]
    return "\n".join(lines) + "\n"
