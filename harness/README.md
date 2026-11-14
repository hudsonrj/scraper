# Loop de harness do classificador de temas

Aplicação das ideias de "O Fim da Engenharia de Harness": o modelo grande fica
congelado, um modelo pequeno reescreve o harness a cada rodada, e o trabalho
humano é escrever a prova.

| Peça | Onde | Quem escreve |
|---|---|---|
| Prova (casos rotulados) | `harness/exam/cases.jsonl` | humano (`review` ou `/revisar`) |
| Harness ativo | `harness/active.yaml` | v0 à mão; depois o loop, com promoção humana |
| Execuções | `harness/runs/<data>/` | o loop |
| Código | `src/harness_loop/`, `src/run_harness_loop.py` | — |

## Uma rodada

1. O modelo congelado classifica o lado **tarefa** com o harness atual; os erros são as falhas.
2. O editor lê só essas falhas e propõe 8 edições (regras, exemplos, instruções).
3. Cada edição é avaliada no lado **held-out**, que o editor nunca vê.
4. Fica a melhor, se subir a nota held-out (empate: decide a nota da tarefa)
   e se não aumentar respostas inválidas (porteira de segurança).

Travas: o formato de saída (`contrato`) e o modelo não podem ser editados;
exemplos só vêm do lado tarefa; texto que cite manchete do held-out é recusado;
nenhuma versão entra em produção sem `promote`.

## Nota

Crédito hierárquico pelo tema principal: se a prova diz `05.02.01`, acertar
`05.02.07` vale 2/3 e acertar só `05` vale 1/3. Casos rotulados só no nível 1
valem 1 se o nível 1 bater.

## Uso

```bash
poetry run python src/run_harness_loop.py check            # estado da prova
poetry run python src/run_harness_loop.py build --n 200    # baixa notícias com rótulo provisório do Cogfy
poetry run python src/run_harness_loop.py review           # revisão humana no terminal
poetry run python src/run_harness_loop.py eval -v          # nota do harness ativo
poetry run python src/run_harness_loop.py loop --rounds 4 --max-calls 6000
poetry run python src/run_harness_loop.py promote harness/runs/<data>/final.yaml
```

Modelos: qualquer API compatível com a OpenAI.

```bash
OPENAI_API_KEY=...            # padrão: gpt-4o-mini para os dois papéis
BIG_MODEL=gpt-4o              # modelo congelado
EDITOR_MODEL=qwen3:4b EDITOR_BASE_URL=http://localhost:11434/v1   # editor local no Ollama
```

As respostas ficam em cache (`harness/.cache/`), então reavaliar não custa de novo.

A prova inicial tem 4 casos escritos à mão (de `tests/test_cases.yaml`) e 200
notícias com rótulo provisório do Cogfy. Provisórios não contam até alguém
revisar; com `--allow-provisional` contam, mas aí o loop aprende a imitar o Cogfy.

## Servidor

`deploy/install.sh` instala o site (página explicativa, painel e revisão pelo
navegador) com systemd e Caddy. Os dados ficam em `/root/apps/harnessend/data`,
fora do git.
