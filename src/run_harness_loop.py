"""Loop que reescreve o harness do classificador de temas.

O humano escreve a prova; o loop escreve o harness. Comandos:

    check     mostra o estado da prova e as checagens de qualidade
    build     baixa notícias do Hugging Face com rótulo provisório do Cogfy
    review    revisão humana dos rótulos provisórios, no terminal
    eval      dá nota a um arquivo de harness, sem mudar nada
    loop      roda o loop: falhas → 8 edições → nota held-out → a melhor fica
    promote   copia uma versão aprovada para harness/active.yaml

Exemplos:

    poetry run python src/run_harness_loop.py check
    poetry run python src/run_harness_loop.py build --n 200
    poetry run python src/run_harness_loop.py review
    poetry run python src/run_harness_loop.py loop --rounds 4 --candidates 8

Modelos (API compatível com a OpenAI, incluindo Ollama):

    BIG_MODEL=gpt-4o-mini                       # modelo congelado
    EDITOR_MODEL=qwen3:4b EDITOR_BASE_URL=http://localhost:11434/v1
"""

import argparse
import difflib
import logging
import os
import shutil
import sys
from datetime import datetime

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness_loop.exam import by_split, find, load_cases, report, save_cases, usable  # noqa: E402
from harness_loop.harness import Harness  # noqa: E402
from harness_loop.taxonomy import Taxonomy  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HDIR = os.getenv("HARNESS_DIR") or os.path.join(ROOT, "harness")
CASES = os.path.join(HDIR, "exam", "cases.jsonl")
ACTIVE = os.path.join(HDIR, "active.yaml")
RUNS = os.path.join(HDIR, "runs")
CACHE = os.path.join(HDIR, ".cache", "llm.jsonl")

log = logging.getLogger("harness_loop")


def _exam(args, tax):
    cases = load_cases(args.cases)
    rep = report(cases, tax, allow_provisional=args.allow_provisional)
    return cases, rep


def _print_report(rep):
    print(
        f"Prova: {rep.total} casos · {rep.humano} revisados por humano · {rep.provisorio} provisórios"
        f" · {rep.descartado} descartados"
    )
    print(f"Em uso: {rep.tarefa} no lado tarefa, {rep.heldout} held-out")
    for c in rep.checks:
        print(f"  [{'ok' if c.ok else '!!'}] {c.text}")


def cmd_check(args):
    tax = Taxonomy.load()
    _, rep = _exam(args, tax)
    _print_report(rep)
    return 1 if rep.blocking else 0


def cmd_build(args):
    from harness_loop.build_exam import fetch

    tax = Taxonomy.load()
    cases = load_cases(args.cases)
    have = {c.id for c in cases}
    new = [c for c in fetch(args.n, tax, seed=args.seed) if c.id not in have]
    save_cases(args.cases, cases + new)
    print(f"{len(new)} notícias novas adicionadas como provisórias em {os.path.relpath(args.cases, ROOT)}")
    print("Próximo passo: revise os rótulos com o comando review.")
    return 0


def cmd_review(args):
    tax = Taxonomy.load()
    cases = load_cases(args.cases)
    todo = [c for c in cases if c.status == "provisorio"]
    if not todo:
        print("Nenhum caso provisório para revisar.")
        return 0
    print(
        "Para cada notícia: Enter aceita o rótulo, digite um código (ex.: 05.02.01) para trocar,\n"
        "'d' descarta a notícia, 'p' pula, 'q' sai. O progresso é salvo a cada resposta.\n"
    )
    done = 0
    for i, c in enumerate(todo, 1):
        print("=" * 78)
        print(f"[{i}/{len(todo)}] {c.title}")
        print(f"órgão: {c.agency or '—'} · categoria: {c.category or '—'} · {c.published_at}")
        print(c.content[:700].replace("\n", " ") + ("…" if len(c.content) > 700 else ""))
        print(f"\nRótulo do Cogfy: {tax.label(c.expected)}")
        while True:
            ans = input("> ").strip()
            if ans == "q":
                print(f"{done} revisados nesta sessão.")
                return 0
            if ans == "p":
                break
            if ans == "d":
                c.status, c.note = "descartado", "descartado na revisão"
                break
            if ans == "":
                c.status = "humano"
                break
            if tax.exists(ans):
                c.note = f"Cogfy dizia {c.expected}"
                c.expected, c.status = ans, "humano"
                break
            options = [k for k in tax.labels if k.startswith(ans)][:12]
            print("Código não existe." + (f" Opções: {', '.join(tax.label(o) for o in options)}" if options else ""))
        if c.status != "provisorio":
            done += 1
            save_cases(args.cases, cases)
    print(f"{done} revisados nesta sessão.")
    return 0


def _models(args):
    from harness_loop.models import CallCounter, DiskCache, from_env

    cache = DiskCache(None if args.no_cache else CACHE)
    counter = CallCounter(limit=args.max_calls)
    big = from_env("big", cache, counter, "gpt-4o-mini")
    editor = from_env("editor", cache, counter, "gpt-4o-mini")
    return big, editor, counter


def cmd_eval(args):
    from harness_loop.runner import evaluate

    tax = Taxonomy.load()
    cases, rep = _exam(args, tax)
    if rep.blocking:
        _print_report(rep)
        return 1
    h = Harness.load(args.harness)
    big, _, counter = _models(args)
    use = usable(cases, args.allow_provisional)
    for split in ("tarefa", "heldout"):
        res = evaluate(h, big, tax, by_split(use, split), args.workers)
        print(
            f"{split:8s} nota {res.score:6.1%} · nível 1 {res.l1_accuracy:6.1%} · inválidas {res.invalid}"
            f" · {len(res.results)} casos"
        )
        if args.verbose:
            for r in res.failures:
                c = find(cases, r.case_id)
                print(f"   ✗ {c.title[:70]} | esperado {r.expected} | previsto {r.predicted} {r.error}")
    print(f"chamadas ao modelo: {counter.calls} novas, {counter.cached} do cache")
    return 0


def cmd_loop(args):
    from harness_loop.loop import run_loop

    tax = Taxonomy.load()
    cases, rep = _exam(args, tax)
    _print_report(rep)
    if rep.blocking:
        print("\nA prova não está pronta. Corrija os itens marcados com BLOQUEIA.")
        return 1
    use = usable(cases, args.allow_provisional)
    n_task, n_held = len(by_split(use, "tarefa")), len(by_split(use, "heldout"))
    est = n_task + n_held + args.rounds * (n_task + args.candidates * n_held + 3)
    print(f"\nEstimativa: até {est} chamadas ao modelo (o cache reduz isso em reexecuções).")
    if args.max_calls:
        print(f"Limite configurado: {args.max_calls} chamadas.")

    h0 = Harness.load(args.harness)
    big, editor, counter = _models(args)
    out = os.path.join(RUNS, datetime.now().strftime("%Y%m%d-%H%M%S"))
    res = run_loop(h0, use, big, editor, tax, rounds=args.rounds, n_candidates=args.candidates,
                   workers=args.workers, out_dir=out)
    print()
    with open(os.path.join(out, "report.md"), encoding="utf-8") as f:
        print(f.read())
    print(f"Arquivos em {os.path.relpath(out, ROOT)} · chamadas: {counter.calls} novas, {counter.cached} do cache")
    return 0


def cmd_promote(args):
    new = Harness.load(args.path)
    old_txt = open(ACTIVE, encoding="utf-8").read() if os.path.exists(ACTIVE) else ""
    new_txt = new.dump()
    diff = list(difflib.unified_diff(old_txt.splitlines(), new_txt.splitlines(), "active.yaml", args.path, lineterm=""))
    if not diff:
        print("Nada a promover: o arquivo é igual ao ativo.")
        return 0
    print("\n".join(diff))
    if not args.yes and input("\nPromover esta versão para harness/active.yaml? [s/N] ").strip().lower() != "s":
        print("Cancelado.")
        return 1
    if os.path.exists(ACTIVE):
        shutil.copy(ACTIVE, ACTIVE + ".anterior")
    with open(ACTIVE, "w", encoding="utf-8") as f:
        f.write(new_txt)
    print("Promovido. A versão anterior ficou em harness/active.yaml.anterior.")
    return 0


def main(argv=None):
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cases", default=CASES, help="arquivo JSONL da prova")
    p.add_argument("--allow-provisional", action="store_true",
                   help="usa também rótulos do Cogfy não revisados (o loop passa a imitar o Cogfy)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="estado da prova")
    b = sub.add_parser("build", help="baixa notícias com rótulo provisório")
    b.add_argument("--n", type=int, default=200)
    b.add_argument("--seed", type=int, default=7)
    sub.add_parser("review", help="revisão humana dos rótulos")

    for name in ("eval", "loop"):
        s = sub.add_parser(name)
        s.add_argument("--harness", default=ACTIVE)
        s.add_argument("--workers", type=int, default=8)
        s.add_argument("--max-calls", type=int, default=None, help="para o loop ao atingir este número de chamadas")
        s.add_argument("--no-cache", action="store_true")
        if name == "eval":
            s.add_argument("-v", "--verbose", action="store_true")
        else:
            s.add_argument("--rounds", type=int, default=4)
            s.add_argument("--candidates", type=int, default=8)

    pr = sub.add_parser("promote", help="promove uma versão aprovada")
    pr.add_argument("path")
    pr.add_argument("--yes", action="store_true")

    args = p.parse_args(argv)
    return {
        "check": cmd_check, "build": cmd_build, "review": cmd_review,
        "eval": cmd_eval, "loop": cmd_loop, "promote": cmd_promote,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
