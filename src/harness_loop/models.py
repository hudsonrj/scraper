"""Clientes de modelo: um endpoint compatível com a API da OpenAI.

Serve para OpenAI, Ollama (``http://localhost:11434/v1``), vLLM, LM Studio etc.
As respostas ficam em cache em disco: reavaliar um harness que já foi avaliado
não custa nada, e rodar o loop de novo com a mesma prova é barato.

Variáveis de ambiente:

    BIG_MODEL / BIG_BASE_URL / BIG_API_KEY          modelo congelado
    EDITOR_MODEL / EDITOR_BASE_URL / EDITOR_API_KEY editor pequeno

Sem ``*_API_KEY`` usa ``OPENAI_API_KEY``. Sem ``*_BASE_URL`` usa a OpenAI.
"""

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from typing import Callable, Optional, Protocol


class ChatModel(Protocol):
    name: str

    def complete(self, prompt: str, temperature: float = 0.0, json_mode: bool = True) -> str: ...


class DiskCache:
    def __init__(self, path: Optional[str]):
        self.path = path
        self.lock = threading.Lock()
        self.data = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                        self.data[row["k"]] = row["v"]
                    except (json.JSONDecodeError, KeyError):
                        continue

    @staticmethod
    def key(*parts) -> str:
        raw = json.dumps(parts, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, k: str) -> Optional[str]:
        return self.data.get(k)

    def put(self, k: str, v: str) -> None:
        with self.lock:
            self.data[k] = v
            if self.path:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"k": k, "v": v}, ensure_ascii=False) + "\n")


@dataclass
class CallCounter:
    calls: int = 0
    cached: int = 0
    limit: Optional[int] = None

    def __post_init__(self):
        self.lock = threading.Lock()

    def tick(self, cached: bool) -> None:
        with self.lock:
            if cached:
                self.cached += 1
                return
            if self.limit is not None and self.calls >= self.limit:
                raise BudgetExceeded(f"limite de {self.limit} chamadas atingido")
            self.calls += 1


class BudgetExceeded(RuntimeError):
    pass


class OpenAICompatible:
    def __init__(
        self,
        model: str,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        cache: Optional[DiskCache] = None,
        counter: Optional[CallCounter] = None,
        timeout: float = 120.0,
    ):
        from openai import OpenAI  # import tardio: os testes não precisam do pacote

        key = api_key or os.getenv("OPENAI_API_KEY")
        if not key and base_url:
            key = "local"  # Ollama e afins ignoram a chave
        if not key:
            raise ValueError(
                "Falta a chave da API. Defina OPENAI_API_KEY (ou BIG_API_KEY / EDITOR_API_KEY), "
                "ou aponte *_BASE_URL para um servidor local como o Ollama."
            )
        self.client = OpenAI(api_key=key, base_url=base_url, timeout=timeout, max_retries=4)
        self.name = model
        self.base_url = base_url or "openai"
        self.cache = cache or DiskCache(None)
        self.counter = counter or CallCounter()

    def complete(self, prompt: str, temperature: float = 0.0, json_mode: bool = True) -> str:
        k = DiskCache.key(self.base_url, self.name, temperature, json_mode, prompt)
        hit = self.cache.get(k)
        if hit is not None:
            self.counter.tick(cached=True)
            return hit
        self.counter.tick(cached=False)
        kwargs = {}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self.client.chat.completions.create(
            model=self.name,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        out = resp.choices[0].message.content or ""
        self.cache.put(k, out)
        return out


class FunctionModel:
    """Modelo de teste: uma função Python faz o papel do LLM."""

    def __init__(self, fn: Callable[[str], str], name: str = "fake"):
        self.fn = fn
        self.name = name
        self.counter = CallCounter()

    def complete(self, prompt: str, temperature: float = 0.0, json_mode: bool = True) -> str:
        self.counter.tick(cached=False)
        return self.fn(prompt)


def from_env(role: str, cache: DiskCache, counter: CallCounter, default_model: str) -> OpenAICompatible:
    p = role.upper()
    return OpenAICompatible(
        model=os.getenv(f"{p}_MODEL", default_model),
        base_url=os.getenv(f"{p}_BASE_URL") or None,
        api_key=os.getenv(f"{p}_API_KEY") or None,
        cache=cache,
        counter=counter,
    )
