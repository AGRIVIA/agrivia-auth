# ===============================================================
# CONFERÊNCIA DE ASSINATURAS DUPLICADAS (SÓ LEITURA!)
# ---------------------------------------------------------------
# Identifica a assinatura ÓRFÃ do caso da cobrança duplicada
# (cliente Luis, 08/07/2026). NÃO altera NADA na Asaas — apenas
# lista as assinaturas e cobranças, e diz qual cancelar/estornar
# pelo painel visual da Asaas.
#
# COMO USAR (Windows):
#   1) Abra a pasta agrivia-auth no Explorer
#   2) Clique na barra de endereço da pasta, digite:  cmd   (e Enter)
#   3) No terminal preto, digite:  python conferir_assinaturas.py
#   4) Cole a chave de PRODUÇÃO da Asaas quando pedir
#      (por segurança ela NÃO aparece na tela ao colar — é normal)
# ===============================================================
import os
import json
import getpass
import urllib.request
import urllib.error

BASE = "https://api.asaas.com/v3"

# Se existir um arquivo 'chave_producao.txt' nesta pasta, a chave é lida
# dele (e o arquivo deve ser APAGADO depois da conferência). Senão, o
# script pede a chave no terminal. O .gitignore impede que esse arquivo
# suba para o GitHub.
_ARQ_CHAVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chave_producao.txt")


def _obter_chave():
    if os.path.exists(_ARQ_CHAVE):
        with open(_ARQ_CHAVE, encoding="utf-8-sig") as f:
            chave = f.read().strip()
        if chave:
            print("(chave lida do arquivo chave_producao.txt — apague-o depois!)")
            return chave
    import sys
    if not sys.stdin.isatty():
        # Sem terminal interativo não dá pra digitar a chave — avisa e sai
        # em vez de ficar travado esperando um teclado que não existe.
        raise SystemExit("ERRO: arquivo chave_producao.txt nao encontrado na pasta do script.")
    return getpass.getpass("Cole a chave de PRODUCAO da Asaas e dê Enter: ").strip()

# Dados do caso:
CUSTOMER_ID = "cus_000186022736"       # cliente Luis na Asaas
SUB_CORRETA = "sub_fv3msa66dgte55i4"   # a que o painel AGRIVIA usa -> MANTER


def api_get(path, chave):
    req = urllib.request.Request(
        BASE + path,
        headers={"access_token": chave, "User-Agent": "agrivia-conferencia"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    print("=" * 62)
    print("CONFERENCIA DE ASSINATURAS — SO LEITURA (nao altera nada)")
    print("=" * 62)
    chave = _obter_chave()
    if chave and not chave.startswith("$"):
        chave = "$" + chave  # recoloca o $ se você colou a versão sem ele

    subs = api_get(f"/subscriptions?customer={CUSTOMER_ID}&limit=50", chave).get("data", [])
    if not subs:
        print(f"\nNenhuma assinatura encontrada para o cliente {CUSTOMER_ID}.")
        return

    print(f"\nO cliente {CUSTOMER_ID} tem {len(subs)} assinatura(s):\n")
    fatura_para_estornar = None

    for s in subs:
        sid = s.get("id")
        eh_correta = (sid == SUB_CORRETA)
        print("-" * 62)
        print(f"  Assinatura: {sid}")
        print(f"  Status:     {s.get('status')}")
        print(f"  Valor:      R$ {s.get('value')}")
        print(f"  Criada em:  {s.get('dateCreated')}")
        print(f"  Prox. venc: {s.get('nextDueDate')}")
        if eh_correta:
            print("  ==> ✅ MANTER — e a assinatura que o painel AGRIVIA usa.")
        else:
            print("  ==> 🔴 ORFA — CANCELE esta assinatura no painel da Asaas!")

        try:
            pays = api_get(f"/subscriptions/{sid}/payments?limit=20", chave).get("data", [])
        except urllib.error.HTTPError:
            pays = []
        for p in pays:
            print(f"      Cobranca: fatura n. {p.get('invoiceNumber')} | R$ {p.get('value')}"
                  f" | status {p.get('status')} | venc. {p.get('dueDate')}")
        if not eh_correta and pays:
            fatura_para_estornar = pays[0].get("invoiceNumber")

    print("-" * 62)
    print("\nRESUMO DO QUE FAZER NO PAINEL DA ASAAS:")
    print("  1) Cancelar a assinatura marcada como 🔴 ORFA acima.")
    if fatura_para_estornar:
        print(f"  2) Estornar a cobranca de fatura n. {fatura_para_estornar}")
        print("     (Cobrancas -> abrir a de fatura n. acima -> menu ⋮ -> Estornar).")
    else:
        print("  2) Estornar a cobranca ligada a assinatura orfa.")
    print("\nEste script NAO alterou nada — tudo e feito por voce no painel.")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        corpo = e.read().decode("utf-8", "ignore")[:300]
        print(f"\nErro da Asaas (HTTP {e.code}): {corpo}")
        print("Dica: confira se colou a chave de PRODUCAO completa.")
    except Exception as e:
        print("\nErro:", e)
