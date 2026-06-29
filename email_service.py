# email_service.py
# -------------------------------------------------------------------
# Envio de e-mails do AGRIVIA via Resend (API HTTP).
# Usa as variáveis de ambiente do Railway:
#   RESEND_API_KEY   -> chave secreta da conta Resend (re_...)
#   EMAIL_REMETENTE  -> remetente exibido, ex.: AGRIVIA <nao-responda@agrivia.com.br>
# Se a chave não estiver definida, apenas registra no log e devolve False
# (assim o sistema nunca quebra por causa do e-mail).
# -------------------------------------------------------------------
import os
import requests

RESEND_API_URL = "https://api.resend.com/emails"


def _remetente() -> str:
    return os.getenv(
        "EMAIL_REMETENTE",
        "AGRIVIA <nao-responda@agrivia.com.br>"
    )


def enviar_email(destino: str, assunto: str, html: str) -> bool:
    """Envia um e-mail HTML. Retorna True se o Resend aceitou, False caso contrário."""
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        print("[email] RESEND_API_KEY nao definido; e-mail NAO enviado.")
        return False

    try:
        resp = requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": _remetente(),
                "to": [destino],
                "subject": assunto,
                "html": html,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f"[email] enviado para {destino} | assunto: {assunto}")
            return True
        print(f"[email] FALHOU para {destino} | {resp.status_code} | {resp.text}")
        return False
    except Exception as e:
        print(f"[email] ERRO ao enviar para {destino}: {e}")
        return False


def _html_confirmacao(nome: str, link: str) -> str:
    """Monta o corpo HTML do e-mail de confirmação (visual AGRIVIA)."""
    saudacao = f"Olá, {nome}!" if nome else "Olá!"
    return f"""<!doctype html>
<html lang="pt-br">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0; padding:0; background:#eef2e8; font-family:Arial, Helvetica, sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef2e8; padding:24px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="520" cellpadding="0" cellspacing="0" style="max-width:520px; width:100%; background:#ffffff; border-radius:14px; overflow:hidden; box-shadow:0 6px 18px rgba(0,0,0,0.08);">
          <!-- Cabeçalho -->
          <tr>
            <td style="background:#476126; padding:24px 32px;">
              <span style="color:#ffffff; font-size:24px; font-weight:bold; letter-spacing:1px;">AGRIVIA</span>
            </td>
          </tr>
          <!-- Corpo -->
          <tr>
            <td style="padding:32px;">
              <h1 style="color:#2b3a22; font-size:20px; margin:0 0 16px 0;">{saudacao}</h1>
              <p style="color:#3a4a30; font-size:15px; line-height:1.6; margin:0 0 18px 0;">
                Sua conta no <b>AGRIVIA</b> foi criada. Para ativá-la e poder entrar no sistema,
                confirme seu e-mail clicando no botão abaixo:
              </p>
              <table role="presentation" cellpadding="0" cellspacing="0" style="margin:24px 0;">
                <tr>
                  <td align="center" style="background:#476126; border-radius:8px;">
                    <a href="{link}" style="display:inline-block; padding:14px 28px; color:#ffffff; font-size:16px; font-weight:bold; text-decoration:none;">
                      Confirmar meu e-mail
                    </a>
                  </td>
                </tr>
              </table>
              <p style="color:#6a7a5e; font-size:13px; line-height:1.6; margin:18px 0 0 0;">
                Se o botão não funcionar, copie e cole este endereço no seu navegador:
              </p>
              <p style="color:#476126; font-size:13px; word-break:break-all; margin:6px 0 0 0;">{link}</p>
              <p style="color:#98a890; font-size:12px; line-height:1.6; margin:24px 0 0 0;">
                Este link vale por 3 dias. Se você não solicitou esta conta, pode ignorar este e-mail.
              </p>
            </td>
          </tr>
          <!-- Rodapé -->
          <tr>
            <td style="background:#f3f6ee; padding:18px 32px; text-align:center;">
              <span style="color:#98a890; font-size:12px;">AGRIVIA &middot; Gestão agrícola</span>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def enviar_confirmacao(destino: str, nome: str, link: str) -> bool:
    """Envia o e-mail de confirmação de conta. Retorna True se aceito pelo Resend."""
    html = _html_confirmacao(nome, link)
    return enviar_email(destino, "Confirme seu e-mail - AGRIVIA", html)


def _html_link_assinatura(nome: str, link: str) -> str:
    """Corpo HTML do e-mail com o link de assinatura (visual AGRIVIA)."""
    saudacao = f"Olá, {nome}!" if nome else "Olá!"
    return f"""<!doctype html>
<html lang="pt-br">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0; padding:0; background:#eef2e8; font-family:Arial, Helvetica, sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef2e8; padding:24px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="520" cellpadding="0" cellspacing="0" style="max-width:520px; width:100%; background:#ffffff; border-radius:14px; overflow:hidden; box-shadow:0 6px 18px rgba(0,0,0,0.08);">
          <!-- Cabeçalho -->
          <tr>
            <td style="background:#476126; padding:24px 32px;">
              <span style="color:#ffffff; font-size:24px; font-weight:bold; letter-spacing:1px;">AGRIVIA</span>
            </td>
          </tr>
          <!-- Corpo -->
          <tr>
            <td style="padding:32px;">
              <h1 style="color:#2b3a22; font-size:20px; margin:0 0 16px 0;">{saudacao}</h1>
              <p style="color:#3a4a30; font-size:15px; line-height:1.6; margin:0 0 18px 0;">
                Para ativar (ou renovar) o seu acesso ao <b>AGRIVIA</b>, finalize sua assinatura
                clicando no botão abaixo. Você escolhe o plano e informa o cartão uma única vez —
                depois a renovação é automática no mesmo cartão.
              </p>
              <table role="presentation" cellpadding="0" cellspacing="0" style="margin:24px 0;">
                <tr>
                  <td align="center" style="background:#476126; border-radius:8px;">
                    <a href="{link}" style="display:inline-block; padding:14px 28px; color:#ffffff; font-size:16px; font-weight:bold; text-decoration:none;">
                      Assinar o AGRIVIA
                    </a>
                  </td>
                </tr>
              </table>
              <p style="color:#6a7a5e; font-size:13px; line-height:1.6; margin:18px 0 0 0;">
                Se o botão não funcionar, copie e cole este endereço no seu navegador:
              </p>
              <p style="color:#476126; font-size:13px; word-break:break-all; margin:6px 0 0 0;">{link}</p>
              <p style="color:#98a890; font-size:12px; line-height:1.6; margin:24px 0 0 0;">
                Este link é pessoal e vale por 7 dias. Em caso de dúvida, fale com o suporte AGRIVIA.
              </p>
            </td>
          </tr>
          <!-- Rodapé -->
          <tr>
            <td style="background:#f3f6ee; padding:18px 32px; text-align:center;">
              <span style="color:#98a890; font-size:12px;">AGRIVIA &middot; Gestão agrícola</span>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def enviar_link_assinatura(destino: str, nome: str, link: str) -> bool:
    """Envia o e-mail com o link de assinatura. Retorna True se aceito pelo Resend."""
    html = _html_link_assinatura(nome, link)
    return enviar_email(destino, "Ative sua assinatura - AGRIVIA", html)
