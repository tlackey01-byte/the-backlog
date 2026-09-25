// IGDB (igdb.com, run by Twitch) -- the game catalog behind the Add Game search (?src=igdb).
// It replaces HowLongToBeat there: HLTB's owner, Ziff Davis, forbids scraping and commercial
// use, while IGDB is free even for commercial use (a free partnership that asks you to credit
// IGDB), holds ~321K games and asks you to store what you fetch. Its own `search` ranks badly,
// though -- Stray, Portal and Until Dawn miss its top five -- so searchGames() runs it
// alongside exact-title, title-contains and alternative-title lookups and ranks the merged
// set itself. Measured 2026-09-25: right game first for 96% of 186 library titles (IGDB's
// search alone: 75%), 20 of 20 short titles (40%), 15 of 15 casually typed ones (47%).
//
// Secrets (wrangler secret put): IGDB_CLIENT_ID, IGDB_CLIENT_SECRET -- a Twitch developer app.
// IGDB allows 4 requests a second per app, shared by every user; a search costs 3.

const IGDB = 'https://api.igdb.com/v4/';
const IMG = 'https://images.igdb.com/igdb/image/upload/';
const STEAM_SOURCE = 1;   // external_game_sources: Steam

// Main game, standalone expansion, remake, remaster, expanded game, port: what a person means
// by "a game". DLC, expansions, bundles, mods, episodes, seasons, packs and updates rank last.
const REAL_TYPES = [0, 4, 8, 9, 10, 11];
const RANK_FIELDS = ['id', 'name', 'first_release_date', 'game_type', 'version_parent', 'total_rating_count', 'cover.image_id'];

// Twitch app token (valid ~60 days), kept for the isolate's lifetime. Each new isolate mints
// its own -- fine for one user; a public version should keep it in KV so thousands of
// isolates don't each ask Twitch for one. Requests that arrive while a token is being fetched
// wait for that one: otherwise a burst of searches on a fresh isolate each mint their own.
let appToken = null, minting = null;
function token(env, renew) {
  if (appToken && !renew && Date.now() < appToken.expires) return Promise.resolve(appToken.value);
  if (!minting) minting = mintToken(env).finally(() => { minting = null; });
  return minting;
}
async function mintToken(env) {
  if (!env.IGDB_CLIENT_ID || !env.IGDB_CLIENT_SECRET) throw new Error('IGDB secrets not set');
  const res = await fetch('https://id.twitch.tv/oauth2/token?' + new URLSearchParams({
    client_id: env.IGDB_CLIENT_ID, client_secret: env.IGDB_CLIENT_SECRET, grant_type: 'client_credentials' }), { method: 'POST' });
  if (!res.ok) throw new Error('twitch token ' + res.status);
  const d = await res.json();
  appToken = { value: d.access_token, expires: Date.now() + (d.expires_in - 3600) * 1000 };
  return appToken.value;
}

// One APICalypse query. A 401 means the token lapsed (renew it, once); a 429 means the shared
// 4-a-second budget is spent (wait a beat, once).
async function igdb(env, endpoint, body, retried) {
  const res = await fetch(IGDB + endpoint, { method: 'POST', body, headers: {
    'Client-ID': env.IGDB_CLIENT_ID, 'Authorization': 'Bearer ' + await token(env, retried === 401), 'Accept': 'application/json' } });
  if ((res.status === 401 || res.status === 429) && !retried) {
    if (res.status === 429) await new Promise(r => setTimeout(r, 350));
    return igdb(env, endpoint, body, res.status);
  }
  if (!res.ok) throw new Error('igdb ' + endpoint + ' ' + res.status);
  return res.json();
}

// Name keys: lower case, accents and ™®© gone, apostrophes dropped ("baldurs gate" finds
// Baldur's Gate), other punctuation to spaces, Roman numerals to digits ("final fantasy 7"
// finds Final Fantasy VII). Like index.js's fold(), which splits "Baldur's" in two instead.
const ROMAN = { ii: '2', iii: '3', iv: '4', v: '5', vi: '6', vii: '7', viii: '8', ix: '9',
  xi: '11', xii: '12', xiii: '13', xiv: '14', xv: '15', xvi: '16' };
const fold = s => String(s || '').toLowerCase().replace(/[™®©]/g, '').normalize('NFKD')
  .replace(/[\u0300-\u036f]/g, '').replace(/['\u2019]/g, '').replace(/&/g, ' and ')
  .replace(/[^a-z0-9]+/g, ' ').trim().split(' ').map(w => ROMAN[w] || w).join(' ');
const yearOf = c => (c.first_release_date ? new Date(c.first_release_date * 1000).getUTCFullYear() : null);
// Seconds -> hours, rounded to the nearest half hour like the rest of the dataset. IGDB's
// averages aren't trimmed for outliers -- Baldur's Gate 3's 100% figure is 5,649 hours -- so
// anything past 1,000 hours is treated as junk rather than shown.
const hrs = s => (s > 0 && s <= 1000 * 3600 ? Math.round(s / 1800) / 2 : null);

// Up to ten games for a typed query, best first, in the shape the Add Game box shows: name,
// year, developer, platforms, a thumbnail, and hours when IGDB has any (it has them for about
// 9,400 games, so usually not).
export async function searchGames(query, env) {
  // Quotes and backslashes would end the APICalypse string they're dropped into.
  const cleaned = String(query || '').replace(/[™®©"\\]/g, ' ').replace(/\s+/g, ' ').trim();
  // A trailing year says which release ("doom 1993"): the title is searched without it, and
  // the whole string is tried as a title too, for names that end in one ("Battlefield 1942").
  const m = /^(.*\S)\s+\(?((?:19|20)\d\d)\)?$/.exec(cleaned);
  const title = m ? m[1] : cleaned, year = m ? Number(m[2]) : null;
  const ft = fold(title);
  if (cleaned.length < 2 || !ft) return [];

  const lookups = [
    `query games "exact" { fields ${RANK_FIELDS}; where name ~ "${title}"; limit 20; };`,
    // Titles containing the text, most-rated first: how "breath of the wild" finds Zelda.
    `query games "contains" { fields ${RANK_FIELDS}; where name ~ *"${title}"* & game_type = (${REAL_TYPES}) & version_parent = null; sort total_rating_count desc; limit 20; };`,
    // Regional and former titles: "Dragon Quest III" is filed as Dragon Warrior III.
    `query alternative_names "alt" { fields ${RANK_FIELDS.map(f => 'game.' + f)}; where name ~ "${title}"; limit 20; };`
  ];
  if (m) lookups.push(`query games "full" { fields ${RANK_FIELDS}; where name ~ "${cleaned}"; limit 10; };`);
  // IGDB answers a multiquery holding a search with an empty list, so the search is its own
  // request, sent alongside the rest.
  const [searched, multi] = await Promise.all([
    igdb(env, 'games', `search "${title}"; fields ${RANK_FIELDS}; limit 20;`),
    igdb(env, 'multiquery', lookups.join('\n'))
  ]);
  const got = Object.fromEntries(multi.map(r => [r.name, r.result || []]));

  const cands = new Map(), pos = new Map(), alt = new Set(), full = new Set();
  searched.forEach((c, i) => { cands.set(c.id, c); pos.set(c.id, i); });
  for (const c of [...(got.exact || []), ...(got.contains || []), ...(got.full || [])]) if (!cands.has(c.id)) cands.set(c.id, c);
  for (const c of got.full || []) full.add(c.id);
  for (const a of got.alt || []) {
    if (!a.game || !a.game.id) continue;
    if (!cands.has(a.game.id)) cands.set(a.game.id, a.game);
    alt.add(a.game.id);
  }

  const words = ft.split(' ');
  const score = c => {
    const fn = fold(c.name), have = fn.split(' ');
    let s = full.has(c.id) ? 110 : fn === ft ? 100 : alt.has(c.id) ? 80
      : words.every(w => have.includes(w)) ? 40 : fn.startsWith(ft) ? 20 : 0;
    s += REAL_TYPES.includes(c.game_type) ? 25 : -40;
    if (c.version_parent) s -= 30;                               // "... Collector's Edition"
    s += 12 * Math.log10(1 + (c.total_rating_count || 0));       // popularity: 10 ratings +12, 1,000 +36
    const y = yearOf(c);
    if (year && y) s += y === year ? 30 : Math.abs(y - year) === 1 ? 15 : 0;
    // IGDB's own order only breaks ties: it's what misranks exact titles in the first place.
    if (pos.has(c.id)) s += Math.max(0, 10 - pos.get(c.id));
    return s;
  };
  const top = [...cands.values()].map(c => [score(c), c]).sort((a, b) => b[0] - a[0]).slice(0, 10).map(x => x[1]);
  if (!top.length) return [];

  // Developer, platforms and hours for just the ten shown -- one more request, not twenty.
  const info = {}, ttb = {};
  try {
    const ids = top.map(c => c.id).join(',');
    const extra = await igdb(env, 'multiquery', [
      `query games "info" { fields platforms.abbreviation,involved_companies.developer,involved_companies.company.name; where id = (${ids}); limit 10; };`,
      `query game_time_to_beats "ttb" { fields game_id,hastily,normally,completely,count; where game_id = (${ids}); limit 10; };`
    ].join('\n'));
    for (const r of extra) {
      for (const x of r.result || []) {
        if (r.name === 'info') info[x.id] = x; else ttb[x.game_id] = x;
      }
    }
  } catch (e) {
    // Names, years and covers are enough to pick from; the preview fills in the rest.
  }

  return top.map(c => {
    const i = info[c.id] || {}, t = ttb[c.id] || {};
    const dev = (i.involved_companies || []).find(x => x.developer && x.company);
    return {
      igdbId: c.id, name: c.name, year: yearOf(c), developer: dev ? dev.company.name : null,
      // IGDB's three averages are HLTB's three columns: to the credits, with some extras, 100%.
      main: hrs(t.hastily), extra: hrs(t.normally), completionist: hrs(t.completely), ttbCount: t.count || 0,
      platforms: (i.platforms || []).map(p => p.abbreviation).filter(Boolean).join(', '),
      thumb: c.cover && c.cover.image_id ? IMG + 't_cover_small_2x/' + c.cover.image_id + '.jpg' : null
    };
  });
}

// IGDB genre and theme names -> tags already used in master_games_final.json. Only for games
// with no Steam page: Steam's genres stay the dataset's primary vocabulary, as with HLTB's.
// Unmapped ones (Fantasy, Science fiction, Pinball, MOBA...) are dropped.
const GENRE_MAP = {
  'Point-and-click': 'Point-and-Click', 'Fighting': 'Fighting', 'Shooter': 'Shooter', 'Music': 'Rhythm',
  'Platform': 'Platformer', 'Puzzle': 'Puzzle', 'Racing': 'Racing', 'Real Time Strategy (RTS)': 'RTS',
  'Role-playing (RPG)': 'RPG', 'Simulator': 'Simulation', 'Sport': 'Sports', 'Strategy': 'Strategy',
  'Turn-based strategy (TBS)': 'Turn-Based Strategy', 'Tactical': 'Tactical', "Hack and slash/Beat 'em up": 'Hack and Slash',
  'Adventure': 'Adventure', 'Indie': 'Indie', 'Arcade': 'Arcade', 'Visual Novel': 'Visual Novel', 'Card & Board Game': 'Card Game',
  'Action': 'Action', 'Horror': 'Horror', 'Survival': 'Survival', 'Stealth': 'Stealth', 'Open world': 'Open World',
  'Party': 'Party', 'Sandbox': 'Sandbox', 'Mystery': 'Mystery', 'Comedy': 'Comedy'
};

// For /details: the game's Steam app, which HLTB's page used to supply (genres and art then
// come from that exact app), plus IGDB's own genres for games with no Steam page.
export async function gameInfo(id, env) {
  const [g] = await igdb(env, 'games', `fields genres.name,themes.name,external_games.uid,external_games.external_game_source; where id = ${Number(id)};`);
  if (!g) return null;
  const steam = (g.external_games || []).find(e => e.external_game_source === STEAM_SOURCE && /^\d+$/.test(e.uid || ''));
  const genres = [...new Set([...(g.genres || []), ...(g.themes || [])].map(x => GENRE_MAP[x.name]).filter(Boolean))];
  return { steamAppId: steam ? steam.uid : null, genres };
}
