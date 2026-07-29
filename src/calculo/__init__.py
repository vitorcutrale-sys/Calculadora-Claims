"""
Pacote de cálculo de claims (Fase 5).

Motor de cálculo do saldo de precatórios ao longo do tempo, organizado em:
- modelo.py  : estruturas de input (premissas do ativo) e regras de datas
- perfis.py  : receitas declarativas por tipo de ativo (quais índices/regimes)
- (próximos) : indices.py, motor.py, memoria.py

Toda aritmética financeira usa Decimal — nunca float — para não acumular
erro de ponto flutuante em séries longas de fatores.
"""
