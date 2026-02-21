import os
import json
import time
import shutil
import subprocess
from time import perf_counter
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
import requests

# -----------------------
# Env / Config
# -----------------------
SOCIALDATA_API_KEY = os.environ["SOCIALDATA_API_KEY"]
IG_USER_ID = os.environ["IG_USER_ID"]
IG_ACCESS_TOKEN = os.environ["IG_ACCESS_TOKEN"]
IMGUR_CLIENT_ID = os.environ.get("IMGUR_CLIENT_ID", "")

TWITTER_ACCOUNTS = os.environ.get("TWITTER_ACCOUNTS", "mufaddal_vohra,criccrazyjohns,academy_dinda")
ACCOUNTS = [a.strip() for a in TWITTER_ACCOUNTS.split(",") if a.strip()]

THRESHOLD = int(os.environ.get("TWEET_THRESHOLD", "10"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
DEBUG = os.environ.get("DEBUG", "0") == "1"
SHOW_STATS = os.environ.get("SHOW_STATS", "0") == "1"

CAPTION = os.environ.get(
    "INSTAGRAM_CAPTION",
    "🏏 Latest Cricket Tweets Roundup!\n\n#Cricket #CricketTwitter #Cricthreads"
)

# tuning (reduce if you want faster)
SLEEP_SCREENSHOT = float(os.environ.get("SLEEP_SCREENSHOT", "0.3"))
SLEEP_IG_CONTAINER = float(os.environ.get("SLEEP_IG_CONTAINER", "0.8"))
SLEEP_BEFORE_PUBLISH = float(os.environ.get("SLEEP_BEFORE_PUBLISH", "5.0"))
SLEEP_IMGUR = float(os.environ.get("SLEEP_IMGUR", "0.2"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
SCREENSHOT_DIR = os.path.join(BASE_DIR, "screenshots")
SCREENSHOT_SCRIPT = os.path.join(BASE_DIR, "screenshot.js")

SESSION = requests.Session()


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
        self.t0 = None

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
    return datetime.fromisoformat(s)

def extract_tweet_time(t: Dict[str, Any]) -> Optional[str]:
    # SocialData uses this key (you confirmed in logs)
    if isinstance(t.get("tweet_created_at"), str) and t["tweet_created_at"].strip():
        return t["tweet_created_at"].strip()

    # fallbacks (just in case)
    for k in ("created_at", "createdAt", "date", "timestamp"):
        v = t.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None
def ensure_dirs():
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
def is_video_tweet(t: Dict[str, Any]) -> bool:
    """
    Returns True if tweet contains video / animated_gif.
    SocialData may store media in entities.media or extended_entities.media.
    """
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
        mtype = (m.get("type") or "").lower()
        if mtype in ("video", "animated_gif"):
            return True
        # Some payloads include video_info even if type is inconsistent
        if "video_info" in m:
            return True
    return False
# -----------------------
# State
# -----------------------
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {
            "start_time": None,
            "queue": [],
            "posted": [],
            "tweet_data": {},
            "total_runs": 0,
            "total_carousels": 0,
        }
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        s = json.load(f)

    s.setdefault("start_time", None)
    s.setdefault("queue", [])
    s.setdefault("posted", [])
    s.setdefault("tweet_data", {})
    s.setdefault("total_runs", 0)
    s.setdefault("total_carousels", 0)

    if not isinstance(s["queue"], list):
        s["queue"] = []
    if not isinstance(s["posted"], list):
        s["posted"] = []
    if not isinstance(s["tweet_data"], dict):
        s["tweet_data"] = {}

    return s

def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def bound_state(state: Dict[str, Any]) -> None:
    # keep state small
    state["queue"] = state["queue"][-5000:]
    state["posted"] = state["posted"][-5000:]

    qset = set(state["queue"])
    # keep only tweet_data for queued items (small!)
    td = state.get("tweet_data", {})
    state["tweet_data"] = {k: v for k, v in td.items() if k in qset}


# -----------------------
# SocialData (fetch tweets)
# -----------------------
def socialdata_search() -> List[Dict[str, Any]]:
    query = " OR ".join([f"from:{u}" for u in ACCOUNTS])
    url = "https://api.socialdata.tools/twitter/search"
    headers = {"Authorization": f"Bearer {SOCIALDATA_API_KEY}"}
    params = {"query": query, "type": "Latest"}

    r = SESSION.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return (r.json().get("tweets") or [])


# -----------------------
# Screenshot renderer (BATCH mode - single Node run)
# -----------------------

def screenshot_batch(tweets: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """
    tweets = { tweet_id: tweet_obj }
    Returns: { tweet_id: screenshot_path }
    """

    ensure_dirs()

    if not os.path.exists(SCREENSHOT_SCRIPT):
        log(f"❌ screenshot.js not found at {SCREENSHOT_SCRIPT}")
        return {}

    batch_payload = []
    id_to_path = {}

    for tweet_id, tweet_obj in tweets.items():
        out_path = os.path.join(SCREENSHOT_DIR, f"{tweet_id}.jpg")

        # Skip if already exists
        if os.path.exists(out_path):
            dbg(f"Reusing screenshot {tweet_id}")
            id_to_path[tweet_id] = out_path
            continue

        # Skip video tweets (fast reject before Node)
        media = (
            tweet_obj.get("extended_entities", {}).get("media")
            or tweet_obj.get("entities", {}).get("media")
            or []
        )

        has_video = any(
            m.get("type") in ("video", "animated_gif") or m.get("video_info")
            for m in media
            if isinstance(m, dict)
        )

        if has_video:
            dbg(f"Skipping video tweet {tweet_id}")
            continue

        batch_payload.append({
            "tweet": tweet_obj,
            "out": out_path
        })

    if not batch_payload:
        return id_to_path

    cmd = [
        "node",
        SCREENSHOT_SCRIPT,
        "--batch",
        json.dumps(batch_payload)
    ]

    dbg(f"Batch screenshot: {len(batch_payload)} tweets")

    with StageTimer("Batch screenshots"):
        try:
            result = subprocess.run(
                cmd,
                timeout=180,   # allow longer since it's one big run
                capture_output=True,
                text=True
            )

            if result.returncode != 0:
                log("❌ screenshot batch failed")
                log(result.stderr[:1000])
                return id_to_path

            # Parse batch result line
            marker = "__BATCH_RESULT__"
            for line in result.stdout.splitlines():
                if line.startswith(marker):
                    data = json.loads(line[len(marker):])
                    for item in data.get("results", []):
                        if item.get("ok") and item.get("out"):
                            tweet_id = os.path.splitext(
                                os.path.basename(item["out"])
                            )[0]
                            id_to_path[tweet_id] = item["out"]

            log(f"✅ Screenshots created: {len(id_to_path)}")

        except subprocess.TimeoutExpired:
            log("❌ screenshot batch timeout")
        except Exception as e:
            log(f"❌ screenshot batch error: {e}")

    return id_to_path
# -----------------------
# Imgur upload (public URL for IG)
# -----------------------
def upload_to_imgur(local_path: str) -> Optional[str]:
    if not IMGUR_CLIENT_ID:
        log("❌ IMGUR_CLIENT_ID missing. Instagram requires public image_url. Add IMGUR_CLIENT_ID secret.")
        return None

    url = "https://api.imgur.com/3/image"
    headers = {"Authorization": f"Client-ID {IMGUR_CLIENT_ID}"}

    with open(local_path, "rb") as f:
        files = {"image": f}
        r = SESSION.post(url, headers=headers, files=files, timeout=60)

    if r.status_code >= 400:
        log(f"  ❌ Imgur HTTP {r.status_code}: {r.text[:200]}")
        return None

    j = r.json()
    if not j.get("success"):
        log(f"  ❌ Imgur response not success: {str(j)[:200]}")
        return None

    return j.get("data", {}).get("link")


# -----------------------
# Instagram Graph API
# -----------------------
def ig_create_image_container(image_url: str) -> Optional[str]:
    url = f"https://graph.facebook.com/v21.0/{IG_USER_ID}/media"
    data = {"image_url": image_url, "is_carousel_item": "true", "access_token": IG_ACCESS_TOKEN}
    r = SESSION.post(url, data=data, timeout=30)
    j = r.json()
    if "error" in j:
        log(f"  ❌ IG container error: {j}")
        return None
    return j.get("id")

def ig_create_carousel(children_ids: List[str], caption: str) -> Optional[str]:
    url = f"https://graph.facebook.com/v21.0/{IG_USER_ID}/media"
    data = {
        "media_type": "CAROUSEL",
        "caption": caption,
        "children": ",".join(children_ids),
        "access_token": IG_ACCESS_TOKEN
    }
    r = SESSION.post(url, data=data, timeout=30)
    j = r.json()
    if "error" in j:
        log(f"  ❌ IG carousel create error: {j}")
        return None
    return j.get("id")

def ig_publish(creation_id: str) -> Optional[str]:
    url = f"https://graph.facebook.com/v21.0/{IG_USER_ID}/media_publish"
    data = {"creation_id": creation_id, "access_token": IG_ACCESS_TOKEN}
    r = SESSION.post(url, data=data, timeout=30)
    j = r.json()
    if "error" in j:
        log(f"  ❌ IG publish error: {j}")
        return None
    return j.get("id")


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
                except:
                    pass
# -----------------------
# Main
# -----------------------
def main():
    total_t0 = perf_counter()
    log("=== cricket-bot starting ===")
    log(f"Accounts: {','.join(ACCOUNTS)}")
    log(f"Threshold: {THRESHOLD} | DRY_RUN: {int(DRY_RUN)} | DEBUG: {int(DEBUG)} | SHOW_STATS: {int(SHOW_STATS)}")

    # ensure screenshot dir exists
    ensure_dirs()

    state = load_state()
    state["total_runs"] = int(state.get("total_runs", 0)) + 1

    # Option A: first run sets start_time and exits (ignore all past)
    if not state.get("start_time"):
        state["start_time"] = utc_now_iso()
        bound_state(state)
        save_state(state)
        log(f"Initialized start_time = {state['start_time']}")
        log("First run exits now. Next runs will only process tweets AFTER this time.")
        log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
        return

    try:
        start_dt = parse_dt(state["start_time"])
    except Exception as e:
        log(f"❌ Bad start_time in state.json: {state.get('start_time')} ({e})")
        state["start_time"] = utc_now_iso()
        bound_state(state)
        save_state(state)
        log(f"Reset start_time = {state['start_time']} (exiting)")
        log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
        return

    queue: List[str] = state["queue"]
    posted_list: List[str] = state["posted"]
    tweet_data: Dict[str, Any] = state.get("tweet_data", {})

    posted_set = set(posted_list)
    queued_set = set(queue)

    # 1) Fetch tweets
    with StageTimer("1) Fetch tweets (SocialData)"):
        tweets = socialdata_search()

    log(f"Fetched tweets: {len(tweets)}")
    dbg(f"start_time cutoff UTC: {state['start_time']}")

    # 2) Filter + enqueue
    with StageTimer("2) Filter + enqueue new tweets"):
        added = 0
        skipped_video = 0
        skipped_old = 0
        skipped_no_time = 0
        skipped_dupe = 0

        for t in tweets:
            tid = t.get("id_str") or t.get("id")
            if not tid:
                continue
            tid = str(tid)

            created_str = extract_tweet_time(t)
            if not created_str:
                skipped_no_time += 1
                continue
            try:
                created_dt = parse_dt(created_str)
            except Exception:
                skipped_no_time += 1
                continue
            if is_video_tweet(t):
                skipped_video += 1
                continue
            if created_dt < start_dt:
                skipped_old += 1
                continue

            if tid in posted_set or tid in queued_set:
                skipped_dupe += 1
                continue

            queue.append(tid)
            queued_set.add(tid)
            tweet_data[tid] = t
            added += 1

    log(f"Queue add: +{added} | skipped_old: {skipped_old} | skipped_no_time: {skipped_no_time} | skipped_dupe: {skipped_dupe}")
    log(f"Queue size now: {len(queue)}/{THRESHOLD}")

    # 3) Save state after enqueue
    with StageTimer("3) Save state"):
        state["queue"] = queue
        state["posted"] = posted_list
        state["tweet_data"] = tweet_data
        bound_state(state)
        save_state(state)

    # If not enough, exit
    if len(state["queue"]) < THRESHOLD:
        log("Not enough queued yet. Exiting.")
        log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
        return

    # 4) Prepare batch
    batch = state["queue"][:THRESHOLD]
    log(f"Batch ready: {len(batch)} tweets (sample: {batch[:3]})")

    # 5) Render screenshots (BATCH: one Node run, one browser)
    with StageTimer("4) Render screenshots (Playwright)"):
        local_paths: List[str] = []
        good_ids: List[str] = []
    
        ensure_dirs()
    
        # Build batch payload for Node
        batch_payload = []
        for i, tid in enumerate(batch, 1):
            log(f"  ▶ prepare {i}/{THRESHOLD}: {tid}")
            t_obj = tweet_data.get(tid)
            if not t_obj:
                log(f"  ⚠️  Missing tweet_data for {tid} (skipping)")
                continue
    
            out_path = os.path.join(SCREENSHOT_DIR, f"{tid}.jpg")
    
            # Reuse if exists
            if os.path.exists(out_path):
                dbg(f"Reusing screenshot {tid}: {out_path}")
                local_paths.append(out_path)
                good_ids.append(tid)
                continue
    
            batch_payload.append({"tweet": t_obj, "out": out_path})
    
        if batch_payload:
            if not os.path.exists(SCREENSHOT_SCRIPT):
                log(f"❌ screenshot.js not found at {SCREENSHOT_SCRIPT}")
            else:
                cmd = ["node", SCREENSHOT_SCRIPT, "--batch", json.dumps(batch_payload)]
                dbg(f"Batch screenshot cmd: node screenshot.js --batch <{len(batch_payload)} items>")
    
                env = os.environ.copy()
                env["SHOW_STATS"] = "1" if SHOW_STATS else "0"
    
                try:
                    result = subprocess.run(
                        cmd,
                        timeout=240,  # one big run
                        capture_output=True,
                        text=True,
                        env=env,
                    )
    
                    if result.returncode != 0:
                        log("❌ screenshot batch failed")
                        log(f"  stderr: {result.stderr[:1200]}")
                        dbg(f"  stdout: {result.stdout[:1200]}")
                    else:
                        # Parse machine-readable result line emitted by screenshot.js
                        marker = "__BATCH_RESULT__"
                        parsed = False
                        for line in result.stdout.splitlines():
                            if line.startswith(marker):
                                parsed = True
                                data = json.loads(line[len(marker):])
                                for item in data.get("results", []):
                                    if item.get("ok") and item.get("out"):
                                        outp = item["out"]
                                        tid = os.path.splitext(os.path.basename(outp))[0]
                                        if os.path.exists(outp):
                                            local_paths.append(outp)
                                            good_ids.append(tid)
    
                        if not parsed:
                            # fallback: just check files exist
                            dbg("No __BATCH_RESULT__ line found; falling back to filesystem checks")
                            for it in batch_payload:
                                outp = it["out"]
                                if os.path.exists(outp):
                                    tid = os.path.splitext(os.path.basename(outp))[0]
                                    local_paths.append(outp)
                                    good_ids.append(tid)
    
                        log(f"  ✅ batch rendered: {len(good_ids)} screenshots")
                        dbg(f"  stdout: {result.stdout[:600]}")
    
                except subprocess.TimeoutExpired:
                    log("❌ screenshot batch timeout")
                except Exception as e:
                    log(f"❌ screenshot batch error: {e}")

    # IMPORTANT: we no longer sleep per tweet, because it's one run now
    log(f"Screenshots ok: {len(local_paths)}/{THRESHOLD}")

    if len(local_paths) < 2:
        log("Not enough screenshots (need >=2). Keeping queue for retry. Exiting.")
        log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
        return

    # 6) Upload to Imgur
    with StageTimer("5) Upload screenshots to Imgur"):
        public_urls: List[str] = []
        imgur_good_ids: List[str] = []

        for i, (path, tid) in enumerate(zip(local_paths, good_ids), 1):
            log(f"  ▶ imgur {i}/{len(local_paths)}: {tid}")
            url = upload_to_imgur(path)
            if url:
                public_urls.append(url)
                imgur_good_ids.append(tid)
                dbg(f"  url: {url}")
            else:
                log(f"  ⚠️  Imgur failed: {tid}")
            time.sleep(SLEEP_IMGUR)

    log(f"Imgur ok: {len(public_urls)}/{len(local_paths)}")

    if len(public_urls) < 2:
        log("Not enough public URLs (need >=2). Keeping queue for retry. Exiting.")
        log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
        return

    # 7) IG containers
    with StageTimer("6) Create IG containers"):
        container_ids: List[str] = []
        for i, url in enumerate(public_urls, 1):
            log(f"  ▶ ig container {i}/{len(public_urls)}")
            cid = ig_create_image_container(url)
            if cid:
                container_ids.append(cid)
                dbg(f"  container_id: {cid}")
            else:
                log("  ⚠️  IG container failed")
            time.sleep(SLEEP_IG_CONTAINER)

    log(f"IG containers ok: {len(container_ids)}/{len(public_urls)}")

    if len(container_ids) < 2:
        log("Not enough IG containers (need >=2). Keeping queue for retry. Exiting.")
        log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
        return

    # 8) Create carousel
    with StageTimer("7) Create IG carousel"):
        car_id = ig_create_carousel(container_ids, CAPTION)
        if not car_id:
            log("❌ Carousel create failed. Keeping queue for retry. Exiting.")
            log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
            return
        log(f"✅ Carousel creation_id: {car_id}")

    # 9) Publish
    with StageTimer("8) Publish carousel"):
        log(f"⏳ Waiting {SLEEP_BEFORE_PUBLISH}s before publish...")
        time.sleep(SLEEP_BEFORE_PUBLISH)

        if DRY_RUN:
            log("🧪 DRY_RUN=1 -> Skipping publish.")
            log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
            return

        post_id = ig_publish(car_id)
        if not post_id:
            log("❌ Publish failed. Keeping queue for retry. Exiting.")
            log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")
            return

        log(f"✅ Published IG post_id: {post_id}")

    # 10) Update state + cleanup
    with StageTimer("9) Update state + cleanup"):
        posted_set = set(posted_list)
        good_set = set(imgur_good_ids)

        # mark posted
        for tid in imgur_good_ids:
            if tid not in posted_set:
                posted_list.append(tid)
                posted_set.add(tid)

        # remove posted from queue; keep failures for retry
        new_queue = [tid for tid in queue if tid not in good_set]

        state["queue"] = new_queue
        state["posted"] = posted_list
        state["tweet_data"] = tweet_data
        state["total_carousels"] = int(state.get("total_carousels", 0)) + 1

        bound_state(state)
        save_state(state)

        cleanup_screenshots(imgur_good_ids)

    log(f"✅ DONE. Total runs: {state['total_runs']} | total carousels: {state['total_carousels']} | queue left: {len(state['queue'])}")
    log(f"[TOTAL] {perf_counter() - total_t0:.2f}s")


if __name__ == "__main__":
    main()
