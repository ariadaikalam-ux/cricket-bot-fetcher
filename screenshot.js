/**
 * screenshot.js — Batch Playwright tweet screenshotter (IG-ready 4:5)
 * Customized: replaces original author name / @ / avatar with your branding
 *
 * Single mode:
 *   node screenshot.js <tweet_url_or_json> <output_path>
 *
 * Batch mode:
 *   node screenshot.js --batch '<json_array>'
 */

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const FINAL_W = 1080;
const FINAL_H = 1350;
const CANVAS_PAD = 30;

// ── YOUR BRANDING ─────────────────────────────────────────────────────
const MY_NAME = "Cric Thread 🏏";
const MY_USERNAME = "@cric.thread";
const MY_PHOTO = path.resolve("IMG_6905.JPG");

let MY_PHOTO_BUFFER;
let MY_PHOTO_B64;
if (fs.existsSync(MY_PHOTO)) {
  MY_PHOTO_BUFFER = fs.readFileSync(MY_PHOTO);
  MY_PHOTO_B64 = `data:image/jpeg;base64,${MY_PHOTO_BUFFER.toString("base64")}`;
} else {
  console.warn(`[WARNING] Profile photo not found: ${MY_PHOTO}`);
}
// ─────────────────────────────────────────────────────────────────────

function nowIso() { return new Date().toISOString(); }
function log(msg) { console.log(`[${nowIso()}] ${msg}`); }

function isProbablyJsonString(s) {
  if (!s) return false;
  const t = String(s).trim();
  return t.startsWith("{") && t.endsWith("}");
}

function buildTweetUrlFromJson(obj) {
  const id = obj?.id_str || (typeof obj?.id === "number" ? String(obj.id) : obj?.id);
  const screen = obj?.user?.screen_name || obj?.screen_name || obj?.username;
  if (!id || !screen) return null;
  return `https://x.com/${screen}/status/${id}`;
}

function normalizeOutputPath(p) {
  const ext = path.extname(p).toLowerCase();
  if (ext === ".png") return { pngOut: p, alsoWriteJpg: false, jpgOut: null };
  if (ext === ".jpg" || ext === ".jpeg") {
    const pngOut = p.replace(/\.(jpg|jpeg)$/i, ".png");
    return { pngOut, alsoWriteJpg: true, jpgOut: p };
  }
  return { pngOut: p + ".png", alsoWriteJpg: false, jpgOut: null };
}

async function timeStep(label, fn) {
  const t0 = Date.now();
  log(`▶ START: ${label}`);
  try {
    const out = await fn();
    log(`✅ END:   ${label} (${((Date.now() - t0) / 1000).toFixed(2)}s)`);
    return out;
  } catch (e) {
    log(`❌ FAIL:  ${label} (${((Date.now() - t0) / 1000).toFixed(2)}s) -> ${e.message}`);
    throw e;
  }
}

function tweetHasVideo(tweetObj) {
  const ext = tweetObj?.extended_entities?.media;
  const ent = tweetObj?.entities?.media;
  const media = Array.isArray(ext) ? ext : Array.isArray(ent) ? ent : [];
  for (const m of media) {
    if (!m || typeof m !== "object") continue;
    const t = String(m.type || "").toLowerCase();
    if (t === "video" || t === "animated_gif") return true;
    if (m.video_info) return true;
  }
  return false;
}

async function preparePage(page) {
  await page.route("**/*", (route) => {
    const url = route.request().url();
    if (url.match(/\.(woff|woff2|ttf|otf)(\?|$)/i)) return route.abort();
    if (url.includes("doubleclick") || url.includes("googletagmanager") ||
        url.includes("google-analytics") || url.includes("analytics")) return route.abort();
    return route.continue();
  });

  if (MY_PHOTO_BUFFER) {
    await page.route((url) => {
      const s = url.toString();
      return s.includes("profile_images") || s.includes("profile-images") || s.includes("pbs.twimg.com/profile");
    }, (route) => route.fulfill({ status: 200, contentType: "image/jpeg", body: MY_PHOTO_BUFFER }));
  }
}

async function forceWhiteCss(page) {
  await page.addStyleTag({
    content: `
      :root, html, body { background: #ffffff !important; }
      body { overflow: hidden !important; }
      header, nav, aside, [role="banner"], [role="navigation"] { display: none !important; }
      [data-testid="sidebarColumn"] { display: none !important; }
      [role="dialog"] { background: transparent !important; }
      [data-testid="tweet"] { background: #ffffff !important; }
    `,
  });
}

async function waitForTweetContent(page) {
  const candidates = ['[data-testid="tweet"]', "article", ".main-tweet", ".tweet-body"];
  for (const sel of candidates) {
    try {
      await page.locator(sel).first().waitFor({ timeout: 20000, state: "visible" });
      return;
    } catch (_) {}
  }
  throw new Error("Tweet selector not found");
}

async function customizeAuthor(page) {
  await page.evaluate(({ name, username }) => {
    const nameSpans = document.querySelectorAll('[data-testid="User-Name"] span');
    if (nameSpans.length >= 1) nameSpans[0].textContent = name;
    for (const span of nameSpans) {
      if (span.textContent.trim().startsWith("@")) { span.textContent = username; break; }
    }
  }, { name: MY_NAME, username: MY_USERNAME });
  await page.waitForTimeout(300);
}

async function replaceProfilePic(page) {
  if (!MY_PHOTO_B64) return;
  await page.evaluate((b64) => {
    const SELECTORS = [
      '[data-testid="Tweet-User-Avatar"] img',
      '[data-testid^="UserAvatar-Container"] img',
      'a[href$="/photo"] img',
      'a[href*="/photo/"] img',
    ];
    const replaced = new Set();
    for (const sel of SELECTORS) {
      for (const img of document.querySelectorAll(sel)) {
        if (replaced.has(img)) continue;
        img.src = b64; img.srcset = ""; replaced.add(img);
      }
    }
  }, MY_PHOTO_B64);
  await page.waitForTimeout(200);
}

async function dedupMediaOnly(page) {
  await page.evaluate(() => {
    const canon = (u) => {
      if (!u) return "";
      try { const url = new URL(u, location.href); return `${url.host}${url.pathname}`.toLowerCase(); }
      catch { return String(u).split("?")[0].toLowerCase(); }
    };
    const tiles = Array.from(document.querySelectorAll('[data-testid="tweetPhoto"], [data-testid="videoPlayer"]'));
    const seenTile = new Set();
    for (const tile of tiles) {
      const img = tile.querySelector("img");
      const sig = img ? canon(img.currentSrc || img.src) : "";
      if (!sig) continue;
      if (seenTile.has(sig)) tile.style.display = "none";
      else seenTile.add(sig);
    }
  });
  await page.waitForTimeout(200);
}

// ── CORE FIX: find where metrics START, use that Y as crop bottom ─────
//
// Strategy: locate the metrics/timestamp elements by finding them
// DIRECTLY (top-down), not by walking up from leaves.
// The timestamp line ("10:53 PM · Feb 22") always appears first.
// We find its top Y and crop there. If no timestamp found, fall back
// to the action group (reply/like/retweet row) top Y.
//
async function findCropRegion(page) {
  return await page.evaluate(() => {
    const tweet = document.querySelector('[data-testid="tweet"]') || document.querySelector("article");
    if (!tweet) return null;

    const tweetRect = tweet.getBoundingClientRect();

    // ── Strategy 1: find the timestamp+views line directly ────────────
    // Twitter renders this as a <time> element or an <a> linking to the tweet
    // The line contains "AM" or "PM" and "·" separators
    let metricsTop = null;

    // Try <time> element first — most reliable
    const timeEl = tweet.querySelector("time");
    if (timeEl) {
      // Walk up to find the row container (the div wrapping the full timestamp line)
      let row = timeEl;
      for (let i = 0; i < 6; i++) {
        const p = row.parentElement;
        if (!p || p === tweet) break;
        row = p;
      }
      const r = row.getBoundingClientRect();
      if (r.top > tweetRect.top && r.height > 0) {
        metricsTop = r.top;
      }
    }

    // ── Strategy 2: find action group (reply/like/retweet row) ───────
    if (metricsTop === null) {
      for (const g of tweet.querySelectorAll('[role="group"]')) {
        if (g.querySelector('[data-testid="reply"], [data-testid="like"], [data-testid="retweet"]')) {
          const r = g.getBoundingClientRect();
          if (r.top > tweetRect.top && r.height > 0) {
            metricsTop = r.top;
            break;
          }
        }
      }
    }

    // ── Strategy 3: find by analytics button ─────────────────────────
    if (metricsTop === null) {
      const analytics = tweet.querySelector('[data-testid="analyticsButton"], a[href$="/analytics"]');
      if (analytics) {
        // Walk up to row container
        let row = analytics;
        for (let i = 0; i < 5; i++) {
          const p = row.parentElement;
          if (!p || p === tweet) break;
          row = p;
        }
        const r = row.getBoundingClientRect();
        if (r.top > tweetRect.top) metricsTop = r.top;
      }
    }

    // ── Strategy 4: scan all elements for timestamp text pattern ─────
    if (metricsTop === null) {
      const allEls = tweet.querySelectorAll("*");
      for (const el of allEls) {
        if (el.children.length > 0) continue; // leaf nodes only
        const txt = (el.innerText || el.textContent || "").trim();
        // matches "10:53 PM" pattern
        if (/^\d{1,2}:\d{2}\s*(AM|PM)$/i.test(txt)) {
          let row = el;
          for (let i = 0; i < 6; i++) {
            const p = row.parentElement;
            if (!p || p === tweet) break;
            row = p;
          }
          const r = row.getBoundingClientRect();
          if (r.top > tweetRect.top && r.height > 0) {
            metricsTop = r.top;
            break;
          }
        }
      }
    }

    // ── If we found metrics, crop just above them ─────────────────────
    // Add 8px buffer below last content element (the tweet image bottom)
    const cropBottom = metricsTop !== null
      ? metricsTop - 2  // 2px gap — cut right at the start of metrics
      : tweetRect.bottom; // fallback: use full tweet height

    return {
      x: tweetRect.left,
      y: tweetRect.top,
      width: tweetRect.width,
      cropBottom,
      metricsFound: metricsTop !== null,
    };
  });
}
// ─────────────────────────────────────────────────────────────────────

function buildCanvasHtml(rawB64) {
  return `<!doctype html><html><head><meta charset="utf-8"/>
  <style>
    html,body{margin:0;padding:0;width:${FINAL_W}px;height:${FINAL_H}px;background:#fff;}
    .canvas{width:${FINAL_W}px;height:${FINAL_H}px;display:flex;align-items:center;justify-content:center;background:#fff;}
    .pad{width:${FINAL_W}px;height:${FINAL_H}px;padding:${CANVAS_PAD}px;box-sizing:border-box;display:flex;align-items:center;justify-content:center;}
    img{max-width:100%;max-height:100%;object-fit:contain;display:block;}
  </style></head><body>
  <div class="canvas"><div class="pad"><img src="data:image/png;base64,${rawB64}"/></div></div>
  </body></html>`;
}

async function captureAndCompose(page, context, pngOut) {
  const cropInfo = await findCropRegion(page);
  if (!cropInfo) throw new Error("Could not determine crop region");

  const { x, y, width, cropBottom, metricsFound } = cropInfo;
  const height = Math.max(50, cropBottom - y);

  log(`  Metrics found: ${metricsFound} | Crop: x=${x.toFixed(0)}, y=${y.toFixed(0)}, w=${width.toFixed(0)}, h=${height.toFixed(0)}`);

  const rawPath = pngOut.replace(/\.png$/i, "") + ".raw.png";

  await page.screenshot({
    path: rawPath,
    type: "png",
    clip: {
      x: Math.max(0, x),
      y: Math.max(0, y),
      width: Math.min(width, 1800),
      height: Math.min(height, 4000),
    },
  });

  if (!fs.existsSync(rawPath)) throw new Error("Raw screenshot did not write");

  const rawB64 = fs.readFileSync(rawPath).toString("base64");

  const page2 = await context.newPage();
  await page2.setViewportSize({ width: FINAL_W, height: FINAL_H });
  await page2.setContent(buildCanvasHtml(rawB64), { waitUntil: "load" });
  await page2.waitForTimeout(80);
  await page2.screenshot({ path: pngOut, type: "png" });
  await page2.close();

  try { fs.unlinkSync(rawPath); } catch (_) {}
}

async function loadTweet(page, tweetUrl) {
  await page.goto(tweetUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
  await waitForTweetContent(page);
  await forceWhiteCss(page);
  // Wait for full JS render including metrics bar
  await page.waitForTimeout(2500);
  await customizeAuthor(page);
  await replaceProfilePic(page);
  await dedupMediaOnly(page);
  await page.waitForTimeout(300);
}

async function renderOne(page, context, tweetObj, outPath) {
  const tweetUrl = buildTweetUrlFromJson(tweetObj);
  if (!tweetUrl) return { ok: false, reason: "missing_url_fields" };
  if (tweetHasVideo(tweetObj)) return { ok: false, reason: "video_tweet_skipped" };

  const { pngOut, alsoWriteJpg, jpgOut } = normalizeOutputPath(outPath);
  await loadTweet(page, tweetUrl);
  await captureAndCompose(page, context, pngOut);
  if (alsoWriteJpg && jpgOut) { try { fs.copyFileSync(pngOut, jpgOut); } catch (_) {} }
  return { ok: true, out: outPath, url: tweetUrl };
}

async function createBrowser() {
  return chromium.launch({
    args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
  });
}

async function createContext(browser) {
  return browser.newContext({
    colorScheme: "light",
    deviceScaleFactor: 3,
    timezoneId: "Asia/Kolkata",
    locale: "en-IN",
  });
}

async function createPage(context) {
  const page = await context.newPage();
  await page.setViewportSize({ width: 600, height: 900 });
  await preparePage(page);
  return page;
}

async function runBatch(batchJson) {
  let browser;
  const results = [];
  try {
    const items = JSON.parse(batchJson);
    if (!Array.isArray(items)) throw new Error("batch must be a JSON array");

    browser = await createBrowser();
    const context = await createContext(browser);
    const page = await createPage(context);

    for (let i = 0; i < items.length; i++) {
      const it = items[i] || {};
      const out = it.out;
      let tweetObj = it.tweet;

      if (!out) { results.push({ ok: false, i, reason: "missing_out" }); continue; }
      if (typeof tweetObj === "string" && isProbablyJsonString(tweetObj)) {
        try { tweetObj = JSON.parse(tweetObj); } catch (_) {}
      }
      if (!tweetObj || typeof tweetObj !== "object") {
        results.push({ ok: false, i, out, reason: "missing_tweet" }); continue;
      }

      log(`▶ item ${i + 1}/${items.length}: ${out}`);
      try {
        const r = await renderOne(page, context, tweetObj, out);
        results.push({ i, ...r });
      } catch (e) {
        results.push({ ok: false, i, out, reason: "exception", error: e.message });
      }
    }

    console.log("__BATCH_RESULT__" + JSON.stringify({ results }));
    return 0;
  } catch (e) {
    console.error(`[${nowIso()}] ❌ batch failed: ${e.message}`);
    return 2;
  } finally {
    if (browser) await browser.close();
  }
}

async function runSingle(inputArg, outputPathArg) {
  let tweetUrl = inputArg;
  if (isProbablyJsonString(inputArg)) {
    const tweetObj = JSON.parse(inputArg);
    const built = buildTweetUrlFromJson(tweetObj);
    if (!built) { console.error(`[${nowIso()}] ❌ Could not build tweet URL from JSON`); return 1; }
    tweetUrl = built;
  }

  let browser;
  try {
    browser = await createBrowser();
    const context = await createContext(browser);
    const page = await createPage(context);

    await timeStep("Load tweet + settle", () => loadTweet(page, tweetUrl));

    const { pngOut, alsoWriteJpg, jpgOut } = normalizeOutputPath(outputPathArg);
    await timeStep("Smart crop + compose 4:5 canvas", () => captureAndCompose(page, context, pngOut));

    if (alsoWriteJpg && jpgOut) { try { fs.copyFileSync(pngOut, jpgOut); } catch (_) {} }

    console.log(`[${nowIso()}] ✅ saved ${outputPathArg}`);
    return 0;
  } catch (e) {
    console.error(`[${nowIso()}] ❌ FAIL: ${e.message}`);
    return 1;
  } finally {
    if (browser) await browser.close();
  }
}

(async () => {
  const args = process.argv.slice(2);
  if (args[0] === "--batch") { process.exit(await runBatch(args[1])); }
  process.exit(await runSingle(args[0], args[1]));
})();
