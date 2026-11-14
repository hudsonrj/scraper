"""Loop que reescreve o harness do classificador de temas.

O humano escreve a prova; o loop escreve o harness. Comandos:

    check         mostra o estado da prova e as checagens de qualidade
    build         monta a prova com atos do DOU (tema sugerido, provisório até revisar)
    dou-ingest    baixa tudo o que saiu no Diário Oficial num dia
    dou-classify  classifica todos os atos de um dia com o harness ativo
    review    revisão humana dos rótulos provisórios, no terminal
    eval      dá nota a um arquivo de harness, sem mudar nada
    loop      roda o loop: falhas → 8 edições → nota held-out → a melhor fica
    promote   copia uma versão aprovada para harness/active.yaml

Exemplos:

    poetry run python src/run_harness_loop.py check
    poetry run python src/run_harness_loop.py build --replace --n 200
    poetry run python src/run_harness_loop.py dou-classify --date 2026-11-13
    poetry run python src/run_harness_loop.py review
    poetry run python src/run_harness_loop.py loop --rounds 4 --candidates 8

Modelos: com ANTHROPIC_API_KEY, o modelo congelado é claude-opus-5-5 e o editor
claude-haiku-5-5. Com OPENAI_API_KEY, gpt-4o-mini. O editor pode ser local:

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
load_dotenv(os.path.join(ROOT, ".env"))  # antes de HDIR: o .env do servidor define HARNESS_DIR
HDIR = os.getenv("HARNESS_DIR") or os.path.join(ROOT, "harness")
CASES = os.path.join(HDIR, "exam", "cases.jsonl")
ACTIVE = os.path.join(HDIR, "active.yaml")
RUNS = os.path.join(HDIR, "runs")
CACHE = os.path.join(HDIR, ".cache", "llm.jsonl")
DOU_DIR = os.path.join(HDIR, "dou")
DOU_TEMPLATE = os.path.join(ROOT, "harness", "active-dou.yaml")

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
    tax = Taxonomy.load()
    if args.replace and os.path.exists(args.cases):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        arch = args.cases.replace(".jsonl", f".arquivo-{stamp}.jsonl")
        os.replace(args.cases, arch)
        print(f"Prova anterior arquivada em {os.path.relpath(arch, ROOT)}")
        if args.source == "dou" and os.path.exists(DOU_TEMPLATE):
            if os.path.exists(ACTIVE):
                shutil.copy(ACTIVE, ACTIVE.replace(".yaml", f".arquivo-{stamp}.yaml"))
            shutil.copy(DOU_TEMPLATE, ACTIVE)
            print("Harness ativo trocado pelo v0 do DOU (o anterior foi arquivado).")
    cases = load_cases(args.cases)
    have = {c.id for c in cases}

    if args.source == "govbr":
        from harness_loop.build_exam import fetch

        new = [c for c in fetch(args.n, tax, seed=args.seed) if c.id not in have]
    else:
        from harness_loop.dou import sample_cases

        new = [c for c in sample_cases(args.n, days=args.days, seed=args.seed, with_text=args.full_text)
               if c.id not in have]
        if not args.no_suggest:
            from harness_loop.models import CallCounter, DiskCache, from_env
            from harness_loop.suggest import suggest_labels

            try:
                model = from_env("suggest", DiskCache(CACHE), CallCounter())
                n_ok = suggest_labels(new, Harness.load(ACTIVE), model, tax)
                print(f"{n_ok} de {len(new)} atos com tema sugerido por {model.name} (provisório até revisar)")
            except ValueError as e:
                print(f"Sem sugestão de tema ({e}); rotule do zero em /revisar.")
    save_cases(args.cases, cases + new)
    print(f"{len(new)} casos novos em {os.path.relpath(args.cases, ROOT)}")
    print("Próximo passo: revise em /revisar (ou com o comando review).")
    return 0


def cmd_dou_ingest(args):
    import json

    from harness_loop.dou import fetch_day, parse_day

    day = parse_day(args.date)
    path = os.path.join(DOU_DIR, f"{day.isoformat()}.jsonl")
    try:
        acts = fetch_day(day)
    except RuntimeError as e:
        print(f"DOU {day.isoformat()}: falhou: {e}")
        return 1
    if not acts:
        # dia sem publicação (fim de semana, feriado): não grava arquivo vazio
        if os.path.exists(path) and os.path.getsize(path) == 0:
            os.remove(path)
        print(f"DOU {day.isoformat()}: nenhum ato publicado")
        return 2
    os.makedirs(DOU_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for a in acts:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    print(f"DOU {day.isoformat()}: {len(acts)} atos em {os.path.relpath(path, ROOT)}")
    return 0 if acts else 2


def cmd_dou_classify(args):
    import json
    from collections import Counter

    from harness_loop.dou import parse_day, to_case
    from harness_loop.runner import CONTENT_CHARS, parse_prediction

    tax = Taxonomy.load()
    day = parse_day(args.date)
    src = os.path.join(DOU_DIR, f"{day.isoformat()}.jsonl")
    if not os.path.exists(src):
        cmd_dou_ingest(args)
    acts = [json.loads(x) for x in open(src, encoding="utf-8") if x.strip()]
    h = Harness.load(args.harness)
    big, _, counter = _models(args)
    tree = Harness.render_prefix(tax)
    dst = os.path.join(DOU_DIR, f"{day.isoformat()}.temas.jsonl")
    done = {}
    if os.path.exists(dst):
        for x in open(dst, encoding="utf-8"):
            if x.strip():
                r = json.loads(x)
                done[r["id"]] = r
    todo = [a for a in acts if a["id"] not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"DOU {day.isoformat()}: {len(acts)} atos, {len(todo)} a classificar com harness v{h.versao} e {big.name}")

    from concurrent.futures import ThreadPoolExecutor
    import threading

    lock = threading.Lock()

    def one(a):
        c = to_case(a)
        prompt = h.render(c.title, c.category, (c.content or "")[:CONTENT_CHARS], c.agency)
        try:
            raw = big.complete(prompt, temperature=0.0, json_mode=True, prefix=tree)
            code, valid, err = parse_prediction(raw, tax)
        except Exception as e:
            if e.__class__.__name__ == "BudgetExceeded":
                raise
            code, valid, err = None, False, f"erro na chamada: {str(e)[:120]}"
        row = {"id": a["id"], "tema": code if valid else None, "erro": err, "harness": h.versao}
        if err.startswith("erro na chamada"):
            return row  # falha de API não fica gravada: a próxima execução tenta de novo
        with lock:
            with open(dst, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for row in ex.map(one, todo):
                done[row["id"]] = row
    except Exception as e:
        print(f"Parado: {e}. Rode de novo para continuar de onde parou.")
    temas = Counter(TAX_L1(tax, r["tema"]) for r in done.values() if r.get("tema"))
    falhas = sum(1 for r in done.values() if not r.get("tema") and not r["erro"].startswith("erro na chamada"))
    erros = sum(1 for r in done.values() if r["erro"].startswith("erro na chamada"))
    print(f"Classificados: {sum(temas.values())} · resposta inválida: {falhas} · falha de API (tenta de novo depois): {erros}"
          f" · chamadas novas: {counter.calls}")
    if erros:
        print("Exemplo de falha de API:", next(r["erro"] for r in done.values() if r["erro"].startswith("erro na chamada")))
    for t, n in temas.most_common(10):
        print(f"  {n:5d}  {t}")
    return 0


def TAX_L1(tax, code):
    return tax.label(code.split(".")[0]) if code else "—"


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
    big = from_env("big", cache, counter)
    editor = from_env("editor", cache, counter)
    log.info("modelo congelado: %s · editor: %s", big.name, editor.name)
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
                   workers=args.workers, out_dir=out, min_gain=args.min_gain)
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
    b = sub.add_parser("build", help="monta a prova: atos do DOU (padrão) ou notícias do gov.br")
    b.add_argument("--source", choices=("dou", "govbr"), default="dou")
    b.add_argument("--n", type=int, default=200)
    b.add_argument("--days", type=int, default=10, help="DOU: dias úteis recentes de onde sortear")
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--replace", action="store_true",
                   help="arquiva a prova atual e começa outra (com --source dou, troca também o harness ativo)")
    b.add_argument("--no-suggest", action="store_true", help="DOU: não pedir sugestão de tema a um modelo")
    b.add_argument("--full-text", action="store_true",
                   help="DOU: usa o texto completo (a classificação diária usa só o trecho do índice)")
    sub.add_parser("review", help="revisão humana dos rótulos")

    di = sub.add_parser("dou-ingest", help="baixa tudo o que saiu no DOU num dia")
    di.add_argument("--date", help="AAAA-MM-DD (padrão: hoje, ou o último dia útil)")
    for name in ("eval", "loop", "dou-classify"):
        s = sub.add_parser(name)
        s.add_argument("--harness", default=ACTIVE)
        s.add_argument("--workers", type=int, default=4, help="chamadas em paralelo; muitas estouram o limite de taxa")
        s.add_argument("--max-calls", type=int, default=None, help="para o loop ao atingir este número de chamadas")
        s.add_argument("--no-cache", action="store_true")
        if name == "eval":
            s.add_argument("-v", "--verbose", action="store_true")
        elif name == "dou-classify":
            s.add_argument("--date", help="AAAA-MM-DD (padrão: hoje, ou o último dia útil)")
            s.add_argument("--limit", type=int, default=0, help="classifica no máximo N atos nesta execução")
        else:
            s.add_argument("--rounds", type=int, default=4)
            s.add_argument("--candidates", type=int, default=8)
            s.add_argument("--min-gain", type=float, default=0.01,
                           help="ganho mínimo no held-out para aceitar uma edição (0.01 = 1 ponto)")

    pr = sub.add_parser("promote", help="promove uma versão aprovada")
    pr.add_argument("path")
    pr.add_argument("--yes", action="store_true")

    args = p.parse_args(argv)
    return {
        "check": cmd_check, "build": cmd_build, "review": cmd_review,
        "eval": cmd_eval, "loop": cmd_loop, "promote": cmd_promote,
        "dou-ingest": cmd_dou_ingest, "dou-classify": cmd_dou_classify,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
