"""
Cricket Tweet Carousel Bot v3
- SocialData API  — fetch tweets (every 30 mins = ~$4.50/month)
- Playwright      — renders tweet as custom HTML, screenshots it (FREE, unlimited, reliable)
- Imgur           — hosts screenshots publicly (FREE, unlimited)
- Instagram API   — posts carousel (FREE)
- Gmail           — email alerts (FREE)
"""

import os
import json
import time
import fcntl
import smtplib
import logging
import requests
import subprocess
from time import perf_counter
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
SOCIALDATA_API_KEY = os.environ.get("SOCIALDATA_API_KEY", "")
IG_USER_ID         = os.environ.get("IG_USER_ID", "")
IG_ACCESS_TOKEN    = os.environ.get("IG_ACCESS_TOKEN", "")
IMGUR_CLIENT_ID    = os.environ.get("IMGUR_CLIENT_ID", "")
DRY_RUN            = os.environ.get("DRY_RUN", "0") == "1"

ACCOUNTS = os.environ.get(
    "TWITTER_ACCOUNTS",
    "mufaddal_vohra,criccrazyjohns,academy_dinda"
).split(",")

THRESHOLD = int(os.environ.get("TWEET_THRESHOLD", "10"))

CAPTION = os.environ.get(
    "INSTAGRAM_CAPTION",
    "🏏 Latest Cricket Tweets Roundup!\n\n#Cricket #CricketTwitter #Cricthreads"
)

EMAIL_FROM     = os.environ.get("EMAIL_FROM", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")

SLEEP_SCREENSHOT     = float(os.environ.get("SLEEP_SCREENSHOT", "1.5"))
SLEEP_IG_CONTAINER   = float(os.environ.get("SLEEP_IG_CONTAINER", "2.0"))
SLEEP_BEFORE_PUBLISH = float(os.environ.get("SLEEP_BEFORE_PUBLISH", "10.0"))

SCREENSHOT_DIR    = os.path.join(DATA_DIR, "screenshots")
STATE_FILE        = os.path.join(DATA_DIR, "state.json")
LOG_FILE          = os.path.join(DATA_DIR, "bot.log")
LOCK_FILE         = os.path.join(DATA_DIR, "bot.lock")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────
logger = logging.getLogger("cricket-bot")
logger.setLevel(logging.INFO)
fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
for h in [logging.StreamHandler(), logging.FileHandler(LOG_FILE)]:
    h.setFormatter(fmt)
    logger.addHandler(h)

def log(msg: str):
    logger.info(msg)

def trim_log():
    try:
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()
        if len(lines) > 500:
            with open(LOG_FILE, "w") as f:
                f.writelines(lines[-300:])
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
# CONCURRENCY LOCK
# ─────────────────────────────────────────────────────────────
def acquire_lock():
    try:
        f = open(LOCK_FILE, "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        return None

def release_lock(f):
    try:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
# EMAIL ALERTS
# ─────────────────────────────────────────────────────────────
last_alert_date = None

def send_email(subject: str, body: str):
    global last_alert_date
    if not all([EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO]):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    is_success = subject.startswith("✅")
    if not is_success and last_alert_date == today:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"Cricket Bot 🏏 <{EMAIL_FROM}>"
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_FROM, EMAIL_PASSWORD)
            s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        if not is_success:
            last_alert_date = today
        log(f"📧 Email sent: {subject}")
    except Exception as e:
        log(f"❌ Email failed: {e}")

# ─────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"queue": [], "posted": [], "start_time": None,
                "total_carousels": 0, "tweet_data": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        s = json.load(f)
    s.setdefault("queue", [])
    s.setdefault("posted", [])
    s.setdefault("start_time", None)
    s.setdefault("total_carousels", 0)
    s.setdefault("tweet_data", {})
    return s

def save_state(state: Dict[str, Any]):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

# ─────────────────────────────────────────────────────────────
# TIME HELPERS
# ─────────────────────────────────────────────────────────────
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_dt(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)

def timed(label: str, fn, *args, **kwargs):
    t0 = perf_counter()
    try:
        return fn(*args, **kwargs)
    finally:
        log(f"[TIMER] {label}: {perf_counter() - t0:.2f}s")

# ─────────────────────────────────────────────────────────────
# SOCIALDATA
# ─────────────────────────────────────────────────────────────
SESSION = requests.Session()

def fetch_tweets() -> List[Dict]:
    accounts = [a.strip() for a in ACCOUNTS if a.strip()]
    query = " OR ".join([f"from:{u}" for u in accounts])
    r = SESSION.get(
        "https://api.socialdata.tools/twitter/search",
        headers={"Authorization": f"Bearer {SOCIALDATA_API_KEY}"},
        params={"query": query, "type": "Latest"},
        timeout=30,
    )
    r.raise_for_status()
    log(f"SocialData quota remaining: {r.headers.get('X-RateLimit-Remaining', '?')}")
    return r.json().get("tweets") or []

def extract_tweet_time(tweet: Dict) -> Optional[str]:
    for k in ("tweet_created_at", "created_at", "createdAt", "date", "timestamp"):
        v = tweet.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

# ─────────────────────────────────────────────────────────────
# PLAYWRIGHT SCREENSHOT — renders custom HTML template
# No X.com, no login walls, 100% reliable
# ─────────────────────────────────────────────────────────────
def screenshot_tweet(tweet_id: str, tweet_data: Dict) -> Optional[str]:
    out_path = os.path.join(SCREENSHOT_DIR, f"{tweet_id}.png")

    if os.path.exists(out_path):
        log(f"  ♻️  Reusing screenshot: {tweet_id}.png")
        return out_path

    log(f"  📸 Rendering tweet: {tweet_id}")
    try:
        result = subprocess.run(
            ["node", SCREENSHOT_SCRIPT, json.dumps(tweet_data), out_path],
            timeout=35,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            log(f"  ❌ screenshot.js failed: {result.stderr[:300]}")
            return None
        if not os.path.exists(out_path):
            log(f"  ❌ PNG not created for {tweet_id}")
            return None
        log(f"  ✅ Saved: {out_path}")
        return out_path
    except subprocess.TimeoutExpired:
        log(f"  ❌ Timeout for {tweet_id}")
        return None
    except Exception as e:
        log(f"  ❌ Error: {e}")
        return None

def cleanup_screenshots(tweet_ids: List[str]):
    for tid in tweet_ids:
        path = os.path.join(SCREENSHOT_DIR, f"{tid}.png")
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

# ─────────────────────────────────────────────────────────────
# IMGUR — host screenshot publicly for Instagram
# Free, unlimited uploads
# ─────────────────────────────────────────────────────────────
def upload_to_imgur(file_path: str) -> Optional[str]:
    try:
        with open(file_path, "rb") as f:
            r = requests.post(
                "https://api.imgur.com/3/image",
                headers={"Authorization": f"Client-ID {IMGUR_CLIENT_ID}"},
                files={"image": f},
                timeout=30,
            )
        j = r.json()
        if not j.get("success"):
            log(f"  ❌ Imgur failed: {j.get('data', {}).get('error')}")
            return None
        url = j["data"]["link"]
        log(f"  ✅ Imgur: {url}")
        return url
    except Exception as e:
        log(f"  ❌ Imgur error: {e}")
        return None

# ─────────────────────────────────────────────────────────────
# INSTAGRAM
# ─────────────────────────────────────────────────────────────
def ig_create_container(image_url: str) -> Optional[str]:
    r = SESSION.post(
        f"https://graph.facebook.com/v25.0/{IG_USER_ID}/media",
        data={"image_url": image_url, "is_carousel_item": "true",
              "access_token": IG_ACCESS_TOKEN},
        timeout=30,
    )
    j = r.json()
    if "error" in j:
        log(f"  ❌ IG container: {j['error'].get('message')}")
        return None
    return j.get("id")

def ig_create_carousel(children_ids: List[str]) -> Optional[str]:
    r = SESSION.post(
        f"https://graph.facebook.com/v25.0/{IG_USER_ID}/media",
        data={"media_type": "CAROUSEL", "caption": CAPTION,
              "children": ",".join(children_ids), "access_token": IG_ACCESS_TOKEN},
        timeout=30,
    )
    j = r.json()
    if "error" in j:
        log(f"  ❌ IG carousel: {j['error'].get('message')}")
        return None
    return j.get("id")

def ig_publish(creation_id: str) -> Optional[str]:
    r = SESSION.post(
        f"https://graph.facebook.com/v25.0/{IG_USER_ID}/media_publish",
        data={"creation_id": creation_id, "access_token": IG_ACCESS_TOKEN},
        timeout=30,
    )
    j = r.json()
    if "error" in j:
        log(f"  ❌ IG publish: {j['error'].get('message')}")
        return None
    return j.get("id")

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    total_t0 = perf_counter()
    log("=" * 55)
    log("=== cricket-bot v3 starting ===")

    lock = acquire_lock()
    if not lock:
        log("⚠️  Another run in progress — skipping")
        return
    log("🔒 Lock acquired")

    try:
        _run(total_t0)
    except Exception as e:
        log(f"💥 Unhandled error: {e}")
        send_email("🔴 Cricket Bot — Unhandled Error!",
                   f"<h2>Unhandled error</h2><pre>{e}</pre>")
    finally:
        release_lock(lock)
        log("🔓 Lock released")
        trim_log()
        log(f"[TIMER] total: {perf_counter() - total_t0:.2f}s")


def _run(total_t0):
    state = load_state()

    # First run — set start_time and exit
    if not state.get("start_time"):
        state["start_time"] = utc_now_iso()
        save_state(state)
        log(f"✅ Initialized. start_time={state['start_time']}")
        log("Next runs will process tweets from now onward.")
        return

    try:
        start_dt = parse_dt(state["start_time"])
    except Exception as e:
        log(f"⚠️  Bad start_time: {e} — resetting")
        state["start_time"] = utc_now_iso()
        save_state(state)
        return

    queue       = state["queue"]
    posted_list = state["posted"]
    posted_set  = set(posted_list)
    queued_set  = set(queue)
    tweet_data  = state["tweet_data"]  # tweet_id → full tweet dict

    # ── 1. Fetch ──────────────────────────────────
    try:
        tweets = timed("fetch_tweets", fetch_tweets)
    except Exception as e:
        log(f"❌ Fetch failed: {e}")
        send_email("🟡 Cricket Bot — Fetch Failed", f"<p>{e}</p>")
        return

    log(f"Fetched {len(tweets)} tweets | cutoff: {state['start_time']}")

    # ── 2. Filter + enqueue ───────────────────────
    added = skipped_old = skipped_no_time = 0

    for t in tweets:
        tid = str(t.get("id_str") or t.get("id") or "")
        if not tid:
            continue

        created_str = extract_tweet_time(t)
        if not created_str:
            skipped_no_time += 1
            continue

        try:
            created_dt = parse_dt(created_str)
        except Exception:
            skipped_no_time += 1
            continue

        if created_dt < start_dt:
            skipped_old += 1
            continue

        if tid in posted_set or tid in queued_set:
            continue

        queue.append(tid)
        queued_set.add(tid)
        tweet_data[tid] = t  # store full tweet for HTML rendering
        added += 1

    log(f"Enqueued: +{added} | old: {skipped_old} | no_time: {skipped_no_time}")
    log(f"Queue: {len(queue)} / {THRESHOLD}")

    # Bound state size — keep tweet_data only for queued tweets
    state["queue"]      = queue[-5000:]
    state["posted"]     = posted_list[-5000:]
    state["tweet_data"] = {k: v for k, v in tweet_data.items() if k in queued_set}
    save_state(state)

    if len(queue) < THRESHOLD:
        log("Not enough tweets yet — waiting.")
        return

    # ── 3. Take batch ─────────────────────────────
    batch = queue[:THRESHOLD]
    log(f"🚀 Processing batch of {len(batch)}...")

    # ── 4. Screenshots ────────────────────────────
    local_paths: List[str] = []
    good_ids: List[str]    = []

    for i, tid in enumerate(batch, 1):
        t_data = tweet_data.get(tid, {})
        if not t_data:
            log(f"  ⚠️  No data stored for {tid} — skipping")
            continue

        path = timed(
            f"screenshot ({i}/{THRESHOLD})",
            screenshot_tweet, tid, t_data
        )
        if path:
            local_paths.append(path)
            good_ids.append(tid)
        else:
            log(f"  ⚠️  Screenshot failed: {tid}")
        time.sleep(SLEEP_SCREENSHOT)

    log(f"Screenshots: {len(local_paths)}/{THRESHOLD} ok")

    if len(local_paths) < 2:
        log("❌ Not enough screenshots. Keeping queue.")
        send_email("🟡 Cricket Bot — Screenshots Failed",
                   f"<p>Only {len(local_paths)}/{THRESHOLD} ok.</p>")
        return

    # ── 5. Imgur uploads ──────────────────────────
    public_urls: List[str]    = []
    imgur_good_ids: List[str] = []

    for i, (path, tid) in enumerate(zip(local_paths, good_ids), 1):
        url = timed(f"imgur ({i}/{len(local_paths)})", upload_to_imgur, path)
        if url:
            public_urls.append(url)
            imgur_good_ids.append(tid)
        time.sleep(1)

    log(f"Imgur: {len(public_urls)}/{len(local_paths)} ok")

    if len(public_urls) < 2:
        log("❌ Not enough Imgur uploads. Keeping queue.")
        return

    # ── 6. IG containers ─────────────────────────
    container_ids: List[str] = []

    for i, url in enumerate(public_urls, 1):
        cid = timed(f"ig_container ({i}/{len(public_urls)})", ig_create_container, url)
        if cid:
            container_ids.append(cid)
        time.sleep(SLEEP_IG_CONTAINER)

    log(f"IG containers: {len(container_ids)}/{len(public_urls)} ok")

    if len(container_ids) < 2:
        log("❌ Not enough containers. Keeping queue.")
        return

    # ── 7. Carousel ───────────────────────────────
    car_id = timed("ig_create_carousel", ig_create_carousel, container_ids)
    if not car_id:
        log("❌ Carousel create failed.")
        return

    # ── 8. Publish ────────────────────────────────
    log(f"⏳ Waiting {SLEEP_BEFORE_PUBLISH}s before publish...")
    time.sleep(SLEEP_BEFORE_PUBLISH)

    if DRY_RUN:
        log(f"🧪 DRY_RUN — skipping publish. car_id={car_id}")
        return

    post_id = timed("ig_publish", ig_publish, car_id)
    if not post_id:
        log("❌ Publish failed.")
        send_email("🔴 Cricket Bot — Publish Failed",
                   "<p>Token may have expired. Update IG_ACCESS_TOKEN in .env</p>")
        return

    # ── 9. Update state ───────────────────────────
    good_set = set(imgur_good_ids)
    for tid in imgur_good_ids:
        if tid not in posted_set:
            posted_list.append(tid)
            posted_set.add(tid)

    state["queue"]           = [t for t in queue if t not in good_set][-5000:]
    state["posted"]          = posted_list[-5000:]
    state["total_carousels"] = state.get("total_carousels", 0) + 1
    state["tweet_data"]      = {k: v for k, v in tweet_data.items()
                                 if k not in good_set and k in queued_set}
    save_state(state)
    cleanup_screenshots(imgur_good_ids)

    log(f"✅ POSTED! ID: {post_id} | Total: {state['total_carousels']} carousels")

    send_email(
        f"✅ Cricket Bot — Carousel #{state['total_carousels']} Posted!",
        f"""
        <h2>✅ New Carousel Posted!</h2>
        <p>📸 <strong>{len(container_ids)} slides</strong></p>
        <p>🆔 Post ID: <code>{post_id}</code></p>
        <p>📊 Total carousels: <strong>{state['total_carousels']}</strong></p>
        <p>📋 Queue remaining: {len(state['queue'])}</p>
        <p>🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        """
    )


if __name__ == "__main__":
    main()
