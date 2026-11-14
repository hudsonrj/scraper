"""Clientes de modelo: Claude (SDK oficial da Anthropic) ou API compatível com a OpenAI.

O cliente OpenAI serve para OpenAI, Ollama (``http://localhost:11434/v1``), vLLM etc.
As respostas ficam em cache em disco: reavaliar um harness que já foi avaliado
não custa nada, e rodar o loop de novo com a mesma prova é barato.

Variáveis de ambiente:

    BIG_PROVIDER / BIG_MODEL / BIG_BASE_URL / BIG_API_KEY / BIG_EFFORT   modelo congelado
    EDITOR_PROVIDER / EDITOR_MODEL / EDITOR_BASE_URL / ...              editor pequeno

``*_PROVIDER`` é ``anthropic`` ou ``openai``. Sem ele: ``*_BASE_URL`` definido
→ openai (Ollama e afins); senão ``ANTHROPIC_API_KEY`` presente → anthropic;
senão openai com ``OPENAI_API_KEY``.
"""

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from typing import Callable, Optional, Protocol


class ChatModel(Protocol):
    name: str

    def complete(self, prompt: str, temperature: float = 0.0, json_mode: bool = True, prefix: str = "") -> str: ...


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
        self.client = OpenAI(api_key=key, base_url=base_url, timeout=timeout, max_retries=8)
        self.name = model
        self.base_url = base_url or "openai"
        self.cache = cache or DiskCache(None)
        self.counter = counter or CallCounter()

    def complete(self, prompt: str, temperature: float = 0.0, json_mode: bool = True, prefix: str = "") -> str:
        prompt = prefix + prompt
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


class AnthropicModel:
    """Claude pelo SDK oficial.

    A árvore temática (``prefix``) vai num bloco com cache de prompt: é igual em
    toda chamada, então só a primeira paga o preço cheio. Temperatura não existe
    nos modelos atuais; o controle é o ``effort``. Recusas de segurança caem
    para outro modelo automaticamente (``fallbacks: "default"``).
    """

    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        effort: str = "low",
        max_tokens: int = 4000,
        cache: Optional[DiskCache] = None,
        counter: Optional[CallCounter] = None,
    ):
        import anthropic

        key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError("Falta ANTHROPIC_API_KEY.")
        self.client = anthropic.Anthropic(api_key=key, max_retries=5, timeout=300.0)
        self.name = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.cache = cache or DiskCache(None)
        self.counter = counter or CallCounter()

    def complete(self, prompt: str, temperature: float = 0.0, json_mode: bool = True, prefix: str = "") -> str:
        # temperature fica só na chave do cache: o editor varia a pedida entre tentativas
        k = DiskCache.key("anthropic", self.name, self.effort, temperature, prefix, prompt)
        hit = self.cache.get(k)
        if hit is not None:
            self.counter.tick(cached=True)
            return hit
        self.counter.tick(cached=False)
        content = []
        if prefix:
            content.append({"type": "text", "text": prefix, "cache_control": {"type": "ephemeral", "ttl": "1h"}})
        content.append({"type": "text", "text": prompt})
        resp = self.client.beta.messages.create(
            model=self.name,
            max_tokens=self.max_tokens,
            output_config={"effort": self.effort},
            betas=[self.FALLBACK_BETA],
            fallbacks="default",
            messages=[{"role": "user", "content": content}],
        )
        if resp.stop_reason == "refusal":
            return ""  # conta como resposta inválida
        out = "".join(b.text for b in resp.content if b.type == "text")
        self.cache.put(k, out)
        return out


class FunctionModel:
    """Modelo de teste: uma função Python faz o papel do LLM."""

    def __init__(self, fn: Callable[[str], str], name: str = "fake"):
        self.fn = fn
        self.name = name
        self.counter = CallCounter()

    def complete(self, prompt: str, temperature: float = 0.0, json_mode: bool = True, prefix: str = "") -> str:
        self.counter.tick(cached=False)
        return self.fn(prefix + prompt)


DEFAULTS = {
    # modelo congelado: o mais capaz; editor: um modelo pequeno, como no artigo
    ("big", "anthropic"): "claude-opus-5-5",
    ("editor", "anthropic"): "claude-haiku-5-5",
    ("big", "openai"): "gpt-4o-mini",
    ("editor", "openai"): "gpt-4o-mini",
}


def provider_for(role: str) -> str:
    p = role.upper()
    explicit = (os.getenv(f"{p}_PROVIDER") or "").strip().lower()
    if explicit:
        return explicit
    if os.getenv(f"{p}_BASE_URL"):
        return "openai"
    if os.getenv(f"{p}_API_KEY", "").startswith("sk-ant-") or os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "openai"


def from_env(role: str, cache: DiskCache, counter: CallCounter, default_model: Optional[str] = None):
    p = role.upper()
    provider = provider_for(role)
    model = os.getenv(f"{p}_MODEL") or default_model or DEFAULTS[(role, provider)]
    if provider == "anthropic":
        return AnthropicModel(
            model=model,
            api_key=os.getenv(f"{p}_API_KEY") or None,
            effort=os.getenv(f"{p}_EFFORT", "low" if role == "big" else "medium"),
            max_tokens=4000 if role == "big" else 16000,
            cache=cache,
            counter=counter,
        )
    return OpenAICompatible(
        model=model,
        base_url=os.getenv(f"{p}_BASE_URL") or None,
        api_key=os.getenv(f"{p}_API_KEY") or None,
        cache=cache,
        counter=counter,
    )
