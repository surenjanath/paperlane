"""Build bank.sqlite3: a small, entirely fictional core-banking database for the loan example.

    python examples/bank-loan/make_bank_db.py          # writes examples/bank-loan/bank.sqlite3

Tables: customers, accounts, loans, credit_bureau. Views: customer_summary (one row per customer with income, existing
monthly debt, credit band and ID-check status) and credit_view (what the loan officer's credit check reads).
Every name, address and number is made up; the same seed always produces the same database.
"""
import datetime as dt
import random
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "bank.sqlite3"
TODAY = dt.date(2026, 11, 1)

FIRST = ["Aaliyah", "Marcus", "Priyanka", "Diego", "Keisha", "Rohan", "Sofia", "Andre", "Nadia", "Tariq", "Elena", "Kwame",
         "Mei", "Jamal", "Chloe", "Ravi", "Imani", "Lucas", "Anika", "Darnell", "Yara", "Gabriel", "Leah", "Sanjay",
         "Zoe", "Malik", "Ingrid", "Omar", "Talia", "Victor", "Amara", "Hugo", "Renee", "Kofi", "Lena", "Samir",
         "Grace", "Mateo", "Asha", "Noah"]
LAST = ["Charles", "Ramdeen", "Alvarez", "Baptiste", "Mohammed", "Okafor", "Lindqvist", "Persaud", "Nakamura", "Joseph",
        "Fernandes", "Boateng", "Chen", "Haynes", "Dubois", "Singh", "Mendes", "Walcott", "Ortiz", "Khan"]
STREETS = ["Harbour View Road", "Cedar Lane", "Old Mill Street", "Lighthouse Drive", "Palm Court", "Orchard Avenue",
           "Station Road", "Hillside Terrace", "Bay Street", "Market Square"]
CITIES = ["Riverton", "Northfield", "Southport", "Millbrook", "Eastbay", "Westmere"]
EMPLOYERS = [("Bluewave IT Services", "Software engineer"), ("Harbour Print Co.", "Production manager"),
             ("Northwind Supplies Ltd.", "Logistics coordinator"), ("Riverton General Hospital", "Registered nurse"),
             ("Ministry of Education", "Teacher"), ("Brightpath Consulting", "Senior consultant"),
             ("Coastal Energy", "Field technician"), ("Self-employed", "Shop owner"), ("City Council", "Planning officer"),
             ("Island Air", "Flight attendant")]
BRANCHES = ["Head office", "North", "Central", "South", "East"]
MANAGERS = ["Lina Ortiz", "Omar Haynes", "Fay Filler"]

# profile → (income range, score range, defaults, arrears days, extra loans, KYC)
PROFILES = {
    "prime": ((9000, 16000), (760, 840), 0, 0, 1, "Verified"),
    "standard": ((5000, 9000), (680, 759), 0, 0, 1, "Verified"),
    "stretched": ((4000, 6500), (640, 700), 0, 0, 3, "Verified"),
    "thin_file": ((3500, 5500), (600, 660), 0, 0, 0, "Verified"),
    "arrears": ((4500, 7000), (540, 610), 1, 45, 2, "Verified"),
    "kyc_expired": ((5500, 9500), (700, 780), 0, 0, 1, "Expired"),
    "new_customer": ((6000, 11000), (690, 760), 0, 0, 0, "Pending"),
}
MIX = (["prime"] * 9 + ["standard"] * 12 + ["stretched"] * 6 + ["thin_file"] * 4 + ["arrears"] * 4
       + ["kyc_expired"] * 3 + ["new_customer"] * 2)

SCHEMA = """
CREATE TABLE customers (
  customer_id TEXT PRIMARY KEY, first_name TEXT, last_name TEXT, email TEXT, phone TEXT, date_of_birth TEXT,
  address TEXT, city TEXT, employer TEXT, job_title TEXT, years_employed INTEGER, monthly_income REAL,
  customer_since TEXT, segment TEXT, branch TEXT, kyc_status TEXT, kyc_expires TEXT, relationship_manager TEXT);
CREATE TABLE accounts (
  account_no TEXT PRIMARY KEY, customer_id TEXT REFERENCES customers, type TEXT, balance REAL, avg_balance_6m REAL,
  opened TEXT, status TEXT);
CREATE TABLE loans (
  loan_id TEXT PRIMARY KEY, customer_id TEXT REFERENCES customers, product TEXT, original_amount REAL, balance REAL,
  monthly_payment REAL, rate REAL, start_date TEXT, status TEXT, days_past_due INTEGER);
CREATE TABLE credit_bureau (
  customer_id TEXT PRIMARY KEY REFERENCES customers, score INTEGER, band TEXT, defaults_5y INTEGER,
  enquiries_6m INTEGER, last_checked TEXT);

CREATE VIEW customer_summary AS
SELECT c.customer_id, c.first_name || ' ' || c.last_name AS full_name, c.first_name, c.last_name, c.date_of_birth, c.email, c.phone,
       c.address || ', ' || c.city AS address, c.employer, c.job_title, c.years_employed, c.monthly_income,
       c.customer_since, c.segment, c.branch, c.kyc_status, c.kyc_expires, c.relationship_manager,
       (SELECT account_no FROM accounts a WHERE a.customer_id = c.customer_id AND a.type = 'Current' LIMIT 1) AS current_account,
       ROUND(COALESCE((SELECT SUM(monthly_payment) FROM loans l WHERE l.customer_id = c.customer_id AND l.status != 'Closed'), 0), 2)
         AS existing_monthly_debt,
       (SELECT COUNT(*) FROM loans l WHERE l.customer_id = c.customer_id AND l.status != 'Closed') AS open_loans
FROM customers c;

CREATE VIEW credit_view AS
SELECT b.customer_id, b.score, b.band, b.defaults_5y, b.enquiries_6m, b.last_checked,
       COALESCE((SELECT MAX(days_past_due) FROM loans l WHERE l.customer_id = b.customer_id), 0) AS max_days_past_due,
       ROUND(COALESCE((SELECT AVG(avg_balance_6m) FROM accounts a WHERE a.customer_id = b.customer_id), 0), 2) AS avg_balance_6m
FROM credit_bureau b;
"""


def band(score):
    return "A" if score >= 760 else "B" if score >= 690 else "C" if score >= 620 else "D"


def payment(amount, rate, months):
    r = rate / 1200
    return round(amount * r / (1 - (1 + r) ** -months), 2)


def build(path=OUT):
    rng = random.Random(2026)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    names = set()
    for i, profile in enumerate(MIX):
        (lo, hi), (slo, shi), defaults, dpd, extra_loans, kyc = PROFILES[profile]
        while True:
            first, last = rng.choice(FIRST), rng.choice(LAST)
            if (first, last) not in names:
                names.add((first, last))
                break
        cid = f"CUS-{10001 + i}"
        since = TODAY - dt.timedelta(days=rng.randint(30, 4000) if profile != "new_customer" else rng.randint(10, 60))
        employer, title = rng.choice(EMPLOYERS)
        income = round(rng.uniform(lo, hi) / 50) * 50
        kyc_expires = (TODAY + dt.timedelta(days=rng.randint(120, 900))) if kyc == "Verified" else \
            (TODAY - dt.timedelta(days=rng.randint(20, 300))) if kyc == "Expired" else None
        con.execute("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            cid, first, last, f"{first}.{last}@example.com".lower(), f"+1 555 01{i:02d} {rng.randint(1000, 9999)}",
            (TODAY - dt.timedelta(days=365 * rng.randint(23, 62) + rng.randint(0, 364))).isoformat(),
            f"{rng.randint(1, 240)} {rng.choice(STREETS)}", rng.choice(CITIES), employer, title, rng.randint(0, 22),
            income, since.isoformat(), "Premier" if income >= 10000 else "Personal", rng.choice(BRANCHES), kyc,
            kyc_expires.isoformat() if kyc_expires else None, rng.choice(MANAGERS)))
        for kind, n in (("Current", 1), ("Savings", 1 if rng.random() < .7 else 0)):
            for _ in range(n):
                bal = round(rng.uniform(income * .2, income * (3 if kind == "Savings" else 1.2)), 2)
                con.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?)", (
                    f"{'10' if kind == 'Current' else '20'}{rng.randint(10**7, 10**8 - 1)}", cid, kind, bal,
                    round(bal * rng.uniform(.8, 1.1), 2), since.isoformat(), "Active"))
        for k in range(extra_loans):
            product = rng.choice(["Car loan", "Personal loan", "Credit card", "Home improvement"])
            amount = rng.choice([8000, 12000, 15000, 20000, 25000, 35000])
            rate = rng.choice([7.9, 8.5, 9.5, 11.9, 14.5, 19.9 if product == "Credit card" else 12.5])
            months = rng.choice([36, 48, 60])
            start = TODAY - dt.timedelta(days=rng.randint(60, months * 28))
            paid = min(.95, (TODAY - start).days / (months * 30.4))
            con.execute("INSERT INTO loans VALUES (?,?,?,?,?,?,?,?,?,?)", (
                f"LN-{cid[4:]}-{k + 1}", cid, product, amount, round(amount * (1 - paid), 2), payment(amount, rate, months),
                rate, start.isoformat(), "In arrears" if dpd and k == 0 else "Current", dpd if k == 0 else 0))
        score = rng.randint(slo, shi)
        con.execute("INSERT INTO credit_bureau VALUES (?,?,?,?,?,?)", (
            cid, score, band(score), defaults, rng.randint(0, 2 if profile in ("prime", "standard") else 5),
            (TODAY - dt.timedelta(days=rng.randint(1, 25))).isoformat()))
    con.commit()
    con.execute("VACUUM")
    con.close()
    return path


if __name__ == "__main__":
    p = build()
    con = sqlite3.connect(p)
    print(f"Wrote {p} — {con.execute('select count(*) from customers').fetchone()[0]} customers, "
          f"{con.execute('select count(*) from loans').fetchone()[0]} loans")
    for row in con.execute("select customer_id, full_name, monthly_income, existing_monthly_debt, kyc_status from customer_summary limit 5"):
        print(" ", row)
