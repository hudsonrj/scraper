"""newsloop: página explicativa, painel do loop e revisão da prova pelo navegador.

Roda com gunicorn atrás do Caddy (ver deploy/install.sh). Variáveis (.env):

    PAINEL_USUARIO, PAINEL_SENHA   protegem /revisar (escrita na prova)
    HARNESS_DIR                    pasta de dados (prova, harness ativo, execuções)
    OPENAI_API_KEY, BIG_*, EDITOR_* usados só pelo loop, não pelo site
"""

import hmac
import json
import os
import re
import sys
import threading
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, Response, abort, redirect, render_template, request, send_from_directory, url_for

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
load_dotenv(os.path.join(ROOT, ".env"))

from harness_loop.exam import by_split, find, load_cases, report, save_cases, usable  # noqa: E402
from harness_loop.harness import Harness  # noqa: E402
from harness_loop.taxonomy import Taxonomy  # noqa: E402

HDIR = os.getenv("HARNESS_DIR") or os.path.join(ROOT, "harness")
CASES = os.path.join(HDIR, "exam", "cases.jsonl")
ACTIVE = os.path.join(HDIR, "active.yaml")
RUNS = os.path.join(HDIR, "runs")
DOU_DIR = os.path.join(HDIR, "dou")


def list_dou_days(limit=15):
    from collections import Counter

    out = []
    if not os.path.isdir(DOU_DIR):
        return out
    days = sorted((f[:-6] for f in os.listdir(DOU_DIR) if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.jsonl", f)), reverse=True)
    for d in days[:limit]:
        acts = [json.loads(x) for x in open(os.path.join(DOU_DIR, d + ".jsonl"), encoding="utf-8") if x.strip()]
        secs = Counter(a["secao"].rstrip("e") for a in acts)
        temas, classif = Counter(), 0
        tpath = os.path.join(DOU_DIR, d + ".temas.jsonl")
        if os.path.exists(tpath):
            for x in open(tpath, encoding="utf-8"):
                if x.strip():
                    r = json.loads(x)
                    if r.get("tema"):
                        classif += 1
                        temas[r["tema"].split(".")[0]] += 1
        out.append({
            "dia": d, "total": len(acts), "secoes": secs, "classificados": classif,
            "temas": [(TAX.label(k), n) for k, n in temas.most_common(5)],
        })
    return out

app = Flask(__name__, static_folder=os.path.join(HERE, "static"), template_folder=os.path.join(HERE, "templates"))
TAX = Taxonomy.load()
LOCK = threading.Lock()
L3 = sorted((c, TAX.labels[c]) for c in TAX.labels if len(c) == 8)
ALL_CODES = sorted((c, TAX.labels[c]) for c in TAX.labels)


def need_auth(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        user, pw = os.getenv("PAINEL_USUARIO", "admin"), os.getenv("PAINEL_SENHA", "")
        auth = request.authorization
        if not pw:
            return Response("Defina PAINEL_SENHA no .env para liberar a revisão.", 503)
        if not auth or not (hmac.compare_digest(auth.username or "", user) and hmac.compare_digest(auth.password or "", pw)):
            return Response("Acesso restrito.", 401, {"WWW-Authenticate": 'Basic realm="newsloop"'})
        return fn(*a, **kw)

    return wrapper


def list_runs():
    out = []
    if not os.path.isdir(RUNS):
        return out
    for name in sorted(os.listdir(RUNS), reverse=True):
        d = os.path.join(RUNS, name)
        hist = os.path.join(d, "history.jsonl")
        if not os.path.isdir(d):
            continue
        rounds = []
        if os.path.exists(hist):
            with open(hist, encoding="utf-8") as f:
                rounds = [json.loads(x) for x in f if x.strip()]
        out.append({"name": name, "rounds": rounds, "kept": sum(1 for r in rounds if r.get("kept"))})
    return out


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/painel")
def painel():
    cases = load_cases(CASES)
    rep = report(cases, TAX)
    rep_prov = report(cases, TAX, allow_provisional=True)
    active = open(ACTIVE, encoding="utf-8").read() if os.path.exists(ACTIVE) else "(sem harness ativo)"
    h = Harness.load(ACTIVE) if os.path.exists(ACTIVE) else None
    return render_template("painel.html", rep=rep, rep_prov=rep_prov, active=active, h=h, runs=list_runs(),
                           dou=list_dou_days())


@app.get("/execucoes/<name>")
def execucao(name):
    d = os.path.join(RUNS, os.path.basename(name))
    if not os.path.isdir(d):
        abort(404)
    read = lambda f: open(os.path.join(d, f), encoding="utf-8").read() if os.path.exists(os.path.join(d, f)) else ""
    hist = [json.loads(x) for x in read("history.jsonl").splitlines() if x.strip()]
    return render_template("execucao.html", name=name, report=read("report.md"), final=read("final.yaml"), hist=hist)


@app.get("/revisar")
@need_auth
def revisar():
    cases = load_cases(CASES)
    skip = set(request.args.getlist("pular"))
    todo = [c for c in cases if c.status in ("provisorio", "sem_rotulo") and c.id not in skip]
    done = sum(1 for c in cases if c.status == "humano")
    c = todo[0] if todo else None
    return render_template(
        "revisar.html", c=c, left=len(todo), done=done, tax=TAX, codes=ALL_CODES,
        skip=list(skip),
        sugestao=TAX.label(c.expected) if c and c.status == "provisorio" else "",
        fonte=(c.note or "sugestão") if c else "",
    )


@app.post("/revisar/<case_id>")
@need_auth
def revisar_post(case_id):
    action = request.form.get("acao")
    skip = request.form.getlist("pular")
    if action == "pular":
        return redirect(url_for("revisar", pular=skip + [case_id]))
    with LOCK:
        cases = load_cases(CASES)
        c = find(cases, case_id)
        if c is None:
            abort(404)
        if action == "aceitar":
            if c.status != "provisorio":
                return Response("Este ato não tem sugestão: escolha o tema na lista.", 400)
            c.status = "humano"
        elif action == "descartar":
            c.status, c.note = "descartado", "descartado na revisão"
        elif action == "trocar":
            code = (request.form.get("codigo") or "").split(" ")[0].strip()
            if not TAX.exists(code):
                return Response(f"Código {code!r} não existe na árvore. Volte e escolha da lista.", 400)
            c.note = f"sugestão era {c.expected}" if c.expected else "rotulado do zero"
            c.expected, c.status = code, "humano"
        else:
            abort(400)
        save_cases(CASES, cases)
    return redirect(url_for("revisar", pular=skip))


@app.get("/saude")
def saude():
    return {"ok": True}
