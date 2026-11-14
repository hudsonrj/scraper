"""Loop que reescreve o harness do classificador de temas.

O modelo grande fica congelado. O que muda a cada rodada é o arquivo de
harness (instruções, regras e exemplos). O humano escreve só a prova:
os casos rotulados em ``harness/exam/cases.jsonl``.
"""
