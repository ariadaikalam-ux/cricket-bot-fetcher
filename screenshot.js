/**
 * screenshot.js — Playwright X(Twitter) screenshotter (4:5 white)
 *
 * Input:
 * 1) node screenshot.js <tweet_url> <output_path>
 * 2) node screenshot.js '<tweet_json>' <output_path>
 *
 * Behavior:
 * - If tweet has ONLY 1 media => use /photo/1 (prevents “duplicate” grid)
 * - If tweet has 2+ media => use base tweet URL (grid/collage allowed)
 * - Safety: if X renders duplicate tiles for same image, hide duplicates
 *
 * Output:
 * - 1080x1350 (4:5)
 * - white background
 * - tweet card visible (dp/name/text)
 */

const { chromium } = require("playwright");

const inputArg = process.argv[2];
const outputPath = process.argv[3];

if (!inputArg || !outputPath) {
  console.error("Usage: node screenshot.js <tweet_url|tweet_json> <output_path>");
  process.exit(1);
}

function ts() {
  return new Date().toISOString();
}
function log(msg) {
  console.log(`[${ts()}] ${msg}`);
}
function warn(msg) {
  console.warn(`[${ts()}] ⚠️ ${msg}`);
}
function fail(msg) {
  console.error(`[${ts()}] ❌ ${msg}`);
}

function isProbablyJson(s) {
  return typeof s === "string" && s.trim().startsWith("{") && s.trim().endsWith("}");
}

function extractMediaCount(tweet) {
  // SocialData usually has entities.media; sometimes extended_entities.media
  const a = tweet?.extended_entities?.media;
  const b = tweet?.entities?.media;
  const arr = Array.isArray(a) ? a : Array.isArray(b) ? b : [];
  return arr.length;
}

function buildUrlsFromJson(tweet) {
  const id =
    tweet?.id_str ||
    (typeof tweet?.id === "number" ? String(tweet.id) : tweet?.id);

  const screen =
    tweet?.user?.screen_name ||
    tweet?.user?.username ||
    tweet?.username ||
    null;

  const base = screen
    ? `https://x.com/${screen}/status/${id}`
    : `https://x.com/i/web/status/${id}`;

  const photo1 = `${base}/photo/1`;
  return { base, photo1, id, screen };
}

function buildUrlsFromUrl(url) {
  const clean = url.trim().replace("twitter.com", "x.com");
  const m = clean.match(/\/([^\/]+)\/status\/(\d+)/);
  const screen = m ? m[1] : null;
  const id = m ? m[2] : null;

  let base = clean.replace(/\/photo\/\d+.*$/i, "");
  const photo1 = `${base}/photo/1`;
  return { base, photo1, id, screen };
}

async function hardenPage(page) {
  await page.route("**/*.{woff,woff2,ttf,otf}", (r) => r.abort());
  await page.route("**/*analytics**", (r) => r.abort());
  await page.route("**/*doubleclick**", (r) => r.abort());
  await page.route("**/*googletagmanager**", (r) => r.abort());
}

async function applyWhite4by5Layout(page) {
  await page.addStyleTag({
    content: `
      html, body { background: #ffffff !important; }
      body { overflow: hidden !important; }

      /* Hide app chrome / sidebars */
      header, nav, [role="banner"], [data-testid="sidebarColumn"],
      [data-testid="AppTabBar_Container"], [data-testid="BottomBar"],
      [data-testid="TopNavBar"] { display: none !important; }

      /* Hide overlays */
      [data-testid="sheetDialog"], [data-testid="toast"], [data-testid="mask"] { display:none !important; }
      [aria-label="Close"] { display:none !important; }

      /* Center main column and make tweet bigger */
      [role="main"], [data-testid="primaryColumn"] {
        margin: 0 auto !important;
        width: 900px !important;
        max-width: 900px !important;
        background: #ffffff !important;
      }

      body { zoom: 1.15; }
    `,
  });
}

async function waitForTweetToRender(page) {
  await page.waitForTimeout(1200);
  await page.waitForSelector("article", { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(1200);
}

async function hideDuplicateMediaTilesIfAny(page) {
  // If X accidentally shows two identical images side-by-side for a single-media tweet,
  // hide duplicates by comparing src.
  await page.evaluate(() => {
    try {
      const article = document.querySelector("article");
      if (!article) return;

      const imgs = Array.from(article.querySelectorAll('img'));
      if (!imgs.length) return;

      // Find likely media images: bigger images (exclude tiny icons)
      const mediaImgs = imgs.filter(img => {
        const r = img.getBoundingClientRect();
        return r.width > 150 && r.height > 150;
      });

      if (mediaImgs.length <= 1) return;

      const seen = new Set();
      for (const img of mediaImgs) {
        const src = img.currentSrc || img.src || "";
        if (!src) continue;

        if (seen.has(src)) {
          // hide the closest tile/container
          const tile =
            img.closest('[data-testid="tweetPhoto"]') ||
            img.closest('a') ||
            img.parentElement;
          if (tile) tile.style.display = "none";
        } else {
          seen.add(src);
        }
      }
    } catch (e) {}
  });
}

async function gotoUrl(page, url, label) {
  log(`▶ START: goto ${label}: ${url}`);
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
  await waitForTweetToRender(page);
  log(`✅ ${label} loaded`);
}

async function screenshot4by5(page, outputPath) {
  const clip = { x: 0, y: 0, width: 1080, height: 1350 };
  const isPng = outputPath.toLowerCase().endsWith(".png");
  await page.screenshot({
    path: outputPath,
    type: isPng ? "png" : "jpeg",
    quality: isPng ? undefined : 92,
    clip,
  });
}

(async () => {
  let browser;
  try {
    let urls;
    let mediaCount = null;

    if (isProbablyJson(inputArg)) {
      const tweet = JSON.parse(inputArg);
      urls = buildUrlsFromJson(tweet);
      mediaCount = extractMediaCount(tweet);
      log(`Input: JSON tweet id=${urls.id} user=${urls.screen || "unknown"} mediaCount=${mediaCount}`);
    } else {
      urls = buildUrlsFromUrl(inputArg);
      log(`Input: URL id=${urls.id || "unknown"} user=${urls.screen || "unknown"}`);
    }

    browser = await chromium.launch({
      args: [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
      ],
    });

    const page = await browser.newPage();
    await page.setViewportSize({ width: 1080, height: 1350 });
    await hardenPage(page);

    // Decide navigation:
    // - If we KNOW mediaCount==1 => go photo1 (prevents duplicate grid)
    // - Else (unknown or 2+) => go base (grid/collage allowed)
    if (mediaCount === 1) {
      await gotoUrl(page, urls.photo1, "photo/1 (single media)");
    } else {
      await gotoUrl(page, urls.base, "base (multi/unknown media)");
    }

    await applyWhite4by5Layout(page);
    await page.waitForTimeout(900);

    // Safety: hide duplicate tiles if X accidentally duplicates single media
    await hideDuplicateMediaTilesIfAny(page);
    await page.waitForTimeout(400);

    await screenshot4by5(page, outputPath);
    log(`✅ OK screenshot: ${outputPath}`);

    process.exit(0);
  } catch (err) {
    fail(`FAIL: ${err.message}`);
    process.exit(1);
  } finally {
    if (browser) await browser.close();
  }
})();
