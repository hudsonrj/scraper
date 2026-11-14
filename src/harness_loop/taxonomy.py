"""Árvore temática de três níveis usada pelo classificador."""

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml

DEFAULT_TREE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "enrichment",
    "themes_tree.yaml",
)

_ENTRY = re.compile(r"^\s*(\d{2}(?:\.\d{2}){0,2})\s*-\s*(.+?)\s*$")


def _split(entry: str):
    m = _ENTRY.match(str(entry))
    if not m:
        raise ValueError(f"Entrada da árvore sem código: {entry!r}")
    return m.group(1), m.group(2)


@dataclass
class Taxonomy:
    labels: Dict[str, str] = field(default_factory=dict)
    children: Dict[str, List[str]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str = DEFAULT_TREE) -> "Taxonomy":
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        tax = cls()
        for l1, l2s in (raw or {}).items():
            c1, n1 = _split(l1)
            tax.labels[c1] = n1
            tax.children[c1] = []
            if isinstance(l2s, dict):
                items = l2s.items()
            else:
                items = [(x, None) for x in (l2s or [])]
            for l2, l3s in items:
                c2, n2 = _split(l2)
                tax.labels[c2] = n2
                tax.children[c1].append(c2)
                tax.children[c2] = []
                for l3 in l3s or []:
                    c3, n3 = _split(l3)
                    tax.labels[c3] = n3
                    tax.children[c2].append(c3)
        return tax

    def exists(self, code: Optional[str]) -> bool:
        return bool(code) and code in self.labels

    def label(self, code: Optional[str]) -> str:
        if not code:
            return "—"
        return f"{code} - {self.labels.get(code, '?')}"

    def render(self) -> str:
        """Árvore em texto compacto para o prompt."""
        lines = []
        for c1 in sorted(c for c in self.labels if len(c) == 2):
            lines.append(f"{c1} {self.labels[c1]}")
            for c2 in self.children.get(c1, []):
                lines.append(f"  {c2} {self.labels[c2]}")
                for c3 in self.children.get(c2, []):
                    lines.append(f"    {c3} {self.labels[c3]}")
        return "\n".join(lines)


def level(code: str) -> int:
    return code.count(".") + 1 if code else 0


def prefix(code: str, lvl: int) -> str:
    return ".".join(code.split(".")[:lvl])
