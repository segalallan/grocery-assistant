"""Daily restock emails. Runs headless (no Streamlit needed) — see jobs/send_alerts.py.

Each stock row is emailed about once: after an alert it's stamped `last_alerted`,
and that stamp is cleared by "still going" (or replaced by a new purchase row).
"""
import html
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import RESTOCK_THRESHOLD, get_secret

log = logging.getLogger(__name__)


def _smtp_credentials():
    sender = get_secret("EMAIL_SENDER", "GMAIL_USER")
    password = get_secret("EMAIL_PASSWORD", "GMAIL_APP_PASSWORD", "GMAIL_PASSWORD")
    if not sender or not password:
        raise RuntimeError("Missing email credentials (EMAIL_SENDER / EMAIL_PASSWORD).")
    return sender, password


def build_alert_html(household_name: str, items: list[dict]) -> str:
    rows = "".join(
        f"""<tr>
              <td style="padding:8px;border:1px solid #ddd;"><b>{html.escape(str(it['name']))}</b></td>
              <td style="padding:8px;border:1px solid #ddd;color:#d9534f;"><b>{int(it['risk'] * 100)}%</b></td>
              <td style="padding:8px;border:1px solid #ddd;">{int(it['days'])}</td>
            </tr>"""
        for it in items
    )
    return f"""
    <div style="font-family:Arial,sans-serif;color:#333;">
      <h2 style="color:#1B365D;">Smart Pantry: Restock Alert</h2>
      <p>These items in <b>{html.escape(household_name)}</b> are probably running out:</p>
      <table style="width:100%;border-collapse:collapse;margin-top:10px;">
        <tr style="background:#f4f4f4;text-align:left;">
          <th style="padding:8px;border:1px solid #ddd;">Item</th>
          <th style="padding:8px;border:1px solid #ddd;">Chance it's gone</th>
          <th style="padding:8px;border:1px solid #ddd;">Days since purchase</th>
        </tr>
        {rows}
      </table>
      <p style="margin-top:20px;">Open the app to add them to your shopping list or mark them as still going.</p>
    </div>"""


def send_alert_email(server, sender, recipient, household_name, items):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Pantry alert: {len(items)} item(s) running low in {household_name}"
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(build_alert_html(household_name, items), "html", "utf-8"))
    refused = server.sendmail(sender, [recipient], msg.as_string())
    if refused:
        raise RuntimeError(f"Recipient refused: {refused}")


def process_all_daily_alerts(service, dry_run: bool = False) -> dict:
    """Emails every member of each household about newly high-risk items.

    Returns a summary dict. With dry_run=True nothing is sent or stamped.
    """
    with service.db.connect() as conn:
        members = [dict(r) for r in conn.execute(
            """SELECT u.household_id, COALESCE(NULLIF(u.email, ''), u.username) AS email,
                      COALESCE(h.name, 'Your Household') AS household_name
               FROM users u JOIN households h ON u.household_id = h.id
               WHERE COALESCE(NULLIF(u.email, ''), u.username) LIKE '%@%'""")]

    by_household: dict[int, dict] = {}
    for m in members:
        hh = by_household.setdefault(m["household_id"], {"name": m["household_name"], "emails": []})
        hh["emails"].append(m["email"].strip())

    summary = {"households": 0, "emails_sent": 0, "items_alerted": 0, "errors": []}
    server = None
    try:
        for hh_id, hh in by_household.items():
            pantry = service.get_pantry(hh_id)
            fresh = [i for i in service.restock_candidates(pantry, RESTOCK_THRESHOLD) if not i["last_alerted"]]
            if not fresh:
                continue
            summary["households"] += 1
            items = [{"name": i["name_he"] or i["name_en"], "risk": i["prob_depleted"], "days": i["days_elapsed"]}
                     for i in fresh]
            if dry_run:
                log.info("[dry run] %s: %s", hh["name"], [i["name"] for i in items])
                continue

            if server is None:
                sender, password = _smtp_credentials()
                server = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
                server.login(sender, password)

            sent_any = False
            for email in hh["emails"]:
                try:
                    send_alert_email(server, sender, email, hh["name"], items)
                    summary["emails_sent"] += 1
                    sent_any = True
                except Exception as e:
                    summary["errors"].append(f"{email}: {e}")
                    log.warning("Alert to %s failed: %s", email, e)
            if sent_any:
                service.mark_alerted(hh_id, [i["id"] for i in fresh])
                summary["items_alerted"] += len(fresh)
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass
    return summary
