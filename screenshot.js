/**
 * screenshot.js — Playwright tweet screenshotter (IG-ready 4:5)
 * Usage:
 *   node screenshot.js <tweet_url_or_json> <output_path>
 *
 * Supports:
 *  - tweet URL (https://x.com/user/status/ID)
 *  - JSON string from SocialData (your bot passes this)
 *
 * Output:
 *  - Always 1080x1350 (4:5)
 *  - White background
 *  - Tweet card centered and scaled
 *  - STRICT: only 1 media item (keeps first, hides the rest)
 */

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const inputArg = process.argv[2];
const outputPathArg = process.argv[3];

if (!inputArg || !outputPathArg) {
  console.error("Usage: node screenshot.js <tweet_url_or_json> <output_path>");
  process.exit(1);
}

const FINAL_W = 1080;
const FINAL_H = 1350;

function nowIso() {
  return new Date().toISOString();
}

function isProbablyJson(s) {
  if (!s) return false;
  const t = s.trim();
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

  // Use x.com (works; twitter.com often redirects)
  return `https://x.com/${screen}/status/${id}`;
}

function normalizeOutputPath(p) {
  // If bot gives .jpg, we’ll still write a PNG internally then convert by renaming extension.
  // Easiest: write exactly to the provided output path, but ensure extension matches screenshot type.
  // We'll output PNG bytes. If extension is .jpg, we still write png bytes; that can confuse some readers.
  // So we force output to .png and also create the .jpg path if needed.
  const ext = path.extname(p).toLowerCase();
  if (ext === ".png") return { pngOut: p, alsoWriteJpg: false, jpgOut: null };

  if (ext === ".jpg" || ext === ".jpeg") {
    const pngOut = p.replace(/\.(jpg|jpeg)$/i, ".png");
    return { pngOut, alsoWriteJpg: true, jpgOut: p };
  }

  // unknown extension → just add .png
  return { pngOut: p + ".png", alsoWriteJpg: false, jpgOut: null };
}

async function timeStep(label, fn) {
  const t0 = Date.now();
  console.log(`[${nowIso()}] ▶ START: ${label}`);
  try {
    const out = await fn();
    const ms = ((Date.now() - t0) / 1000).toFixed(2);
    console.log(`[${nowIso()}] ✅ END:   ${label} (${ms}s)`);
    return out;
  } catch (e) {
    const ms = ((Date.now() - t0) / 1000).toFixed(2);
    console.log(`[${nowIso()}] ❌ FAIL:  ${label} (${ms}s) -> ${e.message}`);
    throw e;
  }
}

(async () => {
  let browser;

  // 1) Resolve tweet URL
  let tweetUrl = inputArg;

  if (isProbablyJson(inputArg)) {
    try {
      const obj = JSON.parse(inputArg);
      const built = buildTweetUrlFromJson(obj);
      if (built) tweetUrl = built;
      else {
        console.error(
          `[${nowIso()}] ❌ Could not build tweet URL from JSON (missing user.screen_name or id_str)`
        );
        process.exit(1);
      }
    } catch (e) {
      console.error(`[${nowIso()}] ❌ JSON parse failed: ${e.message}`);
      process.exit(1);
    }
  } else {
    // Sometimes the bot might pass "id" only in future; handle that too:
    const onlyDigits = (inputArg || "").trim().match(/^\d{10,30}$/);
    if (onlyDigits) {
      console.error(
        `[${nowIso()}] ❌ Got only an ID. Pass JSON or full URL so we know screen_name.`
      );
      process.exit(1);
    }
  }

  // 2) Output path normalization
  const { pngOut, alsoWriteJpg, jpgOut } = normalizeOutputPath(outputPathArg);
  const rawPath = pngOut.replace(/\.png$/i, "") + ".raw.png";

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
    });

    const page = await context.newPage();
    await page.setViewportSize({ width: 1400, height: 2000 });

    // Speed up a bit
    await page.route("**/*", (route) => {
      const url = route.request().url();
      const rtype = route.request().resourceType();

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

    await timeStep(`Goto tweet: ${tweetUrl}`, async () => {
      await page.goto(tweetUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
    });

    await timeStep("Wait for tweet content", async () => {
      const candidates = ['[data-testid="tweet"]', "article", ".main-tweet", ".tweet-body"];

      let found = false;
      for (const sel of candidates) {
        try {
          await page.locator(sel).first().waitFor({ timeout: 15000, state: "visible" });
          found = true;
          break;
        } catch (_) {}
      }
      if (!found) throw new Error("Tweet selector not found");
    });

    await timeStep("Force white background + hide side UI", async () => {
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
    });

    await timeStep("Let images settle", async () => {
      await page.waitForTimeout(1500);
    });

    const tweetEl = await timeStep("Pick best tweet element", async () => {
      const locTweet = page.locator('[data-testid="tweet"]').first();
      if (await locTweet.count()) return locTweet;

      const locArticle = page.locator("article").first();
      if (await locArticle.count()) return locArticle;

      const locMain = page.locator(".main-tweet").first();
      if (await locMain.count()) return locMain;

      return page.locator("body").first();
    });

    // ✅ STRICT 1 MEDIA
    await timeStep("STRICT: keep only first media", async () => {
      await tweetEl.evaluate((root) => {
        const mediaSelectors = [
          '[data-testid="tweetPhoto"]',
          '[data-testid="videoPlayer"]',
          'div[aria-label="Embedded video"]',
          'div[aria-label="Embedded image"]',
        ];

        let mediaBlocks = [];
        for (const sel of mediaSelectors) {
          root.querySelectorAll(sel).forEach((el) => mediaBlocks.push(el));
        }

        if (mediaBlocks.length > 1) {
          mediaBlocks.sort((a, b) => {
            if (a === b) return 0;
            const pos = a.compareDocumentPosition(b);
            if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
            if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
            return 0;
          });

          const keep = mediaBlocks[0];

          for (let i = 1; i < mediaBlocks.length; i++) {
            mediaBlocks[i].style.display = "none";
            mediaBlocks[i].style.visibility = "hidden";
          }

          const imgs = keep.querySelectorAll("img");
          if (imgs.length > 1) {
            for (let i = 1; i < imgs.length; i++) {
              imgs[i].style.display = "none";
              imgs[i].style.visibility = "hidden";
            }
          }
        }

        // Extra safety for duplicated large imgs
        const allImgs = Array.from(root.querySelectorAll("img"));
        const bigImgs = allImgs.filter((img) => {
          const r = img.getBoundingClientRect();
          return r.width >= 180 && r.height >= 180;
        });
        if (bigImgs.length > 1) {
          for (let i = 1; i < bigImgs.length; i++) {
            bigImgs[i].style.display = "none";
            bigImgs[i].style.visibility = "hidden";
          }
        }
      });

      await page.waitForTimeout(200);
    });

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
      await page2.waitForTimeout(150);
      await page2.screenshot({ path: pngOut, type: "png" });
      await page2.close();
    });

    // Cleanup raw
    try { fs.unlinkSync(rawPath); } catch (_) {}

    // If bot asked for .jpg, create a copy with .jpg extension (still PNG bytes)
    // This is fine if YOUR pipeline just uploads; if you truly need real JPEG conversion,
    // tell me and I’ll switch to a real converter step.
    if (alsoWriteJpg && jpgOut) {
      try {
        fs.copyFileSync(pngOut, jpgOut);
      } catch (_) {}
    }

    console.log(`[${nowIso()}] ✅ DONE: ${pngOut}${alsoWriteJpg ? ` (also wrote ${jpgOut})` : ""}`);
    process.exit(0);
  } catch (err) {
    console.error(`[${nowIso()}] ❌ FAIL: ${err.message}`);
    try { if (fs.existsSync(rawPath)) fs.unlinkSync(rawPath); } catch (_) {}
    process.exit(1);
  } finally {
    if (browser) await browser.close();
  }
})();
