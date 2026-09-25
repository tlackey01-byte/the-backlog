// Backlog proxy -- lets the games page search HowLongToBeat and pull SteamGridDB cover art.
// Neither can be called from the browser directly: HLTB sends no CORS headers and binds its
// search token to the caller's IP + User-Agent, and SteamGridDB has no CORS and needs a
// secret API key. Every request must carry the site owner's Firebase ID token, except /img/.
// It also serves every cover image out of R2 (/img/), and stores and deletes the covers of
// games added on the site (/upload-cover, /delete-cover -- the added/ part of the bucket).
// /search?src=igdb searches the IGDB catalog instead of HLTB (igdb.js) -- the prototype for
// moving off HLTB, whose terms forbid scraping.
//
// Secrets (wrangler secret put): SGDB_KEY, ALLOWED_UID, IGDB_CLIENT_ID, IGDB_CLIENT_SECRET.
// Var (wrangler.toml): FIREBASE_PROJECT_ID.

import { searchGames, gameInfo } from './igdb.js';

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36';
const ALLOWED_ORIGINS = ['https://tlackey01-byte.github.io', 'http://localhost:8765'];
const HLTB = 'https://howlongtobeat.com';
const SGDB = 'https://www.steamgriddb.com/api/v2';
const STEAM_ASSETS = 'https://shared.cloudflare.steamstatic.com/store_item_assets/';

export default {
  async fetch(request, env) {
    const origin = request.headers.get('Origin') || '';
    // POST + Content-Type are for the page uploading a site-added game's cover images (a
    // binary body) and asking for them to be deleted (JSON); everything else is a GET.
    const cors = ALLOWED_ORIGINS.includes(origin)
      ? { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
          'Access-Control-Allow-Headers': 'Authorization, Content-Type', 'Vary': 'Origin' }
      : {};
    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: cors });

    const url = new URL(request.url);

    // Cover images out of R2, served publicly and ahead of the token check: an <img> tag
    // can't send an Authorization header. File names are content hashes of public box art,
    // so there's nothing to protect, and the bucket is only writable with account creds.
    if (url.pathname.startsWith('/img/')) return await coverFile(decodeURIComponent(url.pathname.slice(5)), env);

    try {
      await verifyFirebaseToken(request, env);
    } catch (e) {
      return json({ error: 'unauthorized' }, 401, cors);
    }

    try {
      if (url.pathname === '/search') return json(await searchRoute(url.searchParams, env), 200, cors);
      if (url.pathname === '/details') return json(await details(url.searchParams, env), 200, cors);
      if (url.pathname === '/cover') return await cover(url.searchParams.get('url') || '', cors);
      if (url.pathname === '/upload-cover' && request.method === 'POST') return await uploadCover(request, url, env, cors);
      if (url.pathname === '/delete-cover' && request.method === 'POST') return await deleteCover(request, env, cors);
      return json({ error: 'not found' }, 404, cors);
    } catch (e) {
      return json({ error: String(e && e.message || e) }, 502, cors);
    }
  }
};

// Serves docs/games/covers/<name> and covers/hero/<name> out of the R2 bucket. Keeping the
// ~120MB of images here instead of in the repo is the whole point: git would hold every
// version of every cover forever, while R2 just holds the current one. Covers the site
// uploads for games added there live under added/ (same naming), until baking moves them.
async function coverFile(key, env) {
  // Only content-hashed names, so this can never be pointed at anything else in the bucket.
  if (!/^(added\/)?(hero\/)?[0-9a-f]{16}\.(webp|jpg|png)$/.test(key)) return new Response('bad name', { status: 400 });
  if (!env.COVERS) return new Response('bucket not bound', { status: 503 });
  const obj = await env.COVERS.get(key);
  if (!obj) return new Response('not found', { status: 404 });
  return new Response(obj.body, {
    headers: {
      'Content-Type': (obj.httpMetadata && obj.httpMetadata.contentType) || 'image/webp',
      // The name is a hash of the bytes, so a cached copy can never be wrong.
      'Cache-Control': 'public, max-age=31536000, immutable',
      'Access-Control-Allow-Origin': '*',
      'ETag': obj.httpEtag
    }
  });
}

// ---- Cover images for games added on the site ----
// The page builds a game's two images itself (it already has the art on a canvas) and sends
// them here one at a time as the raw request body: POST /upload-cover?kind=wide|hero. They go
// under added/ -- the part of the bucket the site owns -- so git-side cleanup (sync_site.py)
// never mistakes them for unused just because the master file doesn't list them yet.
const MAX_UPLOAD = 1024 * 1024;  // a 600x900 hero is ~60-200KB; anything near this is wrong

async function uploadCover(request, url, env, cors) {
  if (!env.COVERS) return json({ error: 'bucket not bound' }, 503, cors);
  const kind = url.searchParams.get('kind');
  if (kind !== 'wide' && kind !== 'hero') return json({ error: 'kind must be wide or hero' }, 400, cors);
  if (Number(request.headers.get('Content-Length') || 0) > MAX_UPLOAD) return json({ error: 'too large' }, 413, cors);
  const buf = await request.arrayBuffer();
  if (!buf.byteLength || buf.byteLength > MAX_UPLOAD) return json({ error: 'empty or too large' }, 413, cors);

  // Only WebP or JPEG, by their actual signature rather than a header the caller sets.
  const b = new Uint8Array(buf);
  const ascii = (from, to) => String.fromCharCode(...b.subarray(from, to));
  let ext, type;
  if (ascii(0, 4) === 'RIFF' && ascii(8, 12) === 'WEBP') { ext = 'webp'; type = 'image/webp'; }
  else if (b[0] === 0xFF && b[1] === 0xD8 && b[2] === 0xFF) { ext = 'jpg'; type = 'image/jpeg'; }
  else return json({ error: 'not a WebP or JPEG image' }, 415, cors);

  // Named by a hash of the bytes -- the same scheme as Source/refetch_covers.py -- so the
  // caller can't choose (or overwrite) a name, and identical images share one object.
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-1', buf));
  const hash = Array.from(digest.subarray(0, 8), x => x.toString(16).padStart(2, '0')).join('');
  const key = 'added/' + (kind === 'hero' ? 'hero/' : '') + hash + '.' + ext;
  await env.COVERS.put(key, buf, { httpMetadata: { contentType: type } });
  return json({ key, url: new URL('/img/' + key, url).toString() }, 200, cors);
}

// POST /delete-cover {"keys": [...]} -- when a site-added game is deleted for good. Only
// added/ names are accepted: everything else in the bucket belongs to the master file and is
// cleaned up by sync_site.py, so a bug in the page can never delete a catalog game's cover.
// All-or-nothing: one bad key rejects the whole request rather than deleting the rest.
const ADDED_KEY = /^added\/(hero\/)?[0-9a-f]{16}\.(webp|jpg)$/;

async function deleteCover(request, env, cors) {
  if (!env.COVERS) return json({ error: 'bucket not bound' }, 503, cors);
  let keys;
  try { keys = (await request.json()).keys; } catch (e) { return json({ error: 'bad json' }, 400, cors); }
  if (!Array.isArray(keys) || !keys.length || keys.length > 10) return json({ error: 'keys must be a list of 1-10 names' }, 400, cors);
  const bad = keys.filter(k => typeof k !== 'string' || !ADDED_KEY.test(k));
  if (bad.length) return json({ error: 'only added/ cover names can be deleted', bad }, 400, cors);
  await env.COVERS.delete(keys);
  return json({ deleted: keys.length }, 200, cors);
}

function json(obj, status, headers) {
  return new Response(JSON.stringify(obj), { status, headers: Object.assign({ 'Content-Type': 'application/json' }, headers) });
}

// ---- Firebase ID token verification (RS256 JWT against Google's published JWKs) ----
let jwksCache = { keys: null, expires: 0 };
async function getJwks() {
  if (jwksCache.keys && Date.now() < jwksCache.expires) return jwksCache.keys;
  const res = await fetch('https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com');
  const maxAge = /max-age=(\d+)/.exec(res.headers.get('Cache-Control') || '');
  jwksCache = { keys: (await res.json()).keys, expires: Date.now() + (maxAge ? +maxAge[1] * 1000 : 3600000) };
  return jwksCache.keys;
}
function b64urlToBytes(s) {
  s = s.replace(/-/g, '+').replace(/_/g, '/');
  const bin = atob(s + '==='.slice((s.length + 3) % 4));
  return Uint8Array.from(bin, c => c.charCodeAt(0));
}
async function verifyFirebaseToken(request, env) {
  const m = /^Bearer (.+)$/.exec(request.headers.get('Authorization') || '');
  if (!m) throw new Error('no token');
  const [h, p, sig] = m[1].split('.');
  const header = JSON.parse(new TextDecoder().decode(b64urlToBytes(h)));
  const payload = JSON.parse(new TextDecoder().decode(b64urlToBytes(p)));
  const jwk = (await getJwks()).find(k => k.kid === header.kid);
  if (!jwk || header.alg !== 'RS256') throw new Error('bad key');
  const key = await crypto.subtle.importKey('jwk', jwk, { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['verify']);
  const ok = await crypto.subtle.verify('RSASSA-PKCS1-v1_5', key, b64urlToBytes(sig), new TextEncoder().encode(h + '.' + p));
  const now = Math.floor(Date.now() / 1000);
  if (!ok || payload.aud !== env.FIREBASE_PROJECT_ID ||
      payload.iss !== 'https://securetoken.google.com/' + env.FIREBASE_PROJECT_ID ||
      payload.exp < now || payload.sub !== env.ALLOWED_UID) throw new Error('invalid token');
}

// ---- HowLongToBeat ----
// HLTB's own site search: GET .../init hands out a short-lived token plus a honeypot
// key/value pair that must be echoed back as request headers AND as a body field. The
// token is tied to the requesting IP + UA, so it's cached per isolate and re-fetched once
// on a 403. This is undocumented -- if search starts failing, re-check how
// howlongtobeat.com's own search bundle calls /api/search.
let hltbAuth = null;
async function hltbInit() {
  const res = await fetch(HLTB + '/api/search/site/init?t=' + Date.now(), { headers: { 'User-Agent': UA, 'Referer': HLTB + '/' } });
  if (!res.ok) throw new Error('hltb init ' + res.status);
  hltbAuth = Object.assign(await res.json(), { at: Date.now() });
  return hltbAuth;
}
async function hltbSearch(q, retried) {
  const auth = hltbAuth && Date.now() - hltbAuth.at < 5 * 60000 ? hltbAuth : await hltbInit();
  const body = {
    searchType: 'games', searchTerms: q.trim().split(/\s+/), searchPage: 1, size: 10,
    searchOptions: {
      games: { userId: 0, platform: '', sortCategory: 'popular', rangeCategory: 'main', rangeTime: { min: null, max: null },
        gameplay: { perspective: '', flow: '', genre: '', difficulty: '' }, rangeYear: { min: '', max: '' }, modifier: '' },
      users: { sortCategory: 'postcount' }, lists: { sortCategory: 'follows' }, filter: '', sort: 0, randomizer: 0
    },
    useCache: true
  };
  const headers = { 'Content-Type': 'application/json', 'User-Agent': UA, 'Referer': HLTB + '/', 'Origin': HLTB,
    'x-auth-token': auth.token };
  // The honeypot pair comes and goes (on 2026-09-23 init started sending only a token), so
  // echo it back only when it's there -- a Headers value of undefined goes out as "undefined".
  if (auth.hpKey) {
    body[auth.hpKey] = auth.hpVal;
    headers['x-hp-key'] = auth.hpKey;
    headers['x-hp-val'] = auth.hpVal || '';
  }
  const res = await fetch(HLTB + '/api/search/site', { method: 'POST', body: JSON.stringify(body), headers });
  if (res.status === 403 && !retried) { hltbAuth = null; return hltbSearch(q, true); }
  if (!res.ok) throw new Error('hltb search ' + res.status);
  return (await res.json()).data || [];
}
// Seconds -> hours, rounded to the nearest half hour like the rest of the dataset.
const hrs = s => (s > 0 ? Math.round(s / 1800) / 2 : null);

// ?src=igdb searches IGDB (igdb.js). Pages built before that don't send it and keep getting
// HLTB, so this Worker can be deployed while the live site still runs the old page. If IGDB
// fails (secrets not set yet, an outage), HLTB answers instead, so Add Game keeps working.
async function searchRoute(params, env) {
  const q = params.get('q') || '';
  if (params.get('src') !== 'igdb') return search(q, env);
  try {
    return { source: 'igdb', results: await searchGames(q, env) };
  } catch (e) {
    return Object.assign(await search(q, env), { igdbError: String(e.message || e) });
  }
}

async function search(q, env) {
  if (q.trim().length < 2) return { source: 'hltb', results: [] };
  try {
    const data = await hltbSearch(q);
    // Search results don't carry the developer; each game's own page does (~33KB, parsed
    // in well under 1ms), so fetch them in parallel. A failed page just means no developer.
    const pages = await Promise.all(data.map(g => hltbGamePage(g.game_id).catch(() => null)));
    return {
      source: 'hltb',
      results: data.map((g, i) => ({
        hltbId: g.game_id, name: g.game_name, year: g.release_world || null,
        developer: (pages[i] && pages[i].profile_dev) || null,
        main: hrs(g.comp_main), extra: hrs(g.comp_plus), completionist: hrs(g.comp_100),
        platforms: g.profile_platform || '',
        thumb: g.game_image ? HLTB + '/games/' + encodeURIComponent(g.game_image) + '?width=100' : null
      }))
    };
  } catch (e) {
    // HLTB broke (it changes without notice) -- fall back to SteamGridDB names so adding a
    // game still works; the page then asks for hours by hand.
    const data = await sgdb('/search/autocomplete/' + encodeURIComponent(q), env);
    return {
      source: 'sgdb', error: String(e.message || e),
      results: (data || []).slice(0, 10).map(g => ({
        sgdbId: g.id, name: g.name, year: g.release_date ? new Date(g.release_date * 1000).getUTCFullYear() : null, developer: null,
        main: null, extra: null, completionist: null, platforms: '', thumb: null
      }))
    };
  }
}

// ---- Details: genres + cover ----
// HLTB genre names -> tags already used in master_games_final.json. Only used when the game
// has no Steam page (Steam's genres are the dataset's primary vocabulary, same as the skill).
// HLTB mixes perspective/flow terms (Third-Person, Real-Time, Scrolling...) into the same
// field; those are deliberately left unmapped so they get dropped.
const HLTB_GENRE_MAP = {
  'Action': 'Action', 'Adventure': 'Adventure', 'Role-Playing': 'RPG', 'Platform': 'Platformer',
  'Puzzle': 'Puzzle', 'Simulation': 'Simulation', 'Strategy': 'Strategy', 'Racing/Driving': 'Racing',
  'Sports': 'Sports', 'Fighting': 'Fighting', 'Shooter': 'Shooter', 'First-person shooter': 'FPS',
  'Survival': 'Survival', 'Horror': 'Horror', 'Stealth': 'Stealth', 'Visual Novel': 'Visual Novel',
  'Point-and-Click': 'Point-and-Click', 'Open World': 'Open World', 'Roguelike': 'Roguelike',
  'Metroidvania': 'Metroidvania', 'Hack and Slash': 'Hack and Slash', 'Beat em Up': "Beat 'em up",
  "Beat 'em Up": "Beat 'em up", 'Rhythm/Music': 'Rhythm', 'Music/Rhythm': 'Rhythm', 'Party': 'Party',
  'Arcade': 'Arcade', 'Tactical': 'Tactical', 'Shoot em Up': "Shoot 'em up", "Shoot 'em Up": "Shoot 'em up"
};

async function hltbGamePage(id) {
  const res = await fetch(HLTB + '/game/' + encodeURIComponent(id), { headers: { 'User-Agent': UA } });
  if (!res.ok) return null;
  const m = /<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/.exec(await res.text());
  try { return JSON.parse(m[1]).props.pageProps.game.data.game[0]; } catch (e) { return null; }
}

async function steamGenres(appid) {
  const res = await fetch('https://store.steampowered.com/api/appdetails?appids=' + appid + '&filters=genres&cc=us&l=english', { headers: { 'User-Agent': UA } });
  const d = (await res.json())[appid];
  return d && d.success && d.data && d.data.genres ? d.data.genres.map(g => g.description) : [];
}

async function sgdb(path, env) {
  const res = await fetch(SGDB + path, { headers: { 'Authorization': 'Bearer ' + env.SGDB_KEY, 'User-Agent': UA } });
  if (!res.ok) return null;
  const d = await res.json();
  return d.success ? d.data : null;
}
// Portrait sizes, best first. Older and console-only games often have only the 660x930 or
// 342x482 ones (about 0.71); the page centre-crops those to 2:3.
const PORTRAIT_DIMS = [[600, 900], [660, 930], [342, 482]];
const bestPortrait = grids => {
  for (const [w, h] of PORTRAIT_DIMS) {
    const g = (grids || []).find(x => x.width === w && x.height === h);
    if (g) return g.url;
  }
  return null;
};
// Wide list art: the biggest landscape capsule, 920x430 when there is one, else 460x215 --
// the same pick as Source/refetch_covers.py's widest().
const bestWide = grids => {
  const wides = (grids || []).filter(g => (g.width === 920 && g.height === 430) || (g.width === 460 && g.height === 215));
  wides.sort((a, b) => b.width - a.width);
  return wides.length ? wides[0].url : null;
};

// Steam's own store header (460x215) for an app. Asked for by name from the store API rather
// than guessing /apps/<id>/header.jpg: apps from 2025 on keep each asset under a hashed
// folder, and the guessed path 404s (or serves a stale image) for them.
async function steamHeader(appid) {
  const q = { ids: [{ appid: Number(appid) }], context: { language: 'english', country_code: 'US' },
    data_request: { include_assets: true } };
  const res = await fetch('https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?input_json=' +
    encodeURIComponent(JSON.stringify(q)), { headers: { 'User-Agent': UA } });
  if (!res.ok) return null;
  const items = ((await res.json()).response || {}).store_items || [];
  const a = (items[0] && items[0].assets) || {};
  return a.asset_url_format && a.header ? STEAM_ASSETS + a.asset_url_format.replace('${FILENAME}', a.header) : null;
}

// Name keys for matching SteamGridDB entries, looser at each step -- the same ladder as
// Source/find_cover_candidates.py. The old exact-only match missed "Alan Wake II" (filed as
// "Alan Wake 2"), "Ragnarok" ("Ragnarök"), and "Dishonored Definitive Edition" ("Dishonored").
const ROMAN = { ii: '2', iii: '3', iv: '4', v: '5', vi: '6', vii: '7', viii: '8', ix: '9',
  xi: '11', xii: '12', xiii: '13', xiv: '14', xv: '15', xvi: '16' };
const fold = s => String(s || '').toLowerCase().replace(/[™®©]/g, '')
  .normalize('NFKD').replace(/[̀-ͯ]/g, '').replace(/&/g, ' and ')
  .replace(/[^a-z0-9]+/g, ' ').trim().split(' ').map(w => ROMAN[w] || w).join(' ');
const EDITION_TAIL = /\s+(?:the\s+)?(?:game of the year|goty|definitive|complete|enhanced|ultimate|deluxe|special|anniversary|gold|platinum|collectors|collector s|directors cut|director s cut|final cut|remastered|remaster|hd|4k|redux|edition|version|collection|complete adventure|pack)$/;
const core = s => {
  let k = fold(String(s || '').replace(/\([^)]*\)/g, ' ')), prev;
  do { prev = k; k = k.replace(EDITION_TAIL, ''); } while (k !== prev);
  return k.replace(/^the\s+/, '');
};
const yearOf = c => (c.release_date ? new Date(c.release_date * 1000).getUTCFullYear() : null);

async function details(params, env) {
  const hltbId = params.get('hltbId');
  const name = params.get('name') || '';
  const year = parseInt(params.get('year'), 10) || null;
  let sgdbId = params.get('sgdbId');

  // Which Steam app the game is: IGDB says so directly for a game from its search; for one
  // from HLTB's, the game's HLTB page does.
  const info = params.get('igdbId') ? await gameInfo(params.get('igdbId'), env).catch(() => null) : null;
  const page = hltbId ? await hltbGamePage(hltbId) : null;
  const steamAppId = (info && info.steamAppId) || (page && page.profile_steam ? page.profile_steam : null);

  let genres = steamAppId ? await steamGenres(steamAppId) : [];
  if (!genres.length && info) genres = info.genres;
  if (!genres.length && page && page.profile_genre) {
    genres = [...new Set(page.profile_genre.split(',').map(s => HLTB_GENRE_MAP[s.trim()]).filter(Boolean))];
  }

  // Cover: the SteamGridDB entry for this exact Steam app when IGDB or HLTB links one --
  // that's the right release by construction. Its id comes back too, so baking the game later
  // can fetch its wide capsule art for the list.
  let coverUrl = null;
  if (steamAppId) {
    const sg = await sgdb('/games/steam/' + steamAppId, env).catch(() => null);
    if (sg && sg.id) sgdbId = sg.id;
    coverUrl = bestPortrait(await sgdb('/grids/steam/' + steamAppId + '?types=static&nsfw=false&humor=false', env));
  }
  // Otherwise a name search: exact name first, then accents/numerals evened out, then with
  // edition suffixes dropped; within the best tier, the release year closest to the one
  // being added (so picking the 1993 Doom doesn't get the 2016 one's art).
  if (!coverUrl && !sgdbId && name) {
    const cands = await sgdb('/search/autocomplete/' + encodeURIComponent(name), env) || [];
    const tiers = [c => fold(c.name) === fold(name), c => fold(c.name).replace(/ /g, '') === fold(name).replace(/ /g, ''),
      c => core(c.name) === core(name)];
    for (const inTier of tiers) {
      const hits = cands.filter(inTier);
      if (!hits.length) continue;
      hits.sort((a, b) => Math.abs((yearOf(a) || 0) - (year || 0)) - Math.abs((yearOf(b) || 0) - (year || 0)));
      sgdbId = hits[0].id;
      break;
    }
  }
  if (!coverUrl && sgdbId) coverUrl = bestPortrait(await sgdb('/grids/game/' + sgdbId + '?types=static&nsfw=false&humor=false', env));

  // Wide art for the list, from the same SteamGridDB entry (so the same release), else Steam's
  // own header for the linked app. The page crops it to 460x215; with neither, it builds the
  // same blurred-portrait fallback Source/refetch_covers.py does.
  let wideUrl = null;
  if (sgdbId) wideUrl = bestWide(await sgdb('/grids/game/' + sgdbId + '?dimensions=460x215,920x430&types=static&nsfw=false&humor=false', env));
  if (!wideUrl && steamAppId) wideUrl = await steamHeader(steamAppId).catch(() => null);

  return { genres, steamAppId, coverUrl, wideUrl, sgdbId: sgdbId ? Number(sgdbId) : null };
}

// Streams a SteamGridDB or Steam CDN image back with CORS headers so the page can draw it onto
// a canvas and re-encode it. Locked to those CDNs so this can't be used as an open proxy.
async function cover(target, cors) {
  let u;
  try { u = new URL(target); } catch (e) { return json({ error: 'bad url' }, 400, cors); }
  if (u.protocol !== 'https:' || !/^(cdn\d*\.steamgriddb\.com|[a-z0-9-]+(\.[a-z0-9-]+)*\.steamstatic\.com)$/.test(u.hostname)) {
    return json({ error: 'host not allowed' }, 400, cors);
  }
  const res = await fetch(u.toString(), { headers: { 'User-Agent': UA } });
  if (!res.ok) return json({ error: 'cover ' + res.status }, 502, cors);
  return new Response(res.body, { headers: Object.assign({ 'Content-Type': res.headers.get('Content-Type') || 'image/png' }, cors) });
}
