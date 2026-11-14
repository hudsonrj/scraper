# Execução do loop de harness

- Casos: 108 no lado tarefa, 96 held-out
- Held-out: 70.8% (v0) → 78.1% (v4)
- Tarefa: 60.8% (v0) → 65.4%
- Respostas inválidas no held-out na v0: 0
- Fim: 4 rodadas concluídas

| rodada | falhas | melhor edição | held-out | resultado |
|---|---|---|---|---|
| 1 | 45 | + regra: Se a notícia trata de sustentabilidade urbana ou urbanismo, prefira 0… | 73.6% | mantida |
| 2 | 45 | + regra: Debates ou auditorias de segurança operacional no setor de petróleo e… | 75.3% | mantida |
| 3 | 44 | + regra: Para notícias sobre atos normativos (portaria, resolução, decreto), s… | 76.7% | mantida |
| 4 | 42 | + regra: Se a notícia mencionar "diversidade", "igualdade de gênero" ou "equid… | 78.1% | mantida |

## Próximo passo

Revise `final.yaml`. Se fizer sentido, promova com:

```bash
poetry run python src/run_harness_loop.py promote <esta-pasta>/final.yaml
```
