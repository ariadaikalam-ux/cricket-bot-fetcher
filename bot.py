import os
import json
import time
import random
import subprocess
from time import perf_counter
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
import requests
import re

import hashlib


# -----------------------
# Env / Config
# -----------------------
SOCIALDATA_API_KEY = os.environ["SOCIALDATA_API_KEY"]
IG_USER_ID         = os.environ["IG_USER_ID"]
IG_ACCESS_TOKEN    = os.environ["IG_ACCESS_TOKEN"]
IMGUR_CLIENT_ID    = os.environ.get("IMGUR_CLIENT_ID", "")

TWITTER_ACCOUNTS = os.environ.get(
    "TWITTER_ACCOUNTS",
    "mufaddal_vohra,criccrazyjohns,cricketcentrl,tuktuk_academy,shebas_10dulkar"
)
ACCOUNTS = [a.strip() for a in TWITTER_ACCOUNTS.split(",") if a.strip()]

THRESHOLD = int(os.environ.get("TWEET_THRESHOLD", "10"))
DRY_RUN   = os.environ.get("DRY_RUN", "0") == "1"
DEBUG     = os.environ.get("DEBUG", "0") == "1"
SHOW_STATS = os.environ.get("SHOW_STATS", "0") == "1"

# Two alternating captions
CAPTIONS = [
    os.environ.get("INSTAGRAM_CAPTION_0", "🏏 Latest Cricket Tweets Roundup!"),
    os.environ.get("INSTAGRAM_CAPTION_1", "🏏 Best Cricket Tweets Right Now!"),
]

# Tuning
SLEEP_IG_CONTAINER_MIN = float(os.environ.get("SLEEP_IG_CONTAINER_MIN", "1"))
SLEEP_IG_CONTAINER_MAX = float(os.environ.get("SLEEP_IG_CONTAINER_MAX", "2"))
SLEEP_BEFORE_PUBLISH = float(os.environ.get("SLEEP_BEFORE_PUBLISH", "5.0"))
SLEEP_IMGUR          = float(os.environ.get("SLEEP_IMGUR", "0.5"))
VERIFY_WAIT          = float(os.environ.get("VERIFY_WAIT", "8.0"))
VERIFY_WINDOW        = int(os.environ.get("VERIFY_WINDOW", "600"))
DEDUP_MIN_LEN = 25          # don't dedupe very short tweets
DEDUP_HAMMING = 7           # <= 8 means "same-ish"
BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
STATE_FILE        = os.path.join(BASE_DIR, "state.json")
SCREENSHOT_DIR    = os.path.join(BASE_DIR, "screenshots")
SCREENSHOT_SCRIPT = os.path.join(BASE_DIR, "screenshot.js")

SESSION = requests.Session()
RETRY_STATUSES = {429, 500, 502, 503, 504}


# -----------------------
# HTTP helper
# -----------------------
def request_with_retry(method: str, url: str, *, params=None, data=None,
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
# Logging / Timing
# -----------------------
def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

def log(msg: str) -> None:
    print(f"[{now_ts()}] {msg}", flush=True)

def dbg(msg: str) -> None:
    if DEBUG:
        log(f"[DEBUG] {msg}")

class StageTimer:
    def __init__(self, name: str):
        self.name = name
        self.t0: Optional[float] = None

    def __enter__(self):
        self.t0 = perf_counter()
        log(f"➡️  START: {self.name}")
        return self

    def __exit__(self, exc_type, exc, tb):
        dt = perf_counter() - (self.t0 or perf_counter())
        if exc:
            log(f"❌ ERROR in {self.name}: {exc}")
        log(f"✅ END: {self.name} ({dt:.2f}s)")
        return False

# -----------------------
# Time helpers
# -----------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_dt(s: str) -> datetime:
    """
    Parse ISO datetime string → always returns UTC-aware datetime.
    Handles:
      - trailing Z         (2026-02-22T06:00:00Z)
      - offset no colon    (2026-02-22T06:00:00+0000)
      - offset with colon  (2026-02-22T06:00:00+00:00)
      - naive (no tz)      → assumed UTC
    """
    s = (s or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Fix +0000 / -0530 style (no colon) that fromisoformat rejects on Python < 3.11
    s = re.sub(r'([+-])(\d{2})(\d{2})$', r'\1\2:\3', s)
    dt = datetime.fromisoformat(s)
    # If no timezone info, assume UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def extract_tweet_time(t: Dict[str, Any]) -> Optional[str]:
    if isinstance(t.get("tweet_created_at"), str) and t["tweet_created_at"].strip():
        return t["tweet_created_at"].strip()
    for k in ("created_at", "createdAt", "date", "timestamp"):
        v = t.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

def ensure_dirs():
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)

def sort_queue_oldest_first(queue: List[str], tweet_data: Dict[str, Any]) -> List[str]:
    def key(tid: str):
        t = tweet_data.get(tid, {})
        ts = extract_tweet_time(t) or ""
        try:
            return parse_dt(ts)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)
    return sorted(queue, key=key)

# -----------------------
# Tweet filters
# -----------------------
def is_video_tweet(t: Dict[str, Any]) -> bool:
    media = []
    ext = t.get("extended_entities", {})
    ent = t.get("entities", {})
    if isinstance(ext, dict) and isinstance(ext.get("media"), list):
        media = ext["media"]
    elif isinstance(ent, dict) and isinstance(ent.get("media"), list):
        media = ent["media"]
    for m in media:
        if not isinstance(m, dict):
            continue
        if (m.get("type") or "").lower() in ("video", "animated_gif"):
            return True
        if "video_info" in m:
            return True
    return False

def is_retweet(t: Dict[str, Any]) -> bool:
    if t.get("retweeted_status"):
        return True
    if (t.get("type") or "").lower() in ("retweet", "retweeted_tweet"):
        return True
    txt = (t.get("full_text") or t.get("text") or "").lstrip()
    if txt.startswith("RT @"):
        return True
    for k in ("retweeted_status_id", "retweeted_status_id_str", "retweet_id", "retweet_id_str"):
        if t.get(k) not in (None, "", 0):
            return True
    return False

def is_reply(t: Dict[str, Any]) -> bool:
    # 0) SocialData explicit type
    if (t.get("type") or "").lower() == "reply":
        return True
    # 1) reply-to status ID
    for k in ("in_reply_to_status_id", "in_reply_to_status_id_str"):
        if t.get(k) not in (None, "", 0, "0"):
            return True
    # 2) reply-to user ID
    for k in ("in_reply_to_user_id", "in_reply_to_user_id_str"):
        if t.get(k) not in (None, "", 0, "0"):
            return True
    # 3) starts with @mention
    txt = (t.get("full_text") or t.get("text") or "").lstrip()
    if txt.startswith("@"):
        return True
    return False

def is_quote_tweet(t: Dict[str, Any]) -> bool:
    if t.get("is_quote_status") is True:
        return True
    if t.get("quoted_status") and isinstance(t["quoted_status"], dict):
        return True
    for k in ("quoted_status_id", "quoted_status_id_str"):
        if t.get(k) not in (None, "", 0, "0"):
            return True
    return False

def has_photo_media(t: Dict[str, Any]) -> bool:
    ext = t.get("extended_entities", {})
    ent = t.get("entities", {})
    media = []
    if isinstance(ext, dict) and isinstance(ext.get("media"), list):
        media = ext["media"]
    elif isinstance(ent, dict) and isinstance(ent.get("media"), list):
        media = ent["media"]
    for m in media:
        if isinstance(m, dict) and (m.get("type") or "").lower() == "photo":
            return True
    return False
STOPWORDS = {
    "the","a","an","and","or","to","of","in","on","for","with","at","by",
    "is","are","was","were","be","been","it","this","that","these","those",
    "today","yesterday","tomorrow","vs","v"
}

def normalize_text_for_dedupe(t: Dict[str, Any]) -> str:
    txt = (t.get("full_text") or t.get("text") or "").lower()

    # remove urls
    txt = re.sub(r"https?://\S+|www\.\S+", " ", txt)

    # remove mentions
    txt = re.sub(r"@\w+", " ", txt)

    # keep hashtags text but remove the #
    txt = txt.replace("#", " ")

    # normalize numbers (scores/stats) -> 0
    # Keep small numbers (2, 6, 20) because they matter in cricket.
    # Only normalize big numbers (years, 100+, etc.)
    txt = re.sub(r"\b\d{3,}\b", " 0 ", txt)

    # remove non letters/numbers/spaces
    txt = re.sub(r"[^a-z0-9\s]+", " ", txt)

    # split, remove stopwords, collapse
    toks = [w for w in txt.split() if w and w not in STOPWORDS]

    # if it's too short, it’s not safe to dedupe (avoid false positives)
    norm = " ".join(toks)
    return norm.strip()

def simhash64(text: str) -> int:
    if not text:
        return 0
    v = [0] * 64
    for tok in text.split():
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        for i in range(64):
            bit = (h >> i) & 1
            v[i] += 1 if bit else -1
    out = 0
    for i in range(64):
        if v[i] > 0:
            out |= (1 << i)
    return out

def hamming64(a: int, b: int) -> int:
    x = a ^ b
    try:
        return x.bit_count()      # Python 3.10+
    except AttributeError:
        return bin(x).count("1")  # older
# -----------------------
# State
# -----------------------
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {
            "start_time": None,
            "queue": [],
            "posted": [],
            "seen": [],          # all evaluated tweet IDs (pass or fail)
            "tweet_data": {},
            "total_runs": 0,
            "total_carousels": 0,
            "last_caption_index": -1,
            "last_caption_text": "",
            "last_post_time": None,
            "first_cycle": [],
            "first_cycle_idx": 0,
            "next_start_idx": 0,
        }
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        s = json.load(f)

    s.setdefault("start_time", None)
    s.setdefault("queue", [])
    s.setdefault("posted", [])
    s.setdefault("seen", [])
    s.setdefault("tweet_data", {})
    s.setdefault("total_runs", 0)
    s.setdefault("total_carousels", 0)
    s.setdefault("in_flight", [])
    s.setdefault("last_caption_index", -1)
    s.setdefault("last_caption_text", "")
    s.setdefault("last_post_time", None)
    s.setdefault("first_cycle", [])
    s.setdefault("first_cycle_idx", 0)
    s.setdefault("next_start_idx", 0)
    if not isinstance(s["in_flight"], list):  s["in_flight"] = []
    if not isinstance(s["queue"], list):      s["queue"] = []
    if not isinstance(s["posted"], list):     s["posted"] = []
    if not isinstance(s["seen"], list):       s["seen"] = []
    if not isinstance(s["tweet_data"], dict): s["tweet_data"] = {}

    return s

def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def bound_state(state: Dict[str, Any]) -> None:
    state["queue"]  = state["queue"][-5000:]
    state["posted"] = state["posted"][-5000:]
    state["seen"]   = state["seen"][-10000:]  # last 10k evaluated IDs
    qset = set(state["queue"])
    state["tweet_data"] = {k: v for k, v in state.get("tweet_data", {}).items() if k in qset}

def recover_in_flight(state: Dict[str, Any]) -> None:
    inflight = state.get("in_flight") or []
    if not isinstance(inflight, list) or not inflight:
        state["in_flight"] = []
        return
    inflight_set = set(map(str, inflight))
    state.setdefault("posted", [])
    posted_set = set(map(str, state["posted"]))
    for tid in inflight_set:
        if tid not in posted_set:
            state["posted"].append(tid)
            posted_set.add(tid)
    state.setdefault("queue", [])
    state["queue"] = [str(x) for x in state["queue"] if str(x) not in inflight_set]
    state["in_flight"] = []


# -----------------------
# Caption rotation
# -----------------------
def pick_caption(state: Dict[str, Any]) -> Tuple[str, int]:
    last_index = int(state.get("last_caption_index", -1))
    next_index = (last_index + 1) % len(CAPTIONS)
    return CAPTIONS[next_index], next_index
def pick_accounts_fair_first(state: Dict[str, Any], accounts: List[str]) -> List[str]:
    """
    Ensures each account gets equal chance to be 1st:
      - Create a random permutation (cycle)
      - Use next element as the 1st account each run
      - When cycle ends, reshuffle a new cycle
    Rest of accounts are shuffled randomly each run.
    """
    n = len(accounts)
    if n <= 1:
        return accounts[:]

    cycle = state.get("first_cycle") or []
    idx = int(state.get("first_cycle_idx", 0) or 0)

    # Rebuild cycle if missing/invalid/different accounts set
    if (not isinstance(cycle, list)) or (set(cycle) != set(accounts)) or (len(cycle) != n) or idx >= n:
        cycle = accounts[:]
        random.shuffle(cycle)
        idx = 0

    first = cycle[idx]
    idx += 1

    # If cycle finished, reshuffle next cycle
    if idx >= n:
        next_cycle = accounts[:]
        random.shuffle(next_cycle)

        # Optional: avoid same first across cycle boundary
        # (prevents ...A as last of cycle and A as first of next cycle)
        if next_cycle[0] == first and n > 1:
            # simple swap with another position
            j = random.randrange(1, n)
            next_cycle[0], next_cycle[j] = next_cycle[j], next_cycle[0]

        cycle = next_cycle
        idx = 0

    state["first_cycle"] = cycle
    state["first_cycle_idx"] = idx

    # Shuffle the remaining accounts for this run
    rest = [a for a in accounts if a != first]
    random.shuffle(rest)
    return [first] + rest

def pick_accounts_round_robin(state: Dict[str, Any], accounts: List[str]) -> List[str]:
    n = len(accounts)
    if n <= 1:
        return accounts[:]

    idx = int(state.get("next_start_idx", 0) or 0) % n

    # build cyclic order starting from idx
    order = accounts[idx:] + accounts[:idx]

    # move start for next run
    state["next_start_idx"] = (idx + 1) % n
    return order
# -----------------------
# SocialData — single account fetch
# -----------------------
def socialdata_fetch_account(
    account: str,
    cursor: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Fetch ~20 latest tweets for one account.
    Returns (tweets, next_cursor).
    """
    headers = {"Authorization": f"Bearer {SOCIALDATA_API_KEY}"}
    params: Dict[str, Any] = {"query": f"from:{account}", "type": "Latest"}
    if cursor:
        params["cursor"] = cursor
    try:
        r = request_with_retry(
            "GET", "https://api.socialdata.tools/twitter/search",
            headers=headers, params=params, timeout=30, tries=3
        )
        r.raise_for_status()
        j = r.json()
        return j.get("tweets") or [], j.get("next_cursor")
    except Exception as e:
        log(f"  ⚠️  SocialData fetch failed for @{account}: {e}")
        return [], None


# -----------------------
# Filter helper
# -----------------------
def passes_filters(
    t: Dict[str, Any],
    cutoff_dt: datetime,
    posted_set: set,
    queued_set: set,
    seen_set: set,
    content_hashes: set,   # NEW (in-memory per run)
) -> Tuple[bool, str]:
    tid = str(t.get("id_str") or t.get("id") or "")
    if not tid:                 return False, "no_id"
    if tid in seen_set:         return False, "seen"
    if tid in posted_set:       return False, "posted"
    if tid in queued_set:       return False, "queued"
    if is_retweet(t):           return False, "retweet"
    if is_quote_tweet(t):       return False, "quote"
    if is_reply(t):             return False, "reply"
    if is_video_tweet(t):       return False, "video"
    if not has_photo_media(t):  return False, "no_photo"

    created_str = extract_tweet_time(t)
    if not created_str:         return False, "no_time"
    try:
        created_dt = parse_dt(created_str)
    except Exception:
        return False, "bad_time"

    if created_dt <= cutoff_dt:
        return False, "old"

    # ---- NEW: dedupe within same run (text-based) ----
    norm = normalize_text_for_dedupe(t)
    if len(norm) >= DEDUP_MIN_LEN:
        h = simhash64(norm)
    
        for old_h in content_hashes:
            dist = hamming64(h, old_h)
            if dist <= DEDUP_HAMMING:
                if DEBUG:
                    log(f"[DEDUP] near-dup: tid={tid} ham={dist} norm={norm[:120]!r}")
                return False, "near_dup_text"
    
        # only if NOT duplicate
        t["_simhash64"] = h
    return True, ""
# -----------------------
# Fetch + filter + enqueue
# -----------------------
def fetch_and_enqueue(
    state: Dict[str, Any],
    cutoff_dt: datetime,
    queue: List[str],
    posted_list: List[str],
    tweet_data: Dict[str, Any],
) -> None:
    """
    GLOBAL cutoff strategy:

    - cutoff_dt is ONE global time (state["last_checked_time"] or start_time)
    - We fetch accounts (shuffled)
    - We enqueue tweets newer than cutoff_dt and passing filters
    - We track newest tweet time seen globally and store it as last_checked_time
      (you said you accept that this can skip older tweets from accounts not fetched)
    """

    posted_set = set(posted_list)
    queued_set = set(queue)
    seen_set   = set(state.get("seen") or [])
    content_hashes: set = set()   # NEW: resets every run


    counts: Dict[str, int] = {
        "added": 0, "seen": 0, "posted": 0, "queued": 0,
        "retweet": 0, "quote": 0, "reply": 0,
        "video": 0, "no_photo": 0, "old": 0,
        "no_time": 0, "bad_time": 0, "no_id": 0, "near_dup_text": 0,
    }

    def process_batch(tweets: List[Dict[str, Any]]) -> int:
        
        n = 0

        for t in tweets:
            if len(queue) >= THRESHOLD:
                break  # stop adding more
            tid = str(t.get("id_str") or t.get("id") or "")

            ok, reason = passes_filters(
                t, cutoff_dt, posted_set, queued_set, seen_set, content_hashes
            )

            # Mark seen AFTER checking
            if tid:
                seen_set.add(tid)
                counts["seen"] += 1

            if ok:
                # add hash only when actually enqueuing
                h = t.get("_simhash64")
                if isinstance(h, int) and h != 0:
                    content_hashes.add(h)
            
                counts["added"] += 1
                queue.append(tid)
                queued_set.add(tid)
                t.pop("_simhash64", None)
                tweet_data[tid] = t
                
                n += 1
            else:
                counts[reason] = counts.get(reason, 0) + 1

        return n

    # Shuffle accounts every run
    order = pick_accounts_round_robin(state, ACCOUNTS)
    first = order[0]
    rest = order[1:]
    random.shuffle(rest)
    shuffled_accounts = [first] + rest
    log(f"  Account order this run (rr-first+shuffle): {shuffled_accounts}")

    # Round 1: stop early when queue hits threshold
    log(f"  Round 1: fetching up to {len(shuffled_accounts)} accounts (stop at queue={THRESHOLD})...")

    LOW_YIELD_THRESHOLD = 2
    dry_accounts: List[str] = []
    cursors: Dict[str, Optional[str]] = {}
    fetched_accounts = 0

    for account in shuffled_accounts:
        if len(queue) >= THRESHOLD:
            log(f"  ✅ Queue reached threshold ({THRESHOLD}) — stopping early after {fetched_accounts} accounts.")
            break

        tweets, cursor = socialdata_fetch_account(account)
        cursors[account] = cursor
        fetched_accounts += 1

        good = process_batch(tweets)
        dbg(f"  @{account}: {len(tweets)} fetched → {good} good (queue now: {len(queue)})")

        if good < LOW_YIELD_THRESHOLD:
            dry_accounts.append(account)

    log(
        f"  Round 1 done. Fetched {fetched_accounts}/{len(shuffled_accounts)} accounts. "
        f"Queue: {len(queue)}/{THRESHOLD}. Low-yield: {dry_accounts}"
    )

    # Round 2: paginate only low-yield accounts
    if len(queue) < THRESHOLD and dry_accounts:
        log(f"  Round 2: paginating {len(dry_accounts)} low-yield accounts...")

        for account in dry_accounts:
            if len(queue) >= THRESHOLD:
                break

            cursor = cursors.get(account)
            if not cursor:
                dbg(f"  @{account}: no cursor available, skipping round 2")
                continue

            tweets, _ = socialdata_fetch_account(account, cursor=cursor)
            good = process_batch(tweets)
            dbg(f"  @{account} page 2: {len(tweets)} fetched → {good} good")

        log(f"  Round 2 done. Queue: {len(queue)}/{THRESHOLD}")

    # Persist seen list
    state["seen"] = list(seen_set)

    log(
    f"  Filter summary — added: {counts['added']} | "
    f"near_dup_text: {counts['near_dup_text']} | "
    f"reply: {counts['reply']} | quote: {counts['quote']} | retweet: {counts['retweet']} | "
    f"video: {counts['video']} | no_photo: {counts['no_photo']} | old: {counts['old']} | "
    f"dupe(posted/queued): {counts['posted'] + counts['queued']}"
)
# -----------------------
# Imgur upload
# -----------------------
def upload_to_imgur(local_path: str) -> Optional[str]:
    if not IMGUR_CLIENT_ID:
        log("❌ IMGUR_CLIENT_ID missing.")
        return None
    headers = {"Authorization": f"Client-ID {IMGUR_CLIENT_ID}"}
    with open(local_path, "rb") as f:
        r = request_with_retry("POST", "https://api.imgur.com/3/image",
                               headers=headers, files={"image": f}, timeout=60, tries=4)
    if r.status_code >= 400:
        log(f"  ❌ Imgur HTTP {r.status_code}: {r.text[:200]}")
        return None
    j = r.json()
    if not j.get("success"):
        log(f"  ❌ Imgur not success: {str(j)[:200]}")
        return None
    return j.get("data", {}).get("link")


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
        log(f"  ❌ IG container error: {j}")
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
        log(f"  ❌ IG carousel error: {j}")
        return None
    return j.get("id")

def ig_publish_with_backoff(creation_id: str, max_attempts: int = 4) -> Optional[str]:
    url  = f"https://graph.facebook.com/v21.0/{IG_USER_ID}/media_publish"
    data = {"creation_id": creation_id, "access_token": IG_ACCESS_TOKEN}
    for attempt in range(1, max_attempts + 1):
        log(f"  ▶ Publish attempt {attempt}/{max_attempts}...")
        r = request_with_retry("POST", url, data=data, timeout=30, tries=1)
        j = r.json()
        if "error" not in j and j.get("id"):
            return j["id"]
        log(f"  ⚠️  Attempt {attempt} failed: {j.get('error', {}).get('message', j)}")
        if attempt < max_attempts:
            wait = 10 * (2 ** (attempt - 1))  # 10s, 20s, 40s
            log(f"  ⏳ Backing off {wait}s...")
            time.sleep(wait)
    return None

def ig_verify_publish(
    this_caption: str,
    last_caption_text: str,
    within_seconds: int = VERIFY_WINDOW,
) -> Optional[str]:
    log(f"🔍 Verifying publish (window: {within_seconds}s)...")
    try:
        r = request_with_retry(
            "GET", f"https://graph.facebook.com/v21.0/{IG_USER_ID}/media",
            params={"fields": "id,caption,timestamp,media_type",
                    "limit": 10, "access_token": IG_ACCESS_TOKEN},
            timeout=30, tries=3
        )
        j = r.json()
    except Exception as e:
        log(f"  ❌ Verify query failed: {e}")
        return None

    if "error" in j:
        log(f"  ❌ Verify API error: {j['error'].get('message', j)}")
        return None

    posts = j.get("data", [])
    if not posts:
        log("  ℹ️  No recent posts found.")
        return None

    now = datetime.now(timezone.utc)
    carousels = [
        p for p in posts
        if (p.get("media_type") or "").upper() in ("CAROUSEL_ALBUM", "CAROUSEL")
    ]
    dbg(f"  Posts: {len(posts)} total, {len(carousels)} carousels")

    # Check 1: any carousel posted within window?
    check_ts, ts_id = False, None
    for p in carousels:
        try:
            age = (now - parse_dt(p.get("timestamp", ""))).total_seconds()
            if age <= within_seconds:
                check_ts, ts_id = True, p["id"]
                log(f"  ✅ Check 1 PASS (timestamp): {p['id']} is {age:.0f}s old")
                break
        except Exception as e:
            dbg(f"  ⚠️  Timestamp parse error: {e}")
    if not check_ts:
        log(f"  ❌ Check 1 FAIL: no carousel in last {within_seconds}s")

    # Check 2: most recent carousel caption matches this run's caption
    # AND differs from last run's caption (proves it's a new post)
    check_cap, cap_id = False, None
    if carousels:
        recent  = (carousels[0].get("caption") or "").strip()
        matches = (recent == this_caption.strip())
        differs = (recent != last_caption_text.strip()) or not last_caption_text.strip()
        if matches and differs:
            check_cap, cap_id = True, carousels[0]["id"]
            log(f"  ✅ Check 2 PASS (caption): {cap_id}")
        else:
            log(f"  ❌ Check 2 FAIL: matches={matches}, differs_from_last={differs}")
            dbg(f"     expected: {this_caption.strip()!r}")
            dbg(f"     found:    {recent!r}")
            dbg(f"     last:     {last_caption_text.strip()!r}")

    if check_ts and check_cap:
        log(f"  ✅✅ Both checks PASS — confirmed: {ts_id}")
        return ts_id
    if check_ts:
        log(f"  ⚠️  Timestamp only passed — treating as success: {ts_id}")
        return ts_id
    if check_cap:
        log(f"  ⚠️  Caption only passed — treating as success: {cap_id}")
        return cap_id

    log("  ❌ Both checks FAILED — post did not go through.")
    return None


# -----------------------
# Cleanup
# -----------------------
def cleanup_screenshots(tweet_ids: List[str]) -> None:
    for tid in tweet_ids:
        for ext in (".jpg", ".jpeg", ".png"):
            p = os.path.join(SCREENSHOT_DIR, f"{tid}{ext}")
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


# -----------------------
# Main
# -----------------------
def main():
    total_t0 = perf_counter()
    log("=== cricket-bot starting ===")
    log(f"Accounts: {','.join(ACCOUNTS)}")
    log(f"Threshold: {THRESHOLD} | DRY_RUN: {int(DRY_RUN)} | DEBUG: {int(DEBUG)}")

    ensure_dirs()

    state = load_state()
    recover_in_flight(state)
    bound_state(state)
    save_state(state)

    state["total_runs"] = int(state.get("total_runs", 0)) + 1

    # First run: set start_time and exit
    if not state.get("start_time"):
        state["start_time"] = utc_now_iso()
        bound_state(state)
        save_state(state)
        log(f"Initialized start_time = {state['start_time']}")
        log("First run exits. Next runs process tweets AFTER this time.")
        log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
        return

    try:
        cutoff_str = state.get("last_post_time") or state["start_time"]
        cutoff_dt = parse_dt(cutoff_str)
    except Exception as e:
        log(f"❌ Bad start_time: {state.get('start_time')} ({e}) — resetting")
        state["start_time"] = utc_now_iso()
        bound_state(state)
        save_state(state)
        log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
        return

    queue: List[str]           = state["queue"]
    posted_list: List[str]     = state["posted"]
    tweet_data: Dict[str, Any] = state.get("tweet_data", {})

    this_caption, this_caption_index = pick_caption(state)
    last_caption_text = state.get("last_caption_text", "")
    log(f"Caption [{this_caption_index}]: {this_caption!r}  |  Last: {last_caption_text!r}")

    # 1) Fetch + filter
    with StageTimer("1) Fetch & filter (per-account)"):
        fetch_and_enqueue(state, cutoff_dt, queue, posted_list, tweet_data)
    # Sort oldest → newest (IMPORTANT: in-place)
    queue[:] = sort_queue_oldest_first(queue, tweet_data)

    log(f"Queue: {len(queue)}/{THRESHOLD}")

    # 2) Save state
    with StageTimer("2) Save state"):
        state["queue"]      = queue
        state["posted"]     = posted_list
        state["tweet_data"] = tweet_data
        bound_state(state)
        save_state(state)

    if len(state["queue"]) < THRESHOLD:
        log("Not enough queued yet. Exiting.")
        log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
        return

    # 3) Prepare batch
    batch = state["queue"][:THRESHOLD]
    log(f"Batch: {len(batch)} tweets (sample: {batch[:3]})")

    # 4) Render screenshots
    with StageTimer("3) Render screenshots"):
        good_pairs: List[Tuple[str, str]] = []
        ensure_dirs()
        batch_payload = []

        for i, tid in enumerate(batch, 1):
            log(f"  ▶ prepare {i}/{THRESHOLD}: {tid}")
            t_obj = tweet_data.get(tid)
            if not t_obj:
                log(f"  ⚠️  Missing tweet_data for {tid}")
                continue
            out_path = os.path.join(SCREENSHOT_DIR, f"{tid}.jpg")
            if os.path.exists(out_path):
                dbg(f"Reusing {tid}")
                good_pairs.append((tid, out_path))
                continue
            batch_payload.append({"tweet": t_obj, "out": out_path})

        if batch_payload:
            if not os.path.exists(SCREENSHOT_SCRIPT):
                log(f"❌ screenshot.js not found at {SCREENSHOT_SCRIPT}")
            else:
                cmd = ["node", SCREENSHOT_SCRIPT, "--batch", json.dumps(batch_payload)]
                env = os.environ.copy()
                env["SHOW_STATS"] = "1" if SHOW_STATS else "0"
                try:
                    result = subprocess.run(cmd, timeout=240, capture_output=True,
                                            text=True, env=env)
                    if result.returncode != 0:
                        log("❌ screenshot batch failed")
                        log(f"  stderr: {result.stderr[:1200]}")
                    else:
                        marker = "__BATCH_RESULT__"
                        parsed = False
                        for line in result.stdout.splitlines():
                            if line.startswith(marker):
                                parsed = True
                                data = json.loads(line[len(marker):])
                                for item in data.get("results", []):
                                    if item.get("ok") and item.get("out") and os.path.exists(item["out"]):
                                        t = os.path.splitext(os.path.basename(item["out"]))[0]
                                        good_pairs.append((t, item["out"]))
                        if not parsed:
                            dbg("No __BATCH_RESULT__ — falling back to fs check")
                            for it in batch_payload:
                                if os.path.exists(it["out"]):
                                    t = os.path.splitext(os.path.basename(it["out"]))[0]
                                    good_pairs.append((t, it["out"]))
                        log(f"  ✅ {len(good_pairs)} screenshots ready")
                except subprocess.TimeoutExpired:
                    log("❌ screenshot batch timeout")
                except Exception as e:
                    log(f"❌ screenshot batch error: {e}")

    log(f"Screenshots ok: {len(good_pairs)}/{THRESHOLD}")
    if len(good_pairs) < 2:
        log("Not enough screenshots (need >=2). Keeping queue. Exiting.")
        log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
        return

    # 5) Upload to Imgur
    with StageTimer("4) Upload to Imgur"):
        public_urls: List[str] = []
        imgur_good_ids: List[str] = []
        for i, (tid, path) in enumerate(good_pairs, 1):
            log(f"  ▶ imgur {i}/{len(good_pairs)}: {tid}")
            url = upload_to_imgur(path)
            if url:
                public_urls.append(url)
                imgur_good_ids.append(tid)
                dbg(f"  url: {url}")
            else:
                log(f"  ⚠️  Imgur failed: {tid}")
            time.sleep(SLEEP_IMGUR)

    log(f"Imgur ok: {len(public_urls)}/{len(good_pairs)}")
    if len(public_urls) < 2:
        log("Not enough public URLs (need >=2). Keeping queue. Exiting.")
        return

    # 6) IG containers
    with StageTimer("5) Create IG containers"):
        container_ids: List[str] = []
        for i, url in enumerate(public_urls, 1):
            log(f"  ▶ ig container {i}/{len(public_urls)}")
            cid = ig_create_image_container(url)
            if cid:
                container_ids.append(cid)
                dbg(f"  container_id: {cid}")
            else:
                log("  ⚠️  IG container failed")
            sleep_time = random.uniform(SLEEP_IG_CONTAINER_MIN, SLEEP_IG_CONTAINER_MAX)
            time.sleep(sleep_time)

    log(f"IG containers ok: {len(container_ids)}/{len(public_urls)}")
    if len(container_ids) < 2:
        log("Not enough containers (need >=2). Keeping queue. Exiting.")
        log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
        return

    # 7) Create carousel
    with StageTimer("6) Create carousel"):
        car_id = ig_create_carousel(container_ids, this_caption)
        if not car_id:
            log("❌ Carousel create failed. Keeping queue. Exiting.")
            log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
            return
        log(f"✅ Carousel id: {car_id}")

    # 8) Publish
    with StageTimer("7) Publish"):
        log(f"⏳ Waiting {SLEEP_BEFORE_PUBLISH}s...")
        time.sleep(SLEEP_BEFORE_PUBLISH)

        if DRY_RUN:
            log("🧪 DRY_RUN=1 → Skipping publish.")
            log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
            return

        state["in_flight"] = imgur_good_ids[:]
        bound_state(state)
        save_state(state)

        post_id = ig_publish_with_backoff(car_id, max_attempts=1)

        if not post_id:
            log(f"⏳ All attempts failed. Waiting {VERIFY_WAIT}s then verifying...")
            time.sleep(VERIFY_WAIT)
            post_id = ig_verify_publish(this_caption, last_caption_text,
                                        within_seconds=VERIFY_WINDOW)
            if not post_id:
                log("❌ Publish failed + verification negative. Keeping queue. Exiting.")
                log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
                return
            log(f"✅ Verified post_id: {post_id}")
        else:
            log(f"✅ Published post_id: {post_id}")

    # 9) Update state + cleanup
    with StageTimer("8) Update state + cleanup"):
        posted_set = set(posted_list)
        good_set   = set(imgur_good_ids)
        for tid in imgur_good_ids:
            if tid not in posted_set:
                posted_list.append(tid)
                posted_set.add(tid)

        state["queue"]              = [t for t in queue if t not in good_set]
        state["posted"]             = posted_list
        state["tweet_data"]         = tweet_data
        state["total_carousels"]    = int(state.get("total_carousels", 0)) + 1
        state["in_flight"]          = []
        state["last_caption_index"] = this_caption_index
        state["last_caption_text"]  = this_caption
        state["last_post_time"] = utc_now_iso()

        bound_state(state)
        save_state(state)
        cleanup_screenshots(imgur_good_ids)

    log(
        f"✅ DONE — runs: {state['total_runs']} | carousels: {state['total_carousels']} | "
        f"queue left: {len(state['queue'])} | seen: {len(state['seen'])}"
    )
    log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")


if __name__ == "__main__":
    main()
