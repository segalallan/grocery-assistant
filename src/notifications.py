import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import streamlit as st
from datetime import datetime, date

def send_daily_alert(recipient_email, household_name, high_risk_items):
    """Connects to Gmail securely and sends an HTML restock alert."""
    if not high_risk_items or not recipient_email:
        return False
        
    try:
        # We now pull the secrets by their variable names, not their values
        sender_email = st.secrets["EMAIL_SENDER"]
        app_password = st.secrets["EMAIL_PASSWORD"]
    except KeyError:
        print("CRITICAL: Streamlit secrets for GMAIL are missing. Email aborted.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Pantry Alert: Restock required for {household_name}"
    msg["From"] = sender_email
    msg["To"] = recipient_email

    html_content = f"""
    <div style="font-family: Arial, sans-serif; color: #333;">
        <h2 style="color: #1B365D;">Smart Pantry: Daily Restock Alert</h2>
        <p>The following items in <b>{household_name}</b> have a high risk of being depleted today:</p>
        <table style="width: 100%; border-collapse: collapse; margin-top: 10px;">
            <tr style="background-color: #f4f4f4; text-align: left;">
                <th style="padding: 8px; border: 1px solid #ddd;">Item Name</th>
                <th style="padding: 8px; border: 1px solid #ddd;">Depletion Risk</th>
                <th style="padding: 8px; border: 1px solid #ddd;">Days Elapsed</th>
            </tr>
    """
    
    for item in high_risk_items:
        risk_pct = int(item['risk'] * 100)
        html_content += f"""
            <tr>
                <td style="padding: 8px; border: 1px solid #ddd;"><b>{item['name']}</b></td>
                <td style="padding: 8px; border: 1px solid #ddd; color: #d9534f;"><b>{risk_pct}%</b></td>
                <td style="padding: 8px; border: 1px solid #ddd;">{item['days']} days</td>
            </tr>
        """
        
    html_content += """
        </table>
        <p style="margin-top: 20px;">Time to add these to your shopping list!</p>
    </div>
    """

    msg.attach(MIMEText(html_content, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, app_password)
            server.sendmail(sender_email, recipient_email, msg.as_string())
        print(f"Alert sent successfully to {recipient_email}")
        return True
    except Exception as e:
        print(f"Failed to send email to {recipient_email}: {e}")
        return False


def process_all_daily_alerts(db, engine):
    """Scans the database headlessly and passes data to your HTML email sender."""
    # Force connection to the correct v1 database path
    conn = db.get_connection()
    c = conn.cursor()
    
    c.execute("""
        SELECT u.email, u.household_id, h.name as household_name 
        FROM users u
        JOIN households h ON u.household_id = h.id
        WHERE u.email IS NOT NULL
    """)
    users = [dict(r) for r in c.fetchall()]
    print(f"DEBUG: Found {len(users)} users with emails in database.")
    
    today = date.today()
    emails_sent = 0
    
    for user in users:
        c.execute("SELECT item_name_en, item_name_he, category, purchase_date, units, user_lambda FROM inventory WHERE household_id = ?", (user["household_id"],))
        items = [dict(r) for r in c.fetchall()]
        print(f"DEBUG: Found {len(items)} items in inventory for household {user['household_name']}.")
        
        high_risk_items = []
        for it in items:
            p_date = datetime.strptime(it["purchase_date"], "%Y-%m-%d").date()
            days_el = (today - p_date).days
            units_qty = max(1, it["units"] or 1)
            prob = engine.compute_depletion_probability(it["category"], days_el, it["user_lambda"], units_qty)
            print(f"DEBUG: Item {it['item_name_en']} has depletion risk {prob:.2f}")
            
            if prob >= 0.65:
                name = it["item_name_he"] if it["item_name_he"] else it["item_name_en"]
                high_risk_items.append({'name': name, 'risk': prob, 'days': days_el})
        
        if high_risk_items:
            print(f"DEBUG: Sending alert to {user['email']} with {len(high_risk_items)} high risk items.")
            if send_daily_alert(user["email"], user["household_name"], high_risk_items):
                emails_sent += 1
                
    conn.close()
    return f"Sent {emails_sent} alerts."