"""Daily restock emails. Runs headless (no Streamlit needed) — see jobs/send_alerts.py.

Every morning, each member who opted in gets one email listing every item in their
household that is at or above the restock threshold and not already on the shopping list.
An item keeps appearing each day until someone acts on it ("Ran out", "Still have it",
adding it to the list, or buying it again).

Language is per member: English emails use English item names, Hebrew emails are
right-to-left and use Hebrew item names (falling back to the other name when one is missing).
`last_alerted` records the last day an item was included in an email.
"""
import html
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import RESTOCK_THRESHOLD, get_secret

log = logging.getLogger(__name__)

TEXT = {
    "en": {
        "dir": "ltr", "align": "left",
        "title": "Smart Pantry: Restock Alert",
        "intro": "These items in <b>{household}</b> are probably running out:",
        "col_item": "Item", "col_risk": "Chance it's gone", "col_days": "Days since purchase",
        "footer": "Open the app to add them to your shopping list or mark them as still going.",
        "subject": "Pantry alert: {n} item(s) running low in {household}",
    },
    "he": {
        "dir": "rtl", "align": "right",
        "title": "המזווה החכם: התראת מלאי",
        "intro": "הפריטים האלה ב<b>{household}</b> כנראה עומדים להיגמר:",
        "col_item": "פריט", "col_risk": "סיכוי שנגמר", "col_days": "ימים מאז הקנייה",
        "footer": "היכנסו לאפליקציה כדי להוסיף אותם לרשימת הקניות או לסמן שעדיין יש.",
        "subject": "התראת מזווה: {n} פריטים עומדים להיגמר ב{household}",
    },
}


def _smtp_credentials():
    sender = get_secret("EMAIL_SENDER", "GMAIL_USER")
    password = get_secret("EMAIL_PASSWORD", "GMAIL_APP_PASSWORD", "GMAIL_PASSWORD")
    if not sender or not password:
        raise RuntimeError("Missing email credentials (EMAIL_SENDER / EMAIL_PASSWORD).")
    return sender, password


def item_name(item: dict, lang: str) -> str:
    if lang == "he":
        return item["name_he"] or item["name_en"]
    return item["name_en"] or item["name_he"]


def build_alert_html(household_name: str, items: list[dict], lang: str = "en") -> str:
    t = TEXT.get(lang, TEXT["en"])
    cell = f"padding:8px;border:1px solid #ddd;text-align:{t['align']};"
    rows = "".join(
        f"""<tr>
              <td style="{cell}"><b>{html.escape(str(it['name']))}</b></td>
              <td style="{cell}color:#d9534f;"><b>{int(it['risk'] * 100)}%</b></td>
              <td style="{cell}">{int(it['days'])}</td>
            </tr>"""
        for it in items
    )
    return f"""
    <div dir="{t['dir']}" style="font-family:Arial,sans-serif;color:#333;text-align:{t['align']};">
      <h2 style="color:#1B365D;">{t['title']}</h2>
      <p>{t['intro'].format(household=html.escape(household_name))}</p>
      <table dir="{t['dir']}" style="width:100%;border-collapse:collapse;margin-top:10px;">
        <tr style="background:#f4f4f4;">
          <th style="{cell}">{t['col_item']}</th>
          <th style="{cell}">{t['col_risk']}</th>
          <th style="{cell}">{t['col_days']}</th>
        </tr>
        {rows}
      </table>
      <p style="margin-top:20px;">{t['footer']}</p>
    </div>"""


def send_alert_email(server, sender, recipient, household_name, items, lang="en"):
    t = TEXT.get(lang, TEXT["en"])
    msg = MIMEMultipart("alternative")
    msg["Subject"] = t["subject"].format(n=len(items), household=household_name)
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(build_alert_html(household_name, items, lang), "html", "utf-8"))
    refused = server.sendmail(sender, [recipient], msg.as_string())
    if refused:
        raise RuntimeError(f"Recipient refused: {refused}")


def admin_emails() -> list[str]:
    """Who gets feedback and error reports: ADMIN_EMAILS (comma-separated), else the sender address."""
    raw = get_secret("ADMIN_EMAILS") or get_secret("EMAIL_SENDER", "GMAIL_USER") or ""
    return [e.strip().lower() for e in str(raw).split(",") if "@" in e]


def send_admin_email(subject: str, html_body: str) -> bool:
    """Best effort: never raises (it's used while reporting other problems)."""
    to = admin_emails()
    if not to:
        return False
    try:
        sender, password = _smtp_credentials()
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(to)
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20)
        try:
            server.login(sender, password)
            server.sendmail(sender, to, msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass
        return True
    except Exception:
        log.exception("Admin email failed")
        return False


RESET_TEXT = {
    "en": {"dir": "ltr", "subject": "Your Smart Pantry password reset code",
           "title": "Password reset", "intro": "Use this code to set a new password:",
           "footer": "The code is valid for {minutes} minutes. If you didn't ask for it, you can ignore this email."},
    "he": {"dir": "rtl", "subject": "קוד לאיפוס הסיסמה במזווה החכם",
           "title": "איפוס סיסמה", "intro": "השתמשו בקוד הזה כדי לבחור סיסמה חדשה:",
           "footer": "הקוד בתוקף ל-{minutes} דקות. אם לא ביקשתם אותו, אפשר להתעלם מהמייל."},
}


def send_reset_email(recipient: str, code: str, lang: str = "en", minutes: int = 15):
    """Sends the reset code. Raises on failure so the UI can say so."""
    t = RESET_TEXT.get(lang, RESET_TEXT["en"])
    sender, password = _smtp_credentials()
    body = f"""
    <div dir="{t['dir']}" style="font-family:Arial,sans-serif;color:#333;">
      <h2 style="color:#1B365D;">{t['title']}</h2>
      <p>{t['intro']}</p>
      <p style="font-size:32px;font-weight:bold;letter-spacing:6px;" dir="ltr">{html.escape(code)}</p>
      <p style="color:#666;">{t['footer'].format(minutes=minutes)}</p>
    </div>"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = t["subject"]
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(body, "html", "utf-8"))
    server = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
    try:
        server.login(sender, password)
        refused = server.sendmail(sender, [recipient], msg.as_string())
        if refused:
            raise RuntimeError(f"Recipient refused: {refused}")
    finally:
        try:
            server.quit()
        except Exception:
            pass


def process_all_daily_alerts(service, dry_run: bool = False) -> dict:
    """Emails every opted-in member about their household's high-risk items.

    Returns a summary dict. With dry_run=True nothing is sent or stamped.
    """
    with service.db.connect() as conn:
        members = [dict(r) for r in conn.execute(
            """SELECT u.household_id, COALESCE(NULLIF(u.email, ''), u.username) AS email,
                      COALESCE(u.email_lang, 'en') AS lang,
                      COALESCE(h.name, 'Your Household') AS household_name
               FROM users u JOIN households h ON u.household_id = h.id
               WHERE COALESCE(u.email_alerts, 1) != 0
                 AND COALESCE(NULLIF(u.email, ''), u.username) LIKE '%@%'""")]

    by_household: dict[int, dict] = {}
    for m in members:
        hh = by_household.setdefault(m["household_id"], {"name": m["household_name"], "members": []})
        hh["members"].append((m["email"].strip(), m["lang"] if m["lang"] in TEXT else "en"))

    summary = {"households": 0, "emails_sent": 0, "items_alerted": 0, "errors": []}
    server = sender = None
    try:
        for hh_id, hh in by_household.items():
            due = service.restock_candidates(service.get_pantry(hh_id), RESTOCK_THRESHOLD)
            if not due:
                continue
            summary["households"] += 1
            if dry_run:
                log.info("[dry run] %s → %s: %s", hh["name"], [e for e, _ in hh["members"]],
                         [item_name(i, "en") for i in due])
                continue

            if server is None:
                sender, password = _smtp_credentials()
                server = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
                server.login(sender, password)

            sent_any = False
            for email, lang in hh["members"]:
                items = [{"name": item_name(i, lang), "risk": i["prob_depleted"], "days": i["days_elapsed"]}
                         for i in due]
                try:
                    send_alert_email(server, sender, email, hh["name"], items, lang)
                    summary["emails_sent"] += 1
                    sent_any = True
                except Exception as e:
                    summary["errors"].append(f"{email}: {e}")
                    log.warning("Alert to %s failed: %s", email, e)
            if sent_any:
                service.mark_alerted(hh_id, [i["id"] for i in due])
                summary["items_alerted"] += len(due)
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass
    return summary
