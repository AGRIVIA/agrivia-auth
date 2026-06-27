# ===============================================================
# CONFIGURAÇÃO DOS DOCUMENTOS LEGAIS (Termos + Política)
# ---------------------------------------------------------------
# Quando você ATUALIZAR os documentos no site, suba a VERSÃO aqui
# (ou pela variável de ambiente no Railway). O sistema passa a
# considerar a versão nova e — se a checagem no login estiver
# ligada — pede um novo aceite de quem só aceitou a versão antiga.
# ===============================================================
import os

TERMOS_URL = os.getenv("TERMOS_URL", "https://agrivia.com.br/termos-de-uso")
POLITICA_URL = os.getenv("POLITICA_URL", "https://agrivia.com.br/politica-de-privacidade")

TERMOS_VERSAO = os.getenv("TERMOS_VERSAO", "1.0")
POLITICA_VERSAO = os.getenv("POLITICA_VERSAO", "1.0")

TERMOS_ATUALIZADO_EM = os.getenv("TERMOS_ATUALIZADO_EM", "26/06/2026")
