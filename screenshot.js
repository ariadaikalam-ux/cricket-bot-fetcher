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
const CANVAS_PAD = 85;

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
    if (url.match(/\.(woff|woff2|ttf|otf)(\?|$)/i)) {
      // Allow Twitter's own Chirp font, block everything else
      if (!url.includes("abs.twimg.com") && !url.includes("ton.twimg.com")) {
        return route.abort();
      }
    }
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
      header, nav, aside,
      [role="banner"], [role="navigation"],
      [data-testid="sidebarColumn"],
      [data-testid="TopNavBar"],
      [data-testid="BottomBar"],
      [data-testid="sheetDialog"],
      [aria-label="Sign up"],
      [aria-label="Log in"] {
        display: none !important;
      }
      [role="dialog"] { background: transparent !important; }
      [data-testid="tweet"] { background: #ffffff !important; }

      /* Tweet text — unchanged from your tuned version */
      [data-testid="tweetText"] {
        font-size: 1.62em !important;
        font-weight: 401 !important;
        line-height: 1.5 !important;
      }

      /* ── BRANDING: bigger avatar (container & img must match) ── */
      [data-testid="Tweet-User-Avatar"],
      [data-testid^="UserAvatar-Container"] {
        width: 64px !important;
        height: 64px !important;
        min-width: 64px !important;
        min-height: 64px !important;
        flex-shrink: 0 !important;
      }
      [data-testid="Tweet-User-Avatar"] img,
      [data-testid^="UserAvatar-Container"] img {
        width: 64px !important;
        height: 64px !important;
        border-radius: 50% !important;
      }

      /* ── BRANDING: name size + position ── */
      [data-testid="User-Name"] {
        font-size: 1em !important;
        margin-left: 16px !important;
      }
      [data-testid="User-Name"] a span,
      [data-testid="User-Name"] div > span {
        font-size: 20px !important;
        font-weight: 700 !important;
        line-height: 1.3 !important;
      }
      
      [data-testid="caret"] {
        display: none !important;
      }
      [data-testid="tweetText"] {
        margin-top: 16px !important;
        
      }
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

// ── CHANGED: removed waitForTimeout(300) ─────────────────────────────
async function customizeAuthor(page) {
  await page.evaluate(({ name, username }) => {
    const nameSpans = document.querySelectorAll('[data-testid="User-Name"] span');
    if (nameSpans.length >= 1) nameSpans[0].textContent = name;
    for (const span of nameSpans) {
      if (span.textContent.trim().startsWith("@")) { span.textContent = username; break; }
    }
  }, { name: MY_NAME, username: MY_USERNAME });
}

// ── CHANGED: removed waitForTimeout(200) ─────────────────────────────
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
}

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
  const tweetEl = await page.locator('[data-testid="tweet"]').first();

  // Step 1: Expand viewport to fit full tweet height
  const fullTweetHeight = await page.evaluate(() => {
    const tweet = document.querySelector('[data-testid="tweet"]');
    if (!tweet) return 900;
    return Math.ceil(tweet.getBoundingClientRect().bottom) + 20;
  });
  await page.setViewportSize({ width: 600, height: Math.max(900, fullTweetHeight) });

  // Step 2: Re-measure bounding box after viewport expansion
  const box = await tweetEl.boundingBox();
  if (!box) throw new Error("Could not find tweet bounding box");

  // Step 3: Find crop point — just before timestamp row or engagement bar
  const cropH = await page.evaluate(() => {
    const tweet = document.querySelector('[data-testid="tweet"]');
    if (!tweet) return null;
    const tweetRect = tweet.getBoundingClientRect();

    // Try timestamp row first ("8:33 AM · Feb 24" line)
    const timeEl = tweet.querySelector('time');
    if (timeEl) {
      let row = timeEl;
      for (let i = 0; i < 8; i++) {
        const p = row.parentElement;
        if (!p || p === tweet) break;
        if (p.getBoundingClientRect().width >= tweetRect.width * 0.8) { row = p; break; }
        row = p;
      }
      const r = row.getBoundingClientRect();
      if (r.top > tweetRect.top + 50) {
        return r.top - tweetRect.top;
      }
    }

    // Fallback: engagement bar (reply/like/retweet)
    for (const g of tweet.querySelectorAll('[role="group"]')) {
      if (g.querySelector('[data-testid="reply"], [data-testid="like"], [data-testid="retweet"]')) {
        const r = g.getBoundingClientRect();
        if (r.top > tweetRect.top + 50 && r.height > 0) {
          return r.top - tweetRect.top;
        }
      }
    }

    return tweetRect.height;
  });

  const finalCropH = Math.max(50, cropH ?? box.height);
  log(`  viewport=${fullTweetHeight}px | box.height=${box.height.toFixed(0)} | cropH=${finalCropH.toFixed(0)}`);

  // Step 4: Take the raw screenshot
  const rawPath = pngOut.replace(/\.png$/i, "") + ".raw.png";
  await page.screenshot({
    path: rawPath,
    type: "png",
    clip: {
      x: box.x,
      y: box.y,
      width: box.width,
      height: finalCropH,
    },
  });

  if (!fs.existsSync(rawPath)) throw new Error("Raw screenshot did not write");

  // Step 5: Compose onto 4:5 canvas
  const rawB64 = fs.readFileSync(rawPath).toString("base64");
  const page2 = await context.newPage();
  await page2.setViewportSize({ width: FINAL_W, height: FINAL_H });
  await page2.setContent(buildCanvasHtml(rawB64), { waitUntil: "load" });
  await page2.screenshot({ path: pngOut, type: "png" });
  await page2.close();
  try { fs.unlinkSync(rawPath); } catch (_) {}
}
// ── CHANGED: replaced blind 2500+300ms sleeps with smart image-load wait ──
async function loadTweet(page, tweetUrl) {
  page.on('response', response => {
    const url = response.url();
    const status = response.status();
    if (url.includes('twimg.com') && status !== 200) {
      log(`🚫 1) BLOCKED image: ${status} ${url}`);
    }
    if (url.includes('twimg.com') && url.includes('/media/')) {
      log(`📸 1) media response: ${status} ${url}`);
    }
  });
  await page.goto(tweetUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
  // Add after page.goto line
  page.on('response', response => {
    const url = response.url();
    const status = response.status();
    if (url.includes('twimg.com') && status !== 200) {
      log(`🚫 2) BLOCKED image: ${status} ${url}`);
    }
    if (url.includes('twimg.com') && url.includes('/media/')) {
      log(`📸 2) media response: ${status} ${url}`);
    }
  });
  await waitForTweetContent(page);
  await forceWhiteCss(page);
  await page.waitForTimeout(600);

  // Wait for tweet images to actually finish loading instead of sleeping
  // NEW — Phase 1: wait for photo containers to actually appear in DOM (5s grace)
//        Phase 2: once found, wait for the imgs inside to finish loading
  try {
    await page.waitForSelector('[data-testid="tweetPhoto"] img', {
      timeout: 5000,
      state: "visible",
    });
    // Photo containers appeared — now wait for pixel data
    await page.waitForFunction(() => {
      const imgs = Array.from(
        document.querySelectorAll('[data-testid="tweetPhoto"] img')
      );
      return imgs.length > 0 && imgs.every(i => i.complete && i.naturalWidth > 0);
    }, { timeout: 12000 });
  } catch (_) {
    // Text-only tweet or images too slow — continue without blocking
  }

  await customizeAuthor(page);
  await replaceProfilePic(page);
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
