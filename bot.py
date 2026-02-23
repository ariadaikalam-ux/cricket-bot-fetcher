import os
import json
import time
import random
import subprocess
from time import perf_counter
from datetime import datetime, timezone, timedelta
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

THRESHOLD  = random.randint(7, 10)
DRY_RUN    = os.environ.get("DRY_RUN", "0") == "1"
DEBUG      = os.environ.get("DEBUG", "0") == "1"
SHOW_STATS = os.environ.get("SHOW_STATS", "0") == "1"

# Two alternating captions
CAPTIONS = [
    os.environ.get("INSTAGRAM_CAPTION_0", "🏏 Latest Cricket Tweets Roundup!"),
    os.environ.get("INSTAGRAM_CAPTION_1", "🏏 Best Cricket Tweets Right Now!"),
]

# Tuning
SLEEP_IG_CONTAINER_MIN = float(os.environ.get("SLEEP_IG_CONTAINER_MIN", "1"))
SLEEP_IG_CONTAINER_MAX = float(os.environ.get("SLEEP_IG_CONTAINER_MAX", "2"))
SLEEP_BEFORE_PUBLISH   = float(os.environ.get("SLEEP_BEFORE_PUBLISH", "5.0"))
SLEEP_IMGUR            = float(os.environ.get("SLEEP_IMGUR", "0.5"))
VERIFY_WAIT            = float(os.environ.get("VERIFY_WAIT", "8.0"))
VERIFY_WINDOW          = int(os.environ.get("VERIFY_WINDOW", "600"))
DEDUP_MIN_LEN          = 25
DEDUP_HAMMING          = 7

# Seconds subtracted from checked_until_time when building the since_time
# for each API query — avoids missing tweets near the boundary.
SINCE_OVERLAP_SECONDS = int(os.environ.get("SINCE_OVERLAP_SECONDS", "900"))  # 15 min

BASE_DIR            = os.path.dirname(os.path.abspath(__file__))
STATE_FILE          = os.path.join(BASE_DIR, "state.json")
SCREENSHOT_DIR      = os.path.join(BASE_DIR, "screenshots")
SCREENSHOT_SCRIPT   = os.path.join(BASE_DIR, "screenshot.js")
QUEUE_MAX_AGE_HOURS = float(os.environ.get("QUEUE_MAX_AGE_HOURS", "6"))

# Server-side filters baked into every search query.
# API returns only original photo tweets — not retweets, replies, quotes, or videos.
# Local filter functions remain as a safety net but rarely fire.
SOCIALDATA_FILTERS = (
    "filter:images"
    " -filter:videos"
    " -filter:retweets"
    " -filter:nativeretweets"
    " -filter:replies"
    " -filter:quote"
)

# OR-query batching: combine multiple accounts into one request.
#   (from:a OR from:b OR from:c) filter:images ... since_time:N
# With 20 accounts and chunk size 10 → 2 requests instead of 20.
# Both limits must be satisfied to keep a chunk together.
OR_CHUNK_MAX_ACCOUNTS = int(os.environ.get("OR_CHUNK_MAX_ACCOUNTS", "10"))
OR_CHUNK_MAX_CHARS    = int(os.environ.get("OR_CHUNK_MAX_CHARS", "1400"))

SESSION        = requests.Session()
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
    s = re.sub(r'([+-])(\d{2})(\d{2})$', r'\1\2:\3', s)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def dt_to_unix(dt: datetime) -> int:
    return int(dt.timestamp())

def extract_tweet_time(t: Dict[str, Any]) -> Optional[str]:
    if isinstance(t.get("tweet_created_at"), str) and t["tweet_created_at"].strip():
        return t["tweet_created_at"].strip()
    for k in ("created_at", "createdAt", "date", "timestamp"):
        v = t.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

def extract_tweet_author(t: Dict[str, Any]) -> Optional[str]:
    """
    Extract lowercased screen_name of the tweet author.
    Used to attribute per-account stats from OR-query results,
    where multiple accounts share one API request.

    Only uses unique handle fields (screen_name, username, author_username).
    The display `name` field ("Cricket Central", "Mufaddal Vohra") is
    intentionally excluded — it is non-unique and would corrupt stats keys.
    """
    user = t.get("user") or {}
    if isinstance(user, dict):
        sn = user.get("screen_name") or user.get("username")
        if sn:
            return str(sn).lower().strip()
    for k in ("screen_name", "username", "author_username"):
        v = t.get(k)
        if v:
            return str(v).lower().strip()
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
# Tweet filters (safety net — API filters handle most cases)
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
    if (t.get("type") or "").lower() == "reply":
        return True
    for k in ("in_reply_to_status_id", "in_reply_to_status_id_str"):
        if t.get(k) not in (None, "", 0, "0"):
            return True
    for k in ("in_reply_to_user_id", "in_reply_to_user_id_str"):
        if t.get(k) not in (None, "", 0, "0"):
            return True
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
    txt = re.sub(r"https?://\S+|www\.\S+", " ", txt)
    txt = re.sub(r"@\w+", " ", txt)
    txt = txt.replace("#", " ")
    txt = re.sub(r"\b\d{3,}\b", " 0 ", txt)
    txt = re.sub(r"[^a-z0-9\s]+", " ", txt)
    toks = [w for w in txt.split() if w and w not in STOPWORDS]
    return " ".join(toks).strip()

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
        return x.bit_count()
    except AttributeError:
        return bin(x).count("1")


# -----------------------
# State
# -----------------------
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {
            "start_time": None,
            # checked_until_time is the SOLE fetch watermark.
            # Updated each run to the max tweet time seen (or now on quiet runs).
            # ⚠️  last_post_time is AUDIT-ONLY — never use it as a fetch cutoff.
            "checked_until_time": None,
            "queue": [],
            "posted": [],
            "seen": [],
            "tweet_data": {},
            "total_runs": 0,
            "total_carousels": 0,
            "last_caption_index": -1,
            "last_caption_text": "",
            # last_post_time = UTC timestamp when an IG carousel was successfully
            # published. AUDIT / MONITORING USE ONLY.
            # ⚠️  Must NEVER be read for fetch cutoff logic. Use checked_until_time.
            "last_post_time": None,
            "next_start_idx": 0,
            "account_stats": {},
        }
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        s = json.load(f)

    s.setdefault("start_time", None)
    s.setdefault("checked_until_time", None)
    s.setdefault("queue", [])
    s.setdefault("posted", [])
    s.setdefault("seen", [])
    s.setdefault("tweet_data", {})
    s.setdefault("total_runs", 0)
    s.setdefault("total_carousels", 0)
    s.setdefault("in_flight", [])
    s.setdefault("last_caption_index", -1)
    s.setdefault("last_caption_text", "")
    s.setdefault("last_post_time", None)   # audit-only, never read for fetching
    s.setdefault("next_start_idx", 0)
    s.setdefault("account_stats", {})
    # Clean up keys removed in this version
    s.pop("first_cycle", None)
    s.pop("first_cycle_idx", None)
    if not isinstance(s["account_stats"], dict):   s["account_stats"] = {}
    if not isinstance(s.get("in_flight"), list):   s["in_flight"] = []
    if not isinstance(s["queue"], list):            s["queue"] = []
    if not isinstance(s["posted"], list):           s["posted"] = []
    if not isinstance(s["seen"], list):             s["seen"] = []
    if not isinstance(s["tweet_data"], dict):       s["tweet_data"] = {}

    return s

def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def bound_state(state: Dict[str, Any]) -> None:
    state["queue"]  = state["queue"][-5000:]
    state["posted"] = state["posted"][-5000:]
    state["seen"]   = state["seen"][-10000:]
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

def evict_stale_queue(state: Dict[str, Any], tweet_data: Dict[str, Any],
                      max_age_hours: float = 8.0) -> None:
    now = datetime.now(timezone.utc)
    fresh, evicted = [], 0
    for tid in state["queue"]:
        t = tweet_data.get(tid, {})
        ts = extract_tweet_time(t)
        if not ts:
            fresh.append(tid)
            continue
        try:
            age_hours = (now - parse_dt(ts)).total_seconds() / 3600
            if age_hours <= max_age_hours:
                fresh.append(tid)
            else:
                evicted += 1
                dbg(f"  evicted stale: {tid} age={age_hours:.1f}h")
        except Exception:
            fresh.append(tid)
    state["queue"] = fresh
    if evicted:
        log(f"  🗑️  Evicted {evicted} stale tweet(s) from queue (older than {max_age_hours}h)")


# -----------------------
# Caption rotation
# -----------------------
def pick_caption(state: Dict[str, Any]) -> Tuple[str, int]:
    last_index = int(state.get("last_caption_index", -1))
    next_index = (last_index + 1) % len(CAPTIONS)
    return CAPTIONS[next_index], next_index


# -----------------------
# OR-query chunking
# -----------------------
def build_or_query_chunks(accounts: List[str], since_unix: int) -> List[str]:
    """
    Split accounts into chunks and build one search query string per chunk.

    Query format:
        (from:a OR from:b OR from:c) filter:images -filter:videos ... since_time:N

    A new chunk is started when adding the next account would exceed either:
      - OR_CHUNK_MAX_ACCOUNTS  (default 10)
      - OR_CHUNK_MAX_CHARS     (default 1400) — conservative GET URL length limit

    Result: ceil(len(accounts) / chunk_size) requests instead of len(accounts).
    With 20 accounts and chunk=10 → 2 requests per run.
    """
    suffix = f" {SOCIALDATA_FILTERS} since_time:{since_unix}"
    chunks: List[str] = []
    current: List[str] = []

    def flush():
        if not current:
            return
        from_part = " OR ".join(f"from:{a}" for a in current)
        query = f"({from_part}){suffix}" if len(current) > 1 else f"{from_part}{suffix}"
        chunks.append(query)
        current.clear()

    for account in accounts:
        tentative = current + [account]
        from_part = " OR ".join(f"from:{a}" for a in tentative)
        tentative_query = (
            f"({from_part}){suffix}" if len(tentative) > 1 else f"{from_part}{suffix}"
        )
        if (len(tentative) > OR_CHUNK_MAX_ACCOUNTS
                or len(tentative_query) > OR_CHUNK_MAX_CHARS) and current:
            flush()
        current.append(account)

    flush()
    return chunks


# -----------------------
# SocialData — execute one search query
# -----------------------
def socialdata_fetch_query(query: str) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Execute one pre-built search query and return (tweets, ok).

    ok=True  → HTTP 200 received, response parsed successfully (tweets may be [])
    ok=False → request failed or exception raised

    Callers MUST check ok to distinguish "API returned 0 tweets" (safe to advance
    watermark) from "API failed and returned nothing" (unsafe — keep watermark).
    """
    headers = {"Authorization": f"Bearer {SOCIALDATA_API_KEY}"}
    params: Dict[str, Any] = {"query": query, "type": "Latest"}
    dbg(f"  query ({len(query)} chars): {query}")
    try:
        r = request_with_retry(
            "GET", "https://api.socialdata.tools/twitter/search",
            headers=headers, params=params, timeout=30, tries=3
        )
        r.raise_for_status()
        j = r.json()
        return j.get("tweets") or [], True
    except Exception as e:
        log(f"  ⚠️  SocialData fetch failed: {e}")
        return [], False


# -----------------------
# Per-account stats
# -----------------------
def update_account_stats(
    state: Dict[str, Any],
    account: str,
    *,
    fetched: int,
    evaluated: int,
    good: int,
) -> None:
    state.setdefault("account_stats", {})
    st = state["account_stats"].setdefault(account, {
        "runs": 0, "fetched": 0, "evaluated": 0, "good": 0,
        "last_run": None, "last_fetched": 0, "last_evaluated": 0,
        "last_good": 0, "history": [],
    })
    st["runs"]      = int(st.get("runs", 0)) + 1
    st["fetched"]   = int(st.get("fetched", 0)) + int(fetched)
    st["evaluated"] = int(st.get("evaluated", 0)) + int(evaluated)
    st["good"]      = int(st.get("good", 0)) + int(good)
    st["last_run"]       = utc_now_iso()
    st["last_fetched"]   = int(fetched)
    st["last_evaluated"] = int(evaluated)
    st["last_good"]      = int(good)
    hist = st.get("history")
    if not isinstance(hist, list):
        hist = []
    hist.append({"ts": st["last_run"], "fetched": int(fetched),
                 "evaluated": int(evaluated), "good": int(good)})
    st["history"] = hist[-50:]

def flush_per_account_stats(
    state: Dict[str, Any],
    per_account: Dict[str, Dict[str, int]],
) -> None:
    """
    Commit per-account counters (derived from each tweet's author field)
    into persistent account_stats.

    Because we use OR queries, multiple accounts share one API request.
    Per-account granularity is recovered here by reading extract_tweet_author()
    on each returned tweet — no stats are lost.
    """
    for account, c in per_account.items():
        update_account_stats(
            state, account,
            fetched=c.get("fetched", 0),
            evaluated=c.get("evaluated", 0),
            good=c.get("good", 0),
        )


# -----------------------
# Filter helper (safety net — API handles most filtering now)
# -----------------------
def passes_filters(
    t: Dict[str, Any],
    cutoff_dt: datetime,
    posted_set: set,
    queued_set: set,
    seen_set: set,
    content_hashes: set,
) -> Tuple[bool, str]:
    tid = str(t.get("id_str") or t.get("id") or "")
    if not tid:                 return False, "no_id"
    if tid in seen_set:         return False, "seen"
    if tid in posted_set:       return False, "posted"
    if tid in queued_set:       return False, "queued"

    # Safety-net: API query already excludes these, but a small fraction
    # may slip through — keep local checks as a defensive layer.
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

    # Near-duplicate text check within this run's batch
    norm = normalize_text_for_dedupe(t)
    if len(norm) >= DEDUP_MIN_LEN:
        h = simhash64(norm)
        for old_h in content_hashes:
            if hamming64(h, old_h) <= DEDUP_HAMMING:
                if DEBUG:
                    log(f"[DEDUP] near-dup: tid={tid} norm={norm[:120]!r}")
                return False, "near_dup_text"
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
) -> Tuple[Optional[datetime], bool, bool]:
    """
    Fetch tweets for all accounts using OR-query batching + global since_time.

    Fetch cutoff:
      - cutoff_dt = checked_until_time − SINCE_OVERLAP_SECONDS (computed by caller)
      - since_unix = dt_to_unix(cutoff_dt), embedded in each query string
      ⚠️  last_post_time is AUDIT-ONLY and is NEVER read here.

    OR-query batching:
      - Accounts are split into chunks (≤OR_CHUNK_MAX_ACCOUNTS, ≤OR_CHUNK_MAX_CHARS)
      - Each chunk = 1 API request  →  20 accounts = ~2 requests instead of 20
      - Fetch stops early if queue reaches THRESHOLD mid-chunk-loop

    Per-account stats:
      - Derived from each tweet's author field via extract_tweet_author()
      - No granularity lost despite batching

    Returns:
      (max_tweet_dt, all_chunks_completed)
      - max_tweet_dt: newest tweet timestamp observed across ALL returned tweets
        (updated before filter decisions, so reflects true API output)
      - all_chunks_completed: True only if every chunk was fetched AND each
        returned HTTP 200 with no exception (i.e. no early break, no API error)
    """
    posted_set     = set(posted_list)
    queued_set     = set(queue)
    seen_set       = set(state.get("seen") or [])
    content_hashes: set = set()

    since_unix = dt_to_unix(cutoff_dt)
    log(f"  since_time: {cutoff_dt.isoformat()} (unix={since_unix})")

    counts: Dict[str, int] = {
        "added": 0, "seen": 0, "posted": 0, "queued": 0,
        "retweet": 0, "quote": 0, "reply": 0,
        "video": 0, "no_photo": 0, "old": 0,
        "no_time": 0, "bad_time": 0, "no_id": 0, "near_dup_text": 0,
    }

    # Per-account counters keyed by lowercased screen_name.
    # Populated by reading each tweet's author — not by request grouping.
    per_account: Dict[str, Dict[str, int]] = {}

    def inc(bucket: str, field: str, n: int = 1) -> None:
        pa = per_account.setdefault(bucket, {"fetched": 0, "evaluated": 0, "good": 0})
        pa[field] = pa.get(field, 0) + n

    max_tweet_dt: Optional[datetime] = None

    def _update_max_dt(t: Dict[str, Any]) -> None:
        """
        Update max_tweet_dt from this tweet's timestamp.
        Called for EVERY returned tweet before any filter decision,
        so the watermark reflects the newest tweet the API gave us —
        not just the newest tweet we decided to keep.
        Prevents max_tweet_dt staying None when all tweets fail filters
        (seen/queued/near-dup/etc), which would cause the caller to
        incorrectly treat this as a "no tweets seen" quiet run.
        """
        nonlocal max_tweet_dt
        ts = extract_tweet_time(t)
        if ts:
            try:
                tdt = parse_dt(ts)
                if max_tweet_dt is None or tdt > max_tweet_dt:
                    max_tweet_dt = tdt
            except Exception:
                pass

    def process_batch(tweets: List[Dict[str, Any]]) -> None:
        queue_full = False
    
        for t in tweets:
            # Always update watermark for every returned tweet
            _update_max_dt(t)
    
            # If queue is already full, don't evaluate/enqueue,
            # but keep scanning to capture the newest timestamp.
            if queue_full:
                continue
    
            if len(queue) >= THRESHOLD:
                queue_full = True
                continue
    
            tid    = str(t.get("id_str") or t.get("id") or "")
            author = (extract_tweet_author(t) or "unknown")
            inc(author, "fetched")
    
            ok, reason = passes_filters(
                t, cutoff_dt, posted_set, queued_set, seen_set, content_hashes
            )
    
            if tid:
                seen_set.add(tid)
                counts["seen"] += 1
            inc(author, "evaluated")
    
            if ok:
                h = t.get("_simhash64")
                if isinstance(h, int) and h != 0:
                    content_hashes.add(h)
                counts["added"] += 1
                queue.append(tid)
                queued_set.add(tid)
                t.pop("_simhash64", None)
                tweet_data[tid] = t
                inc(author, "good")
            else:
                counts[reason] = counts.get(reason, 0) + 1
    # Build OR-query chunks from the full account list
    chunks = build_or_query_chunks(ACCOUNTS, since_unix)
    log(f"  OR-query chunks: {len(chunks)} request(s) for {len(ACCOUNTS)} account(s) "
        f"(max_per_chunk={OR_CHUNK_MAX_ACCOUNTS}, max_chars={OR_CHUNK_MAX_CHARS})")

    # all_chunks_completed: True only if every chunk ran AND every request succeeded.
    # Set to False on early queue-threshold break OR on any API failure.
    # Used by caller to gate "advance watermark to now" on quiet runs — we must
    # never advance the watermark if a chunk failed, because we can't confirm
    # the window was truly empty (the failed chunk might have had tweets).
    all_chunks_completed = True

    # any_tweets_returned: True if at least one chunk returned ≥1 tweet from the API.
    # Tracked independently of max_tweet_dt so the "genuinely quiet" check in the
    # caller remains correct even if _update_max_dt() logic changes in future.
    any_tweets_returned = False

    for i, query in enumerate(chunks, 1):
        if len(queue) >= THRESHOLD:
            log(f"  ✅ Queue reached threshold ({THRESHOLD}) — skipping remaining chunks.")
            all_chunks_completed = False
            break
        log(f"  Chunk {i}/{len(chunks)} ({len(query)} chars)")
        tweets, ok = socialdata_fetch_query(query)
        if not ok:
            # API error — mark incomplete so caller won't advance watermark to now
            all_chunks_completed = False
        if tweets:
            any_tweets_returned = True
        log(f"  Chunk {i}: {len(tweets)} tweet(s) returned (ok={ok})")
        process_batch(tweets)

    # Commit per-account stats derived from tweet authors
    flush_per_account_stats(state, per_account)

    # Persist updated seen list
    state["seen"] = list(seen_set)

    log(
        f"  Filter summary — added: {counts['added']} | "
        f"near_dup_text: {counts['near_dup_text']} | "
        f"reply: {counts['reply']} | quote: {counts['quote']} | "
        f"retweet: {counts['retweet']} | video: {counts['video']} | "
        f"no_photo: {counts['no_photo']} | old: {counts['old']} | "
        f"dupe(posted/queued): {counts['posted'] + counts['queued']}"
    )
    log(f"  all_chunks_completed={all_chunks_completed} | any_tweets_returned={any_tweets_returned}")
    if DEBUG and per_account:
        log("  Per-account this run:")
        for acct, c in sorted(per_account.items()):
            log(f"    @{acct}: fetched={c['fetched']} evaluated={c['evaluated']} good={c['good']}")

    return max_tweet_dt, all_chunks_completed, any_tweets_returned


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
            wait = 10 * (2 ** (attempt - 1))
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
    log(f"OR_CHUNK: max_accounts={OR_CHUNK_MAX_ACCOUNTS} max_chars={OR_CHUNK_MAX_CHARS} | "
        f"SINCE_OVERLAP: {SINCE_OVERLAP_SECONDS}s")

    ensure_dirs()

    state = load_state()
    recover_in_flight(state)
    bound_state(state)
    evict_stale_queue(state, state.get("tweet_data", {}), max_age_hours=QUEUE_MAX_AGE_HOURS)
    save_state(state)

    state["total_runs"] = int(state.get("total_runs", 0)) + 1

    # ── First run ──────────────────────────────────────────────────────────
    # Initialise watermark to now and exit.
    # Subsequent runs fetch only tweets posted AFTER this moment.
    if not state.get("start_time"):
        now_iso = utc_now_iso()
        state["start_time"]         = now_iso
        state["checked_until_time"] = now_iso
        bound_state(state)
        save_state(state)
        log(f"Initialized start_time = checked_until_time = {now_iso}")
        log("First run exits. Next runs process tweets AFTER this time.")
        log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
        return

    # ── Fetch cutoff ───────────────────────────────────────────────────────
    # checked_until_time is the ONLY source of truth for the fetch window.
    # Fallback to start_time if checked_until_time was not yet written
    # (e.g. migrating from an older state file).
    #
    # ⚠️  last_post_time is AUDIT-ONLY — it records when an IG post succeeded.
    #     It must NEVER be used here. Using it would cause the fetch window to
    #     stall on runs where no post happens, leading to repeated re-fetches
    #     and unnecessary API cost.
    raw_cutoff_str = state.get("checked_until_time") or state.get("start_time")
    try:
        raw_cutoff_dt = parse_dt(raw_cutoff_str)
    except Exception as e:
        log(f"❌ Bad checked_until_time: {raw_cutoff_str!r} ({e}) — resetting to now")
        now_iso = utc_now_iso()
        state["start_time"]         = now_iso
        state["checked_until_time"] = now_iso
        bound_state(state)
        save_state(state)
        log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
        return

    # Subtract overlap to avoid missing tweets right at the boundary
    cutoff_dt = raw_cutoff_dt - timedelta(seconds=SINCE_OVERLAP_SECONDS)
    log(f"Raw watermark:    {raw_cutoff_dt.isoformat()}")
    log(f"Cutoff (overlap): {cutoff_dt.isoformat()} (-{SINCE_OVERLAP_SECONDS}s)")

    queue: List[str]           = state["queue"]
    posted_list: List[str]     = state["posted"]
    tweet_data: Dict[str, Any] = state.get("tweet_data", {})

    this_caption, this_caption_index = pick_caption(state)
    last_caption_text = state.get("last_caption_text", "")
    log(f"Caption [{this_caption_index}]: {this_caption!r}  |  Last: {last_caption_text!r}")

    # ── 1) Fetch + filter ──────────────────────────────────────────────────
    with StageTimer("1) Fetch & filter (OR-query batches)"):
        max_tweet_dt, all_chunks_completed, any_tweets_returned = fetch_and_enqueue(
            state, cutoff_dt, queue, posted_list, tweet_data
        )

    # ── Advance checked_until_time (safe watermark logic) ──────────────────
    #
    # Three cases:
    #
    # A) Tweets were observed (max_tweet_dt is not None):
    #    → Use max tweet timestamp. Precise, always safe. Doesn't matter
    #      whether we finished all chunks or broke early — the watermark only
    #      moves to what we actually observed from the API.
    #
    # B) No tweets observed AND all chunks completed successfully AND
    #    no chunk returned any tweets:
    #    → Genuinely quiet window (API returned 0 for every chunk, no errors).
    #      Safe to advance to now so we don't re-query this empty window forever.
    #      all three conditions must hold — any_tweets_returned guards against
    #      a future change where max_tweet_dt logic is modified.
    #
    # C) Anything else (early break, API failure, or uncertain state):
    #    → Keep the previous watermark unchanged. The overlap will re-cover
    #      this window next run. This is the critical fix — the old code advanced
    #      to "now" here, which could permanently skip tweets in chunks we never
    #      fetched or chunks that silently failed.
    #
    prev_watermark = state.get("checked_until_time") or state.get("start_time")

    if max_tweet_dt is not None:
        # Case A — always safe
        state["checked_until_time"] = max_tweet_dt.isoformat()
        log(f"  ✅ checked_until_time → {state['checked_until_time']} (max tweet observed)")
    elif all_chunks_completed and (not any_tweets_returned):
        # Case B — confirmed quiet: every chunk ran, every request succeeded, 0 tweets total
        state["checked_until_time"] = utc_now_iso()
        log(f"  ℹ️  All chunks OK, 0 tweets — watermark advanced to now: {state['checked_until_time']}")
    else:
        # Case C — incomplete or uncertain run, keep prev watermark
        state["checked_until_time"] = prev_watermark
        log(f"  ⚠️  Incomplete/uncertain fetch — watermark unchanged: {prev_watermark}")

    queue[:] = sort_queue_oldest_first(queue, tweet_data)
    log(f"Queue: {len(queue)}/{THRESHOLD}")

    # ── 2) Save state ──────────────────────────────────────────────────────
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

    # ── 3) Prepare batch ───────────────────────────────────────────────────
    batch = state["queue"][:THRESHOLD]
    log(f"Batch: {len(batch)} tweets (sample: {batch[:3]})")

    # ── 4) Render screenshots ──────────────────────────────────────────────
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
                                    if item.get("ok") and item.get("out") \
                                            and os.path.exists(item["out"]):
                                        t = os.path.splitext(
                                            os.path.basename(item["out"]))[0]
                                        good_pairs.append((t, item["out"]))
                        if not parsed:
                            dbg("No __BATCH_RESULT__ — falling back to fs check")
                            for it in batch_payload:
                                if os.path.exists(it["out"]):
                                    t = os.path.splitext(
                                        os.path.basename(it["out"]))[0]
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

    # ── 5) Upload to Imgur ─────────────────────────────────────────────────
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

    # ── 6) IG containers ───────────────────────────────────────────────────
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
            time.sleep(random.uniform(SLEEP_IG_CONTAINER_MIN, SLEEP_IG_CONTAINER_MAX))

    log(f"IG containers ok: {len(container_ids)}/{len(public_urls)}")
    if len(container_ids) < 2:
        log("Not enough containers (need >=2). Keeping queue. Exiting.")
        log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
        return

    # ── 7) Create carousel ─────────────────────────────────────────────────
    with StageTimer("6) Create carousel"):
        car_id = ig_create_carousel(container_ids, this_caption)
        if not car_id:
            log("❌ Carousel create failed. Keeping queue. Exiting.")
            log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
            return
        log(f"✅ Carousel id: {car_id}")

    # ── 8) Publish ─────────────────────────────────────────────────────────
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

    # ── 9) Update state + cleanup ──────────────────────────────────────────
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
        # ⚠️  last_post_time = AUDIT-ONLY. Records when IG publish succeeded.
        #     Never read this for fetch cutoff — use checked_until_time only.
        state["last_post_time"]     = utc_now_iso()

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
