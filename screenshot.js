/**
 * screenshot.js — 4:5 Instagram-ready tweet renderer
 * - Always outputs EXACT 1080x1350 (4:5)
 * - Pure white background (no transparency)
 * - Fits ANY tweet card (tall/long text/4 photos) by scaling down + centering
 * - Prevents header (DP/name) from being clipped
 * - Exports JPEG to avoid IG alpha/black-bar issues
 *
 * Usage: node screenshot.js '<tweet_json>' <output_path>
 */

const { chromium } = require('playwright');

const tweetJson  = process.argv[2];
const outputPath = process.argv[3];

if (!tweetJson || !outputPath) {
  console.error('Usage: node screenshot.js <tweet_json> <output_path>');
  process.exit(1);
}

let tweet;
try {
  tweet = JSON.parse(tweetJson);
} catch (e) {
  console.error('Invalid JSON:', e.message);
  process.exit(1);
}

const FRAME_W = 1080;
const FRAME_H = 1350;

// Tweak these if you want “bigger tweet” on canvas
const FRAME_PAD = 44;     // smaller pad => bigger tweet in final
const CARD_W    = 980;    // base card width

function fmt(n) {
  if (n === null || n === undefined) return '0';
  const x = Number(n) || 0;
  if (x >= 1_000_000) return (x / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M';
  if (x >= 1_000)     return (x / 1_000).toFixed(1).replace(/\.0$/, '') + 'K';
  return String(x);
}

function fmtDate(dateStr) {
  if (!dateStr) return '';
  try {
    const d = new Date(dateStr);
    return d.toLocaleString('en-US', {
      month: 'short', day: 'numeric', year: 'numeric',
      hour: 'numeric', minute: '2-digit', hour12: true,
      timeZone: 'UTC'
    });
  } catch {
    return String(dateStr);
  }
}

function escapeHtml(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function initialsFromName(name) {
  const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
  const a = (parts[0]?.[0] || '').toUpperCase();
  const b = (parts[1]?.[0] || '').toUpperCase();
  return (a + b) || 'U';
}

function buildHTML(t) {
  const name        = t.user?.name || t.author?.name || 'Unknown';
  const screen_name = t.user?.screen_name || t.author?.username || '';
  const avatar      = t.user?.profile_image_url_https
                   || t.user?.profile_image_url
                   || t.author?.profile_image_url
                   || '';

  const verified    = !!(t.user?.verified || t.user?.is_blue_verified);
  const textRaw     = t.full_text || t.text || '';
  const created_at  = t.tweet_created_at || t.created_at || '';

  const likes    = t.favorite_count ?? t.likes ?? 0;
  const retweets = t.retweet_count ?? t.retweets ?? 0;
  const replies  = t.reply_count ?? t.replies ?? 0;

  const entitiesMedia =
    t.extended_entities?.media ||
    t.entities?.media ||
    t.media ||
    [];

  const photos = entitiesMedia
    .filter(m => m && (m.type === 'photo' || m.type === 'image'))
    .map(m => m.media_url_https || m.media_url || m.url)
    .filter(Boolean)
    .slice(0, 4);

  // format text (links stripped, mentions/hashtags colored)
  const text = escapeHtml(textRaw)
    .replace(/https?:\/\/t\.co\/\S+/g, '')
    .replace(/@([A-Za-z0-9_]+)/g, '<span class="link">@$1</span>')
    .replace(/#([A-Za-z0-9_]+)/g, '<span class="link">#$1</span>')
    .replace(/\n/g, '<br>');

  let mediaHTML = '';
  if (photos.length === 1) {
    mediaHTML = `<div class="media-single"><img src="${photos[0]}" /></div>`;
  } else if (photos.length >= 2) {
    mediaHTML = `<div class="media-grid grid-${photos.length}">
      ${photos.map(p => `<img src="${p}" />`).join('')}
    </div>`;
  }

  const initials = initialsFromName(name);

  return `<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=${FRAME_W}, height=${FRAME_H}">
<style>
  * { box-sizing: border-box; }
  html, body {
    width: ${FRAME_W}px;
    height: ${FRAME_H}px;
    margin: 0;
    background: #ffffff;
    overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }

  .frame {
    width: ${FRAME_W}px;
    height: ${FRAME_H}px;
    background: #ffffff;
    padding: ${FRAME_PAD}px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .wrap {
    width: 100%;
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .card {
    width: ${CARD_W}px;
    background: #fff;
    border: 1px solid #eff3f4;
    border-radius: 26px;
    padding: 26px 26px 20px;
    box-shadow: 0 1px 0 rgba(0,0,0,0.02);
  }

  .header { display:flex; gap:14px; align-items:center; margin-bottom: 14px; }
  .avatar {
    width: 56px; height: 56px; border-radius: 50%;
    background: #e9ecef; overflow: hidden;
    display:flex; align-items:center; justify-content:center;
    flex-shrink:0;
  }
  .avatar img { width:100%; height:100%; object-fit: cover; display:block; }
  .avatar .init { font-weight: 800; color:#6b7280; font-size: 18px; }

  .user { min-width:0; flex:1; }
  .nameRow { display:flex; align-items:center; gap:6px; }
  .name {
    font-weight: 850;
    font-size: 18px;
    color: #0f1419;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 740px;
  }
  .handle {
    font-size: 15px;
    color:#536471;
    margin-top: 2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 740px;
  }
  .verified svg { width:18px; height:18px; }

  .xlogo { margin-left:auto; opacity:0.8; flex-shrink:0; }
  .xlogo svg { width:22px; height:22px; }

  .text {
    font-size: 21px;
    line-height: 1.55;
    color: #0f1419;
    margin-bottom: 16px;
    word-break: break-word;
  }
  .link { color:#1d9bf0; font-weight:600; }

  .media-single img {
    width: 100%;
    max-height: 560px;
    object-fit: cover;
    border-radius: 18px;
    display:block;
    background:#f3f4f6;
    margin-bottom: 16px;
  }

  .media-grid {
    display:grid;
    gap:4px;
    border-radius: 18px;
    overflow:hidden;
    background:#f3f4f6;
    margin-bottom: 16px;
  }
  .media-grid img {
    width:100%;
    height: 300px;
    object-fit: cover;
    display:block;
    background:#f3f4f6;
  }
  .grid-2 { grid-template-columns: 1fr 1fr; }
  .grid-3 { grid-template-columns: 1fr 1fr; }
  .grid-3 img:first-child { grid-row: span 2; height: 604px; }
  .grid-4 { grid-template-columns: 1fr 1fr; }

  .footer {
    border-top: 1px solid #eff3f4;
    padding-top: 14px;
    margin-top: 10px;
    color:#536471;
    font-size: 15px;
  }

  .stats {
    margin-top: 12px;
    display:flex;
    gap: 18px;
    color:#536471;
    font-size: 14px;
  }
  .stats b { color:#0f1419; font-weight: 800; }
</style>
</head>

<body>
  <div class="frame">
    <div class="wrap">
      <div class="card" id="card">
        <div class="header">
          <div class="avatar">
            ${avatar ? `<img src="${avatar}" />` : `<div class="init">${escapeHtml(initials)}</div>`}
          </div>

          <div class="user">
            <div class="nameRow">
              <div class="name">${escapeHtml(name)}</div>
              ${verified ? `<span class="verified">
                <svg viewBox="0 0 24 24" fill="#1d9bf0"><path d="M22.25 12c0-1.43-.88-2.67-2.19-3.34.46-1.39.2-2.9-.81-3.91s-2.52-1.27-3.91-.81c-.66-1.31-1.91-2.19-3.34-2.19s-2.67.88-3.33 2.19c-1.4-.46-2.91-.2-3.92.81s-1.26 2.52-.8 3.91C3.13 9.33 2.25 10.57 2.25 12s.88 2.67 2.19 3.33c-.46 1.4-.2 2.91.81 3.92s2.52 1.26 3.91.8c.67 1.31 1.91 2.19 3.34 2.19s2.67-.88 3.33-2.19c1.4.46 2.91.2 3.92-.81s1.26-2.52.8-3.91C21.37 14.67 22.25 13.43 22.25 12zM10.5 17.19l-4.25-4.25 1.41-1.41 2.84 2.83 5.59-5.59 1.41 1.42-7 7z"/></svg>
              </span>` : ``}
            </div>
            <div class="handle">@${escapeHtml(screen_name)}</div>
          </div>

          <div class="xlogo">
            <svg viewBox="0 0 24 24" fill="#000"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-4.714-6.231-5.401 6.231H2.744l7.737-8.835L1.254 2.25H8.08l4.254 5.622 5.91-5.622zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>
          </div>
        </div>

        <div class="text">${text}</div>
        ${mediaHTML}

        <div class="footer">${escapeHtml(fmtDate(created_at))}</div>
        <div class="stats">
          <span><b>${fmt(replies)}</b> Replies</span>
          <span><b>${fmt(retweets)}</b> Reposts</span>
          <span><b>${fmt(likes)}</b> Likes</span>
        </div>
      </div>
    </div>
  </div>

<script>
  // Robust "fit card into frame" that prevents clipping DP/name in ALL cases.
  (function fit() {
    const wrap = document.querySelector('.wrap');
    const card = document.getElementById('card');
    if (!wrap || !card) return;

    // Wait for layout to stabilize
    const availW = wrap.clientWidth;
    const availH = wrap.clientHeight;

    const rect = card.getBoundingClientRect();
    const scale = Math.min(availW / rect.width, availH / rect.height, 1);

    // Apply scale
    card.style.transformOrigin = 'center center';
    card.style.transform = 'scale(' + scale + ')';

    // After scaling, re-center precisely (avoid left cropping)
    requestAnimationFrame(() => {
      const r2 = card.getBoundingClientRect();
      const dx = (availW - r2.width) / 2 - (r2.left - wrap.getBoundingClientRect().left);
      const dy = (availH - r2.height) / 2 - (r2.top - wrap.getBoundingClientRect().top);
      card.style.transform = 'translate(' + dx + 'px,' + dy + 'px) scale(' + scale + ')';
    });
  })();
</script>
</body>
</html>`;
}

(async () => {
  let browser;
  try {
    browser = await chromium.launch({
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
    });

    const page = await browser.newPage({
      viewport: { width: FRAME_W, height: FRAME_H },
      deviceScaleFactor: 2,
    });

    await page.setContent(buildHTML(tweet), { waitUntil: 'domcontentloaded', timeout: 30000 });

    // Best-effort wait for images (avatars/media)
    await page.waitForFunction(() => {
      const imgs = Array.from(document.images || []);
      return imgs.every(i => i.complete);
    }, { timeout: 12000 }).catch(() => {});

    // Give fit() time to run and re-center after images load
    await page.waitForTimeout(300);

    // IMPORTANT: JPEG prevents alpha/transparency edge cases in IG rendering
    await page.screenshot({
      path: outputPath,
      type: outputPath.toLowerCase().endsWith('.png') ? 'png' : 'jpeg',
      quality: outputPath.toLowerCase().endsWith('.png') ? undefined : 92,
      fullPage: false,
      omitBackground: false,
    });

    console.log(`OK: ${outputPath}`);
    process.exit(0);
  } catch (err) {
    console.error(`FAIL: ${err.stack || err.message}`);
    process.exit(1);
  } finally {
    if (browser) await browser.close();
  }
})();
