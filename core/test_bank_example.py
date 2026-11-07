"""The bank loan example end to end, plus the read-only SQLite connector it is built on."""
import io
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from accounts.models import Workspace
from flows import connectors, intake
from flows.models import Connector, IntakeForm, Packet, Workflow

User = get_user_model()
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


class SqliteConnectorTests(TestCase):
    """Read-only, single SELECT, bound parameters, files only from the allowed data folders."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0, stdout=io.StringIO())
        cls.ws = Workspace.objects.get(slug="demo")

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        con = sqlite3.connect(self.dir / "t.sqlite3")
        con.executescript("CREATE TABLE people (id TEXT, name TEXT, income REAL);"
                          "INSERT INTO people VALUES ('A1', 'Ada', 5000), ('B2', 'Bo', 7000);")
        con.commit()
        con.close()
        (self.dir / "secret.txt").write_text("nope")
        self.settings_ctx = override_settings(PAPERLANE_DATA_DIRS=[self.dir])
        self.settings_ctx.enable()
        self.addCleanup(self.settings_ctx.disable)
        self.c = Connector.objects.create(workspace=self.ws, name="T", kind=Connector.KIND_SQLITE, database="t.sqlite3",
                                          actions=[{"key": "find", "sql": "SELECT name, income FROM people WHERE id = {value}",
                                                    "map": [{"field": "Name", "path": "name"}]}])

    def q(self, sql, values=None):
        return connectors.query_sqlite(self.c, sql, values or {})

    def test_lookup_maps_the_first_row(self):
        res = connectors.run(self.c, "find", {}, value="B2")
        self.assertTrue(res["ok"])
        self.assertEqual(res["mapped"], {"Name": "Bo"})
        self.assertEqual(res["data"]["income"], 7000)
        self.assertEqual(self.c.last_status, "SELECT 1 row")

    def test_values_are_parameters_not_sql(self):
        res = connectors.run(self.c, "find", {}, value="x' OR '1'='1")
        self.assertTrue(res["ok"])
        self.assertEqual(res["rows"], [])  # the quote is part of the value, not of the query

    def test_only_one_select(self):
        for sql in ["DELETE FROM people", "UPDATE people SET name = 'x'", "SELECT 1; DROP TABLE people",
                    "PRAGMA table_info(people)", "ATTACH DATABASE 'x.db' AS x", ""]:
            with self.assertRaises(connectors.ConnectorError, msg=sql):
                self.q(sql)

    def test_writes_attach_and_extensions_are_refused_by_sqlite_too(self):
        for sql in ["WITH x AS (SELECT 1) SELECT load_extension('evil')",
                    "SELECT * FROM pragma_table_info('people') JOIN people"]:
            with self.assertRaises(connectors.ConnectorError, msg=sql):
                self.q(sql)
        self.assertEqual(len(self.q("SELECT * FROM people")[1]), 2)  # data untouched

    def test_files_outside_the_data_folders_are_refused(self):
        for name in ["../t.sqlite3", "/etc/passwd", "secret.txt", "missing.sqlite3", str(self.dir / "t.sqlite3")]:
            self.c.database = name
            with self.assertRaises(connectors.ConnectorError, msg=name):
                self.q("SELECT 1")

    def test_long_queries_are_stopped(self):
        cols, rows = self.q("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n LIMIT 1000) SELECT count(*) AS c FROM n")
        self.assertEqual(rows[0]["c"], 1000)
        with self.assertRaisesMessage(connectors.ConnectorError, "took too long"):  # endless: stopped by the time limit
            self.q("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n")

    def test_settings_screens(self):
        owner = User.objects.get(username="owner")
        self.client.force_login(owner)
        r = self.client.post("/workspace/connectors/", {"name": "Bank", "kind": "sqlite", "database": "t.sqlite3"})
        c = Connector.objects.get(workspace=self.ws, name="Bank")
        self.assertRedirects(r, f"/workspace/connectors/{c.pk}/", fetch_redirect_response=False)
        self.assertEqual((c.kind, c.base_url), ("sqlite", ""))
        r = self.client.post(f"/workspace/connectors/{c.pk}/", {"form": "action", "key": "by_id", "name": "By id",
                                                                "sql": "SELECT name FROM people WHERE id = {value}", "map": "Name = name"})
        c.refresh_from_db()
        self.assertEqual(c.action("by_id")["sql"], "SELECT name FROM people WHERE id = {value}")
        self.client.post(f"/workspace/connectors/{c.pk}/", {"form": "action", "key": "bad", "sql": "DELETE FROM people"})
        c.refresh_from_db()
        self.assertIsNone(c.action("bad"))
        self.client.post(f"/workspace/connectors/{c.pk}/test/", {"key": "by_id", "value": "A1"})
        page = self.client.get(f"/workspace/connectors/{c.pk}/")
        self.assertContains(page, "Ada")
        self.assertContains(page, "Run test query")
        r = self.client.post("/workspace/connectors/", {"name": "Escape", "kind": "sqlite", "database": "../../db.sqlite3"})
        self.assertFalse(Connector.objects.filter(name="Escape").exists())


class BankExampleTests(TestCase):
    """seed_bank_demo: connector, workflow, form and ten applications that each end where their story says."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0, stdout=io.StringIO())
        call_command("seed_bank_demo", stdout=io.StringIO())
        cls.ws = Workspace.objects.get(slug="demo")
        cls.wf = Workflow.objects.live().get(workspace=cls.ws, name="Personal loan")

    def doc(self, customer):
        return Packet.objects.get(workflow=self.wf, name__contains=customer)

    def open_step(self, p):
        r = p.runs.exclude(status__in=["completed", "declined"]).first()
        return r.step_name if r else None

    def test_built_clean(self):
        from flows import checks

        self.assertEqual([i for i in checks.run(self.wf) if i["level"] == "error"], [])
        self.assertEqual(Packet.objects.filter(workflow=self.wf).count(), 10)
        self.assertEqual(Connector.objects.get(workspace=self.ws, name="Core banking (sample)").kind, "sqlite")

    def test_every_story_ends_where_it_should(self):
        expect = {"Ingrid Ortiz": ("completed", None, "#Funded"), "Noah Persaud": ("in_progress", "Disbursement", None),
                  "Kofi Lindqvist": ("in_progress", "Client signs the offer", "#Committee"),
                  "Hugo Walcott": ("in_progress", "Credit committee", None),
                  "Samir Baptiste": ("in_progress", "Compliance screening", None),
                  "Victor Persaud": ("completed", None, "#Declined"), "Diego Ramdeen": ("completed", None, "#Declined"),
                  "Sofia Persaud": ("in_progress", "Customer: more information", None),
                  "Tariq Fernandes": ("in_progress", "ID check refresh", None),
                  "Priyanka Mendes": ("in_progress", "Loan officer review", None)}
        for name, (status, step, tag) in expect.items():
            p = self.doc(name)
            self.assertEqual((p.status, self.open_step(p)), (status, step), name)
            if tag:
                self.assertIn(tag, set(p.tags.values_list("name", flat=True)), name)

    def test_credit_check_comes_from_the_bank_database(self):
        con = sqlite3.connect(EXAMPLES / "bank-loan" / "bank.sqlite3")
        for name in ("Ingrid Ortiz", "Samir Baptiste"):
            p = self.doc(name)
            v = p.flat_values()
            score, band = con.execute("SELECT score, band FROM credit_view WHERE customer_id = ?", (v["Customer number"],)).fetchone()
            self.assertEqual((int(v["Credit score"]), v["Credit band"]), (score, band), name)

    def test_formulas_and_key_dates(self):
        v = self.doc("Ingrid Ortiz").flat_values()
        amount, rate, term = float(v["Loan amount"]), float(v["Indicative rate (%)"]), int(v["Term (months)"])
        r = rate / 1200
        self.assertAlmostEqual(float(v["Estimated monthly payment"]), amount * r / (1 - (1 + r) ** -term), places=1)
        self.assertAlmostEqual(float(v["Total to repay"]), float(v["Monthly repayment"]) * term, places=0)
        self.assertEqual(set(self.doc("Ingrid Ortiz").key_dates.values_list("label", flat=True)), {"First repayment", "Loan matures"})

    def test_decline_letters_and_funded_pack_were_sent(self):
        from flows.models import BotLog

        for name, bot in (("Victor Persaud", "Decline letter"), ("Diego Ramdeen", "Decline letter"),
                          ("Ingrid Ortiz", "Send the signed loan pack"), ("Ingrid Ortiz", "Credit check (core banking)")):
            self.assertTrue(BotLog.objects.filter(packet=self.doc(name), bot_name=bot, status="success").exists(), (name, bot))
        self.assertFalse(BotLog.objects.filter(packet=self.doc("Ingrid Ortiz"), bot_name="Decline letter", status="success").exists())

    def test_simulator_reaches_every_step(self):
        from flows import simulator

        res = simulator.simulate(self.wf, User.objects.get(username="owner"), seconds=60)
        self.assertEqual(res["problems"], [])
        self.assertEqual(res["steps_reached"], res["steps_total"])

    def test_public_form_needs_number_and_birth_date(self):
        form = IntakeForm.objects.get(workspace=self.ws, name="Apply for a personal loan")
        lk = form.lookups[0]
        con = sqlite3.connect(EXAMPLES / "bank-loan" / "bank.sqlite3")
        cid, dob, name = con.execute("SELECT customer_id, date_of_birth, full_name FROM customer_summary WHERE customer_id = 'CUS-10005'").fetchone()
        miss = intake.run_lookup(form, lk, "1990-01-01", {"customer_id": cid})
        self.assertFalse(miss["found"])
        hit = self.client.post(f"/form/{form.token}/lookup/{lk['id']}/", json.dumps({"value": dob, "values": {"customer_id": cid}}),
                               content_type="application/json").json()
        self.assertTrue(hit["found"])
        self.assertEqual(hit["values"]["full_name"], name)
        self.assertNotIn("date_of_birth", json.dumps(hit))

    def test_submitting_the_form_starts_a_loan_with_a_credit_check(self):
        form = IntakeForm.objects.get(workspace=self.ws, name="Apply for a personal loan")
        con = sqlite3.connect(EXAMPLES / "bank-loan" / "bank.sqlite3")
        row = con.execute("SELECT customer_id, date_of_birth, full_name, email, monthly_income, existing_monthly_debt, kyc_status "
                          "FROM customer_summary WHERE customer_id = 'CUS-10009'").fetchone()
        values = dict(zip(["customer_id", "dob", "full_name", "email", "monthly_income", "existing_debt", "kyc_status"], row))
        values.update(product="Car loan", amount="18000", term="48", rate="11.5", purpose="A car", consent=True,
                      app_sig="data:image/png;base64,iVBORw0KGgo=")
        before = Packet.objects.filter(workflow=self.wf).count()
        intake.submit(form, values, {})
        p = Packet.objects.filter(workflow=self.wf).order_by("-pk").first()
        self.assertEqual(Packet.objects.filter(workflow=self.wf).count(), before + 1)
        self.assertEqual(self.open_step(p), "Loan officer review")
        self.assertTrue(p.flat_values().get("Credit score"))


class FormulaPowerTests(TestCase):
    def test_power_matches_python_and_refuses_huge_exponents(self):
        from flows.conditions import compute_formula as c

        self.assertEqual(c("round({P} * ({r}/1200) / (1 - (1 + {r}/1200) ** -{n}), 2)", {"P": 25000, "r": 9.5, "n": 60}), "525.05")
        self.assertEqual(c("-2**2", {}), "-4")
        self.assertEqual(c("2**3**2", {}), "512")
        self.assertEqual(c("9**9**9", {}), "")


class BankDuplicateTests(TestCase):
    def test_second_application_from_the_same_customer_is_flagged(self):
        call_command("seed_demo", verbosity=0, stdout=io.StringIO())
        call_command("seed_bank_demo", stdout=io.StringIO())
        ws = Workspace.objects.get(slug="demo")
        form = IntakeForm.objects.get(workspace=ws, name="Apply for a personal loan")
        con = sqlite3.connect(EXAMPLES / "bank-loan" / "bank.sqlite3")
        cid, dob, name, email = con.execute("SELECT customer_id, date_of_birth, full_name, email FROM customer_summary "
                                            "WHERE customer_id = 'CUS-10003'").fetchone()  # Ingrid Ortiz, funded 19 days ago
        intake.submit(form, {"customer_id": cid, "dob": dob, "full_name": name, "email": email, "product": "Car loan",
                             "amount": "5000", "term": "12", "rate": "11.5", "purpose": "Again", "consent": True}, {})
        p = Packet.objects.filter(workflow__name="Personal loan").order_by("-pk").first()
        self.assertIn("#Possible duplicate", set(p.tags.values_list("name", flat=True)))
