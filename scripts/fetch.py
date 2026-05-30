import imaplib
import email
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
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(account, password)
    return mail


def search_emails(mail, sender):
    mail.select("INBOX")
    status, messages = mail.search(None, f'(FROM "{sender}")')
    if status != "OK":
        return []
    return messages[0].split()


def fetch_latest_email(mail, msg_ids):
    """Fetch the latest (most recent) email body."""
    if not msg_ids:
        return None
    latest_id = msg_ids[-1]
    status, data = mail.fetch(latest_id, "(RFC822)")
    if status != "OK":
        return None
    raw_email = data[0][1]
    msg = email.message_from_bytes(raw_email)
    return get_email_body(msg)


def get_email_body(msg):
    """Extract plain text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
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
        for i in range(start_idx + 1, min(start_idx + 10, len(lines))):
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

    print("Fetching latest email...")
    try:
        body = fetch_latest_email(mail, msg_ids)
    finally:
        mail.logout()

    if not body:
        print("Could not extract email body")
        sys.exit(1)

    print("Parsing fund data...")
    try:
        new_record = parse_funds(body)
    except ValueError as e:
        print(f"Error parsing email: {e}")
        sys.exit(1)
    print(f"Parsed {len(new_record['funds'])} funds, date={new_record['date']}")

    data = load_existing_data(DATA_FILE)

    # Check if this date already exists
    existing_dates = {r["date"] for r in data["records"]}
    if new_record["date"] in existing_dates:
        print(f"Data for {new_record['date']} already exists, skipping")
        sys.exit(0)

    # Add new record and sort by date
    data["records"].append(new_record)
    data["records"].sort(key=lambda r: r["date"])
    data["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    save_data(DATA_FILE, data)
    print(f"Saved to {DATA_FILE}")

    # Print summary
    print(f"\n{'='*50}")
    print(f"Fund Data Summary - {new_record['date']}")
    print(f"Total Market Value: {new_record['total_value']:,.2f}")
    print(f"{'='*50}")
    for fund in new_record["funds"]:
        print(f"  {fund['name']}")
        print(f"    Shares: {fund['shares']:,.2f}")
        print(f"    Market Value: {fund['market_value']:,.2f}")
        print(f"    Unit NAV: {fund['unit_nav']:.4f}")
        print(f"    Weekly Return: {fund['weekly_return']*100:.2f}%")
        print()


if __name__ == "__main__":
    main()
