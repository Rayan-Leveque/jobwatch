"""Candidature par email, quand l'offre ne propose que « Postuler par mail ».

L'agent navigateur ne fait que relever l'adresse ; le serveur envoie lui-même
un mail court (modèle fixe, aucun texte généré) avec le CV et la lettre en
pièces jointes, depuis la boîte de `autoapply.imap` (même compte Gmail, même
mot de passe d'application, serveur `smtp_host`).
"""

from __future__ import annotations

import mimetypes
import re
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from jobwatch.config import ImapConfig

SMTP_TIMEOUT_SECONDS = 30
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_FIELD_RE = r"^{label}\s*:\s*(.+?)\s*$"


class MailApplyError(Exception):
    """Envoi impossible ; le message part dans submission_job.reason."""


def profile_field(context: str, label: str) -> str:
    """Valeur d'une ligne « Label : valeur » des informations réutilisables du profil."""
    found = re.search(_FIELD_RE.format(label=re.escape(label)), context, re.MULTILINE)
    return found.group(1) if found else ""


def compose(
    *, sender: str, to: str, title: str, name: str, phone: str, attachments: list[Path]
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((name, sender)) if name else sender
    message["To"] = to
    message["Subject"] = f"Candidature - {title}"
    signature = "\n".join(part for part in (name, phone) if part)
    message.set_content(
        "Madame, Monsieur,\n\n"
        f"Je vous adresse ma candidature au poste de {title}. Vous trouverez "
        "ci-joints mon CV et ma lettre de motivation.\n\n"
        "Je reste à votre disposition pour en échanger.\n\n"
        f"Cordialement,\n{signature}\n"
    )
    for path in attachments:
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        maintype, subtype = kind.split("/", 1)
        message.add_attachment(
            path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
        )
    return message


def send(imap: ImapConfig, message: EmailMessage) -> None:
    if not imap.smtp_host:
        raise MailApplyError("envoi par mail non configuré (autoapply.imap.smtp_host)")
    try:
        with smtplib.SMTP_SSL(imap.smtp_host, imap.smtp_port, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
            smtp.login(imap.user, imap.password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailApplyError(f"envoi du mail impossible : {exc}") from exc
