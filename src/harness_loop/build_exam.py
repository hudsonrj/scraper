"""Monta uma prova inicial a partir do dataset público no Hugging Face.

Os rótulos vêm do Cogfy e entram como ``provisorio``. Eles só passam a contar
para a nota depois que uma pessoa revisa (comando ``review``).
"""

import logging
import random
import re
import time
from typing import Dict, List, Optional

import requests

from .exam import Case
from .taxonomy import Taxonomy

API = "https://datasets-server.huggingface.co"
DATASET = "nitaibezerra/govbrnews"
_CODE = re.compile(r"^\s*(\d{2}(?:\.\d{2}){0,2})")


def _get(path: str, params: Dict, tries: int = 4) -> Dict:
    last = None
    for i in range(tries):
        try:
            r = requests.get(f"{API}/{path}", params=params, timeout=60)
            data = r.json()
            if r.status_code == 200 and "error" not in data:
                return data
            last = data.get("error") or r.status_code
        except (requests.RequestException, ValueError) as e:
            last = e
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"Hugging Face respondeu com erro: {last}")


def cogfy_code(row: Dict, tax: Taxonomy) -> Optional[str]:
    for key in ("most_specific_theme_code", "theme_1_level_3_code", "theme_1_level_2_code", "theme_1_level_1_code", "theme_1_level_1"):
        v = row.get(key)
        if not v:
            continue
        m = _CODE.match(str(v))
        if m and tax.exists(m.group(1)):
            return m.group(1)
    return None


def fetch(n: int, tax: Taxonomy, seed: int = 7, batch: int = 10, min_content: int = 300) -> List[Case]:
    """Sorteia ``n`` notícias com tema do Cogfy, espalhadas pelo dataset inteiro."""
    total = _get("size", {"dataset": DATASET})["size"]["dataset"]["num_rows"]
    rng = random.Random(seed)
    out: Dict[str, Case] = {}
    tries = 0
    while len(out) < n and tries < n * 3:
        tries += 1
        off = rng.randrange(0, max(1, total - batch))
        data = _get("rows", {"dataset": DATASET, "config": "default", "split": "train", "offset": off, "length": batch})
        rows = [r["row"] for r in data.get("rows", [])]
        rng.shuffle(rows)
        for row in rows[:2]:  # no máximo 2 por lote, para não concentrar num órgão
            code = cogfy_code(row, tax)
            content = (row.get("content") or "").strip()
            uid = row.get("unique_id")
            if not code or not uid or uid in out or len(content) < min_content:
                continue
            out[uid] = Case(
                id=uid,
                title=(row.get("title") or "").strip(),
                content=content[:4000],
                category=(row.get("category") or "") or "",
                expected=code,
                status="provisorio",
                source="cogfy",
                url=row.get("url") or "",
                published_at=str(row.get("published_at") or "")[:10],
                agency=row.get("agency") or "",
            )
            if len(out) >= n:
                break
        logging.info("prova: %d/%d notícias", len(out), n)
    return list(out.values())
