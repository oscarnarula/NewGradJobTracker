"""
Job Tracker for Oscar
----------------------
Checks company career pages (via their ATS APIs) for new postings matching
broad analyst/data/business keywords, tags each with a rough entry-level
likelihood, and emails a digest of anything NEW since the last run.

Philosophy: cast a wide net. It is much cheaper to get an email about a
job you don't want than to silently miss one you did.
"""

import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

CONFIG_PATH = "config.json"
SEEN_PATH = "seen_jobs.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (job-tracker script)"}


# ---------------------------------------------------------------------------
# Fetchers — one per ATS platform. Add a new function here for a new platform.
# ---------------------------------------------------------------------------

def fetch_greenhouse(company):
    """Greenhouse exposes a clean public JSON API per company 'board token'."""
    token = company["token"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "id": f"greenhouse-{token}-{j['id']}",
            "title": j["title"],
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "description": strip_html(j.get("content", "")),
        })
    return jobs


def fetch_workday(company):
    """
    Workday's public job search runs on a JSON API under /wday/cxs/.
    Pattern: https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    It's a POST endpoint that supports pagination via 'offset'.

    Workday's API will report an accurate 'total' but silently stop
    returning postings after a couple pages if the request doesn't look
    like it's coming from a real browser session on the actual site. To
    work around that: use a persistent session, visit the human-facing
    search page first to pick up cookies, and send Referer/Origin/Accept
    headers matching that page on every subsequent request.
    """
    tenant = company["tenant"]
    wd_host = company["wd_host"]
    site = company["site"]
    site_url = f"https://{tenant}.{wd_host}.myworkdayjobs.com/{site}"
    base = f"https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": site_url,
        "Origin": f"https://{tenant}.{wd_host}.myworkdayjobs.com",
    })

    # Visit the human-facing page first so Workday sets normal session cookies
    # before we start hitting the JSON API — this is what a real browser does.
    try:
        session.get(site_url, timeout=20)
    except requests.RequestException:
        pass  # not fatal, worth trying the API calls regardless

    jobs = []
    offset = 0
    limit = 20
    reported_total = None
    stall_count = 0
    while True:
        payload = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}
        resp = session.post(base, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        postings = data.get("jobPostings", [])
        if reported_total is None:
            reported_total = data.get("total", 0)
            print(f"  [debug] {company['name']}: Workday reports 'total'={reported_total}")

        if not postings:
            if len(jobs) < reported_total and stall_count == 0:
                # Got cut off before reaching the reported total — retry once
                # in case it was a one-off hiccup rather than a hard cap.
                stall_count += 1
                continue
            print(f"  [debug] {company['name']}: stopped at offset={offset}, "
                  f"fetched {len(jobs)}/{reported_total}")
            break

        for p in postings:
            path = p.get("externalPath", "")
            jobs.append({
                "id": f"workday-{tenant}-{path}",
                "title": p.get("title", ""),
                "location": p.get("locationsText", ""),
                "url": f"{site_url}{path}",
                "description": "",  # Workday list view doesn't include full JD; title/location is enough to match on
            })

        offset += limit
        if offset >= reported_total:
            break
        if offset > 1000:  # safety valve
            break

    return jobs


def fetch_lever(company):
    """Lever also exposes a clean public JSON API per company slug."""
    slug = company["slug"]
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    jobs = []
    for j in data:
        categories = j.get("categories", {})
        jobs.append({
            "id": f"lever-{slug}-{j['id']}",
            "title": j.get("text", ""),
            "location": categories.get("location", ""),
            "url": j.get("hostedUrl", ""),
            "description": strip_html(j.get("descriptionPlain", "")),
        })
    return jobs


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "workday": fetch_workday,
    "lever": fetch_lever,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_html(text):
    return re.sub("<[^<]+?>", " ", text or "")


def matches_keywords(job, keywords):
    # Match on the TITLE only. Matching against full descriptions catches
    # boilerplate (EEO statements, privacy footers) that mentions generic
    # words like "data" in every posting regardless of role — that's what
    # was pulling in daycare and network engineer postings.
    title = job["title"].lower()
    return any(kw.lower().strip() in title for kw in keywords)


def matches_location(job, location_filter):
    if not location_filter:
        return True
    loc = (job.get("location") or "").lower()
    return any(f.lower() in loc for f in location_filter)


def tag_seniority(job, entry_signals, exp_signals):
    haystack = f"{job['title']} {job.get('description', '')}".lower()
    has_entry = any(s.lower() in haystack for s in entry_signals)
    has_exp = any(s.lower() in haystack for s in exp_signals)

    if has_exp and not has_entry:
        return "likely experienced"
    if has_entry and not has_exp:
        return "likely entry-level"
    if has_entry and has_exp:
        return "unclear (mixed signals)"
    return "unclear (no signal)"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def send_email(new_matches):
    email_from = os.environ["EMAIL_ADDRESS"]
    email_to = os.environ.get("EMAIL_TO", email_from)
    app_password = os.environ["EMAIL_APP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Job Tracker: {len(new_matches)} new posting(s)"
    msg["From"] = email_from
    msg["To"] = email_to

    lines = []
    for m in new_matches:
        lines.append(
            f"[{m['tag']}] {m['company']} — {m['title']}\n"
            f"  {m['location']}\n"
            f"  {m['url']}\n"
        )
    body = "\n".join(lines)
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(email_from, app_password)
        server.sendmail(email_from, email_to, msg.as_string())

    print(f"Email sent: {len(new_matches)} new matches.")


def main():
    config = load_json(CONFIG_PATH, {})
    seen = load_json(SEEN_PATH, {})  # {job_id: true}

    keywords = config.get("keywords", [])
    entry_signals = config.get("entry_level_signals", [])
    exp_signals = config.get("experienced_signals", [])

    new_matches = []

    for company in config.get("companies", []):
        ats = company.get("ats")
        name = company["name"]

        if ats not in FETCHERS:
            print(f"[SKIP] {name}: ATS not configured yet ({ats}). "
                  f"See README for how to find its board token/tenant.")
            continue

        try:
            jobs = FETCHERS[ats](company)
        except Exception as e:
            print(f"[ERROR] {name}: failed to fetch jobs ({e})")
            continue

        print(f"[OK] {name}: {len(jobs)} total postings fetched")

        for job in jobs:
            if job["id"] in seen:
                continue  # already notified about this one

            if not matches_location(job, company.get("location_filter")):
                continue

            if not matches_keywords(job, keywords):
                continue

            tag = tag_seniority(job, entry_signals, exp_signals)
            new_matches.append({
                "company": name,
                "title": job["title"],
                "location": job["location"],
                "url": job["url"],
                "tag": tag,
            })
            seen[job["id"]] = True

    if new_matches:
        print(f"Found {len(new_matches)} new matching posting(s).")
        send_email(new_matches)
    else:
        print("No new matching postings this run.")

    save_json(SEEN_PATH, seen)


if __name__ == "__main__":
    main()
