"""
post_from_phone.py — runs on Termux (Samsung Tab S7)
Pulls pending_post.json from GitHub repo and posts to Instagram.

Schedule: every 2 hours at :10 (10 min after GitHub Actions runs at :00)
Cron:  10 1,7,9,11,13,15,17,19,21,23 * * *  cd ~/cricket-bot-fetcher && python post_from_phone.py >> post.log 2>&1
"""

import os
import json
import time
import random
import subprocess
from datetime import datetime, timezone
from typing import Optional, List

import requests

# -----------------------
# Config — set these as env vars in ~/.bashrc
# -----------------------
IG_USER_ID      = os.environ["IG_USER_ID"]
IG_ACCESS_TOKEN = os.environ["IG_ACCESS_TOKEN"]

SLEEP_IG_CONTAINER_MIN = float(os.environ.get("SLEEP_IG_CONTAINER_MIN", "2.5"))
SLEEP_IG_CONTAINER_MAX = float(os.environ.get("SLEEP_IG_CONTAINER_MAX", "5.5"))
SLEEP_BEFORE_PUBLISH   = float(os.environ.get("SLEEP_BEFORE_PUBLISH", "15.0"))
VERIFY_WINDOW          = int(os.environ.get("VERIFY_WINDOW", "600"))

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
PENDING_FILE = os.path.join(BASE_DIR, "pending_post.json")
STATE_FILE   = os.path.join(BASE_DIR, "state.json")

SESSION        = requests.Session()
RETRY_STATUSES = {429, 500, 502, 503, 504}


# -----------------------
# Helpers
# -----------------------
def now_ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")

def log(msg):
    print(f"[{now_ts()}] {msg}", flush=True)

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

def parse_dt(s):
    import re
    s = (s or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = re.sub(r'([+-])(\d{2})(\d{2})$', r'\1\2:\3', s)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def request_with_retry(method, url, *, params=None, data=None,
                       headers=None, files=None, timeout=30, tries=3):
    last = None
    for i in range(tries):
        try:
            r = SESSION.request(method, url, params=params, data=data,
                                headers=headers, files=files, timeout=timeout)
            last = r
            if r.status_code in RETRY_STATUSES:
                ra = r.headers.get("Retry-After")
                time.sleep(int(ra) if (ra and ra.isdigit()) else 2 ** i)
                continue
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(2 ** i)
    if isinstance(last, requests.Response):
        return last
    raise last


# -----------------------
# Instagram Graph API
# -----------------------
def ig_create_image_container(image_url: str) -> Optional[str]:
    r = request_with_retry(
        "POST", f"https://graph.facebook.com/v21.0/{IG_USER_ID}/media",
        data={"image_url": image_url, "is_carousel_item": "true",
              "access_token": IG_ACCESS_TOKEN},
        timeout=30, tries=4
    )
    j = r.json()
    if "error" in j:
        log(f"  ❌ IG container error: {j['error'].get('message', j)}")
        return None
    return j.get("id")

def ig_create_carousel(children_ids: List[str], caption: str) -> Optional[str]:
    r = request_with_retry(
        "POST", f"https://graph.facebook.com/v21.0/{IG_USER_ID}/media",
        data={"media_type": "CAROUSEL", "caption": caption,
              "children": ",".join(children_ids),
              "access_token": IG_ACCESS_TOKEN},
        timeout=30, tries=4
    )
    j = r.json()
    if "error" in j:
        log(f"  ❌ IG carousel error: {j['error'].get('message', j)}")
        return None
    return j.get("id")

def ig_publish(creation_id: str) -> Optional[str]:
    url  = f"https://graph.facebook.com/v21.0/{IG_USER_ID}/media_publish"
    data = {"creation_id": creation_id, "access_token": IG_ACCESS_TOKEN}
    for attempt in range(1, 4):
        log(f"  ▶ Publish attempt {attempt}/3...")
        r = request_with_retry("POST", url, data=data, timeout=30, tries=1)
        j = r.json()
        if "error" not in j and j.get("id"):
            return j["id"]
        log(f"  ⚠️  Attempt {attempt} failed: {j.get('error', {}).get('message', j)}")
        if attempt < 3:
            wait = 10 * (2 ** (attempt - 1))
            log(f"  ⏳ Backing off {wait}s...")
            time.sleep(wait)
    return None

def ig_verify_publish(this_caption: str, within_seconds: int = VERIFY_WINDOW) -> Optional[str]:
    log(f"🔍 Verifying publish (window: {within_seconds}s)...")
    try:
        r = request_with_retry(
            "GET", f"https://graph.facebook.com/v21.0/{IG_USER_ID}/media",
            params={"fields": "id,caption,timestamp,media_type",
                    "limit": 5, "access_token": IG_ACCESS_TOKEN},
            timeout=30, tries=3
        )
        j = r.json()
    except Exception as e:
        log(f"  ❌ Verify query failed: {e}")
        return None

    posts = j.get("data", [])
    now = datetime.now(timezone.utc)
    for p in posts:
        if (p.get("media_type") or "").upper() not in ("CAROUSEL_ALBUM", "CAROUSEL"):
            continue
        try:
            age = (now - parse_dt(p.get("timestamp", ""))).total_seconds()
            if age <= within_seconds:
                log(f"  ✅ Verified: {p['id']} is {age:.0f}s old")
                return p["id"]
        except Exception:
            pass
    log("  ❌ Verification failed — no recent carousel found")
    return None


# -----------------------
# State helpers
# -----------------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE) as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def git_pull():
    try:
        result = subprocess.run(
            ["git", "pull", "--rebase"],
            capture_output=True, text=True, timeout=30
        )
        log(f"  git pull: {result.stdout.strip() or result.stderr.strip()}")
        return True
    except Exception as e:
        log(f"  ⚠️  git pull failed: {e}")
        return False

def git_push_done(imgur_good_ids):
    """Remove pending_post.json, update state, push."""
    try:
        subprocess.run(["git", "config", "user.name", "cricket-bot"], check=True)
        subprocess.run(["git", "config", "user.email", "cricket-bot@users.noreply.github.com"], check=True)

        if os.path.exists(PENDING_FILE):
            os.remove(PENDING_FILE)

        subprocess.run(["git", "add", "pending_post.json", "state.json"], check=True)
        result = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if result.returncode == 0:
            log("  ℹ️  Nothing to commit after posting")
            return
        subprocess.run(["git", "commit", "-m", "posted carousel - cleared pending"], check=True)
        subprocess.run(["git", "push"], check=True)
        log("  ✅ Pushed: pending_post.json cleared")
    except subprocess.CalledProcessError as e:
        log(f"  ⚠️  git push failed: {e} (post still succeeded)")


# -----------------------
# Main
# -----------------------
def main():
    log("=== post_from_phone.py starting ===")
    wait = SLEEP_BEFORE_PUBLISH + random.uniform(0, 60)
    log(f"⏳ Waiting {wait:.1f}s before publish...")
    time.sleep(wait)

    # Step 1: Pull latest from GitHub
    log("📥 Pulling latest from GitHub...")
    git_pull()

    # Step 2: Check if there's a pending post
    if not os.path.exists(PENDING_FILE):
        log("ℹ️  No pending_post.json — nothing to post. Exiting.")
        return

    with open(PENDING_FILE) as f:
        job = json.load(f)

    # Step 3: Check staleness — don't post if older than 4 hours
    created_at = job.get("created_at", "")
    try:
        age_hours = (datetime.now(timezone.utc) - parse_dt(created_at)).total_seconds() / 3600
        log(f"  pending_post.json is {age_hours:.1f}h old")
        if age_hours > 4:
            log("  ⚠️  Job is stale (>4h) — skipping and removing.")
            if os.path.exists(PENDING_FILE):
                os.remove(PENDING_FILE)
            return
    except Exception as e:
        log(f"  ⚠️  Could not parse created_at: {e}")

    public_urls    = job["public_urls"]
    caption        = job["caption"]
    caption_index  = job.get("caption_index", 0)
    imgur_good_ids = job["imgur_good_ids"]

    log(f"  URLs: {len(public_urls)} | Caption: {caption!r}")

    if len(public_urls) < 2:
        log("❌ Not enough URLs in pending job. Removing.")
        if os.path.exists(PENDING_FILE):
            os.remove(PENDING_FILE)
        return

    # Step 4: Create IG containers
    log("📦 Creating IG containers...")
    container_ids = []
    for i, url in enumerate(public_urls, 1):
        log(f"  ▶ container {i}/{len(public_urls)}")
        cid = ig_create_image_container(url)
        if cid:
            container_ids.append(cid)
            log(f"  ✅ {cid}")
        else:
            log(f"  ⚠️  Container failed for image {i}")
        time.sleep(random.uniform(SLEEP_IG_CONTAINER_MIN, SLEEP_IG_CONTAINER_MAX))

    if len(container_ids) < 2:
        log("❌ Not enough containers. Keeping pending_post.json for retry. Exiting.")
        return

    # Step 5: Create carousel
    log("🎠 Creating carousel...")
    car_id = ig_create_carousel(container_ids, caption)
    if not car_id:
        log("❌ Carousel create failed. Keeping pending_post.json for retry. Exiting.")
        return
    log(f"  ✅ Carousel id: {car_id}")

    # Step 6: Wait then publish
    wait = SLEEP_BEFORE_PUBLISH + random.uniform(0, 8)
    log(f"⏳ Waiting {wait:.1f}s before publish...")
    time.sleep(wait)

    post_id = ig_publish(car_id)

    if not post_id:
        log("⏳ Publish attempts failed — verifying...")
        time.sleep(10)
        post_id = ig_verify_publish(caption)
        if not post_id:
            log("❌ Post failed + verification negative. Keeping pending_post.json. Exiting.")
            return
    
    log(f"✅ Posted! post_id: {post_id}")

    # Step 7: Update state
    state = load_state()
    posted_set  = set(state.get("posted", []))
    posted_list = state.get("posted", [])
    good_set    = set(imgur_good_ids)

    for tid in imgur_good_ids:
        if tid not in posted_set:
            posted_list.append(tid)
            posted_set.add(tid)

    state["queue"]              = [t for t in state.get("queue", []) if t not in good_set]
    state["posted"]             = posted_list[-5000:]
    state["in_flight"]          = []
    state["total_carousels"]    = int(state.get("total_carousels", 0)) + 1
    state["last_post_time"]     = utc_now_iso()
    save_state(state)

    # Step 8: Push cleared state back to GitHub
    log("📤 Pushing to GitHub...")
    git_push_done(imgur_good_ids)

    log(f"✅ ALL DONE — total carousels: {state['total_carousels']}")


if __name__ == "__main__":
    main()
