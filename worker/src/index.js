// Backlog proxy -- lets the games page search HowLongToBeat and pull SteamGridDB cover art.
// Neither can be called from the browser directly: HLTB sends no CORS headers and binds its
// search token to the caller's IP + User-Agent, and SteamGridDB has no CORS and needs a
// secret API key. Every request must carry the site owner's Firebase ID token.
//
// Secrets (wrangler secret put): SGDB_KEY, ALLOWED_UID. Var (wrangler.toml): FIREBASE_PROJECT_ID.

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36';
const ALLOWED_ORIGINS = ['https://tlackey01-byte.github.io', 'http://localhost:8765'];
const HLTB = 'https://howlongtobeat.com';
const SGDB = 'https://www.steamgriddb.com/api/v2';

export default {
  async fetch(request, env) {
    const origin = request.headers.get('Origin') || '';
    const cors = ALLOWED_ORIGINS.includes(origin)
      ? { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Headers': 'Authorization', 'Vary': 'Origin' }
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
      if (url.pathname === '/search') return json(await search(url.searchParams.get('q') || '', env), 200, cors);
      if (url.pathname === '/details') return json(await details(url.searchParams, env), 200, cors);
      if (url.pathname === '/cover') return await cover(url.searchParams.get('url') || '', cors);
      return json({ error: 'not found' }, 404, cors);
    } catch (e) {
      return json({ error: String(e && e.message || e) }, 502, cors);
    }
  }
};

// Serves docs/games/covers/<name> and covers/hero/<name> out of the R2 bucket. Keeping the
// ~120MB of images here instead of in the repo is the whole point: git would hold every
// version of every cover forever, while R2 just holds the current one.
async function coverFile(key, env) {
  // Only content-hashed names, so this can never be pointed at anything else in the bucket.
  if (!/^(hero\/)?[0-9a-f]{16}\.(webp|jpg|png)$/.test(key)) return new Response('bad name', { status: 400 });
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
  body[auth.hpKey] = auth.hpVal;
  const res = await fetch(HLTB + '/api/search/site', {
    method: 'POST', body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json', 'User-Agent': UA, 'Referer': HLTB + '/', 'Origin': HLTB,
      'x-auth-token': auth.token, 'x-hp-key': auth.hpKey, 'x-hp-val': auth.hpVal }
  });
  if (res.status === 403 && !retried) { hltbAuth = null; return hltbSearch(q, true); }
  if (!res.ok) throw new Error('hltb search ' + res.status);
  return (await res.json()).data || [];
}
// Seconds -> hours, rounded to the nearest half hour like the rest of the dataset.
const hrs = s => (s > 0 ? Math.round(s / 1800) / 2 : null);

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
const first600x900 = grids => ((grids || []).find(g => g.width === 600 && g.height === 900) || {}).url || null;

async function details(params, env) {
  const hltbId = params.get('hltbId');
  const name = params.get('name') || '';
  const year = parseInt(params.get('year'), 10) || null;
  let sgdbId = params.get('sgdbId');

  const page = hltbId ? await hltbGamePage(hltbId) : null;
  const steamAppId = page && page.profile_steam ? page.profile_steam : null;

  let genres = steamAppId ? await steamGenres(steamAppId) : [];
  if (!genres.length && page && page.profile_genre) {
    genres = [...new Set(page.profile_genre.split(',').map(s => HLTB_GENRE_MAP[s.trim()]).filter(Boolean))];
  }

  // Cover: precise Steam lookup first, then SGDB name search picking the exact-name
  // candidate whose release year is closest to the edition being added.
  let coverUrl = steamAppId ? first600x900(await sgdb('/grids/steam/' + steamAppId + '?dimensions=600x900', env)) : null;
  if (!coverUrl && !sgdbId && name) {
    const norm = s => s.toLowerCase().replace(/[™®©]/g, '').replace(/[^a-z0-9]+/g, ' ').trim();
    const cands = (await sgdb('/search/autocomplete/' + encodeURIComponent(name), env) || []).filter(c => norm(c.name) === norm(name));
    const yr = c => (c.release_date ? new Date(c.release_date * 1000).getUTCFullYear() : null);
    cands.sort((a, b) => Math.abs((yr(a) || 0) - (year || 0)) - Math.abs((yr(b) || 0) - (year || 0)));
    if (cands.length) sgdbId = cands[0].id;
  }
  if (!coverUrl && sgdbId) coverUrl = first600x900(await sgdb('/grids/game/' + sgdbId + '?dimensions=600x900', env));

  return { genres, steamAppId, coverUrl };
}

// Streams a SteamGridDB CDN image back with CORS headers so the page can draw it onto a
// canvas and re-encode it. Locked to SGDB's CDN so this can't be used as an open proxy.
async function cover(target, cors) {
  let u;
  try { u = new URL(target); } catch (e) { return json({ error: 'bad url' }, 400, cors); }
  if (u.protocol !== 'https:' || !/^cdn\d*\.steamgriddb\.com$/.test(u.hostname)) return json({ error: 'host not allowed' }, 400, cors);
  const res = await fetch(u.toString(), { headers: { 'User-Agent': UA } });
  if (!res.ok) return json({ error: 'cover ' + res.status }, 502, cors);
  return new Response(res.body, { headers: Object.assign({ 'Content-Type': res.headers.get('Content-Type') || 'image/png' }, cors) });
}
