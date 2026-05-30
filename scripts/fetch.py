import imaplib
import email
from email.header import decode_header
import re
import json
import os
import sys
from datetime import datetime

IMAP_HOST = "imap.163.com"
IMAP_PORT = 993
SENDER_FILTER = "product_service@ubiquant.com"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "..", "data", "funds.json")


def connect():
    account = os.environ["EMAIL_ACCOUNT"]
    password = os.environ["EMAIL_PASSWORD"]
    # 163.com requires IMAP ID command before SELECT
    imaplib.Commands["ID"] = "AUTH"
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(account, password)
    args = ("name", "fund-tracker", "contact", account,
            "version", "1.0.0", "vendor", "python-client")
    mail._simple_command("ID", '("' + '" "'.join(args) + '")')
    return mail


def search_emails(mail, sender):
    mail.select("INBOX")
    status, messages = mail.search(None, "(SINCE 01-Jan-2026)")
    if status != "OK":
        return []
    return messages[0].split()


def fetch_all_bodies(mail, msg_ids):
    """Fetch plain text bodies from matching emails, filtering by sender and subject."""
    bodies = []
    for msg_id in msg_ids:
        status, data = mail.fetch(msg_id, "(RFC822)")
        if status != "OK":
            continue
        raw_email = data[0][1]
        msg = email.message_from_bytes(raw_email)
        # Filter by sender and subject
        sender_header = str(msg.get("From", ""))
        subject_raw = msg.get("Subject", "")
        if subject_raw:
            subject_parts = decode_header(subject_raw)
            subject_header = ""
            for part, charset in subject_parts:
                if isinstance(part, bytes):
                    try:
                        subject_header += part.decode(charset or "utf-8")
                    except (LookupError, UnicodeDecodeError):
                        subject_header += part.decode("utf-8", errors="replace")
                else:
                    subject_header += part
        else:
            subject_header = ""
        if "ubiquant" not in sender_header.lower() or "周度" not in subject_header:
            continue
        body = get_email_body(msg)
        if body:
            bodies.append(body)
    return bodies


def html_to_text(html):
    """Strip HTML tags to extract plain text."""
    # Remove style/script content
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Replace <br> and block elements with newlines
    html = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</?(p|div|tr|td|th|h\d|li)[^>]*>', '\n', html, flags=re.IGNORECASE)
    # Remove all other tags
    html = re.sub(r'<[^>]+>', '', html)
    # Decode HTML entities
    html = html.replace('&nbsp;', ' ').replace('&gt;', '>').replace('&lt;', '<')
    html = html.replace('&amp;', '&').replace('&quot;', '"')
    # Clean up whitespace
    html = re.sub(r'\n\s*\n', '\n\n', html)
    html = re.sub(r'[ \t]+', ' ', html)
    return html.strip()


def get_email_body(msg):
    """Extract plain text body from email message. Falls back to HTML if needed."""
    if msg.is_multipart():
        html_body = None
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
            elif content_type == "text/html" and html_body is None:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html_body = payload.decode(charset, errors="replace")
        if html_body:
            return html_to_text(html_body)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                return html_to_text(text)
            return text
    return ""


def parse_funds(text):
    """
    Parse fund data from email body text.
    Returns: {
        "date": "2026-05-22",
        "total_value": 9894384.54,
        "funds": [
            {
                "name": "...",
                "shares": ...,
                "market_value": ...,
                "product_nav": ...,  # optional, may be None
                "unit_nav": ...,
                "cumulative_nav": ...,
                "adjusted_nav": ...,
                "weekly_return": ...
            }
        ]
    }
    """
    # Extract date and total value
    date_match = re.search(r'净值日期：(\d{4}-\d{2}-\d{2})', text)
    total_match = re.search(r'持仓总市值：([\d,]+(?:\.\d+)?)', text)

    if not date_match or not total_match:
        raise ValueError("Could not find date or total value in email")

    date = date_match.group(1)
    total_value = float(total_match.group(1).replace(',', ''))

    # Find all fund product names - they contain Chinese fund name identifiers
    product_lines = re.finditer(
        r'^(.+?(?:基金|产品).*)$',
        text,
        re.MULTILINE
    )

    lines = text.split('\n')
    funds = []

    for match in product_lines:
        name = match.group(1).strip()
        start_idx = text[:match.start()].count('\n')

        # Read numeric values after the product name
        values = []
        for i in range(start_idx + 1, min(start_idx + 20, len(lines))):
            line = lines[i].strip()
            if not line:
                continue
            # Check if this line looks like a number (may have commas, %, dots)
            num_match = re.match(r'^(-?[\d,]+(?:\.\d+)?%?)$', line)
            if num_match:
                val_str = num_match.group(1).replace(',', '')
                if val_str.endswith('%'):
                    values.append(float(val_str[:-1]) / 100)
                else:
                    values.append(float(val_str))
            elif re.match(r'^.+?(?:基金|产品).*$', line):
                # Another product name - stop
                break
            elif line.startswith('注') or line.startswith('持有产品'):
                break

        # Determine field layout based on value count
        # 7 values = with product_nav: shares, market_value, product_nav, unit_nav, cumulative_nav, adjusted_nav, weekly_return
        # 6 values = without product_nav: shares, market_value, unit_nav, cumulative_nav, adjusted_nav, weekly_return
        if len(values) == 7:
            fund = {
                "name": name,
                "shares": values[0],
                "market_value": values[1],
                "product_nav": values[2],
                "unit_nav": values[3],
                "cumulative_nav": values[4],
                "adjusted_nav": values[5],
                "weekly_return": values[6],
            }
        elif len(values) == 6:
            fund = {
                "name": name,
                "shares": values[0],
                "market_value": values[1],
                "product_nav": None,
                "unit_nav": values[2],
                "cumulative_nav": values[3],
                "adjusted_nav": values[4],
                "weekly_return": values[5],
            }
        else:
            print(f"Warning: unexpected value count {len(values)} for {name}, skipping")
            continue

        funds.append(fund)

    return {"date": date, "total_value": total_value, "funds": funds}


def load_existing_data(filepath):
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"records": []}


def save_data(filepath, data):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    print("Connecting to IMAP...")
    mail = connect()

    print(f"Searching emails from {SENDER_FILTER}...")
    msg_ids = search_emails(mail, SENDER_FILTER)
    print(f"Found {len(msg_ids)} emails")

    if not msg_ids:
        print("No emails found, exiting")
        sys.exit(0)

    print(f"Fetching all {len(msg_ids)} emails...")
    try:
        bodies = fetch_all_bodies(mail, msg_ids)
    finally:
        mail.logout()

    print(f"Fetched {len(bodies)} email bodies")

    data = load_existing_data(DATA_FILE)
    existing_dates = {r["date"] for r in data["records"]}
    new_count = 0

    for body in bodies:
        try:
            record = parse_funds(body)
        except ValueError as e:
            print(f"  Skipping email: {e}")
            continue

        if record["date"] in existing_dates:
            print(f"  {record['date']} already exists, skipping")
            continue

        data["records"].append(record)
        existing_dates.add(record["date"])
        new_count += 1
        print(f"  Parsed {record['date']}: {len(record['funds'])} funds, total={record['total_value']:,.2f}")

    if new_count == 0:
        print("No new records to add")
        sys.exit(0)

    data["records"].sort(key=lambda r: r["date"])
    data["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    save_data(DATA_FILE, data)
    print(f"\nAdded {new_count} new records, total {len(data['records'])} records saved to {DATA_FILE}")


if __name__ == "__main__":
    main()
