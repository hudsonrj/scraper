"""Coleta do Diário Oficial da União (in.gov.br): todas as seções de um dia.

A página ``leiturajornal`` de cada seção traz os atos num JSON embutido
(``<script id="params">``): título, tipo do ato, órgão, trecho do texto e o
endereço do ato. O texto completo fica em ``/web/dou/-/<urlTitle>``.
"""

import html
import json
import logging
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
        return []
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


def fetch_day(day: date, sections: Iterable[str] = SECTIONS) -> List[Dict]:
    """Tudo o que saiu no DOU num dia (seções normais e extras)."""
    acts, seen = [], set()
    for sec in sections:
        try:
            items = fetch_section(day, sec)
        except RuntimeError as e:
            logging.warning("%s %s: %s", day, sec, e)
            continue
        for a in items:
            if a["id"] not in seen:
                seen.add(a["id"])
                acts.append(a)
        logging.info("DOU %s %s: %d atos", day, sec, len(items))
        time.sleep(0.5)
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
        text = ""
        if with_text:
            try:
                text = full_text(a["url"])
            except RuntimeError as e:
                logging.warning("texto de %s: %s", a["id"], e)
            time.sleep(0.3)
        cases.append(to_case(a, text))
        if i % 20 == 0:
            logging.info("textos completos: %d/%d", i, len(picked))
    return cases


def parse_day(s: Optional[str]) -> date:
    if not s:
        return recent_weekdays(1, date.today() + timedelta(days=1))[0]
    return datetime.strptime(s, "%Y-%m-%d").date()
