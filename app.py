"""TikTok referral dashboard: paste your link, the server makes the accounts."""

import os
import re
import time
import random
import string
import sqlite3
import threading

import requests
from faker import Faker
from flask import Flask, redirect, render_template_string, request
from playwright.sync_api import sync_playwright

FAKE = Faker()
MAIL_API = "https://api.mail.tm"
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]
VIEWPORTS = [(1920, 1080), (1536, 864), (1440, 900), (1366, 768), (1280, 800)]

app = Flask(__name__)
DB_LOCK = threading.Lock()
RUNNING = threading.Event()
LOG = []

PAGE = """
<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="15">
<title>Referral Dashboard</title>
<style>
body{font-family:Arial,Helvetica,sans-serif;max-width:900px;margin:30px auto;padding:0 15px;background:#111;color:#eee}
h1{font-size:22px} h2{font-size:16px;margin-top:28px}
input{width:100%;padding:8px;margin:4px 0 10px;border-radius:6px;border:1px solid #444;background:#1c1c1c;color:#eee;box-sizing:border-box}
button{padding:9px 16px;border-radius:6px;border:0;background:#fe2c55;color:#fff;font-weight:bold;cursor:pointer}
table{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}
td,th{border-bottom:1px solid #333;padding:6px;text-align:left}
pre{background:#1c1c1c;padding:10px;border-radius:6px;max-height:250px;overflow:auto;font-size:12px}
.ok{color:#7ce38b}.failed{color:#ff7b72}.idle{color:#f5c26b}
</style></head><body>
<h1>TikTok Referral Dashboard</h1>
<p>Status: {% if running %}<b class="ok">RUNNING</b>{% else %}<b class="idle">idle</b>{% endif %}</p>

<h2>Run accounts</h2>
<form method="post" action="/run">
<input name="link" placeholder="Paste your TikTok referral link here" required>
<input name="count" type="number" min="1" max="10" value="1" style="max-width:120px">
<button type="submit">Run</button>
</form>

<h2>Settings (saved on this server)</h2>
<form method="post" action="/settings">
<input name="proxy" placeholder="Rotating residential proxy http://user:pass@host:port" value="{{ proxy }}">
<input name="mail_pw" placeholder="Mailbox password, leave default" value="{{ mail_pw }}">
<button type="submit">Save</button>
</form>

<h2>Log</h2>
<pre>{% for l in log %}{{ l }}
{% endfor %}</pre>

<h2>Accounts</h2>
<table><tr><th>id</th><th>username</th><th>email</th><th>status</th><th>created</th></tr>
{% for a in accounts %}<tr><td>{{ a['id'] }}</td><td>{{ a['username'] }}</td><td>{{ a['email'] }}</td>
<td class="{{ a['status'] }}">{{ a['status'] }}</td><td>{{ a['created_at'] }}</td></tr>
{% endfor %}</table>
</body></html>
"""


def db():
    con = sqlite3.connect("dash.db", check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)")
    con.execute("""CREATE TABLE IF NOT EXISTS accounts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT, email TEXT, password TEXT,
        referral TEXT, status TEXT, created_at TEXT)""")
    con.commit()
    con.close()


def setting(k, default=""):
    row = db().execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def save_setting(k, v):
    with DB_LOCK:
        con = db()
        con.execute("INSERT INTO settings(k,v) VALUES(?,?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        con.commit()
        con.close()


def note(msg):
    LOG.append(time.strftime("%H:%M:%S") + "  " + msg)
    del LOG[:-200]
    print(msg, flush=True)


def make_identity():
    first = FAKE.first_name().lower()
    tag = random.choice(["vibes", "editz", "hq", "daily", "world", "fan", "hub", "studio", "clipz"])
    pw = "".join(random.choice(string.ascii_letters + string.digits + "!@#$%&*")
                for _ in range(14))
    return {"username": f"{first}_{tag}{random.randint(100, 99999)}"[:24],
            "password": pw, "month": random.randint(1, 12),
            "day": random.randint(1, 28), "year": random.randint(1988, 2004)}


def make_email():
    pw = setting("mail_pw", "GenMail!42")
    for _ in range(6):
        domains = requests.get(f"{MAIL_API}/domains", timeout=30).json()["hydra:member"]
        addr = f"{FAKE.first_name().lower()}{random.randint(1000, 99999)}@{random.choice(domains)['domain']}"
        r = requests.post(f"{MAIL_API}/accounts", json={"address": addr, "password": pw}, timeout=30)
        if r.status_code in (200, 201):
            tok = requests.post(f"{MAIL_API}/token", json={"address": addr, "password": pw},
                                timeout=30).json()["token"]
            return addr, tok
    raise RuntimeError("could not create mailbox")


def wait_for_code(mail_token, timeout_s):
    headers = {"Authorization": f"Bearer {mail_token}"}
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        msgs = requests.get(f"{MAIL_API}/messages", headers=headers, timeout=30).json()["hydra:member"]
        for m in msgs:
            if "tiktok" not in m["from"]["address"].lower():
                continue
            detail = requests.get(f"{MAIL_API}/messages/{m['id']}", headers=headers, timeout=30).json()
            body = detail.get("text") or detail.get("intro") or ""
            nums = re.findall(r"\b\d{6}\b", body)
            if nums:
                return nums[0]
        time.sleep(6)
    raise RuntimeError("verification code never arrived")


def first_input(page, selectors, wait_s=10):
    for sel in selectors:
        try:
            page.wait_for_selector(sel, state="visible", timeout=wait_s * 1000)
            return page.locator(sel).first
        except Exception:
            continue
    raise RuntimeError("no input matched: " + ", ".join(selectors))


def type_human(page, loc, text):
    loc.click()
    for ch in text:
        page.keyboard.type(ch, delay=random.randint(40, 150))


def click_text(page, texts, wait_s=8):
    for t in texts:
        for getter in (lambda: page.get_by_role("button", name=re.compile(t, re.I)),
                       lambda: page.get_by_text(t, exact=False)):
            try:
                getter().first.click(timeout=wait_s * 1000)
                return True
            except Exception:
                continue
    return False


def pick_option(sel, candidates):
    for kind, val in candidates:
        try:
            if kind == "label":
                sel.select_option(label=val)
            elif kind == "value":
                sel.select_option(value=str(val))
            else:
                sel.select_option(index=val)
            return
        except Exception:
            continue


def try_birthday(page, ident):
    try:
        page.wait_for_selector("select", timeout=5000)
    except Exception:
        return False
    sels = page.locator("select")
    if sels.count() < 3:
        return False
    pick_option(sels.nth(0), [("label", MONTHS[ident["month"] - 1]),
                               ("value", ident["month"]), ("index", ident["month"])])
    pick_option(sels.nth(1), [("label", ident["day"]),
                              ("value", ident["day"]), ("index", ident["day"])])
    pick_option(sels.nth(2), [("label", ident["year"]),
                              ("value", ident["year"]), ("index", ident["year"] - 1900)])
    return True


def wait_captcha(page, timeout_s):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not any("captcha" in f.url for f in page.frames):
            return
        time.sleep(2)
    if any("captcha" in f.url for f in page.frames):
        raise RuntimeError("captcha appeared and did not clear by itself")


def signup_flow(page, ident, email, mail_token):
    page.goto("https://www.tiktok.com/signup/phone-or-email/email",
              wait_until="domcontentloaded", timeout=60000)
    click_text(page, ["Allow all", "Accept all", "Agree"], wait_s=3)
    type_human(page, first_input(page, ['input[name="email"]', 'input[type="email"]',
                                        'input[placeholder*="mail" i]']), email)
    type_human(page, first_input(page, ['input[name="password"]', 'input[type="password"]']),
               ident["password"])
    if try_birthday(page, ident):
        click_text(page, ["Sign up", "Next", "Continue"])
    click_text(page, ["Send code"])
    wait_captcha(page, 90)
    code = wait_for_code(mail_token, 300)
    type_human(page, first_input(page, ['input[name="code"]', 'input[placeholder*="code" i]',
                                        'input[inputmode="numeric"]']), code)
    if try_birthday(page, ident):
        click_text(page, ["Sign up", "Next", "Continue"])
    click_text(page, ["Sign up", "Next", "Continue", "Done"])
    try:
        box = first_input(page, ['input[name="username"]', 'input[placeholder*="user" i]'], wait_s=6)
        type_human(page, box, ident["username"])
        click_text(page, ["Sign up", "Next", "Continue", "Done"])
    except Exception:
        pass


def create_account(referral, invite):
    ident = make_identity()
    email, mail_token = make_email()
    proxy = setting("proxy").strip()
    w, h = random.choice(VIEWPORTS)
    with sync_playwright() as p:
        opts = {"headless": True,
                "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"]}
        if proxy:
            opts["proxy"] = {"server": proxy}
        browser = p.chromium.launch(**opts)
        context = browser.new_context(user_agent=random.choice(UAS),
                                     viewport={"width": w, "height": h},
                                     locale="en-US", timezone_id="America/New_York")
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = context.new_page()
        try:
            note("opening referral link so the signup is attributed to you")
            page.goto(referral, wait_until="domcontentloaded", timeout=60000)
            time.sleep(random.uniform(4, 9))
            signup_flow(page, ident, email, mail_token)
            if invite:
                try:
                    box = first_input(page, ['input[placeholder*="code" i]'], wait_s=4)
                    type_human(page, box, invite)
                    click_text(page, ["Apply", "Submit", "Confirm"], wait_s=4)
                    note("invite code entered")
                except Exception:
                    pass
            page.goto("https://www.tiktok.com/foryou",
                      wait_until="domcontentloaded", timeout=60000)
            for _ in range(random.randint(3, 7)):
                time.sleep(random.uniform(3, 8))
                page.mouse.wheel(0, random.randint(400, 1200))
            return ident, email
        finally:
            context.close()
            browser.close()


def run_job(referral, count, invite):
    RUNNING.set()
    try:
        if not setting("proxy").strip():
            note("WARNING: no proxy saved in settings, datacenter IPs often get flagged")
        for i in range(count):
            ident = email = None
            for attempt in (1, 2):
                try:
                    note(f"account {i + 1}/{count}, attempt {attempt}: starting")
                    ident, email = create_account(referral, invite)
                    break
                except Exception as e:
                    note(f"attempt {attempt} failed: {e}")
                    time.sleep(10)
            status = "ok" if ident else "failed"
            with DB_LOCK:
                con = db()
                con.execute("INSERT INTO accounts(username,email,password,referral,status,created_at)"
                            " VALUES(?,?,?,?,?,datetime('now'))",
                            (ident["username"] if ident else "", email or "",
                             ident["password"] if ident else "", referral, status))
                con.commit()
                con.close()
            note(f"account {i + 1}/{count}: {status}")
            if i < count - 1:
                gap = random.randint(60, 180)
                note(f"waiting {gap}s before the next account")
                time.sleep(gap)
        note("job finished")
    finally:
        RUNNING.clear()


def keepalive():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    while True:
        time.sleep(540)
        if url:
            try:
                requests.get(url, timeout=20)
            except Exception:
                pass


@app.route("/")
def index():
    con = db()
    rows = con.execute("SELECT * FROM accounts ORDER BY id DESC LIMIT 50").fetchall()
    con.close()
    return render_template_string(PAGE, running=RUNNING.is_set(), log=LOG[-40:],
                                 accounts=rows, proxy=setting("proxy"),
                                 mail_pw=setting("mail_pw", "GenMail!42"))


@app.route("/run", methods=["POST"])
def run():
    link = request.form.get("link", "").strip()
    try:
        count = max(1, min(10, int(request.form.get("count", "1") or 1)))
    except ValueError:
        count = 1
    if "tiktok.com" not in link or RUNNING.is_set():
        return redirect("/")
    m = re.search(r"invite[_-]?code=([A-Za-z0-9]+)", link, re.I)
    invite = m.group(1) if m else ""
    threading.Thread(target=run_job, args=(link, count, invite), daemon=True).start()
    return redirect("/")


@app.route("/settings", methods=["POST"])
def settings():
    save_setting("proxy", request.form.get("proxy", "").strip())
    if request.form.get("mail_pw"):
        save_setting("mail_pw", request.form.get("mail_pw").strip())
    return redirect("/")


init_db()
threading.Thread(target=keepalive, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
