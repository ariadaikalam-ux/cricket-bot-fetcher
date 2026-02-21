/**
 * screenshot.js — Playwright tweet screenshotter (IG-ready 4:5)
 * Usage: node screenshot.js <tweet_url> <output_path>
 *
 * Output:
 *  - Always 1080x1350 (4:5)
 *  - White background
 *  - Tweet card centered and scaled
 *  - STRICT: only 1 media item (keeps first, hides the rest)
 */

const fs = require("fs");
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

      // block some heavy media previews if needed (optional)
      if (rtype === "media" && url.includes("video")) return route.abort();

      return route.continue();
    });

    await timeStep(`Goto tweet: ${tweetUrl}`, async () => {
      await page.goto(tweetUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
    });

    await timeStep("Wait for tweet content", async () => {
      const candidates = [
        '[data-testid="tweet"]',
        "article",
        ".main-tweet",
        ".tweet-body",
      ];

      let found = false;
      for (const sel of candidates) {
        try {
          await page.locator(sel).first().waitFor({ timeout: 12000, state: "visible" });
          found = true;
          break;
        } catch (_) {}
      }
      if (!found) throw new Error("Tweet selector not found");
    });

    await timeStep("Force white background + hide junk UI", async () => {
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

    // ✅ STRICT 1 MEDIA: keep first image/video, hide the rest (even if duplicates)
    await timeStep("STRICT: keep only first media", async () => {
      await tweetEl.evaluate((root) => {
        // Media wrappers we commonly see in X
        const mediaSelectors = [
          '[data-testid="tweetPhoto"]',           // photos
          '[data-testid="videoPlayer"]',          // videos
          'div[aria-label="Embedded video"]',     // fallback video
          'div[aria-label="Embedded image"]',     // fallback image
        ];

        // Collect unique media blocks inside the tweet
        let mediaBlocks = [];
        for (const sel of mediaSelectors) {
          root.querySelectorAll(sel).forEach((el) => mediaBlocks.push(el));
        }

        // If X renders a grid, tweetPhoto can be nested; remove duplicates by DOM order
        // Keep first block, hide everything after it.
        if (mediaBlocks.length > 1) {
          // Sort by DOM position to reliably pick "first"
          mediaBlocks.sort((a, b) => {
            if (a === b) return 0;
            const pos = a.compareDocumentPosition(b);
            if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
            if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
            return 0;
          });

          const keep = mediaBlocks[0];

          for (let i = 1; i < mediaBlocks.length; i++) {
            const el = mediaBlocks[i];
            el.style.display = "none";
            el.style.visibility = "hidden";
          }

          // Also: if inside the kept block there are multiple <img> (rare),
          // keep only the first <img>
          const imgs = keep.querySelectorAll("img");
          if (imgs.length > 1) {
            for (let i = 1; i < imgs.length; i++) {
              imgs[i].style.display = "none";
              imgs[i].style.visibility = "hidden";
            }
          }
        }

        // Extra safety: sometimes the same image is duplicated as multiple imgs in the tweet
        // Keep only first "meaningful" image within the tweet media area.
        const allImgs = Array.from(root.querySelectorAll("img"));
        if (allImgs.length > 0) {
          // We DO NOT want to hide profile pics etc.
          // Heuristic: media images tend to be bigger; keep first big image and hide other big ones.
          const bigImgs = allImgs.filter((img) => {
            const r = img.getBoundingClientRect();
            return r.width >= 180 && r.height >= 180; // treat as "media-like"
          });

          if (bigImgs.length > 1) {
            // Keep first big image only
            for (let i = 1; i < bigImgs.length; i++) {
              bigImgs[i].style.display = "none";
              bigImgs[i].style.visibility = "hidden";
            }
          }
        }
      });

      // Let layout reflow after hiding
      await page.waitForTimeout(200);
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
      await page2.screenshot({ path: outputPath, type: "png" });
      await page2.close();
    });

    // cleanup
    try { fs.unlinkSync(rawPath); } catch (_) {}

    console.log(`[${nowIso()}] ✅ DONE: ${outputPath}`);
    process.exit(0);
  } catch (err) {
    console.error(`[${nowIso()}] ❌ FAIL: ${err.message}`);
    try { if (fs.existsSync(rawPath)) fs.unlinkSync(rawPath); } catch (_) {}
    process.exit(1);
  } finally {
    if (browser) await browser.close();
  }
})();
