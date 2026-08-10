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
from xml.etree import ElementTree

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
                "path": path,  # kept so we can fetch the full description later, only for matches
                "description": "",  # Workday list view doesn't include full JD; fetched later only for matches
            })

        offset += limit
        if offset >= reported_total:
            break
        if offset > 1000:  # safety valve
            break

    return jobs


def fetch_workday_job_description(tenant, wd_host, site, external_path):
    """
    Fetch the full description for a single Workday job posting. Only called
    for jobs that already matched on title — fetching this for every posting
    up front would mean one extra request per job (hundreds to thousands per
    company), which is far too slow to do unconditionally.
    """
    url = f"https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job{external_path}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        desc_html = data.get("jobPostingInfo", {}).get("jobDescription", "")
        return strip_html(desc_html)
    except Exception:
        return ""  # if this fails, tagging just falls back to title-only, same as before


def fetch_icims_job_detail(job_url, headers):
    """
    Fetch location + full description for a single iCIMS job. iCIMS doesn't
    put location in the sitemap, only in the individual job page — so this
    is only called for jobs that already matched on title, same pattern as
    the Workday description enrichment.
    """
    try:
        resp = requests.get(job_url, headers=headers, timeout=20)
        resp.raise_for_status()
        text = strip_html(resp.text)
        text = re.sub(r"\s+", " ", text).strip()

        location = ""
        loc_match = re.search(r"Job Locations?\s*(.+?)\s*Job Category", text)
        if loc_match:
            location = loc_match.group(1).strip()

        description = ""
        desc_match = re.search(r"Job Description\s*(.+?)\s*(Options|Share on your newsfeed|$)", text)
        if desc_match:
            description = desc_match.group(1).strip()

        return location, description
    except Exception:
        return "", ""


def fetch_icims(company):
    """
    iCIMS has no public JSON API, but every iCIMS career site auto-generates
    a sitemap.xml listing every job posting URL — and the URL itself encodes
    the job title as a slug (e.g. .../jobs/123/data-analyst/job). That gives
    us every job title for the price of one request, without needing a full
    browser. Location and full description are fetched afterward, only for
    postings that already matched a keyword (same enrichment pattern used
    for Workday).
    """
    subdomain = company["subdomain"]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    resp = requests.get(f"https://{subdomain}/sitemap.xml", headers=headers, timeout=20)
    resp.raise_for_status()

    root = ElementTree.fromstring(resp.content)
    urls = [el.text for el in root.iter() if el.tag.endswith("loc") and el.text]

    jobs = []
    pattern = re.compile(r"/jobs/(\d+)/([^/]+)/job")
    for url in urls:
        m = pattern.search(url)
        if not m:
            continue  # skip non-job URLs (intro page, search page, etc.)
        req_id, slug = m.groups()
        title = slug.replace("-", " ").strip()
        title = (title[:1].upper() + title[1:]) if title else title
        jobs.append({
            "id": f"icims-{subdomain}-{req_id}",
            "title": title,
            "location": "",  # unknown until enrichment — see fetch_icims_job_detail
            "url": url,
            "description": "",
        })
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


def fetch_ashby(company):
    """Ashby exposes a public, no-auth JSON API per company job-board slug."""
    slug = company["slug"]
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    jobs = []
    for j in data.get("jobs", []):
        loc = j.get("location") or j.get("locationName") or ""
        jobs.append({
            "id": f"ashby-{slug}-{j.get('id', j.get('jobId', j.get('title')))}",
            "title": j.get("title", ""),
            "location": loc,
            "url": j.get("jobUrl") or j.get("applyUrl", ""),
            "description": strip_html(j.get("descriptionHtml") or j.get("description", "")),
        })
    return jobs


def fetch_workable(company):
    """Workable exposes a public, no-auth JSON widget API per company account slug."""
    slug = company["slug"]
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "id": f"workable-{slug}-{j.get('shortcode', j.get('title'))}",
            "title": j.get("title", ""),
            "location": j.get("location", {}).get("location_str", "") if isinstance(j.get("location"), dict) else str(j.get("location", "")),
            "url": j.get("url", ""),
            "description": strip_html(j.get("description", "")),
        })
    return jobs


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "workday": fetch_workday,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "icims": fetch_icims,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_html(text):
    return re.sub("<[^<]+?>", " ", text or "")


def matches_keywords(job, keywords):
    # Match on the TITLE only, using word boundaries so short tokens (like
    # "bi") match as whole words instead of needing hacks like a trailing
    # space, and don't accidentally match inside unrelated words.
    title = job["title"]
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, title, re.IGNORECASE):
            return True
    return False


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


def send_email(new_matches, full_report=False):
    email_from = os.environ["EMAIL_ADDRESS"]
    email_to = os.environ.get("EMAIL_TO", email_from)
    app_password = os.environ["EMAIL_APP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    subject_prefix = "Job Tracker FULL REPORT" if full_report else "Job Tracker"
    msg["Subject"] = f"{subject_prefix}: {len(new_matches)} posting(s)"
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

    full_report = os.environ.get("FULL_REPORT", "false").lower() == "true"
    if full_report:
        print("[mode] FULL REPORT — showing every current match, ignoring seen history. "
              "seen_jobs.json will NOT be updated this run.")

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
            if not full_report and job["id"] in seen:
                continue  # already notified about this one

            if ats == "icims":
                # iCIMS doesn't give us location up front — only fetch the
                # detail page (location + description) once a title already
                # matched a keyword, to avoid fetching every single posting.
                if not matches_keywords(job, keywords):
                    continue
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                                          "Chrome/124.0.0.0 Safari/537.36"}
                job["location"], job["description"] = fetch_icims_job_detail(job["url"], headers)
                if not matches_location(job, company.get("location_filter")):
                    continue
            else:
                if not matches_location(job, company.get("location_filter")):
                    continue

                if not matches_keywords(job, keywords):
                    continue

                # Enrich with the full description now, only for this one matched
                # job — this is what lets us catch "3+ years" style requirements
                # that live in the description, not the title, for Workday
                # companies (their listing view doesn't include description text).
                if ats == "workday" and not job.get("description") and job.get("path"):
                    job["description"] = fetch_workday_job_description(
                        company["tenant"], company["wd_host"], company["site"], job["path"]
                    )

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
        label = "matching posting(s)" if full_report else "new matching posting(s)"
        print(f"Found {len(new_matches)} {label}.")
        send_email(new_matches, full_report=full_report)
    else:
        print("No matching postings this run." if full_report else "No new matching postings this run.")

    if not full_report:
        save_json(SEEN_PATH, seen)
    else:
        print("[mode] FULL REPORT complete — seen_jobs.json left untouched.")


if __name__ == "__main__":
    main()
