/**
 * screenshot.js — Playwright tweet screenshotter (IG-ready 4:5)
 * Usage: node screenshot.js <tweet_url> <output_path>
 *
 * Output:
 *  - Always 1080x1350 (4:5)
 *  - White background
 *  - Tweet card centered and scaled to use space nicely
 *  - Keeps author DP/name visible (captures the tweet "card")
 */

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const tweetUrl = process.argv[2];
const outputPath = process.argv[3];

if (!tweetUrl || !outputPath) {
  console.error("Usage: node screenshot.js <tweet_url> <output_path>");
  process.exit(1);
}

const FINAL_W = 1080;
const FINAL_H = 1350;

function nowIso() {
  return new Date().toISOString();
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
  const rawPath = outputPath.replace(/\.png$/i, "") + ".raw.png";

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
      // Make sure we get LIGHT mode always
      colorScheme: "light",
      deviceScaleFactor: 2,
    });

    const page = await context.newPage();

    // Big viewport for clean element capture (we compose to 1080x1350 later)
    await page.setViewportSize({ width: 1400, height: 2000 });

    // Speed up: block fonts + some heavy stuff
    await page.route("**/*", (route) => {
      const url = route.request().url();
      const rtype = route.request().resourceType();

      // Block fonts
      if (url.match(/\.(woff|woff2|ttf|otf)(\?|$)/i)) return route.abort();

      // Block some common analytics/beacons
      if (
        url.includes("doubleclick") ||
        url.includes("googletagmanager") ||
        url.includes("google-analytics") ||
        url.includes("analytics")
      ) {
        return route.abort();
      }

      // Allow everything else
      return route.continue();
    });

    await timeStep(`Goto tweet: ${tweetUrl}`, async () => {
      await page.goto(tweetUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
    });

    await timeStep("Wait for tweet content", async () => {
      // X (Twitter) usually uses [data-testid="tweet"]
      // Fallbacks are included
      const candidates = [
        '[data-testid="tweet"]',
        "article",
        ".main-tweet",
        ".tweet-body",
      ];

      let found = false;
      for (const sel of candidates) {
        const loc = page.locator(sel).first();
        try {
          await loc.waitFor({ timeout: 12000, state: "visible" });
          found = true;
          break;
        } catch (_) {}
      }
      if (!found) throw new Error("Tweet selector not found");
    });

    await timeStep("Force white background + hide junk UI", async () => {
      // Clean page visuals + force white
      await page.addStyleTag({
        content: `
          :root, html, body { background: #ffffff !important; }
          body { overflow: hidden !important; }

          /* Hide top/bottom bars, login nags etc (best-effort) */
          header, nav, aside, [role="banner"], [role="navigation"] { display: none !important; }
          [data-testid="sidebarColumn"], [data-testid="primaryColumn"] { background: #ffffff !important; }

          /* Remove any dim/overlay backgrounds */
          [role="dialog"] { background: transparent !important; }

          /* Make tweet card look like a clean card */
          [data-testid="tweet"] { background: #ffffff !important; }
        `,
      });
    });

    await timeStep("Let images settle", async () => {
      await page.waitForTimeout(1500);
    });

    const tweetEl = await timeStep("Pick best tweet element", async () => {
      // Prefer the tweet testid, then fallback
      const locTweet = page.locator('[data-testid="tweet"]').first();
      if (await locTweet.count()) return locTweet;

      const locArticle = page.locator("article").first();
      if (await locArticle.count()) return locArticle;

      const locMain = page.locator(".main-tweet").first();
      if (await locMain.count()) return locMain;

      return page.locator("body").first();
    });

    await timeStep("Capture RAW tweet element (tight crop)", async () => {
      await tweetEl.screenshot({ path: rawPath, type: "png" });
      if (!fs.existsSync(rawPath)) throw new Error("Raw screenshot did not write");
    });

    await timeStep("Compose IG 4:5 (1080x1350) with white canvas", async () => {
      const rawBuf = fs.readFileSync(rawPath);
      const rawB64 = rawBuf.toString("base64");

      const page2 = await context.newPage();
      await page2.setViewportSize({ width: FINAL_W, height: FINAL_H });

      // We render the raw image inside a fixed 1080x1350 canvas,
      // centered and scaled to use space without cropping.
      const html = `
        <!doctype html>
        <html>
          <head>
            <meta charset="utf-8"/>
            <meta name="viewport" content="width=device-width, initial-scale=1"/>
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
                padding: 64px; /* controls “use of space” */
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
      await page2.waitForTimeout(250);

      await page2.screenshot({ path: outputPath, type: "png" });
      await page2.close();
    });

    // cleanup raw
    try {
      fs.unlinkSync(rawPath);
    } catch (_) {}

    console.log(`[${nowIso()}] ✅ DONE: ${outputPath}`);
    process.exit(0);
  } catch (err) {
    console.error(`[${nowIso()}] ❌ FAIL: ${err.message}`);
    try {
      if (fs.existsSync(rawPath)) fs.unlinkSync(rawPath);
    } catch (_) {}
    process.exit(1);
  } finally {
    if (browser) await browser.close();
  }
})();
