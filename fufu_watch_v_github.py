#!/usr/bin/env python3
"""
FuFu Kawaguchiko watcher: emails you when a night between 2027-02-28 and 2027-03-06
becomes available for the Komorebi Stylish Suite plan.

Usage (Command Prompt, in the folder with this file):
    python fufu_watch_v2.py --debug        one check, prints what it found
    python fufu_watch_v2.py --test-email   sends a test email
    python fufu_watch_v2.py                checks every 10 min until you press Ctrl+C
"""
import os, re, sys, time, smtplib, json, random
from email.message import EmailMessage
from pathlib import Path
from playwright.sync_api import sync_playwright

# ---------------- EDIT THESE ----------------
URL = "https://go-kpgluxuryhotels.reservation.jp/en/hotels/fufukawaguchiko/plans/10099814?room_id=10030913&checkin_date=20270228&checkout_date=20270301&adults=2&children=0&rooms=1&show_facility=1"
WATCH_DATES = {"2027-02-28", "2027-03-01", "2027-03-02", "2027-03-03", "2027-03-04", "2027-03-05", "2027-03-06"}
SMTP_USER = os.environ.get("SMTP_USER", "")      # your Gmail address, e.g. "kendr@gmail.com"
SMTP_PASS = os.environ.get("SMTP_PASS", "")      # Gmail App Password (16 letters) from myaccount.google.com/apppasswords
MAIL_TO = os.environ.get("MAIL_TO", "")        # leave "" to send to yourself
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")  # optional: email via resend.com (free). Key looks like "re_xxxxxxxx". Emails go to MAIL_TO_RESEND
MAIL_TO_RESEND = os.environ.get("MAIL_TO_RESEND", "") # must be the Gmail address you signed up to Resend with
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")     # optional phone alerts via ntfy.sh: pick a long random name, e.g. "fufu-kendr-83hd92kq"
CHECK_MINUTES = 10
# --------------------------------------------

MAIL_TO = MAIL_TO or SMTP_USER
STATE = Path(__file__).with_name("fufu_state.json")
DEBUG = "--debug" in sys.argv
UNASSIGNED = []

# Available nights are buttons whose text looks like "6 3 78,100" (day, optional rooms left, price).
# Month/year come from the nearest preceding "3 2027"-style heading in page order.
JS = r"""
() => {
  const re = /^(\d{1,2})\s+(?:(\d{1,2})\s+)?(\d{1,3}(?:,\d{3})+)$/;
  let year = null, month = null, prev = "";
  const out = [], unassigned = [];
  for (const el of document.querySelectorAll("body *")) {
    const tag = el.tagName;
    if (tag === "BUTTON" || tag === "A" || el.getAttribute("role") === "button") {
      const t = el.innerText.replace(/\s+/g, " ").trim();
      const m = t.match(re);
      if (m) {
        if (year !== null) out.push({year, month, day: +m[1], rooms: m[2] ? +m[2] : null, price: m[3]});
        else unassigned.push(t);
      }
    }
    // month heading: any element (leaf or not) whose whole text is like "3 2027" / "32027"
    const full = (el.textContent || "").replace(/\s+/g, " ").trim();
    if (full.length > 0 && full.length <= 9) {
      const h = full.match(/^(\d{1,2})\s*(20\d\d)$/);
      if (h && +h[1] >= 1 && +h[1] <= 12) { month = +h[1]; year = +h[2]; }
    }
    if (el.children.length === 0 && full) {
      if (/^20\d\d$/.test(full) && /^\d{1,2}$/.test(prev) && +prev >= 1 && +prev <= 12) {
        month = +prev; year = +full;
      }
      prev = full;
    }
  }
  const headings = (document.body.innerText.match(/\b\d{1,2}\s+20\d\d\b/g) || []);
  return {days: out, headings, unassigned};
}
"""

def fetch_open_nights():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="msedge", headless=os.environ.get("HEADFUL") != "1")
        except Exception:
            b = p.chromium.launch(headless=os.environ.get("HEADFUL") != "1")
        page = b.new_page(locale="en-US", viewport={"width": 1400, "height": 1000})
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)
        results = [page.evaluate(JS)]
        b.close()
    if not results[0]["headings"]:
        raise RuntimeError("Couldn't find the calendar on the page (blocked or layout changed).")
    nights, headings = {}, []
    global UNASSIGNED
    UNASSIGNED = []
    for res in results:
        headings += res["headings"]
        UNASSIGNED += res["unassigned"]
        for d in res["days"]:
            key = f'{d["year"]}-{d["month"]:02d}-{d["day"]:02d}'
            nights[key] = d
    return nights, headings

def send_mail(subject, body):
    if not (SMTP_USER and SMTP_PASS):
        print("[email not set up - would have sent]", subject, "\n", body)
        return
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = SMTP_USER, MAIL_TO, subject
    msg.set_content(body)
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        if "--test-email" in sys.argv:
            s.set_debuglevel(1)   # prints the conversation with Gmail
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS.replace(" ", ""))
        s.send_message(msg)

def send_resend(subject, body):
    import urllib.request
    payload = json.dumps({"from": "FuFu Watcher <onboarding@resend.dev>",
                          "to": [MAIL_TO_RESEND], "subject": subject, "text": body}).encode("utf-8")
    req = urllib.request.Request("https://api.resend.com/emails", data=payload, headers={
        "Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json",
        "User-Agent": "fufu-watcher/1.0"})
    urllib.request.urlopen(req, timeout=30).read()

def send_ntfy(subject, body):
    import urllib.request
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"),
        headers={"Title": subject.encode("ascii", "ignore").decode(), "Priority": "high"})
    urllib.request.urlopen(req, timeout=30).read()

def notify(subject, body):
    """Try every configured channel. Returns True if at least one worked (or none are configured)."""
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1000, 300)
    except Exception:
        pass
    configured = bool(SMTP_USER and SMTP_PASS) or bool(NTFY_TOPIC) or bool(RESEND_API_KEY and MAIL_TO_RESEND)
    if not configured:
        send_mail(subject, body)   # just prints
        return True
    ok = False
    if SMTP_USER and SMTP_PASS:
        try:
            send_mail(subject, body); ok = True; print("email sent")
        except Exception as e:
            print("email failed:", e)
    if RESEND_API_KEY and MAIL_TO_RESEND:
        try:
            send_resend(subject, body); ok = True; print("email sent (Resend)")
        except Exception as e:
            detail = getattr(e, "read", lambda: b"")().decode("utf-8", "ignore")
            print("Resend failed:", e, detail)
    if NTFY_TOPIC:
        try:
            send_ntfy(subject, body); ok = True; print("phone notification sent")
        except Exception as e:
            print("ntfy failed:", e)
    return ok

def check():
    nights, headings = fetch_open_nights()
    if DEBUG:
        print("Months seen on page:", headings)
        print("All available nights found:", ", ".join(sorted(nights)) or "none")
        print("Price buttons with no month assigned:", UNASSIGNED)
    mine = {d: v for d, v in nights.items() if d in WATCH_DATES}
    seen = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    new = {d: v for d, v in mine.items() if d not in seen}
    if new:
        lines = "\n".join(
            f'  {d}  ¥{v["price"]}' + (f'  ({v["rooms"]} left)' if v["rooms"] else "")
            for d, v in sorted(new.items()))
        delivered = notify("FuFu Kawaguchiko: Komorebi Suite available!",
                           f"Newly available nights:\n{lines}\n\nBook now:\n{URL}")
        print("ALERT:", sorted(new))
        if not delivered:
            print("No notification got through; will retry next check.")
            return
    else:
        print(time.strftime("%F %T"), f"nothing new for 2/28-3/6 ({len(mine)} open)")
    STATE.write_text(json.dumps(sorted(mine)))

if __name__ == "__main__":
    if "--test-email" in sys.argv:
        if RESEND_API_KEY and MAIL_TO_RESEND:
            send_resend("FuFu watcher test", "If you got this, Resend email alerts work.")
            print("Resend test sent")
        if NTFY_TOPIC:
            send_ntfy("FuFu watcher test", "If you got this, phone alerts work.")
            print("ntfy test sent")
        if SMTP_USER and SMTP_PASS:
            send_mail("FuFu watcher test", "If you got this, email alerts work.")
            print("email test sent")
        sys.exit()
    once = DEBUG or "--once" in sys.argv
    while True:
        try:
            check()
        except Exception as e:
            print("check failed:", e)
        if once:
            break
        time.sleep(CHECK_MINUTES * 60 + random.randint(0, 60))
