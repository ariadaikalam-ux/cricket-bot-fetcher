/**
 * screenshot.js — Batch Playwright tweet screenshotter (IG-ready 4:5)
 *
 * Single mode (compatible):
 *   node screenshot.js <tweet_url_or_json> <output_path>
 *
 * Batch mode (NEW):
 *   node screenshot.js --batch '<json_array>'
 *
 * Where json_array = [
 *   { "tweet": <tweet_json_object_or_json_string>, "out": "/abs/path/123.jpg" },
 *   ...
 * ]
 *
 * Output
 * - Always 1080x1350 (4:5)
 * - White background
 * - Tweet card centered and scaled
 * - DEDUP ONLY: hide duplicate media, keep real multi-media
 * - SKIP videos (video/animated_gif) when tweet JSON includes media info
 * - REPLACE author header inside tweet with YOUR brand (name/username/photo)
 */

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const FINAL_W = 1080;
const FINAL_H = 1350;

// ===== YOUR BRAND (edit these) =====
const MY_NAME = "Cric Thread 🏏";
const MY_USERNAME = "@cric.thread"; // include @ or not
const MY_PHOTO = path.resolve("IMG_6905.JPG"); // local path or https URL

function nowIso() {
  return new Date().toISOString();
}

function log(msg) {
  console.log(`[${nowIso()}] ${msg}`);
}

function isProbablyJsonString(s) {
  if (!s) return false;
  const t = String(s).trim();
  return t.startsWith("{") && t.endsWith("}");
}

function buildTweetUrlFromJson(obj) {
  const id =
    obj?.id_str ||
    (typeof obj?.id === "number" ? String(obj.id) : obj?.id);

  const screen =
    obj?.user?.screen_name ||
    obj?.screen_name ||
    obj?.username;

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
    const ms = ((Date.now() - t0) / 1000).toFixed(2);
    log(`✅ END:   ${label} (${ms}s)`);
    return out;
  } catch (e) {
    const ms = ((Date.now() - t0) / 1000).toFixed(2);
    log(`❌ FAIL:  ${label} (${ms}s) -> ${e.message}`);
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
  // Speed up: block fonts + common analytics
  await page.route("**/*", (route) => {
    const url = route.request().url();
    if (url.match(/\.(woff|woff2|ttf|otf)(\?|$)/i)) return route.abort();
    if (
      url.includes("doubleclick") ||
      url.includes("googletagmanager") ||
      url.includes("google-analytics") ||
      url.includes("analytics")
    ) {
      return route.abort();
    }
    return route.continue();
  });
}

async function forceWhiteCss(page) {
  // IMPORTANT: Do NOT hide all <header> tags, because tweet header is inside the tweet card.
  await page.addStyleTag({
    content: `
      :root, html, body { background: #ffffff !important; }
      body { overflow: hidden !important; }

      /* Hide X site chrome, keep tweet internals */
      nav, aside, [role="banner"], [role="navigation"] { display: none !important; }
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
      await page.locator(sel).first().waitFor({ timeout: 15000, state: "visible" });
      return;
    } catch (_) {}
  }
  throw new Error("Tweet selector not found");
}

async function pickTweetElement(page) {
  const locTweet = page.locator('[data-testid="tweet"]').first();
  if (await locTweet.count()) return locTweet;

  const locArticle = page.locator("article").first();
  if (await locArticle.count()) return locArticle;

  const locMain = page.locator(".main-tweet").first();
  if (await locMain.count()) return locMain;

  return page.locator("body").first();
}

async function dedupMediaOnly(tweetEl, page) {
  await tweetEl.evaluate((root) => {
    const canon = (u) => {
      if (!u) return "";
      try {
        const url = new URL(u, location.href);
        return `${url.host}${url.pathname}`.toLowerCase();
      } catch {
        return String(u).split("?")[0].toLowerCase();
      }
    };

    const tiles = Array.from(
      root.querySelectorAll(
        '[data-testid="tweetPhoto"], [data-testid="videoPlayer"], div[aria-label="Embedded video"], div[aria-label="Embedded image"]'
      )
    );
    if (!tiles.length) return;

    const tileSig = (tile) => {
      const imgs = Array.from(tile.querySelectorAll("img"));
      if (imgs.length) {
        let best = imgs[0];
        let bestArea = 0;
        for (const img of imgs) {
          const r = img.getBoundingClientRect();
          const area = r.width * r.height;
          if (area > bestArea) {
            bestArea = area;
            best = img;
          }
        }
        return canon(best.currentSrc || best.src);
      }
      const vid = tile.querySelector("video");
      if (vid) return canon(vid.poster || vid.currentSrc || vid.src);
      return "";
    };

    // within tile: hide duplicate imgs
    for (const tile of tiles) {
      const imgs = Array.from(tile.querySelectorAll("img"));
      const seen = new Set();
      for (const img of imgs) {
        const sig = canon(img.currentSrc || img.src);
        if (!sig) continue;
        if (seen.has(sig)) {
          img.style.display = "none";
          img.style.visibility = "hidden";
        } else {
          seen.add(sig);
        }
      }
    }

    // across tiles: hide tiles with same sig
    const seenTile = new Set();
    for (const tile of tiles) {
      const sig = tileSig(tile);
      if (!sig) continue;
      if (seenTile.has(sig)) {
        tile.style.display = "none";
        tile.style.visibility = "hidden";
      } else {
        seenTile.add(sig);
      }
    }
  });

  await page.waitForTimeout(200);
}

async function toDataUrlFromPhoto(photo) {
  if (!photo) return null;

  if (typeof photo === "string" && photo.startsWith("data:image/")) return photo;

  // URL (Node 18+ fetch)
  if (typeof photo === "string" && /^https?:\/\//i.test(photo)) {
    const res = await fetch(photo);
    if (!res.ok) throw new Error(`brand photo fetch failed: ${res.status}`);
    const buf = Buffer.from(await res.arrayBuffer());
    const ct = res.headers.get("content-type") || "image/jpeg";
    return `data:${ct};base64,${buf.toString("base64")}`;
  }

  // local file path
  const p = String(photo);
  const buf = fs.readFileSync(p);

  const ext = path.extname(p).toLowerCase();
  const mime =
    ext === ".png" ? "image/png" :
    ext === ".webp" ? "image/webp" :
    ext === ".gif" ? "image/gif" :
    "image/jpeg";

  return `data:${mime};base64,${buf.toString("base64")}`;
}

async function getMyBrand() {
  const name = String(MY_NAME || "").trim();
  const userRaw = String(MY_USERNAME || "").trim();
  const username = userRaw ? (userRaw.startsWith("@") ? userRaw : `@${userRaw}`) : "";
  const photoDataUrl = await toDataUrlFromPhoto(MY_PHOTO).catch(() => null);

  if (!name && !username && !photoDataUrl) return null;
  return { name, username, photoDataUrl };
}

/**
 * Replace the author header INSIDE the tweet card with your brand.
 * Uses stable X testid anchor when available.
 */

async function renderOne(page, context, tweetObj, outPath, myBrand) {
  const tweetUrl = buildTweetUrlFromJson(tweetObj);
  if (!tweetUrl) return { ok: false, reason: "missing_url_fields" };

  if (tweetHasVideo(tweetObj)) {
    return { ok: false, reason: "video_tweet_skipped" };
  }

  const { pngOut, alsoWriteJpg, jpgOut } = normalizeOutputPath(outPath);
  const rawPath = pngOut.replace(/\.png$/i, "") + ".raw.png";

  await page.goto(tweetUrl, { waitUntil: "domcontentloaded", timeout: 45000 });

  await waitForTweetContent(page);
  await forceWhiteCss(page);
  await page.waitForTimeout(600);

  const tweetEl = await pickTweetElement(page);

  await dedupMediaOnly(tweetEl, page);

  // ✅ Replace author header INSIDE tweet
  await replaceTweetHeaderWithMine(tweetEl, page, myBrand);

  await tweetEl.screenshot({ path: rawPath, type: "png" });
  if (!fs.existsSync(rawPath)) return { ok: false, reason: "raw_missing" };

  // compose 4:5 in a second page
  const rawBuf = fs.readFileSync(rawPath);
  const rawB64 = rawBuf.toString("base64");

  const page2 = await context.newPage();
  await page2.setViewportSize({ width: FINAL_W, height: FINAL_H });

  const html = `
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8"/>
        <style>
          html, body {
            margin: 0;
            padding: 0;
            width: ${FINAL_W}px;
            height: ${FINAL_H}px;
            background: #ffffff;
          }
          .canvas {
            width: ${FINAL_W}px;
            height: ${FINAL_H}px;
            background: #ffffff;
            display: flex;
            align-items: center;
            justify-content: center;
          }
          .pad {
            width: ${FINAL_W}px;
            height: ${FINAL_H}px;
            padding: 64px;
            box-sizing: border-box;
            display: flex;
            align-items: center;
            justify-content: center;
          }
          img {
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
            display: block;
          }
        </style>
      </head>
      <body>
        <div class="canvas">
          <div class="pad">
            <img src="data:image/png;base64,${rawB64}" />
          </div>
        </div>
      </body>
    </html>
  `;

  await page2.setContent(html, { waitUntil: "load" });
  await page2.waitForTimeout(80);
  await page2.screenshot({ path: pngOut, type: "png" });
  await page2.close();

  try { fs.unlinkSync(rawPath); } catch (_) {}

  if (alsoWriteJpg && jpgOut) {
    try { fs.copyFileSync(pngOut, jpgOut); } catch (_) {}
  }

  return { ok: true, out: outPath, url: tweetUrl };
}

async function runBatch(batchJson) {
  let browser;
  const results = [];

  try {
    const items = JSON.parse(batchJson);
    if (!Array.isArray(items)) throw new Error("batch must be a JSON array");

    browser = await chromium.launch({
      args: [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
      ],
    });

    const context = await browser.newContext({
      colorScheme: "light",
      deviceScaleFactor: 2,
      timezoneId: "Asia/Kolkata",
      locale: "en-IN",
    });

    const page = await context.newPage();
    await page.setViewportSize({ width: 1400, height: 2000 });
    await preparePage(page);

    const myBrand = await getMyBrand();

    for (let i = 0; i < items.length; i++) {
      const it = items[i] || {};
      const out = it.out;
      let tweetObj = it.tweet;

      if (!out) {
        results.push({ ok: false, i, reason: "missing_out" });
        continue;
      }

      if (typeof tweetObj === "string" && isProbablyJsonString(tweetObj)) {
        try { tweetObj = JSON.parse(tweetObj); } catch (_) {}
      }
      if (!tweetObj || typeof tweetObj !== "object") {
        results.push({ ok: false, i, out, reason: "missing_tweet" });
        continue;
      }

      log(`▶ item ${i + 1}/${items.length}: ${out}`);
      try {
        const r = await renderOne(page, context, tweetObj, out, myBrand);
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
  let tweetObj = null;

  if (isProbablyJsonString(inputArg)) {
    tweetObj = JSON.parse(inputArg);
    const built = buildTweetUrlFromJson(tweetObj);
    if (!built) {
      console.error(`[${nowIso()}] ❌ Could not build tweet URL from JSON`);
      return 1;
    }
    tweetUrl = built;
  }

  let browser;
  try {
    browser = await chromium.launch({
      args: [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
      ],
    });

    const context = await browser.newContext({
      colorScheme: "light",
      deviceScaleFactor: 2,
      timezoneId: "Asia/Kolkata",
      locale: "en-IN",
    });

    const page = await context.newPage();
    await page.setViewportSize({ width: 1400, height: 2000 });
    await preparePage(page);

    const myBrand = await getMyBrand();

    await timeStep(`Goto tweet: ${tweetUrl}`, async () => {
      await page.goto(tweetUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
    });

    await timeStep("Wait for tweet content", async () => {
      await waitForTweetContent(page);
    });

    await timeStep("Force white background + hide side UI", async () => {
      await forceWhiteCss(page);
    });

    await timeStep("Let images settle", async () => {
      await page.waitForTimeout(600);
    });

    const tweetEl = await timeStep("Pick best tweet element", async () => {
      return await pickTweetElement(page);
    });

    await timeStep("DEDUP + replace header with my brand", async () => {
      await dedupMediaOnly(tweetEl, page);
      await replaceTweetHeaderWithMine(tweetEl, page, myBrand);
    });

    const { pngOut, alsoWriteJpg, jpgOut } = normalizeOutputPath(outputPathArg);
    const rawPath = pngOut.replace(/\.png$/i, "") + ".raw.png";

    await timeStep("Capture RAW tweet element", async () => {
      await tweetEl.screenshot({ path: rawPath, type: "png" });
      if (!fs.existsSync(rawPath)) throw new Error("Raw screenshot did not write");
    });

    await timeStep("Compose IG 4:5 (1080x1350) white canvas", async () => {
      const rawBuf = fs.readFileSync(rawPath);
      const rawB64 = rawBuf.toString("base64");

      const page2 = await context.newPage();
      await page2.setViewportSize({ width: FINAL_W, height: FINAL_H });

      const html = `
        <!doctype html><html><head><meta charset="utf-8"/>
        <style>
          html, body { margin:0; padding:0; width:${FINAL_W}px; height:${FINAL_H}px; background:#fff; }
          .canvas { width:${FINAL_W}px; height:${FINAL_H}px; display:flex; align-items:center; justify-content:center; background:#fff; }
          .pad { width:${FINAL_W}px; height:${FINAL_H}px; padding:64px; box-sizing:border-box; display:flex; align-items:center; justify-content:center; }
          img { max-width:100%; max-height:100%; object-fit:contain; display:block; }
        </style></head><body>
        <div class="canvas"><div class="pad"><img src="data:image/png;base64,${rawB64}"/></div></div>
        </body></html>
      `;

      await page2.setContent(html, { waitUntil: "load" });
      await page2.waitForTimeout(80);
      await page2.screenshot({ path: pngOut, type: "png" });
      await page2.close();
    });

    try { fs.unlinkSync(rawPath); } catch (_) {}
    if (alsoWriteJpg && jpgOut) {
      try { fs.copyFileSync(pngOut, jpgOut); } catch (_) {}
    }

    console.log(`[${nowIso()}] ✅ saved ${outputPathArg}`);
    return 0;
  } catch (e) {
    console.error(`[${nowIso()}] ❌ FAIL: ${e.message}`);
    return 1;
  } finally {
    if (browser) await browser.close();
  }
}

// ---- Entry ----
(async () => {
  const args = process.argv.slice(2);

  if (args[0] === "--batch") {
    const batchJson = args[1];
    const code = await runBatch(batchJson);
    process.exit(code);
  }

  // single mode
  const inputArg = args[0];
  const outArg = args[1];
  const code = await runSingle(inputArg, outArg);
  process.exit(code);
})();
