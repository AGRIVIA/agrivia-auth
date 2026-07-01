# AGRIVIA — Módulo de Assinaturas (Asaas)

Documentação de operação do módulo de **assinaturas recorrentes** do AGRIVIA,
que vive no servidor **`agrivia-auth`** (FastAPI + Postgres, publicado no Railway).
Pagamento por **cartão de crédito** com renovação automática, via **Asaas**.

> Linguagem direta, passo a passo. Se algo aqui divergir do sistema, o que vale é
> o comportamento real no painel/Asaas — teste sempre no **sandbox** primeiro.

---

## 1. Visão geral (como funciona)

1. **Cadastro / convite:** você cria o cliente no painel admin (ou ele é convidado).
   O cliente recebe um e-mail e abre o link `…/confirmar?token=…`.
2. **Onboarding na web:** o cliente aceita os Termos → escolhe o plano → informa o
   cartão. O cartão é **tokenizado** na Asaas e **descartado** — o servidor guarda
   **só o token** + os IDs da Asaas (nunca o número do cartão).
3. **Acesso:** o app desktop, no login, pergunta ao servidor se o acesso está liberado.
4. **Renovação automática:** a Asaas cobra o cartão a cada ciclo e avisa o servidor
   por **webhook**, que atualiza o status da assinatura sozinho.

### Como o ACESSO é decidido (no login)

Ordem de decisão (arquivo `assinatura_service.acesso_liberado`):

1. Usuário com `status = bloqueado` → **negado**.
2. **Override manual** do admin na assinatura:
   - `bloqueado_manual` → **negado** (ignora a Asaas).
   - `liberado_manual` → **liberado** (com prazo opcional `controle_ate`).
3. **Automático** (vem da Asaas/webhook): status `active`/`trial` → **liberado**;
   `pending_payment`/`overdue`/`suspended`/`cancelled` → **negado**.
4. Cliente **sem assinatura** nenhuma = **grandfathered** (cliente antigo): segue o
   `status` do usuário (`ativo` = liberado). É o caso de quem usava o sistema antes
   das assinaturas e de quem foi "desvinculado".

---

## 2. Variáveis de ambiente (no Railway)

| Variável | Para que serve | Sandbox | Produção |
|---|---|---|---|
| `ASAAS_API_KEY` | Chave da Asaas | `aact_hmlg_...` | `aact_prod_...` |
| `ASAAS_ENVIRONMENT` | Qual ambiente | `sandbox` | `producao` |
| `ASAAS_BASE_URL` | (opcional) URL da API | deduzido | deduzido |
| `ASAAS_WEBHOOK_TOKEN` | Senha do webhook (≥ 32 caracteres) | igual ao painel Asaas | igual ao painel Asaas |
| `PUBLIC_BASE_URL` | Endereço público do servidor (monta os links) | mesmo | mesmo |
| `RESEND_API_KEY` / `EMAIL_REMETENTE` | Envio de e-mails | mesmo | mesmo |
| `DATABASE_URL` | Banco Postgres | (Railway) | (Railway) |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | Login do admin | mesmo | mesmo |

### 🔴 ATENÇÃO — o pulo do gato da chave (`$`)

A chave da Asaas, no painel deles, começa com **`$`** (ex.: `$aact_prod_...`).
**O Railway "come" esse `$` inicial** (acha que é nome de variável) e a chave vira
**vazia**. Por isso:

> **Guarde a chave no Railway SEM o `$`** (começando em `aact_...`).
> O código (`asaas/config.py`) recoloca o `$` automaticamente.

Vale **igual para a chave de produção**. Sintoma de que esqueceu: nos logs aparece
`key_len=0` e a Asaas responde "access_token obrigatório".

---

## 3. Operando o painel admin

Acesse `…/admin/login`. No topo há três áreas: **👥 Usuários**, **💳 Assinaturas**
e **🏷️ Planos**.

### 💳 Assinaturas → Gerenciar (tela do cliente)

- **🔗 Gerar link de assinatura:** cria um link pessoal (vale 7 dias) para o cliente
  escolher o plano e informar o cartão. Pode enviar por e-mail e/ou copiar para o
  WhatsApp. Use para **clientes atuais migrarem** e para **teste de negociação**.
  > Não gere link para quem já tem assinatura **ativa** na Asaas (criaria uma 2ª
  > assinatura e cobraria em dobro).
- **Alterar plano:** troca o plano; se houver assinatura na Asaas, ajusta lá também.
- **Alterar valor:** muda só o valor cobrado (mantém o plano).
- **🔒 Travar valor (promoção):** quando travado, o **reajuste em massa** do plano
  **pula** este cliente e mantém o valor atual.
- **Controle de acesso (manual):** **Liberar** (com prazo opcional), **Bloquear** na
  hora, ou **Voltar ao automático** (segue a Asaas de novo).
- **🔄 Sincronizar com a Asaas:** puxa o estado atual (assinatura + última cobrança).
  Use quando achar que um webhook foi perdido.
- **✖ Cancelar assinatura:** cancela na Asaas; o cliente deixa de ser cobrado.
- **🔌 Desvincular da Asaas:** apaga os IDs/token e o histórico salvos **aqui** (não
  cobra nem cancela na Asaas). O cliente **mantém o acesso** e pode **assinar do zero**.
  Use para **limpar testes do sandbox** ou recomeçar. Se houver assinatura **ativa**
  de verdade, **cancele antes**.
- **Histórico de eventos:** cada webhook recebido da Asaas, com data e payload.

### 🏷️ Planos → Reajuste em massa

Fluxo em 3 passos (seguro): **lista** → **Revisar** (mostra de→para e a lista dos
clientes afetados) → **Confirmar** (aplica e mostra o relatório).

- Sobe o valor do plano (para **novos** clientes) **e** reajusta todas as assinaturas
  atuais daquele plano na Asaas.
- Clientes com **🔒 valor travado** são **pulados** (mantêm o valor antigo).
- O relatório separa: ✅ atualizadas na Asaas · ⚠️ falhas (com motivo) · 🟡 só local
  · 🔒 mantidas (travadas).

> **Dica:** trave o cliente da promoção **antes** de rodar o reajuste em massa.

---

## 4. Quando a nova cobrança acontece (reajuste)

O reajuste usa `PUT /subscriptions/{id}` com `updatePendingPayments=true`:

- O **novo valor vale na próxima cobrança** (próximo vencimento). Não cobra na hora
  nem a diferença do período já pago.
- **Cobrança já paga não muda.**
- Cobrança **já gerada e em aberto** (não paga/vencida) é ajustada para o novo valor.

---

## 5. 🚀 Virar do SANDBOX para a PRODUÇÃO (passo a passo)

> Faça **só quando decidir lançar de verdade**. Teste tudo no sandbox antes (seção 6).

1. **Pegue a chave de PRODUÇÃO** no painel da Asaas (conta de produção →
   Configurações → Integração → API). Ela começa com `$aact_prod_...`.
2. **No Railway**, ajuste as variáveis:
   - `ASAAS_API_KEY` = a chave de produção **SEM o `$`** (começando em `aact_prod_...`).
   - `ASAAS_ENVIRONMENT` = `producao`.
   - (não precisa mexer em `ASAAS_BASE_URL` — é deduzido.)
3. **Configure o WEBHOOK no painel da Asaas de PRODUÇÃO:**
   - URL: `https://SEU-ENDERECO/webhooks/asaas`
   - Token de autenticação: **≥ 32 caracteres** (pode reusar o do sandbox ou gerar
     outro). Coloque **o mesmo** valor em `ASAAS_WEBHOOK_TOKEN` no Railway.
   - Tipo **Sequencial**, fila **ativada**, eventos de **cobrança** e **assinatura**.
4. **Limpe os testes do sandbox** (importante — ver seção 5.1).
5. **Deploy** (commit + push pelo GitHub Desktop). Nos **logs do Railway**, confira:
   `[asaas] configurado=True | ambiente=producao | base=https://api.asaas.com/v3 | key_len=...`
   (o `key_len` tem que ser **maior que 0**).
6. **Teste com um cartão real seu** (ou de valor baixo): gere um link, assine, veja a
   assinatura aparecer no painel da Asaas de produção. Cancele/estorne depois se quiser.
7. Pronto: passe a mandar os **links de assinatura reais** para os clientes.

### 5.1 O que fazer com as assinaturas de TESTE

As assinaturas criadas no **sandbox** têm IDs e token que **não existem em produção**
(a Asaas separa 100% os ambientes). Se você não limpar, um cliente que testou no
sandbox **não consegue** assinar em produção (o sistema tentaria reusar o ID antigo).

**Antes de operar em produção**, em cada cliente de teste:
**💳 Assinaturas → Gerenciar → 🔌 Desvincular da Asaas.**
Isso limpa o vínculo; o cliente mantém o acesso (grandfathered) e assina do zero,
agora em produção.

---

## 6. ✅ Checklist de testes (no sandbox, antes de virar)

- [ ] **Onboarding completo:** criar cliente → e-mail → `/confirmar` (aceitar termos)
      → `/assinar` (escolher plano) → cartão de teste → "assinatura ativa" → o login
      no desktop libera o acesso.
- [ ] **Webhook:** ao confirmar o pagamento na Asaas, o status vira `active`; ao
      vencer/recusar, o login passa a **negar** com a mensagem certa.
- [ ] **Alterar plano** e **alterar valor** → conferir o novo valor na Asaas.
- [ ] **Cancelar** → status `cancelled`, login negado.
- [ ] **Sincronizar** → puxa o estado certo da Asaas.
- [ ] **Controle manual:** Bloquear (login nega), Liberar com prazo (libera), Voltar
      ao automático.
- [ ] **Gerar link** (com e sem e-mail) e concluir uma assinatura por ele.
- [ ] **Travar valor + reajuste em massa:** o travado mantém o valor; os demais sobem.
- [ ] **Desvincular** → some o vínculo; o cliente vira "sem assinatura" e mantém acesso.

> **Cartão de teste do sandbox que funciona:** `5162306219378829`, validade `12/2028`,
> CVV `123`, CPF `24971563792`.

---

## 7. 🛠️ Troubleshooting (problemas comuns)

| Sintoma | Causa provável | Solução |
|---|---|---|
| Log `key_len=0` / "access_token obrigatório" | A chave está com o `$` no Railway | Tire o `$` (começar em `aact_...`) |
| Webhook responde **401** | Token do Railway ≠ token no painel Asaas | Iguale os dois (≥ 32 caracteres) |
| Erro ao assinar em produção um cliente que testou no sandbox | IDs do sandbox não valem em produção | **🔌 Desvincular** o cliente e reassinar |
| Cartão recusado | Recusa real do cartão | A mensagem vem da Asaas; conferir dados/cartão |
| Cobra em dobro | Gerou link para quem já tinha assinatura ativa | Só gere link para quem **não** tem assinatura ativa |
| Reajuste em massa lento / cai | Muitos clientes (é síncrono, 1 por vez) | Rodar em horário tranquilo; futuro: processo em 2º plano |
| Cliente novo bloqueado no login | Assinatura `pending_payment` (não pagou) | Concluir a assinatura, ou Liberar manual |

---

## 8. Segurança (o que o sistema garante)

- **Nunca** salva/loga número do cartão, CVV, validade ou a chave da API.
- Guarda **apenas** o token + `customer_id` + `subscription_id`.
- O webhook só é aceito com o **token secreto** (`ASAAS_WEBHOOK_TOKEN`).
- O payload do webhook é **sanitizado** (remove qualquer dado de cartão) antes de
  ser guardado no histórico.
- A chave de **produção** vive **só no Railway** (nunca no código/Git).

---

## 9. Mapa dos arquivos (para o desenvolvedor)

- `asaas/config.py` — lê as variáveis; recoloca o `$` da chave.
- `asaas/asaas_client.py` — só fala HTTP com a Asaas (sem regra de negócio).
- `assinatura_service.py` — regra de negócio (acesso, onboarding, ações do admin,
  reajuste em massa, webhook).
- `planos_config.py` — valores iniciais dos planos (depois editados no painel).
- `models.py` — tabelas `Plano`, `Assinatura`, `AsaasEvento`.
- `main.py` — onboarding web (`/confirmar`, `/assinar`, `/assinar/cartao`), webhook
  (`/webhooks/asaas`), login (`/api/login`) e as migrações de coluna.
- `admin/admin_web.py` + `admin/templates/` — painel admin (Usuários, Assinaturas,
  Planos).
