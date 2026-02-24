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
from collections import defaultdict
import hashlib

# -----------------------
# Env / Config
# -----------------------
SOCIALDATA_API_KEY = os.environ["SOCIALDATA_API_KEY"]
IMGUR_CLIENT_ID    = os.environ.get("IMGUR_CLIENT_ID", "")

TWITTER_ACCOUNTS = os.environ.get(
    "TWITTER_ACCOUNTS",
    "mufaddal_vohra,cricketgyann,wxtreme18,rcbtweets,chennaiipl,ctrlmemes_,mipaltan,criccrazyjohns,cricketcentrl,tuktuk_academy,shebas_10dulkar,gemsofcricket,mohalimonster,mahi_patel_07,1no_aalsi_,vipintiwari952,justtalkcricket,vikrant_1589,selflesscricket"
)
ACCOUNTS = [a.strip() for a in TWITTER_ACCOUNTS.split(",") if a.strip()]

THRESHOLD = int(os.environ.get("THRESHOLD", "9"))
DRY_RUN    = os.environ.get("DRY_RUN", "0") == "1"
DEBUG      = os.environ.get("DEBUG", "0") == "1"
SHOW_STATS = os.environ.get("SHOW_STATS", "0") == "1"

# Two alternating captions
CAPTIONS = [
    os.environ.get("INSTAGRAM_CAPTION_0", "🏏 Latest Cricket Tweets Roundup!"),
    os.environ.get("INSTAGRAM_CAPTION_1", "🏏 Best Cricket Tweets Right Now!"),
]

# Tuning
SLEEP_IMGUR            = float(os.environ.get("SLEEP_IMGUR", "0.5"))
DEDUP_MIN_LEN          = 25
DEDUP_HAMMING          = 7

SINCE_OVERLAP_SECONDS = int(os.environ.get("SINCE_OVERLAP_SECONDS", "200"))

BASE_DIR            = os.path.dirname(os.path.abspath(__file__))
STATE_FILE          = os.path.join(BASE_DIR, "state.json")
PENDING_FILE        = os.path.join(BASE_DIR, "pending_post.json")
SCREENSHOT_DIR      = os.path.join(BASE_DIR, "screenshots")
SCREENSHOT_SCRIPT   = os.path.join(BASE_DIR, "screenshot.js")
QUEUE_MAX_AGE_HOURS = float(os.environ.get("QUEUE_MAX_AGE_HOURS", "6"))

SOCIALDATA_FILTERS = (
    "filter:images"
    " -filter:videos"
    " -filter:retweets"
    " -filter:nativeretweets"
    " -filter:replies"
    " -filter:quote"
)

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
            "checked_until_time": None,
            "queue": [],
            "posted": [],
            "seen": [],
            "tweet_data": {},
            "total_runs": 0,
            "total_carousels": 0,
            "last_caption_index": -1,
            "last_caption_text": "",
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
    s.setdefault("last_post_time", None)
    s.setdefault("next_start_idx", 0)
    s.setdefault("account_stats", {})
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
# SocialData
# -----------------------
def socialdata_fetch_query(query: str) -> Tuple[List[Dict[str, Any]], bool]:
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
def update_account_stats(state, account, *, fetched, evaluated, good):
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

def flush_per_account_stats(state, per_account):
    for account, c in per_account.items():
        update_account_stats(
            state, account,
            fetched=c.get("fetched", 0),
            evaluated=c.get("evaluated", 0),
            good=c.get("good", 0),
        )


# -----------------------
# Filter helper
# -----------------------
def passes_filters(t, cutoff_dt, posted_set, queued_set, seen_set, content_hashes):
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
def fetch_and_enqueue(state, cutoff_dt, queue, posted_list, tweet_data, accounts):
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

    per_account: Dict[str, Dict[str, int]] = {}

    def inc(bucket, field, n=1):
        pa = per_account.setdefault(bucket, {"fetched": 0, "evaluated": 0, "good": 0})
        pa[field] = pa.get(field, 0) + n

    max_tweet_dt: Optional[datetime] = None

    def _update_max_dt(t):
        nonlocal max_tweet_dt
        ts = extract_tweet_time(t)
        if ts:
            try:
                tdt = parse_dt(ts)
                if max_tweet_dt is None or tdt > max_tweet_dt:
                    max_tweet_dt = tdt
            except Exception:
                pass

    def process_batch(tweets):
        for t in tweets:
            _update_max_dt(t)

        if len(queue) >= THRESHOLD:
            if DEBUG:
                log("  [STOP] threshold reached before processing batch")
            return

        def tweet_dt(t):
            ts = extract_tweet_time(t) or "1970-01-01T00:00:00+00:00"
            try:
                return parse_dt(ts)
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)

        by_author: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for t in tweets:
            author = (extract_tweet_author(t) or "unknown")
            by_author[author].append(t)

        for author, lst in by_author.items():
            lst.sort(key=tweet_dt, reverse=True)

        authors_sorted = sorted(
            by_author.keys(),
            key=lambda a: (-len(by_author[a]), -tweet_dt(by_author[a][0]).timestamp(), a)
        )

        for author in authors_sorted:
            for t in by_author[author]:
                if len(queue) >= THRESHOLD:
                    return

                tid = str(t.get("id_str") or t.get("id") or "")
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

    chunks = build_or_query_chunks(accounts, since_unix)
    log(f"  OR-query chunks: {len(chunks)} request(s) for {len(accounts)} account(s)")

    all_chunks_completed = True
    any_tweets_returned = False

    for i, query in enumerate(chunks, 1):
        if len(queue) >= THRESHOLD:
            log(f"  ✅ Queue reached threshold ({THRESHOLD}) — skipping remaining chunks.")
            all_chunks_completed = False
            break
        log(f"  Chunk {i}/{len(chunks)} ({len(query)} chars)")
        tweets, ok = socialdata_fetch_query(query)
        if not ok:
            all_chunks_completed = False
        if tweets:
            any_tweets_returned = True
        log(f"  Chunk {i}: {len(tweets)} tweet(s) returned (ok={ok})")
        process_batch(tweets)

    flush_per_account_stats(state, per_account)
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
# Git push helper
# -----------------------
def git_push_pending():
    """Commit and push pending_post.json + state.json to repo."""
    try:
        subprocess.run(["git", "config", "user.name", "cricket-bot"], check=True)
        subprocess.run(["git", "config", "user.email", "cricket-bot@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", "pending_post.json", "state.json"], check=True)
        result = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if result.returncode == 0:
            log("  ℹ️  No changes to commit")
            return True
        subprocess.run(["git", "commit", "-m", "pending post ready"], check=True)
        subprocess.run(["git", "push"], check=True)
        log("  ✅ Pushed pending_post.json to repo")
        return True
    except subprocess.CalledProcessError as e:
        log(f"  ❌ Git push failed: {e}")
        return False


# -----------------------
# Main
# -----------------------
def main():
    total_t0 = perf_counter()
    log("=== cricket-bot (fetch+screenshot+imgur) starting ===")

    max_jitter = float(os.environ.get("RUN_JITTER_SECONDS", "8.67"))
    j = random.uniform(0, max_jitter)
    log(f"⏳ Run jitter: sleeping {j:.1f}s...")
    time.sleep(j)

    log(f"Accounts: {','.join(ACCOUNTS)}")
    log(f"Threshold: {THRESHOLD} | DRY_RUN: {int(DRY_RUN)} | DEBUG: {int(DEBUG)}")

    ensure_dirs()

    state = load_state()
    recover_in_flight(state)
    bound_state(state)
    evict_stale_queue(state, state.get("tweet_data", {}), max_age_hours=QUEUE_MAX_AGE_HOURS)
    save_state(state)

    state["total_runs"] = int(state.get("total_runs", 0)) + 1

    # ── First run — initialise watermark ──────────────────────────────────
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

    # ── Check if there's already a pending post waiting ───────────────────
    # If phone hasn't consumed the last one yet, don't overwrite it
    if os.path.exists(PENDING_FILE):
        try:
            with open(PENDING_FILE) as f:
                existing = json.load(f)
            created_at = existing.get("created_at", "")
            age_hours = (datetime.now(timezone.utc) - parse_dt(created_at)).total_seconds() / 3600
            if age_hours < 3:
                log(f"⏭️  pending_post.json already exists ({age_hours:.1f}h old) — phone hasn't posted yet. Exiting.")
                log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
                return
            else:
                log(f"⚠️  Stale pending_post.json ({age_hours:.1f}h old) — overwriting.")
        except Exception:
            pass  # corrupted file, overwrite it

    # ── Fetch cutoff ───────────────────────────────────────────────────────
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

    cutoff_dt = raw_cutoff_dt - timedelta(seconds=SINCE_OVERLAP_SECONDS)
    log(f"Raw watermark:    {raw_cutoff_dt.isoformat()}")
    log(f"Cutoff (overlap): {cutoff_dt.isoformat()} (-{SINCE_OVERLAP_SECONDS}s)")

    queue: List[str]           = state["queue"]
    posted_list: List[str]     = state["posted"]
    tweet_data: Dict[str, Any] = state.get("tweet_data", {})

    this_caption, this_caption_index = pick_caption(state)
    log(f"Caption [{this_caption_index}]: {this_caption!r}")

    # ── 1) Fetch + filter ──────────────────────────────────────────────────
    with StageTimer("1) Fetch & filter"):
        accounts_for_run = ACCOUNTS[:]
        random.shuffle(accounts_for_run)
        max_tweet_dt, all_chunks_completed, any_tweets_returned = fetch_and_enqueue(
            state, cutoff_dt, queue, posted_list, tweet_data, accounts_for_run
        )

    # ── Advance watermark ──────────────────────────────────────────────────
    prev_watermark = state.get("checked_until_time") or state.get("start_time")
    if max_tweet_dt is not None:
        state["checked_until_time"] = max_tweet_dt.isoformat()
        log(f"  ✅ checked_until_time → {state['checked_until_time']}")
    elif all_chunks_completed and (not any_tweets_returned):
        state["checked_until_time"] = utc_now_iso()
        log(f"  ℹ️  Quiet run — watermark advanced to now: {state['checked_until_time']}")
    else:
        state["checked_until_time"] = prev_watermark
        log(f"  ⚠️  Incomplete fetch — watermark unchanged: {prev_watermark}")

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

    # ── 3) Render screenshots ──────────────────────────────────────────────
    batch = state["queue"][:THRESHOLD]
    log(f"Batch: {len(batch)} tweets")

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
        log("Not enough screenshots. Keeping queue. Exiting.")
        log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
        return

    # ── 4) Upload to Imgur ─────────────────────────────────────────────────
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
        log("Not enough public URLs. Keeping queue. Exiting.")
        log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
        return

    if DRY_RUN:
        log("🧪 DRY_RUN=1 — skipping pending_post.json write.")
        log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
        return

    # ── 5) Save pending_post.json for phone to pick up ────────────────────
    with StageTimer("5) Save pending_post.json"):
        pending = {
            "public_urls":     public_urls,
            "caption":         this_caption,
            "caption_index":   this_caption_index,
            "imgur_good_ids":  imgur_good_ids,
            "created_at":      utc_now_iso(),
        }
        with open(PENDING_FILE, "w") as f:
            json.dump(pending, f, indent=2)
        log(f"  ✅ Saved pending_post.json ({len(public_urls)} images)")

        # Mark in-flight so state recovery works if something crashes
        state["in_flight"] = imgur_good_ids[:]
        # Update caption rotation now (phone just confirms after posting)
        state["last_caption_index"] = this_caption_index
        state["last_caption_text"]  = this_caption
        bound_state(state)
        save_state(state)

        cleanup_screenshots(imgur_good_ids)

    # ── 6) Push to GitHub so phone can pull ───────────────────────────────
    with StageTimer("6) Git push"):
        git_push_pending()

    log(
        f"✅ DONE — runs: {state['total_runs']} | "
        f"queue left: {len(state['queue'])} | "
        f"pending_post.json ready for phone"
    )
    log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")


if __name__ == "__main__":
    main()
