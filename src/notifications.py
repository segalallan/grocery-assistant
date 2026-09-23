import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import streamlit as st

def send_daily_alert(recipient_email, household_name, high_risk_items):
    """
    Connects to Gmail securely and sends an HTML restock alert.
    high_risk_items should be a list of dicts: [{'name': 'Milk', 'risk': 0.88, 'days': 14}, ...]
    """
    if not high_risk_items or not recipient_email:
        return 
        
    try:
        sender_email = st.secrets["smartpantry.alerts@gmail.com"]
        app_password = st.secrets["fbvascjjfnjlprrv"]
    except KeyError:
        print("CRITICAL: Streamlit secrets for GMAIL are missing. Email aborted.")
        return

    # Build the HTML email
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
        # Connect to Gmail's secure SMTP server
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, app_password)
            server.sendmail(sender_email, recipient_email, msg.as_string())
        print(f"Alert sent successfully to {recipient_email}")
    except Exception as e:
        print(f"Failed to send email to {recipient_email}: {e}")