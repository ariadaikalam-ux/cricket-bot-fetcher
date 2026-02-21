/**
 * screenshot.js — Renders tweet data into a custom HTML template
 * and screenshots it. No X.com, no login walls, reliable.
 *
 * Usage: node screenshot.js '<json_tweet_data>' <output_path>
 *
 * Env:
 *   SHOW_STATS=1   (default 0)  -> show replies/retweets/likes/views row
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

const SHOW_STATS = process.env.SHOW_STATS === '1';

// ─── Format numbers ───────────────────────────────────────────
function fmt(n) {
  if (n === null || n === undefined) return '0';
  const x = Number(n) || 0;
  if (x >= 1_000_000) return (x / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M';
  if (x >= 1_000) return (x / 1_000).toFixed(1).replace(/\.0$/, '') + 'K';
  return String(x);
}

// ─── Format date ──────────────────────────────────────────────
function fmtDate(dateStr) {
  if (!dateStr) return '';
  try {
    const d = new Date(String(dateStr).replace('Z', '+00:00'));
    return d.toLocaleString('en-US', {
      hour: 'numeric', minute: '2-digit', hour12: true,
      month: 'short', day: 'numeric', year: 'numeric',
      timeZone: 'UTC'
    });
  } catch {
    return String(dateStr);
  }
}

// ─── Build tweet HTML ─────────────────────────────────────────
function buildHTML(t) {
  const name        = t.user?.name || t.author?.name || 'Unknown';
  const screen_name = t.user?.screen_name || t.author?.username || '';
  const avatar      = t.user?.profile_image_url_https
                   || t.user?.profile_image_url
                   || t.author?.profile_image_url
                   || '';
  const verified    = Boolean(t.user?.verified || t.user?.is_blue_verified);
  const text        = t.full_text || t.text || '';
  const created_at  = t.tweet_created_at || t.created_at || '';

  const likes    = t.favorite_count ?? t.likes ?? 0;
  const retweets = t.retweet_count ?? t.retweets ?? 0;
  const replies  = t.reply_count ?? t.replies ?? 0;
  const views    = t.views_count ?? t.views ?? null;

  // Media images (photos only)
  const mediaEntities =
    t.entities?.media ||
    t.extended_entities?.media ||
    t.media ||
    [];
  const photos = (mediaEntities || [])
    .filter(m => m?.type === 'photo')
    .map(m => m.media_url_https || m.media_url || m.url)
    .filter(Boolean)
    .slice(0, 4);

  // Format text (light styling)
  const formattedText = String(text)
    .replace(/https?:\/\/t\.co\/\S+/g, '') // strip t.co links
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/@(\w+)/g, '<span class="mention">@$1</span>')
    .replace(/#(\w+)/g, '<span class="hashtag">#$1</span>')
    .replace(/\n/g, '<br>');

  // Media HTML
  let mediaHTML = '';
  if (photos.length === 1) {
    mediaHTML = `<div class="media-single"><img src="${photos[0]}" /></div>`;
  } else if (photos.length >= 2) {
    const imgs = photos.map(p => `<img src="${p}" />`).join('');
    mediaHTML = `<div class="media-grid media-grid-${Math.min(photos.length, 4)}">${imgs}</div>`;
  }

  const statsHTML = !SHOW_STATS ? '' : `
    <div class="stats">
      <div class="stat">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M1.751 10c0-4.42 3.584-8 8.005-8h4.366c4.49 0 7.501 3.58 7.501 8 0 4.31-3.011 7.9-7.501 8h-4.01c-.28 0-.556.012-.83.024-.188.01-.376.02-.565.02-.12 0-.24-.003-.36-.01a8.02 8.02 0 01-.65-.063 7.936 7.936 0 01-1.285-.327A7.97 7.97 0 011.75 10z"/></svg>
        <span class="stat-count">${fmt(replies)}</span>
      </div>
      <div class="stat">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M4.5 3.88l4.432 4.14-1.364 1.46L5.5 7.55V16c0 1.1.896 2 2 2H13v2H7.5c-2.21 0-4-1.79-4-4V7.55L1.432 9.48.068 8.02 4.5 3.88zM19.5 20.12l-4.432-4.14 1.364-1.46 2.068 1.93V8c0-1.1-.896-2-2-2H11V4h5.5c2.21 0 4 1.79 4 4v8.45l2.068-1.93 1.364 1.46-4.432 4.14z"/></svg>
        <span class="stat-count">${fmt(retweets)}</span>
      </div>
      <div class="stat">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>
        <span class="stat-count">${fmt(likes)}</span>
      </div>
      ${views !== null ? `
      <div class="stat">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5c-1.73-4.39-6-7.5-11-7.5zM12 17c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5zm0-8c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3z"/></svg>
        <span class="stat-count">${fmt(views)}</span>
      </div>` : ''}
    </div>
  `;

  return `<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    background: #fff;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    width: 600px;
  }

  /* This creates the big white padding like TwitterShots */
  .frame {
    width: 600px;
    padding: 24px;
    background: #fff;
  }

  .card {
    background: #fff;
    border: 1px solid #eff3f4;
    border-radius: 16px;
    overflow: hidden;
  }

  .card-inner {
    padding: 20px 20px 16px 20px;
  }

  .header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 14px;
  }

  .avatar {
    width: 48px; height: 48px;
    border-radius: 50%;
    object-fit: cover;
    flex-shrink: 0;
    background: #e7e7e7;
  }

  .user-info { flex: 1; min-width: 0; }

  .name-row {
    display: flex;
    align-items: center;
    gap: 6px;
    font-weight: 700;
    font-size: 15px;
    color: #0f1419;
    line-height: 1.3;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .verified-badge { display: inline-flex; align-items: center; flex-shrink: 0; }
  .verified-badge svg { width: 18px; height: 18px; }

  .screen-name {
    font-size: 14px;
    color: #536471;
    line-height: 1.3;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .x-logo {
    margin-left: auto;
    flex-shrink: 0;
    opacity: 0.75;
  }
  .x-logo svg { width: 22px; height: 22px; }

  .tweet-text {
    font-size: 17px;
    line-height: 1.6;
    color: #0f1419;
    margin-bottom: 14px;
    word-break: break-word;
  }
  .mention, .hashtag { color: #1d9bf0; }

  /* Media */
  .media-single img {
    width: 100%;
    border-radius: 14px;
    max-height: 340px;
    object-fit: cover;
    display: block;
    margin-bottom: 14px;
  }

  .media-grid {
    display: grid;
    gap: 3px;
    border-radius: 14px;
    overflow: hidden;
    margin-bottom: 14px;
  }
  .media-grid-2 { grid-template-columns: 1fr 1fr; }
  .media-grid-3 { grid-template-columns: 1fr 1fr; }
  .media-grid-3 img:first-child { grid-row: span 2; }
  .media-grid-4 { grid-template-columns: 1fr 1fr; }

  .media-grid img {
    width: 100%;
    height: 180px;
    object-fit: cover;
    display: block;
  }

  .tweet-date {
    font-size: 14px;
    color: #536471;
    margin-bottom: 14px;
    padding-bottom: 14px;
    border-bottom: 1px solid #eff3f4;
  }

  .stats {
    display: flex;
    gap: 20px;
    font-size: 13.5px;
    color: #536471;
  }
  .stat {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .stat svg { width: 16px; height: 16px; opacity: 0.7; }
  .stat-count { font-weight: 600; color: #0f1419; }
</style>
</head>
<body>
  <div class="frame">
    <div class="card">
      <div class="card-inner">
        <div class="header">
          ${avatar ? `<img class="avatar" src="${avatar}" onerror="this.style.background='#e7e7e7';this.src=''" />` : '<div class="avatar"></div>'}
          <div class="user-info">
            <div class="name-row">
              <span>${escapeHtml(name)}</span>
              ${verified ? `<span class="verified-badge">
                <svg viewBox="0 0 24 24" fill="#1d9bf0"><path d="M22.25 12c0-1.43-.88-2.67-2.19-3.34.46-1.39.2-2.9-.81-3.91s-2.52-1.27-3.91-.81c-.66-1.31-1.91-2.19-3.34-2.19s-2.67.88-3.33 2.19c-1.4-.46-2.91-.2-3.92.81s-1.26 2.52-.8 3.91C3.13 9.33 2.25 10.57 2.25 12s.88 2.67 2.19 3.33c-.46 1.4-.2 2.91.81 3.92s2.52 1.26 3.91.8c.67 1.31 1.91 2.19 3.34 2.19s2.67-.88 3.33-2.19c1.4.46 2.91.2 3.92-.81s1.26-2.52.8-3.91C21.37 14.67 22.25 13.43 22.25 12zM10.5 17.19l-4.25-4.25 1.41-1.41 2.84 2.83 5.59-5.59 1.41 1.42-7 7z"/></svg>
              </span>` : ''}
            </div>
            <div class="screen-name">@${escapeHtml(screen_name)}</div>
          </div>
          <div class="x-logo">
            <svg viewBox="0 0 24 24" fill="#000"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-4.714-6.231-5.401 6.231H2.744l7.737-8.835L1.254 2.25H8.08l4.254 5.622 5.91-5.622zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>
          </div>
        </div>

        <div class="tweet-text">${formattedText}</div>

        ${mediaHTML}

        <div class="tweet-date">${escapeHtml(fmtDate(created_at))}</div>

        ${statsHTML}
      </div>
    </div>
  </div>
</body>
</html>`;
}

// simple escape for name/screen_name/date (formattedText already sanitized-ish)
function escapeHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// ─── Main ─────────────────────────────────────────────────────
(async () => {
  let browser;
  try {
    browser = await chromium.launch({
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
    });

    const page = await browser.newPage({
      viewport: { width: 600, height: 800 },
      deviceScaleFactor: 2,
    });

    const html = buildHTML(tweet);
    await page.setContent(html, { waitUntil: 'domcontentloaded', timeout: 20000 });

    // Wait for images (avatar + photos)
    await page.waitForFunction(() => {
      const imgs = Array.from(document.querySelectorAll('img'));
      return imgs.every(img => img.complete);
    }, { timeout: 12000 }).catch(() => {});

    // Screenshot the padded frame (this creates the white margin like your sample)
    const frame = await page.$('.frame');
    if (frame) {
      await frame.screenshot({ path: outputPath, type: 'png' });
    } else {
      await page.screenshot({ path: outputPath, type: 'png', fullPage: true });
    }

    console.log(`OK: ${outputPath}`);
    process.exit(0);
  } catch (err) {
    console.error(`FAIL: ${err.message}`);
    process.exit(1);
  } finally {
    if (browser) await browser.close();
  }
})();