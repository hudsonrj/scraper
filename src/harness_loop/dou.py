"""Coleta do Diário Oficial da União: todas as seções de um dia.

Duas fontes, com o mesmo formato de saída:

* **INLABS** (``inlabs.in.gov.br``), o serviço oficial de dados abertos da
  Imprensa Nacional: um ``.zip`` de XMLs por seção e dia, com o texto completo.
  Exige cadastro gratuito; usado quando ``INLABS_EMAIL`` e ``INLABS_SENHA``
  estão no ambiente. Guarda os últimos 4 meses.
* **leiturajornal** (``www.in.gov.br``): a página de leitura de cada seção
  traz os atos num JSON embutido. Sem cadastro, mas o site bloqueia alguns
  servidores (ex.: data centers fora do Brasil).
* **github**: o workflow ``dou-ingest`` coleta pela página de leitura nos
  servidores do GitHub e grava na branch ``dou-dados``; quem é bloqueado lê
  de lá. Escolha com ``DOU_SOURCE=github``.
"""

import html
import io
import json
import logging
import os
import xml.etree.ElementTree as ET
import zipfile
import random
import re
import time
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional

import requests

from .exam import Case

BASE = "https://www.in.gov.br"
SECTIONS = ("do1", "do2", "do3", "do1e", "do2e", "do3e")
SECTION_LABEL = {
    "do1": "Seção 1", "do2": "Seção 2", "do3": "Seção 3",
    "do1e": "Seção 1 Extra", "do2e": "Seção 2 Extra", "do3e": "Seção 3 Extra",
}
UA = {"User-Agent": "Mozilla/5.0 (newsloop; leitura do DOU)"}
_PARAMS = re.compile(r'<script id="params" type="application/json">\s*(\{.*?\})\s*</script>', re.S)
_TEXT = re.compile(r'<div class="texto-dou">(.*?)</div>\s*</div>', re.S)


def _get(url: str, params: Optional[Dict] = None, tries: int = 4) -> str:
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=60)
            if r.status_code == 200:
                r.encoding = "utf-8"
                return r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = e
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"in.gov.br não respondeu ({url}): {last}")


def clean(raw: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", raw or "")).split())


def act_id(url_title: str) -> str:
    m = re.search(r"-(\d{6,})$", url_title or "")
    return f"dou-{m.group(1)}" if m else f"dou-{url_title}"


def fetch_section(day: date, section: str) -> List[Dict]:
    page = _get(f"{BASE}/leiturajornal", {"data": day.strftime("%d-%m-%Y"), "secao": section})
    m = _PARAMS.search(page)
    if not m:
        title = re.search(r"<title>(.*?)</title>", page, re.S)
        raise RuntimeError(
            f"a página não trouxe os atos ({len(page)} bytes, título: "
            f"{clean(title.group(1)) if title else 'sem título'!r}); o in.gov.br pode estar bloqueando este servidor"
        )
    out = []
    for a in json.loads(m.group(1)).get("jsonArray", []):
        out.append({
            "id": act_id(a.get("urlTitle", "")),
            "data": day.isoformat(),
            "secao": section,
            "tipo": a.get("artType") or "",
            "titulo": clean(a.get("title") or a.get("titulo") or ""),
            "orgao": a.get("hierarchyStr") or "",
            "trecho": clean(a.get("content") or ""),
            "pagina": a.get("numberPage") or "",
            "edicao": a.get("editionNumber") or "",
            "url": f"{BASE}/web/dou/-/{a.get('urlTitle', '')}",
        })
    return out


# ---------------- INLABS ----------------

INLABS = "https://inlabs.in.gov.br"
INLABS_SECTIONS = {"do1": "DO1", "do2": "DO2", "do3": "DO3", "do1e": "DO1E", "do2e": "DO2E", "do3e": "DO3E"}


def inlabs_enabled() -> bool:
    return bool(os.getenv("INLABS_EMAIL") and os.getenv("INLABS_SENHA"))


def inlabs_session() -> requests.Session:
    """Login como no script oficial (github.com/Imprensa-Nacional/inlabs)."""
    s = requests.Session()
    s.headers.update(UA)
    last = None
    for i in range(3):
        try:
            r = s.post(
                f"{INLABS}/logar.php",
                data={"email": os.getenv("INLABS_EMAIL"), "password": os.getenv("INLABS_SENHA")},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=60,
            )
            if s.cookies.get("inlabs_session_cookie"):
                return s
            last = f"HTTP {r.status_code}, sem cookie de sessão"
            if "Manutenção" in r.text:
                last = "o INLABS está em manutenção programada"
        except requests.RequestException as e:
            last = e
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"login no INLABS falhou: {last}. Confira INLABS_EMAIL e INLABS_SENHA no .env")


def inlabs_zip(s: requests.Session, day: date, section: str) -> Optional[bytes]:
    d = day.isoformat()
    name = f"{d}-{INLABS_SECTIONS[section]}.zip"
    r = s.get(
        f"{INLABS}/index.php",
        params={"p": d, "dl": name},
        headers={"Cookie": "inlabs_session_cookie=" + s.cookies.get("inlabs_session_cookie", ""), "origem": "736372697074"},
        timeout=180,
    )
    if r.status_code == 404:
        return None  # seção sem edição nesse dia
    if r.status_code != 200 or not r.content.startswith(b"PK"):
        raise RuntimeError(f"INLABS devolveu HTTP {r.status_code} ({len(r.content)} bytes) para {name}")
    return r.content


def parse_inlabs_xml(raw: bytes, day: date, section: str) -> Optional[Dict]:
    """Um XML do INLABS = um ato: atributos em <article>, texto em <body>."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    art = root if root.tag == "article" else root.find(".//article")
    if art is None:
        return None
    a = art.attrib
    body = art.find("body")

    def part(tag: str) -> str:
        el = body.find(tag) if body is not None else None
        return clean(el.text or "") if el is not None else ""

    texto = part("Texto")
    titulo = part("Identifica") or part("Titulo") or a.get("name", "")
    ident = a.get("idMateria") or a.get("id") or a.get("name", "")
    return {
        "id": f"dou-{ident}",
        "data": day.isoformat(),
        "secao": section,
        "tipo": a.get("artType", ""),
        "titulo": titulo,
        "orgao": a.get("artCategory", ""),
        "trecho": (part("Ementa") + " " + texto).strip()[:400],
        "texto": texto[:6000],
        "pagina": a.get("numberPage", ""),
        "edicao": a.get("editionNumber", ""),
        "url": a.get("pdfPage", ""),
    }


def parse_inlabs_zip(content: bytes, day: date, section: str) -> List[Dict]:
    out = []
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        for name in z.namelist():
            if name.lower().endswith(".xml"):
                act = parse_inlabs_xml(z.read(name), day, section)
                if act:
                    out.append(act)
    return out


def fetch_day_inlabs(day: date, sections: Iterable[str] = SECTIONS) -> List[Dict]:
    s = inlabs_session()
    acts, seen = [], set()
    for sec in sections:
        content = inlabs_zip(s, day, sec)
        items = parse_inlabs_zip(content, day, sec) if content else []
        for a in items:
            if a["id"] not in seen:
                seen.add(a["id"])
                acts.append(a)
        logging.info("DOU %s %s (INLABS): %d atos", day, sec, len(items))
        time.sleep(1)
    return acts


# ---------------- espelho no GitHub ----------------

MIRROR = "https://raw.githubusercontent.com/hudsonrj/scraper/dou-dados/dou"


def fetch_day_mirror(day: date) -> List[Dict]:
    """Atos já coletados pelo GitHub Actions (branch dou-dados).

    Para servidores que o in.gov.br bloqueia. Devolve [] se o dia não existe lá.
    """
    base = os.getenv("DOU_MIRROR_URL", MIRROR).rstrip("/")
    url = f"{base}/{day.isoformat()}.jsonl"
    last = None
    for i in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=60)
            if r.status_code == 404:
                return []
            if r.status_code == 200:
                r.encoding = "utf-8"
                return [json.loads(x) for x in r.text.splitlines() if x.strip()]
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = e
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"espelho do DOU no GitHub não respondeu ({url}): {last}")


# ---------------- leiturajornal ----------------

def fetch_day(day: date, sections: Iterable[str] = SECTIONS) -> List[Dict]:
    """Tudo o que saiu no DOU num dia (seções normais e extras).

    Usa o INLABS quando há credenciais; senão, a página de leitura.
    """
    source = (os.getenv("DOU_SOURCE") or "").lower()
    if source == "github":
        return fetch_day_mirror(day)
    if source == "inlabs" or (not source and inlabs_enabled()):
        return fetch_day_inlabs(day, sections)
    return fetch_day_leitura(day, sections)


def fetch_day_leitura(day: date, sections: Iterable[str] = SECTIONS) -> List[Dict]:
    acts, seen, errors = [], set(), []
    for sec in sections:
        try:
            items = fetch_section(day, sec)
        except RuntimeError as e:
            logging.warning("%s %s: %s", day, sec, e)
            errors.append(f"{sec}: {e}")
            continue
        for a in items:
            if a["id"] not in seen:
                seen.add(a["id"])
                acts.append(a)
        logging.info("DOU %s %s: %d atos", day, sec, len(items))
        time.sleep(0.5)
    if not acts and errors:
        raise RuntimeError("nenhuma seção do DOU veio: " + errors[0])
    return acts


def full_text(url: str, limit: int = 6000) -> str:
    m = _TEXT.search(_get(url))
    if not m:
        return ""
    text = clean(m.group(1)).split("Este conteúdo não substitui o publicado")[0].strip()
    return text[:limit]


def orgao_curto(orgao: str) -> str:
    parts = [p for p in orgao.split("/") if p]
    return "/".join(parts[:2])


def to_case(act: Dict, text: str = "") -> Case:
    title = act["titulo"] or act["tipo"]
    return Case(
        id=act["id"],
        title=title,
        content=text or act["trecho"],
        category=f"{SECTION_LABEL.get(act['secao'], act['secao'])} · {act['tipo']}",
        expected="",
        status="sem_rotulo",
        source="dou",
        url=act["url"],
        published_at=act["data"],
        agency=orgao_curto(act["orgao"]),
    )


def recent_weekdays(n: int, until: Optional[date] = None) -> List[date]:
    d = until or date.today()
    out = []
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def sample_cases(n: int, days: int = 10, seed: int = 7, with_text: bool = True) -> List[Case]:
    """Amostra estratificada: cada seção entra na proporção da raiz do seu volume.

    Proporção pura daria 70% de extratos de contrato (Seção 3); a raiz equilibra
    sem ignorar o que mais sai.
    """
    rng = random.Random(seed)
    pool: Dict[str, List[Dict]] = {}
    for d in recent_weekdays(days):
        for a in fetch_day(d):
            key = a["secao"].rstrip("e")
            pool.setdefault(key, []).append(a)
    if not pool:
        raise RuntimeError("Nenhum ato coletado do DOU nos dias pedidos.")
    weights = {k: len(v) ** 0.5 for k, v in pool.items()}
    total = sum(weights.values())
    picked: List[Dict] = []
    for k, acts in pool.items():
        rng.shuffle(acts)
        quota = max(1, round(n * weights[k] / total))
        # no máximo 3 atos do mesmo tipo e órgão, para variar a prova
        per = {}
        for a in acts:
            key2 = (a["tipo"], orgao_curto(a["orgao"]))
            if per.get(key2, 0) >= 3:
                continue
            per[key2] = per.get(key2, 0) + 1
            picked.append(a)
            if sum(1 for p in picked if p["secao"].rstrip("e") == k) >= quota:
                break
    rng.shuffle(picked)
    picked = picked[:n]
    cases = []
    for i, a in enumerate(picked, 1):
        text = a.get("texto", "") if with_text else ""
        if with_text and not text:
            try:
                text = full_text(a["url"])
            except RuntimeError as e:
                logging.warning("texto de %s: %s", a["id"], e)
            time.sleep(0.3)
        cases.append(to_case(a, text))
        if with_text and i % 20 == 0:
            logging.info("textos completos: %d/%d", i, len(picked))
    return cases


def parse_day(s: Optional[str]) -> date:
    if not s:
        return recent_weekdays(1, date.today() + timedelta(days=1))[0]
    return datetime.strptime(s, "%Y-%m-%d").date()
