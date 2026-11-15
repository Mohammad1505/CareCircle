import os, re, json, hmac, random, hashlib, secrets, sqlite3, smtplib, time
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from flask import Flask, render_template, request, redirect, session, flash, abort
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
SECRET = os.getenv("SECRET_KEY", "dev-secret-change-me")  # set a strong value in production
app.secret_key = SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.getenv("K_SERVICE")), MAX_CONTENT_LENGTH=64 * 1024)
FAILS = {}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DB = os.getenv("DB_PATH", "carecircle.db")
GAP = int(os.getenv("ESCALATE_MIN", 30))
F = "%Y-%m-%d %H:%M"
NOTE = "\n\nCareCircle is a reminder tool. It is not a substitute for a doctor's advice."
SCHEMA = """
CREATE TABLE IF NOT EXISTS elders(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS members(id INTEGER PRIMARY KEY, elder_id INT, name TEXT, email TEXT, role TEXT,
  passcode TEXT UNIQUE, can_view INT DEFAULT 1, can_edit INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS meds(id INTEGER PRIMARY KEY, elder_id INT, name TEXT, dose TEXT, times TEXT);
CREATE TABLE IF NOT EXISTS doses(id INTEGER PRIMARY KEY, med_id INT, elder_id INT, due TEXT,
  status TEXT DEFAULT 'pending', stage INT DEFAULT 0, taken_at TEXT, UNIQUE(med_id, due));
CREATE TABLE IF NOT EXISTS appts(id INTEGER PRIMARY KEY, elder_id INT, title TEXT, at TEXT, reminded INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY, elder_id INT, ts TEXT, to_addr TEXT, subject TEXT);
"""


def q(sql, a=(), one=False):
    c = sqlite3.connect(DB, timeout=10); c.row_factory = sqlite3.Row
    try:
        cur = c.execute(sql, a); rows = cur.fetchall(); c.commit()
        if sql.lstrip().upper().startswith("INSERT"): return cur.lastrowid
        return (rows[0] if rows else None) if one else rows
    finally:
        c.close()


def now(): return datetime.now()


# ---------- notifications ----------
def notify(eid, member, subject, body):
    q("INSERT INTO logs(elder_id,ts,to_addr,subject) VALUES(?,?,?,?)",
      (eid, now().strftime(F), f'{member["name"]} <{member["email"] or "no email"}>', subject))
    host, to = os.getenv("SMTP_HOST"), member["email"]
    if not (host and to and os.getenv("SMTP_USER")):
        print(f"[demo mail] to={to} | {subject}"); return
    try:
        m = EmailMessage(); m["From"] = os.getenv("SMTP_USER"); m["To"] = to; m["Subject"] = subject
        m.set_content(body + NOTE)
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", 587))) as s:
            s.starttls(); s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS")); s.send_message(m)
    except Exception as e:
        print("mail error:", e)


def people(eid, *roles):
    return q(f"SELECT * FROM members WHERE elder_id=? AND can_view=1 AND role IN ({','.join('?' * len(roles))})", (eid, *roles))


def ensure_doses(eid=None):
    n = now(); day = n.strftime("%Y-%m-%d")
    for m in q("SELECT * FROM meds" + (" WHERE elder_id=?" if eid else ""), (eid,) if eid else ()):
        for t in m["times"].split(","):
            due = datetime.strptime(f"{day} {t}", F)
            if due > n - timedelta(minutes=60):  # do not create doses that are already long past
                q("INSERT OR IGNORE INTO doses(med_id,elder_id,due) VALUES(?,?,?)", (m["id"], m["elder_id"], due.strftime(F)))


def tick():
    """Runs every minute: reminder -> missed (caretaker) -> family escalation, plus appointment reminders."""
    n = now(); ensure_doses()
    rows = q("SELECT d.*, x.name AS mname, x.dose AS mdose, e.name AS ename FROM doses d JOIN meds x ON x.id=d.med_id "
             "JOIN elders e ON e.id=d.elder_id WHERE d.stage<3 AND d.status IN ('pending','missed')")
    for d in rows:
        due = datetime.strptime(d["due"], F); stage = d["stage"]; eid = d["elder_id"]
        what = f'{d["mname"]} {d["mdose"]} at {due:%I:%M %p}'
        if stage < 1 and n >= due - timedelta(minutes=10):
            for p in people(eid, "elder"): notify(eid, p, f"Upcoming dose: {what}", f"Hello {p['name']}, your dose of {what} is coming up. Please mark it as taken in CareCircle.")
            stage = 1
        if stage < 2 and n >= due + timedelta(minutes=GAP):
            for p in people(eid, "caretaker"): notify(eid, p, f"Missed dose: {d['ename']} - {what}", f"{d['ename']} has not marked {what} as taken. Please check in.")
            stage = 2
        if stage < 3 and n >= due + timedelta(minutes=2 * GAP):
            for p in people(eid, "family"): notify(eid, p, f"Missed dose: {d['ename']} - {what}", f"{d['ename']} still has not marked {what} as taken, and the caretaker has been alerted.")
            stage = 3
        q("UPDATE doses SET stage=?, status=? WHERE id=?", (stage, "missed" if stage >= 2 else "pending", d["id"]))
    for a in q("SELECT a.*, e.name AS ename FROM appts a JOIN elders e ON e.id=a.elder_id WHERE a.reminded=0"):
        at = datetime.strptime(a["at"], F)
        if now() <= at <= now() + timedelta(hours=24):
            for p in people(a["elder_id"], "elder", "caretaker", "family"):
                notify(a["elder_id"], p, f"Appointment: {a['title']} at {at:%d %b, %I:%M %p}", f"Reminder: {a['ename']} has '{a['title']}' on {at:%d %b %Y at %I:%M %p}.")
            q("UPDATE appts SET reminded=1 WHERE id=?", (a["id"],))


# ---------- natural-language medicine entry ----------
PROMPT = ('Convert this medicine instruction to JSON only: {"name":"","dose":"","times":["HH:MM"]}. Use 24-hour times. '
          'twice daily = 08:00 and 20:00; three times = 08:00, 14:00, 20:00; "after meals" = 08:30, 13:30, 20:00. '
          'Do not add medical advice. Instruction: ')


def clean_times(s):
    out = []
    for t in re.split(r"[,\s]+", s.strip()):
        mm = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
        if mm and int(mm[1]) < 24 and int(mm[2]) < 60: out.append(f"{int(mm[1]):02d}:{mm[2]}")
    return sorted(set(out))


def parse_med(text):
    if os.getenv("GEMINI_API_KEY"):
        try:
            from google import genai
            from google.genai import types
            r = genai.Client(api_key=os.getenv("GEMINI_API_KEY")).models.generate_content(
                model=os.getenv("MODEL", "gemini-2.5-flash"), contents=PROMPT + text,
                config=types.GenerateContentConfig(response_mime_type="application/json"))
            d = json.loads(r.text); times = clean_times(",".join(d.get("times", [])))
            if d.get("name") and times: return d["name"], d.get("dose", ""), times
        except Exception as e:
            print("gemini error:", e)
    low = text.lower()  # simple fallback so the app works without an API key
    name = re.split(r"\d", text)[0].strip() or text.split()[0]
    dose = re.search(r"\d+(\.\d+)?\s*(mg|mcg|ml|g|units?|tablets?|tabs?)", low)
    if re.search(r"thrice|three times|3 times|\btds\b", low): times = ["08:00", "14:00", "20:00"]
    elif re.search(r"twice|two times|2 times|\bbd\b", low): times = ["08:00", "20:00"]
    elif re.search(r"night|bedtime", low): times = ["21:00"]
    elif "afternoon" in low: times = ["14:00"]
    else: times = ["08:00"]
    return name, dose.group(0) if dose else "", times


# ---------- summary ----------
def stats(eid):
    rows = q("SELECT status FROM doses WHERE elder_id=? AND due>=? AND status IN ('taken','missed')",
             (eid, (now() - timedelta(days=7)).strftime(F)))
    t = sum(r["status"] == "taken" for r in rows); tot = len(rows)
    return t, tot, (round(100 * t / tot) if tot else None)


def summary(name, lang):
    t, tot, p = stats(session_elder())
    if not tot: return "No doses recorded yet this week." if lang == "en" else "इस हफ़्ते अभी कोई दवा दर्ज नहीं हुई है।"
    if lang == "hi": return f"{name} ने इस हफ़्ते {tot} में से {t} दवाइयाँ समय पर लीं ({p}%). {tot - t} दवाइयाँ छूट गईं।"
    return f"{name} took {t} of {tot} scheduled doses this week ({p}%). {tot - t} doses were missed."


def session_elder(): return q("SELECT elder_id FROM members WHERE id=?", (session.get("mid"),), one=True)["elder_id"]


# ---------- security helpers ----------
def h(code): return hmac.new(SECRET.encode(), code.encode(), hashlib.sha256).hexdigest()  # passcodes are stored hashed


def csrf_token():
    if "csrf" not in session: session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def csrf_guard():
    tok = session.get("csrf")
    if request.method == "POST" and not (tok and hmac.compare_digest(request.form.get("csrf", "").encode(), tok.encode())): abort(400)


@app.after_request
def secure_headers(r):
    r.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
                      "Content-Security-Policy": "default-src 'self'; form-action 'self'; frame-ancestors 'none'"})
    return r


def fv(k, n=80): return request.form.get(k, "").strip()[:n]


def valid_email(k="email"):
    e = fv(k, 120)
    if e and not EMAIL.match(e): flash("That email looks invalid, so it was left blank."); return ""
    return e


def client_ip(): return request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()


def limited(ip):
    FAILS[ip] = [t for t in FAILS.get(ip, []) if time.time() - t < 600]
    return len(FAILS[ip]) >= 5


# ---------- auth helpers ----------
def new_code():
    while True:
        c = f"{random.SystemRandom().randint(0, 999999):06d}"
        if not q("SELECT 1 FROM members WHERE passcode=?", (h(c),), one=True): return c


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        m = q("SELECT * FROM members WHERE id=?", (session.get("mid"),), one=True)
        return f(m, *a, **k) if m else redirect("/")
    return w


def editor(m): return m["role"] == "elder" or bool(m["can_edit"])


def need_edit(m):
    if not editor(m): abort(403)


# ---------- routes ----------
@app.get("/")
def home():
    return redirect("/dash") if session.get("mid") else render_template("home.html")


@app.post("/create")
def create():
    name, email = fv("name"), valid_email()
    if not name: flash("Please enter a name."); return redirect("/")
    eid = q("INSERT INTO elders(name) VALUES(?)", (name,)); code = new_code()
    mid = q("INSERT INTO members(elder_id,name,email,role,passcode) VALUES(?,?,?,?,?)", (eid, name, email, "elder", h(code)))
    session.clear(); session["mid"] = mid
    flash(f"Profile created. Your passcode is {code}. Please note it down."); return redirect("/dash")


@app.post("/login")
def login():
    ip = client_ip()
    if limited(ip): flash("Too many attempts. Please try again in a few minutes."); return redirect("/")
    m = q("SELECT * FROM members WHERE passcode=?", (h(fv("passcode", 6)),), one=True)
    if not m: FAILS.setdefault(ip, []).append(time.time()); flash("Passcode not found."); return redirect("/")
    session.clear(); session["mid"] = m["id"]; return redirect("/dash")


@app.get("/logout")
def logout(): session.pop("mid", None); return redirect("/")


@app.get("/big")
def big(): session["big"] = not session.get("big"); return redirect(request.referrer or "/")


@app.get("/dash")
@login_required
def dash(m):
    eid = m["elder_id"]; elder = q("SELECT * FROM elders WHERE id=?", (eid,), one=True)
    if m["role"] != "elder" and not m["can_view"]: return render_template("dash.html", m=m, elder=elder, paused=True)
    ensure_doses(eid); lang = request.args.get("lang", "en")
    return render_template("dash.html", m=m, elder=elder, paused=False, ed=editor(m), lang=lang,
        doses=q("SELECT d.*, x.name, x.dose FROM doses d JOIN meds x ON x.id=d.med_id WHERE d.elder_id=? AND d.due LIKE ? ORDER BY d.due", (eid, now().strftime("%Y-%m-%d") + "%")),
        meds=q("SELECT * FROM meds WHERE elder_id=?", (eid,)), appts=q("SELECT * FROM appts WHERE elder_id=? ORDER BY at", (eid,)),
        members=q("SELECT * FROM members WHERE elder_id=?", (eid,)), logs=q("SELECT * FROM logs WHERE elder_id=? ORDER BY id DESC LIMIT 8", (eid,)),
        stat=stats(eid), text=summary(elder["name"], lang),
        history=q("SELECT d.due, d.status, x.name FROM doses d JOIN meds x ON x.id=d.med_id WHERE d.elder_id=? AND d.status IN ('taken','missed') ORDER BY d.due DESC LIMIT 10", (eid,)))


@app.post("/dose/<int:did>/taken")
@login_required
def taken(m, did):
    need_edit(m)
    q("UPDATE doses SET status='taken', taken_at=? WHERE id=? AND elder_id=?", (now().strftime(F), did, m["elder_id"]))
    flash("Dose marked as taken."); return redirect("/dash")


def save_med(m, name, dose, times, mid=None):
    if not name or not times: flash("Please give a medicine name and valid times like 08:00,20:00."); return
    t = ",".join(times)
    if mid:
        q("UPDATE meds SET name=?, dose=?, times=? WHERE id=? AND elder_id=?", (name, dose, t, mid, m["elder_id"]))
        q("DELETE FROM doses WHERE med_id=? AND status='pending' AND stage=0", (mid,))
    else:
        q("INSERT INTO meds(elder_id,name,dose,times) VALUES(?,?,?,?)", (m["elder_id"], name, dose, t))
    ensure_doses(m["elder_id"]); flash(f"Saved {name} at {t}.")


@app.post("/meds/add")
@login_required
def med_add(m):
    need_edit(m)
    save_med(m, fv("name"), fv("dose", 40), clean_times(fv("times", 60))); return redirect("/dash")


@app.post("/meds/quick")
@login_required
def med_quick(m):
    need_edit(m); name, dose, times = parse_med(fv("text", 200))
    save_med(m, name, dose, times); return redirect("/dash")


@app.post("/meds/<int:mid>/edit")
@login_required
def med_edit(m, mid):
    need_edit(m)
    save_med(m, fv("name"), fv("dose", 40), clean_times(fv("times", 60)), mid); return redirect("/dash")


@app.post("/meds/<int:mid>/delete")
@login_required
def med_del(m, mid):
    need_edit(m)
    q("DELETE FROM meds WHERE id=? AND elder_id=?", (mid, m["elder_id"])); q("DELETE FROM doses WHERE med_id=?", (mid,))
    return redirect("/dash")


@app.post("/appts/add")
@login_required
def appt_add(m):
    need_edit(m); at = fv("at", 16).replace("T", " ")
    try: datetime.strptime(at, F)
    except ValueError: flash("Please choose a valid date and time."); return redirect("/dash")
    if not fv("title"): flash("Please enter a title."); return redirect("/dash")
    q("INSERT INTO appts(elder_id,title,at) VALUES(?,?,?)", (m["elder_id"], fv("title"), at))
    flash("Appointment added."); return redirect("/dash")


@app.post("/appts/<int:aid>/delete")
@login_required
def appt_del(m, aid):
    need_edit(m); q("DELETE FROM appts WHERE id=? AND elder_id=?", (aid, m["elder_id"])); return redirect("/dash")


@app.post("/members/add")
@login_required
def member_add(m):
    if m["role"] != "elder": abort(403)
    name = fv("name")
    if not name: flash("Please enter a name."); return redirect("/dash")
    code = new_code()
    q("INSERT INTO members(elder_id,name,email,role,passcode) VALUES(?,?,?,?,?)",
      (m["elder_id"], name, valid_email(), "family" if fv("role") == "family" else "caretaker", h(code)))
    flash(f"{name} added. Their passcode is {code}. It is shown only once; you can reset it later."); return redirect("/dash")


@app.post("/members/<int:xid>/reset")
@login_required
def member_reset(m, xid):
    if m["role"] != "elder": abort(403)
    x = q("SELECT * FROM members WHERE id=? AND elder_id=? AND role!='elder'", (xid, m["elder_id"]), one=True)
    if not x: abort(404)
    code = new_code(); q("UPDATE members SET passcode=? WHERE id=?", (h(code), xid))
    flash(f"New passcode for {x['name']}: {code}"); return redirect("/dash")


@app.post("/members/<int:xid>/toggle/<field>")
@login_required
def member_toggle(m, xid, field):
    if m["role"] != "elder" or field not in ("can_view", "can_edit"): abort(403)
    q(f"UPDATE members SET {field}=1-{field} WHERE id=? AND elder_id=? AND role!='elder'", (xid, m["elder_id"])); return redirect("/dash")


@app.post("/members/<int:xid>/delete")
@login_required
def member_del(m, xid):
    if m["role"] != "elder": abort(403)
    q("DELETE FROM members WHERE id=? AND elder_id=? AND role!='elder'", (xid, m["elder_id"])); return redirect("/dash")


@app.post("/summary/send")
@login_required
def summary_send(m):
    need_edit(m); lang = "hi" if fv("lang", 2) == "hi" else "en"; eid = m["elder_id"]
    name = q("SELECT name FROM elders WHERE id=?", (eid,), one=True)["name"]
    for p in people(eid, "caretaker", "family"): notify(eid, p, f"Weekly summary: {name}", summary(name, lang))
    flash("Weekly summary sent."); return redirect("/dash")


@app.post("/tick")
@login_required
def run_tick(m): need_edit(m); tick(); flash("Reminder check completed."); return redirect("/dash")


@app.get("/health")
def health(): return "ok"


def init():
    c = sqlite3.connect(DB); c.execute("PRAGMA journal_mode=WAL"); c.executescript(SCHEMA); c.close()


init()
if not os.getenv("DISABLE_SCHEDULER"):
    sched = BackgroundScheduler(); sched.add_job(tick, "interval", minutes=1); sched.start()

if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", 5000)), debug=False)
