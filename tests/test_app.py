import os, sys, importlib, tempfile
from datetime import datetime, timedelta
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")
    monkeypatch.setenv("ESCALATE_MIN", "1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    import app
    return importlib.reload(app)


class Client:
    def __init__(self, A):
        self.c = A.app.test_client()
        with self.c.session_transaction() as s: s["csrf"] = "tok"

    def post(self, url, **data):
        with self.c.session_transaction() as s: s["csrf"] = "tok"  # session is reset on login, so re-seed the token
        data["csrf"] = "tok"; return self.c.post(url, data=data, follow_redirects=True)

    def get(self, url): return self.c.get(url)


def setup_family(A):
    c = Client(A); c.post("/create", name="Asha", email="asha@example.com")
    for name, role in (("Ravi", "caretaker"), ("Neha", "family")): c.post("/members/add", name=name, email=f"{name}@example.com", role=role)
    return c


def test_health_and_security_headers(A):
    r = Client(A).get("/health"); assert r.data == b"ok"
    assert r.headers["X-Frame-Options"] == "DENY" and "default-src" in r.headers["Content-Security-Policy"]


def test_post_without_csrf_is_rejected(A):
    assert A.app.test_client().post("/create", data={"name": "x"}).status_code == 400


def test_passcodes_are_hashed_and_login_works(A):
    c = Client(A); r = c.post("/create", name="Asha", email="a@example.com")
    code = [w for w in r.get_data(as_text=True).split() if w.rstrip(".").isdigit() and len(w.rstrip(".")) == 6][0].rstrip(".")
    assert A.q("SELECT passcode FROM members", one=True)["passcode"] != code
    c2 = Client(A); assert b"care plan" in c2.post("/login", passcode=code).data
    assert b"Passcode not found" in Client(A).post("/login", passcode="000000").data


def test_login_rate_limit(A):
    c = Client(A)
    for _ in range(6): r = c.post("/login", passcode="111111")
    assert b"Too many attempts" in r.data


def test_parse_med_fallback(A):
    assert A.parse_med("Metformin 500mg twice daily after meals") == ("Metformin", "500mg", ["08:00", "20:00"])
    assert A.parse_med("Vitamin D 1 tablet at night")[2] == ["21:00"]


def test_clean_times(A):
    assert A.clean_times("20:00, 8:05 25:00 abc") == ["08:05", "20:00"]


def test_medicine_crud_and_validation(A):
    c = Client(A); c.post("/create", name="Asha", email="")
    c.post("/meds/add", name="Aspirin", dose="75mg", times="09:00")
    assert A.q("SELECT times FROM meds", one=True)["times"] == "09:00"
    assert b"valid times" in c.post("/meds/add", name="Bad", dose="", times="xx").data
    mid = A.q("SELECT id FROM meds", one=True)["id"]; c.post(f"/meds/{mid}/delete")
    assert not A.q("SELECT * FROM meds")


def test_invalid_appointment_rejected(A):
    c = Client(A); c.post("/create", name="Asha", email="")
    assert b"valid date" in c.post("/appts/add", title="Dr", at="not-a-date").data


def test_escalation_reminder_caretaker_family(A):
    c = setup_family(A)
    due = (datetime.now() + timedelta(minutes=5)).strftime("%H:%M")
    c.post("/meds/add", name="Aspirin", dose="75mg", times=due)
    A.tick(); assert [l["to_addr"] for l in A.q("SELECT * FROM logs")] == ["Asha <asha@example.com>"]
    past = lambda m: (datetime.now() - timedelta(minutes=m)).strftime(A.F)
    A.q("UPDATE doses SET due=?, stage=1 WHERE id=(SELECT MIN(id) FROM doses)", (past(2),)); A.tick()
    assert any("Ravi" in l["to_addr"] for l in A.q("SELECT * FROM logs WHERE subject LIKE 'Missed%'"))
    A.q("UPDATE doses SET due=? WHERE id=(SELECT MIN(id) FROM doses)", (past(3),)); A.tick()
    assert any("Neha" in l["to_addr"] for l in A.q("SELECT * FROM logs WHERE subject LIKE 'Missed%'"))


def test_taken_stops_escalation(A):
    c = setup_family(A); c.post("/meds/add", name="Aspirin", dose="", times=(datetime.now() + timedelta(minutes=5)).strftime("%H:%M"))
    did = A.q("SELECT id FROM doses", one=True)["id"]; c.post(f"/dose/{did}/taken")
    A.q("UPDATE doses SET due=?", ((datetime.now() - timedelta(minutes=5)).strftime(A.F),)); A.tick()
    assert not A.q("SELECT * FROM logs WHERE subject LIKE 'Missed%'")


def test_elder_controls_access(A):
    c = setup_family(A); x = A.q("SELECT id FROM members WHERE role='caretaker'", one=True)["id"]
    r = c.post(f"/members/{x}/reset")
    code = [w for w in r.get_data(as_text=True).split("New passcode for Ravi:")[1].split()[:1]][0][:6]
    cc = Client(A); cc.post("/login", passcode=code)
    assert cc.post("/meds/add", name="X", dose="", times="09:00").status_code == 403   # view-only by default
    c.post(f"/members/{x}/toggle/can_view"); assert b"Access paused" in cc.get("/dash").data
