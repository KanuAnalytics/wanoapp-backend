// Wano admin page. Everything is put on the page as text (no HTML strings),
// and the login lives in sessionStorage, so it ends when the tab closes.
"use strict";

const API = "/api/v1";
const KEY = "wano_admin_token";
let token = null;
try { token = sessionStorage.getItem(KEY); } catch (e) { token = null; }

// ------------------------------------------------------------ helpers

function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "style") n.setAttribute("style", v);
    else n.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of kids.flat()) {
    if (c === null || c === undefined || c === false) continue;
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}
const $ = (id) => document.getElementById(id);
const money = (v) => "$" + Number(v || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const num = (v) => Number(v || 0).toLocaleString();
function when(v) {
  if (!v) return "—";
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(v) ? v : v + "Z");
  return isNaN(d) ? String(v) : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
function safeUrl(u) {
  try { const x = new URL(u); return x.protocol === "https:" ? x.href : null; } catch (e) { return null; }
}

function tvWaitingForPremiere(t) {
  return t.status === "coming_soon" && t.premiere_at && Date.parse(t.premiere_at) > Date.now() && t.problems?.length === 1 && t.problems[0] === "No released episode is ready yet";
}

let toastTimer = null;
function toast(msg, err) {
  const t = $("toast");
  t.textContent = msg;
  t.className = err ? "err" : "";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), err ? 6000 : 3000);
}

function confirmBox(title, text, yesLabel, danger) {
  return new Promise((resolve) => {
    $("modal-title").textContent = title;
    $("modal-text").textContent = text;
    const yes = $("modal-yes"), no = $("modal-no");
    yes.textContent = yesLabel || "Confirm";
    yes.className = danger ? "btn danger" : "btn";
    $("modal").classList.remove("hidden");
    const done = (v) => { $("modal").classList.add("hidden"); yes.onclick = no.onclick = null; resolve(v); };
    yes.onclick = () => done(true);
    no.onclick = () => done(false);
  });
}

class ApiError extends Error { constructor(status, msg) { super(msg); this.status = status; } }

async function api(method, path, body) {
  const headers = { "Accept": "application/json" };
  if (token) headers["Authorization"] = "Bearer " + token;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let r;
  try {
    r = await fetch(API + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (e) { throw new ApiError(0, "Can't reach the server. Check your internet."); }
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (r.status === 401 && path !== "/admin/login") { logout("Your session ended. Log in again."); throw new ApiError(401, "Logged out"); }
  if (!r.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join("; ");
    throw new ApiError(r.status, msg || ("Something went wrong (" + r.status + ")"));
  }
  return data;
}

async function act(btn, fn, okMsg) {
  if (btn) btn.disabled = true;
  try { const out = await fn(); if (okMsg) toast(okMsg); return out; }
  catch (e) {
    const m = /^Turn on (\w+) first$/.exec(e.message);
    if (e.status !== 401) toast(m && FEATURES[m[1]] ? `Turn on ${FEATURES[m[1]][0]} first` : e.message, true);
    return undefined;
  }
  finally { if (btn) btn.disabled = false; }
}

// ------------------------------------------------------------ login

function showLogin(msg) {
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
  $("login-err").textContent = msg || "";
  $("login-user").focus();
}

function logout(msg) {
  token = null;
  try { sessionStorage.removeItem(KEY); } catch (e) { /* private mode */ }
  showLogin(msg);
}

$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("login-btn");
  btn.disabled = true;
  $("login-err").textContent = "";
  try {
    const r = await api("POST", "/admin/login", { username: $("login-user").value.trim(), password: $("login-pass").value });
    token = r.access_token;
    try { sessionStorage.setItem(KEY, token); } catch (e) { /* keeps working for this page */ }
    $("login-pass").value = "";
    await start();
  } catch (e) {
    $("login-err").textContent = e.message;
  } finally { btn.disabled = false; }
});
$("logout").addEventListener("click", () => logout());

// ------------------------------------------------------------ navigation

const PAGES = [
  ["overview", "Overview"], ["payouts", "Payouts"], ["pool", "Cash pool"], ["creators", "Creators"],
  ["kampala", "Kampala growth"], ["moderation", "Reported posts"], ["messagereports", "Reported messages"], ["quickreports", "Reported quick posts"], ["commentreports", "Reported comments"], ["safety", "Safety record"], ["whatsnew", "What's new"], ["challenges", "Challenges"], ["picks", "Wano Picks"], ["studio", "Studio"], ["cities", "Cities"], ["categories", "Categories"], ["films", "Film submissions"], ["tvtitles", "Wano TV titles"], ["tvnumbers", "Wano TV numbers"], ["tvstrip", "Wano TV strip"], ["siteenq", "Website enquiries"], ["sitemail", "Mailing list"], ["sitedel", "Account deletion"], ["flyers", "Local flyers"], ["places", "Places"], ["failedups", "Failed uploads"], ["switches", "Feature switches"], ["updates", "Force update"], ["speed", "Speed"], ["storage", "Video storage"], ["log", "Activity log"],
];
let current = "overview";
const counts = {};

function drawNav() {
  const nav = $("nav");
  nav.replaceChildren(...PAGES.map(([id, label]) => el("button", {
    class: id === current ? "on" : "", "data-page": id, onclick: () => go(id),
  }, label, counts[id] ? el("span", { class: "count", text: counts[id] }) : null)));
}

async function go(page) {
  current = page;
  try { history.replaceState(null, "", "#" + page); } catch (e) { /* ignore */ }
  drawNav();
  const main = $("main");
  main.replaceChildren(el("p", { class: "muted", text: "Loading…" }));
  try { await RENDER[page](main); }
  catch (e) { if (e.status !== 401) main.replaceChildren(el("div", { class: "card" }, el("p", { text: e.message }))); }
}

function head(title, sub) {
  return [el("h2", { text: title }), sub ? el("p", { class: "sub", text: sub }) : null];
}

// ------------------------------------------------------------ pages

const ACTION_TEXT = { none: "Nothing (just the message)", quick_post: "Make a quick post", wano_tv: "Wano TV",
  my_profile: "Their profile", settings: "Settings", categories: "Points categories", points: "Wano points" };
const FEATURES = {
  points: ["Wano points", "Creators earn points for real views on their posts."],
  boost: ["Boost", "Creators spend points to show a post to more people. Needs Wano points."],
  earnings: ["Cash earnings", "The monthly cash pool, the creator checklist and payouts. Needs Wano points."],
  replies: ["Replies and duets", "Reply to a post with your own video."],
  challenges: ["Challenges", "Hashtag challenges you create on the Challenges page."],
  sounds: ["Trending sounds", "Sound pages and the trending sounds list."],
  city: ["Local mix", "Popular posts in the viewer's city."],
  leaderboard: ["Leaderboards", "Top creators this week."],
  badges: ["Badges", "Badges on profiles."],
  tv_series: ["Wano TV series", "Series, seasons and episodes on Wano TV."],
  hashtags: ["Hashtag suggestions", "Suggestions while typing a caption, popular hashtags and video ideas."],
  home_ground: ["Home Ground", "Users represent a city and country: weekly country and city battles, local creator boards, weekly crowns."],
  picks: ["Wano Picks", "Posts you pick on the Wano Picks page show on Discover for 7 days; the creator gets a badge."],
  missions: ["Weekly missions", "Tasks like posting 3 times that earn boost points (never cash)."],
  creator_stats: ["Creator stats", "Views, likes, comments and shares per day, best posts and best time to post."],
  mentions: ["@mentions", "Name suggestions while typing @, tappable @names, and a notification to the person mentioned."],
  ai_label: ["AI disclosure", "Creators mark a post Made with AI; the label shows in the feed and people can report undisclosed AI."],
  messages: ["Messages", "In-house messaging: chats between people who follow each other, message requests from others, shared posts, reports to this page."],
  feed_fx: ["Livelier feeds", "Tap effects, drag-to-seek, sound disc, top comment, local reactions (+1 your city, Eish, Ayaa), Trending as a chart with ranks and country chips."],
  profile_plus: ["Profile extras", "Wano passport chips (city, rank, points, tier), compact buttons, \"Followed by\" faces and Play all on profiles."],
  tv_plus: ["Wano TV extras", "Poster rows, Top 10, Continue watching with resume, next episode, My List and share on Wano TV."],
  perf: ["Speed measurement", "The app reports how long the first video, each video and each feed page take, and buffering, by country and Wi-Fi/mobile. Nothing personal is kept. See the Speed page."],
  instant_feed: ["Instant feed", "On opening the app, the New feed starts with the videos the person had loaded but not reached last time (saved on their phone, up to 8, for 12 hours), then the newest posts. Needs app 2.3.23 or later."],
  gzip_api: ["Compressed replies", "The server sends app data compressed: a feed page is about 7 KB instead of 34 KB. Works for every app version already installed. Turn off at once if anything looks wrong."],
  msg_lite: ["Light messages", "Messages use far less data: chats only fetch new messages, checks slow down and pause when the app is in the background, and a push brings a new message in at once. Needs app 2.3.24 or later."],
  wide_fill: ["Fill around wide videos", "Wide (landscape) videos get their own cover picture, blurred, above and below instead of black bands, and a small fullscreen button instead of the \"Fullscreen\" label. No extra data (the cover is already downloaded). Needs app 2.3.39."],
  quiet_counts: ["Hide zero counts", "Like, comment and reply counts show from 1 up; a \"0\" under a button is hidden (85% of posts showed \"0\" comments). Creators still see every number in Your stats. Needs app 2.3.39."],
  follow_plus: ["Follow with +", "A small green-to-orange \"+\" on the creator's picture while you don't follow them, instead of the Follow / Following pill on posts and Discover tiles (one tap on \"Following\" used to unfollow). Unfollow from the profile. Needs app 2.3.39."],
  dark_nav: ["Dark bottom bar on videos", "On Home and Wano TV the bottom bar is Wano's dark charcoal instead of a white card over the video; Wano green stays the active colour. Other screens keep the white bar. Needs app 2.3.39."],
  photo_full: ["Full-screen photos", "Photo posts use the whole screen like videos (dark header and caption over the picture) instead of a white header and white panel around a smaller photo. Needs app 2.3.40."],
  photo_zoom: ["Pinch to zoom photos (Android)", "Two fingers zoom into a photo post on Android, letting go springs back. iPhone already zooms. Needs app 2.3.39."],
  upload_retry: ["Keep trying broken uploads", "When a video upload breaks (weak signal, app closed), the app starts it again on the same Cloudflare upload, which carries on from what already arrived, up to 6 tries within 22 hours, instead of the post silently failing. About 1 upload in 7 broke this way in November 2026. The file stays on the phone until it's done. Needs app 2.3.42."],
  places: ["Places and areas", "People search a big list of towns and neighbourhoods (Ntinda, Kololo, Yaba...) and pick their area; flyers can then aim at a circle round a place (0.5, 1, 3 or 10 km). The old Home Ground city list and battles don't change. An area is picked by the person, never read from the phone's location. Tidy the list on the Places page first. Needs a new app version for people to pick an area."],
  flyers: ["Local flyers", "Businesses' flyers in the Wano TV ticket's spot on shorts (Sponsored, with a WhatsApp, Call or Visit button), made on the Local flyers page. Shown only in the countries and cities picked; never to under-18s or logged-out people; drinks only to people whose birthday shows 18+. TV and flyers share the same limits: never in the first 5 posts, 8 posts apart, 2 a session. Needs app 2.3.37."],
  msg_plus: ["Message extras", "In chats: long-press a message to react (one of 6 emojis), reply to it, edit your own (15 minutes, shows \"edited\") or delete it for everyone (1 hour, shows \"This message was deleted\"). Messages typed with no signal send when it comes back. The original text of edited or deleted messages is kept privately for 30 days so reports still show what was said. Needs app 2.3.37."],
  msg_voice: ["Voice messages", "Hold the mic in a chat to send a voice message, up to 2 minutes (about 30 KB for 10 seconds). Not allowed in message requests until they're accepted. Needs app 2.3.37; older apps see \"Voice message (update Wano to listen)\"."],
  phone_signup: ["Phone sign-up", "A \"Sign up with phone number\" button on the first screen: the code comes by WhatsApp (each one costs a few cents through Twilio). While on, WhatsApp codes are capped at 400 a day in total and 20 an hour from one network, so a script can't run up the bill; when the cap is hit people are told to use email. Needs app 2.3.36."],
  code_login: ["Sign in with a code", "\"Email me a code instead\" on the sign-in screen: a 6-digit code to the account's email (or WhatsApp if it has no email, or they typed their phone number). For people who forgot their password. Codes last 10 minutes, die after 5 wrong tries, 10 a day per account. Needs app 2.3.36."],
  signup_names: ["Fairer sign-up", "Fixes for people refused at sign-up, mostly on the old 2.1.1 app. A username with a space (\"Nkanda Rita\") is refused at the email/WhatsApp code step with a tip (\"Try Nkanda_Rita\") instead of after all the steps; stray spaces around a username are dropped. A blank Display Name becomes the username. Names like Essex, Dickson, Hancock or Lucumba are no longer refused as rude: short words (sex, cum, dick, cock, rape, nude) only count as whole words in a display name. Works for every app version already installed."],
  whats_new: ["What's new", "In-app announcements you write on the What's new page: one a day at most, each once per person, never to accounts under 3 days old, only when its feature is on and the app is new enough. A small card over the feed that never pauses a video, with a button that opens the feature. Needs app 2.3.28."],
  age_check: ["Birthday check", "People whose saved birthday makes them under 18 get a gentle \"Is your birthday right?\" (Fix / It's right / Later), and are asked again when they try to message. A birthday can be corrected once (13 to 110). Needs app 2.3.28."],
  quick_posts: ["Quick posts", "The ring around a creator's photo: Wano Voice notes (up to 30 s, about 30 KB) and Status cards (text on colour). Gone after 24 hours, 3 at a time. Under-18s: cards only, seen by their followers only. 3 reports hide one until you look (Reported quick posts). Needs app 2.3.26."],
  tv_strip: ["Wano TV strip", "A small dark ticket (poster, title, Watch) slides in above the caption on some videos, pointing at Wano TV (any video from app 2.3.27, Wano's house accounts on 2.3.26): after half the video has played, never in the first 5 posts, at most twice a session, gone after 6 seconds. Counts on the Wano TV strip page. Needs app 2.3.25."],
  more_categories: ["More categories", "16 points categories instead of 6 (Fashion & beauty, Health, Faith, Tech & gaming...), a Suggest a category button, and posts reported as false information by 2 people stop earning. See the Categories page. Works for every app version; the Suggest button needs app 2.3.25."],
  content_guide: ["What we look for", "Shows creators the standard for a Wano Pick and Wano TV under the Road to Wano TV, with a Submit your film link. Needs app 2.3.25."],
  clear_icons: ["Clearer icons", "Icons and tab names stay readable on bright videos (a thin dark edge and a soft top fade, measured at 3:1 or better on pure white), and Messages becomes two chat bubbles instead of a paper plane. Needs app 2.3.24 or later."],
  recombee_fast: ["Faster For You (Recombee)", "The server keeps its connections to Recombee open instead of opening a new one for every call (the Recombee step was 0.6-1 s of every For You page), asks for 60 picks instead of 100, and sends view signals in the background. Works for every app version. Check Recombee health: GET /api/v1/recombee/health."],
  recombee_tune: ["Better For You picks (Recombee)", "Recombee stops pushing down good picks the viewer was never shown (rotation 0.5 to 0.1). Compare personal vs popular watch rates on the For You report a few days after turning it on. Works for every app version."],
  recombee_eu: ["For You from Europe (Recombee)", "For You asks the new EU Recombee database (about 4x faster from our server) instead of the US one. Every write still goes to both, so turning it off goes straight back with nothing lost. If the EU database can't answer, the US one does. Check: GET /api/v1/recombee/health (answering: eu-west). Works for every app version."],
  lean_preload: ["Lighter preloading (iPhone)", "iPhones load the next 2 videos instead of 5, and read only about 10 seconds ahead in a video. About half the minutes Cloudflare billed in November were never watched. Android already loads 2. Needs app 2.3.43."],
  loop_cap: ["Stop after 3 loops", "A video plays 3 times, then pauses with the play button up (a tap plays 3 more). Each loop was downloaded and billed again: short clips were billed 1.46 full plays per view. Watch the For You report after turning it on. Needs app 2.3.43."],
  tv_age_gate: ["Wano TV: age ratings", "Titles rated above someone's age (16+, 18+…) are left out of Wano TV, History, the feed strip and downloads for accounts whose birthday makes them under that age. Unknown birthdays count as adult. Server only: works for every app version."],
  tv_loves: ["Wano TV: hearts", "A heart on each title, with the count. Needs the next app build."],
  tv_coming_soon: ["Wano TV: Coming soon", "Titles set to Coming soon in Wano TV titles show in their own row with Remind me; going Live sends those people a notification. Needs the next app build."],
  tv_clips: ["Wano TV: clips in the feed", "Feed posts linked to a title (Wano TV titles > Clips) show a Watch the full film button. Needs the next app build."],
  tv_downloads: ["Wano TV: downloads", "Download a film to watch with no data (signed-in people, 20 a day, kept 30 days in the app). Only titles with an offline copy. Needs the next app build."],
  tv_pip: ["Wano TV: picture in picture", "A film keeps playing in a small window when you leave the app or check a message. Needs the next app build."],
  tv_player_plus: ["Wano TV: player extras", "Choose the subtitle language, and when a film ends suggest another one (series already roll into the next episode). Needs the next app build."],
  data_saver: ["Data saver on mobile data", "On mobile data, videos play in standard quality (about 480p, at most ~22 MB a minute instead of up to 61 MB); people can turn on \"HD on mobile data\" in Settings. Wi-Fi is unchanged. Doesn't change the Cloudflare bill. Needs app 2.3.43."],
  fast_start: ["Faster start on slow connections", "For places where videos are slow to start (Uganda median 3 s, Kenya 5 s vs 0.1 s in the US). Android starts a video after 1 s of it has arrived instead of 2.5 s and resumes after a pause sooner. Each phone measures its own connection: the slower it is, the smaller the video (about 480p when videos take over 2 s to start, 360p over 4 s, the smallest copy over 8 s), back to full quality when starts are quick. Less waiting and less data. \"HD on mobile data\" is respected. Needs app 2.3.58."],
  challenge_fair: ["Fair challenge scoring", "Challenges created while this is on are scored so they can't be bought: only accounts at least 3 days old count (never the creator), each viewer once, signed-out views at a tenth with a cap, one point-earning comment per person, held comments never count. A post enters one challenge, and not with more than 10 hashtags. Admin accounts never rank. The board freezes at the end time. Challenges made before keep their old scoring. Works for every app version (the hashtag still enters)."],
  reaction_country: ["Reactions by country", "People who haven't set a Home Ground city react \"+1 Uganda\" (their account's country) instead of being asked to pick a city, and a post whose creator has no city shows \"📍 Uganda · 3 reactions here\". Cities still win when set."],
  post_limits: ["Posting limits", "One account can post at most 30 times an hour and 100 times a day (ordinary, photo, scheduled and multi-clip posts together), so a script can't flood feeds or fill our Cloudflare storage. Far above what real creators post. Admins are exempt. Someone over the limit sees: \"You've posted a lot in a short time. Please try again a little later.\""],
  open_after_install: ["Open the shared post after installing", "Someone on Android taps a shared Wano link without having the app: Google Play installs it and, on the first launch, the app opens that exact post (over the welcome screens) instead of a generic start. Needs app 2.3.59. iPhones can't pass this through the App Store, so they open as today."],
  share_play: ["Play button on shared videos", "When someone shares a Wano video on WhatsApp, iMessage, Facebook or X, the preview picture gets a ▶ in the middle, like TikTok and Instagram previews, so people see it's a video and tap. Videos only; photo posts unchanged. Previews already cached by WhatsApp keep their old picture."],
  upload_locked: ["Uploads need an account", "The upload address the video editor (Lambda) uses works without signing in, so anyone on the internet could store videos on our Cloudflare bill. On: without an account, only a running video-editor job gets an upload address (it asks under the job's own file name). Before turning on, open Failed uploads in Admin: \"Without an account: others\" should stay 0 for a few days while people use the video editor."],
  brand_links: ["Share links on wanoafrica.com", "Shared posts, profiles and Wano TV titles show wanoafrica.com instead of devbe.wanoafrica.com: the link previews and pages call themselves wanoafrica.com, and anyone opening an old devbe share link in a browser is sent to the same page on wanoafrica.com. Turn on after the website update is live. App 2.3.59 shares wanoafrica.com links itself."],
  comment_plus: ["Comments, new look", "In the app (2.3.59): anyone can open and read comments, signing in only to write, like or report; hearts on comments; a Creator label; \"...\" on a comment to report it with a reason or delete it (yours, or any on your own post), asking first; a comment that fails to send says so; teens 16-17 choose who can comment in Settings. Needs Comment safety on."],
  teen_comments: ["Teen comment safety", "On teen accounts' posts: under 16, only friends (people who follow each other) can comment, and it can't be opened up; 16 and 17, only people the teen follows unless they choose everyone (app 2.3.59). Comments with offensive words are held. A held comment is visible only to its writer. Every held comment and every switch change is kept in Safety record."],
  comment_guard: ["Comment safety", "Comments with links, from people going too fast (over 5 a minute, 20 on one post in an hour, 200 a day), or between people who blocked each other are kept visible only to the person who wrote them (old apps show no error, so nothing is refused). Comments from people you blocked are hidden. Signed-out viewers can read comments. Comment reports: three reports from accounts at least 3 days old hide a comment until you review it (Comment reports)."],
  quick_replies: ["Quick post replies and comments", "On voice notes and cards: type a private reply or tap a reaction without leaving, and it arrives in their messages with a small preview of what it answers. Posters can also turn on public comments for a card or voice note (off unless they choose it). Needs Quick posts on and app 2.3.58."],
  small_images: ["Small pictures", "The app loads small copies of pictures (avatars ~10 KB instead of up to 2 MB). Turn on once the Speed page shows the copies are done."],
  trial_posts: ["Trial posts", "Creators can post without telling followers, then share it later."],
  milestones: ["Creator milestones", "Celebrations at follower and view milestones, tier badges (Rising, Star, Gold, Legend) next to names, achievements page."],
  tv_play: ["Play Wano TV films", "Films play inside the app. Needs Wano TV series. Turn on only after testing a film on a phone."],
};

const CHECKS = {
  age: (c) => "18 or older",
  phone: (c) => "Phone number on the account",
  account_days: (c) => `Account at least ${c.need} days old (${num(c.have)} days)`,
  followers: (c) => `${num(c.need)} followers (has ${num(c.have)})`,
  views_28d: (c) => `${num(c.need)} earning views in 28 days (has ${num(c.have)})`,
  viewers_28d: (c) => `${num(c.need)} different viewers in 28 days (has ${num(c.have)})`,
  no_copied_posts: (c) => c.have ? `No copied posts in 90 days (${c.have} marked)` : "No copied posts in 90 days",
};

const RENDER = {
  async overview(main) {
    const o = await api("GET", "/admin/overview");
    counts.payouts = o.payouts_waiting; counts.moderation = o.moderation_waiting; drawNav();
    const on = Object.entries(o.features).filter(([, v]) => v).map(([k]) => (FEATURES[k] || [k])[0]);
    const stat = (label, value, hint, page) => el("div", { class: "stat" + (page ? " link" : ""), onclick: page ? () => go(page) : null },
      el("div", { class: "label", text: label }), el("div", { class: "value", text: value }), el("div", { class: "hint", text: hint }));
    main.replaceChildren(...head("Overview", "What needs you today."),
      el("div", { class: "grid" },
        stat("Payouts to send", money(o.payouts_waiting_usd), `${o.payouts_waiting} waiting`, "payouts"),
        stat("Reported posts", num(o.moderation_waiting), "waiting for a decision", "moderation"),
        stat(`Cash pool ${o.pool.month}`, money(o.pool.pool_usd), `${num(o.pool.creators)} creators earning points`, "pool"),
        stat("Live challenges", num(o.challenges_live), "running now", "challenges")),
      el("div", { class: "card" }, el("div", { class: "row space" }, el("h3", { text: "Features turned on" }),
        el("button", { class: "btn secondary small", onclick: () => go("switches"), text: "Change" })),
        el("p", { class: on.length ? "" : "muted", text: on.length ? on.join(", ") : "Everything is off. The app looks the same as before." })));
  },

  async payouts(main, state) {
    state = state || "requested";
    const rows = await api("GET", "/admin/payouts?state=" + state);
    if (state === "requested") { counts.payouts = rows.length; drawNav(); }
    const tabs = el("div", { class: "tabs" }, ...[["requested", "Waiting"], ["approved", "Paid"], ["rejected", "Rejected"]].map(([s, l]) =>
      el("button", { class: s === state ? "on" : "", text: l, onclick: () => RENDER.payouts(main, s) })));
    const body = rows.length ? el("table", {},
      el("thead", {}, el("tr", {}, el("th", { text: "Creator" }), el("th", { text: "Amount" }), el("th", { text: "Send to" }),
        el("th", { text: state === "requested" ? "Asked" : "Decided" }), state === "requested" ? el("th", { text: "" }) : null)),
      el("tbody", {}, ...rows.map((p) => payoutRow(p, state, main)))) : el("div", { class: "empty", text: state === "requested" ? "No payouts waiting." : "Nothing here yet." });
    main.replaceChildren(...head("Payouts", "Send the money by mobile money, Pix or bank first, then press Mark as paid. Reject gives the cash back to the creator."),
      tabs, el("div", { class: "card" }, body));
  },

  async pool(main, month) {
    month = month || lastMonth();
    const p = await api("GET", "/points/admin/pool/" + month);
    const open = p.state !== "distributed";
    const rev = el("input", { type: "number", min: 0, step: "0.01", value: p.revenue_usd || 0 });
    const pct = el("input", { type: "number", min: 0, max: 100, step: "0.1", value: p.percent });
    const floor = el("input", { type: "number", min: 0, step: "1", value: p.floor_usd });
    const pick = el("input", { type: "month", value: month, onchange: (e) => e.target.value && RENDER.pool(main, e.target.value) });
    const save = el("button", { class: "btn", text: "Save", onclick: async (e) => {
      const body = { revenue_usd: Number(rev.value), percent: Number(pct.value), floor_usd: Number(floor.value) };
      if ([body.revenue_usd, body.percent, body.floor_usd].some((v) => !isFinite(v) || v < 0)) return toast("Numbers can't be empty or negative", true);
      const send = async (confirm) => api("PUT", "/points/admin/pool/" + month, { ...body, confirm });
      const r = await act(e.target, async () => {
        try { return await send(false); }
        catch (err) {
          if (err.status === 400 && /confirm/i.test(err.message)) {
            const sure = await confirmBox("That's a big pool", err.message.replace(/ and send confirm: true if it's right\./, "."), "Yes, it's right");
            return sure ? await send(true) : undefined;
          }
          throw err;
        }
      });
      if (r) { toast("Saved"); RENDER.pool(main, month); }
    }});
    const share = el("button", { class: "btn danger", text: "Share out now", disabled: !open || !p.wano_creators, onclick: async (e) => {
      const ok = await confirmBox(`Share out ${month}?`,
        `${money(p.pool_usd)} goes to the top ${p.top_n} Wano Creators (${p.wano_creators} qualify). This can't be undone. If you do nothing it happens automatically on the 5th.`,
        "Share out", true);
      if (!ok) return;
      const r = await act(e.target, () => api("POST", `/points/admin/pool/${month}/distribute`));
      if (r) { toast(`Shared ${money(r.paid_usd)} with ${r.creators} creators`); RENDER.pool(main, month); }
    }});
    main.replaceChildren(...head("Cash pool", "Each month the pool is the bigger of the floor or your % of revenue. Only the top Wano Creators share it, $10 each on average."),
      el("div", { class: "row", style: "margin-bottom: 14px;" }, el("span", { class: "muted", text: "Month" }), el("div", { style: "width: 190px;" }, pick),
        el("span", { class: "pill " + (open ? "green" : "grey"), text: open ? "Open" : "Shared out" })),
      el("div", { class: "grid" },
        el("div", { class: "stat" }, el("div", { class: "label", text: "Pool" }), el("div", { class: "value", text: money(p.pool_usd) }),
          el("div", { class: "hint", text: `max(${money(p.floor_usd)}, ${p.percent}% of ${money(p.revenue_usd)})` })),
        el("div", { class: "stat" }, el("div", { class: "label", text: "Paid to" }), el("div", { class: "value", text: `top ${num(p.top_n)}` }),
          el("div", { class: "hint", text: `${num(p.wano_creators)} Wano Creators qualify` + (p.kept_usd ? ` · ${money(p.kept_usd)} stays with Wano ` +
            (p.wano_creators ? "(no one gets more than 25%)" : "(nobody qualifies)") : "") })),
        el("div", { class: "stat" }, el("div", { class: "label", text: "Points earned" }), el("div", { class: "value", text: num(p.total_points) }),
          el("div", { class: "hint", text: `${num(p.creators)} creators` }))),
      el("div", { class: "two" },
        el("div", { class: "card" }, el("h3", { text: "Revenue for " + month }),
          open ? null : el("div", { class: "warn", text: "This month is already shared out, so it can't change." }),
          el("label", { class: "field" }, el("span", { text: "Wano revenue that month (US$)" }), rev),
          el("label", { class: "field" }, el("span", { text: "% of revenue for creators" }), pct),
          el("label", { class: "field" }, el("span", { text: "Floor: the least you pay (US$)" }), floor),
          el("div", { class: "row" }, open ? save : null, open ? share : null)),
        el("div", { class: "card" }, el("h3", { text: open ? "Who would get paid now" : "Top shares" }),
          p.top.length ? el("table", {}, el("tbody", {}, ...p.top.map((t) => el("tr", {},
            el("td", {}, el("a", { href: "#creators", onclick: (ev) => { ev.preventDefault(); openCreator(t.user_id); }, text: t.username ? "@" + t.username : t.user_id })),
            el("td", { class: "muted", text: num(t.points) + " pts" }), el("td", { text: money(t.usd) })))))
            : el("div", { class: "empty", text: "Nobody qualifies yet." }))));
    [rev, pct, floor].forEach((i) => { i.disabled = !open; });
  },

  async creators(main, q) {
    const box = el("input", { type: "text", placeholder: "Username, user ID or a post link", value: q || "" });
    const out = el("div");
    const find = async () => {
      const v = box.value.trim();
      if (!v) return;
      out.replaceChildren(el("p", { class: "muted", text: "Looking…" }));
      try { out.replaceChildren(creatorCard(await api("GET", "/admin/creators/lookup?q=" + encodeURIComponent(v)), main, v)); }
      catch (e) { if (e.status !== 401) out.replaceChildren(el("div", { class: "card empty", text: e.message })); }
    };
    box.addEventListener("keydown", (e) => { if (e.key === "Enter") find(); });
    main.replaceChildren(...head("Creators", "Check whether someone is a Wano Creator, see their points and cash, and mark copied posts."),
      el("div", { class: "row", style: "margin-bottom: 14px; flex-wrap: nowrap;" }, box, el("button", { class: "btn", text: "Find", onclick: find })), out);
    if (q) find(); else box.focus();
  },

  async moderation(main) {
    const rows = await api("GET", "/reports/admin/queue?limit=100");
    counts.moderation = rows.length; drawNav();
    main.replaceChildren(...head("Reported posts", "Held posts are hidden until you decide. Restore puts a post back; Remove deletes it."),
      el("div", { class: "card" }, rows.length ? el("table", {}, el("tbody", {}, ...rows.map((v) => {
        const thumb = safeUrl(v.thumbnail);
        const cats = Object.entries(v.categories || {}).map(([k, n]) => `${k.replace(/_/g, " ")} (${n})`).join(", ");
        return el("tr", {},
          el("td", { style: "width: 56px;" }, thumb ? el("img", { class: "thumb", src: thumb, alt: "", onerror: (e) => { e.target.style.visibility = "hidden"; } }) : el("div", { class: "thumb" })),
          el("td", {}, el("div", { text: v.title || "(no title)" }),
            el("div", { class: "small muted", text: `${v.reports} reports${cats ? ": " + cats : ""}` }),
            el("div", { class: "row small", style: "margin-top: 4px;" },
              v.state === "held" ? el("span", { class: "pill amber", text: "Hidden" }) : el("span", { class: "pill grey", text: `${v.flags} flags` }),
              el("a", { href: postLink(v.id), target: "_blank", rel: "noopener noreferrer", text: "Open post" }),
              v.creator_id ? el("a", { href: "#creators", onclick: (e) => { e.preventDefault(); openCreator(v.creator_id); }, text: "Creator" }) : null)),
          el("td", { style: "text-align: right; white-space: nowrap;" },
            el("button", { class: "btn secondary small", text: "Restore", onclick: (e) => moderate(e.target, v, "restore", main) }), " ",
            el("button", { class: "btn ghost-danger small", text: "Remove", onclick: (e) => moderate(e.target, v, "remove", main) })));
      }))) : el("div", { class: "empty", text: "Nothing reported." })));
  },

  async messagereports(main) {
    const r = await api("GET", "/admin/message-reports");
    counts.messagereports = r.items.length; drawNav();
    const decide = async (btn, rep, action) => {
      if (await act(btn, () => api("POST", `/admin/message-reports/${encodeURIComponent(rep.id)}`, { action }),
        action === "remove" ? "Message removed" : "Report dismissed")) RENDER.messagereports(main);
    };
    main.replaceChildren(...head("Reported messages", "Only the reported message and a few around it are shown, never the whole chat. Remove replaces it with \"Message removed by Wano\" for both people."),
      el("div", { class: "card" }, r.items.length ? el("table", {}, el("tbody", {}, ...r.items.map((rep) => el("tr", {},
        el("td", {},
          el("div", { class: "row small", style: "margin-bottom: 6px;" },
            el("span", { class: "pill amber", text: (rep.reason || "other").replace(/_/g, " ") }),
            el("span", { class: "muted", text: `@${(rep.sender && rep.sender.username) || "?"} reported by @${(rep.reporter && rep.reporter.username) || "?"}` })),
          ...(rep.context || []).map((m) => el("div", { class: "small", style: `padding: 3px 8px; border-radius: 6px; margin: 2px 0;${m.reported ? " background: #FFF4E5; font-weight: 600;" : ""}`,
            text: `@${m.from || "?"}: ${m.kind === "voice" ? "[voice message]" : m.kind === "post" && !m.text ? "[shared a post]" : (m.text || "[removed]")}${m.unsent ? "  (deleted by sender)" : ""}${(m.edits || []).length ? `  (edited; before: ${m.edits.map((t) => JSON.stringify(t)).join(", ")})` : ""}` },
              m.kind === "voice" && safeUrl(m.audio_url) ? el("audio", { controls: true, preload: "none", src: safeUrl(m.audio_url) }) : null))),
        el("td", { style: "text-align: right; white-space: nowrap; vertical-align: top;" },
          el("button", { class: "btn secondary small", text: "Dismiss", onclick: (e) => decide(e.target, rep, "dismiss") }), " ",
          el("button", { class: "btn ghost-danger small", text: "Remove", onclick: (e) => decide(e.target, rep, "remove") })))))) :
        el("div", { class: "empty", text: "No reported messages." })));
  },

  async quickreports(main) {
    const r = await api("GET", "/admin/quick-reports");
    counts.quickreports = r.items.filter((q) => !q.deleted).length; drawNav();
    const decide = async (btn, q, action) => {
      if (await act(btn, () => api("POST", `/admin/quick-reports/${encodeURIComponent(q.id)}`, { action }),
        action === "remove" ? "Quick post removed" : "Quick post restored")) RENDER.quickreports(main);
    };
    main.replaceChildren(...head("Reported quick posts", "Voice notes and Status cards people reported. Three reports from established accounts hide one at once. Restore puts it back; Remove deletes it."),
      el("div", { class: "card" }, r.items.length ? el("table", {}, el("tbody", {}, ...r.items.map((q) => {
        const audio = q.kind === "voice" ? safeUrl(q.audio_url) : null;
        return el("tr", {},
          el("td", {},
            el("div", { class: "row small", style: "margin-bottom: 6px;" },
              el("span", { class: "pill amber", text: (q.reasons || []).join(", ").replace(/_/g, " ") || "other" }),
              q.deleted ? el("span", { class: "pill grey", text: "Removed" }) : q.hidden ? el("span", { class: "pill amber", text: "Hidden" }) : null,
              el("span", { class: "muted", text: `@${(q.owner && q.owner.username) || "?"} · ${q.reports} reports` })),
            q.kind === "card" ? el("div", { style: "font-weight: 600;", text: q.text || "[removed]" })
              : audio && !q.deleted ? el("audio", { controls: true, preload: "none", src: audio }) : el("div", { class: "muted", text: "[voice note removed]" })),
          el("td", { style: "text-align: right; white-space: nowrap; vertical-align: top;" },
            q.deleted ? null : el("button", { class: "btn secondary small", text: "Restore", onclick: (e) => decide(e.target, q, "restore") }), " ",
            q.deleted ? null : el("button", { class: "btn ghost-danger small", text: "Remove", onclick: (e) => decide(e.target, q, "remove") })));
      }))) : el("div", { class: "empty", text: "No reported quick posts." })));
  },

  async commentreports(main) {
    const r = await api("GET", "/admin/comment-reports");
    counts.commentreports = r.items.filter((c) => !c.deleted).length; drawNav();
    const decide = async (btn, c, action) => {
      if (await act(btn, () => api("POST", `/admin/comment-reports/${encodeURIComponent(c.comment_id)}`, { action }),
        action === "hide" ? "Comment hidden" : "Comment kept")) RENDER.commentreports(main);
    };
    main.replaceChildren(...head("Reported comments", "Comments people reported (needs the Comment safety switch and app 2.3.59). Three reports from accounts at least 3 days old hide one at once. Keep puts it back; Hide takes it down for everyone."),
      el("div", { class: "card" }, r.items.length ? el("table", {}, el("tbody", {}, ...r.items.map((c) => el("tr", {},
        el("td", {},
          el("div", { class: "row small", style: "margin-bottom: 6px;" },
            el("span", { class: "pill amber", text: String(c.reason || "other").replace(/_/g, " ") }),
            c.deleted ? el("span", { class: "pill grey", text: "Deleted" }) : c.hidden ? el("span", { class: "pill amber", text: "Hidden" }) : null,
            el("span", { class: "muted", text: `${c.reports} reports` })),
          el("div", { style: "font-weight: 600;", text: c.text || "[empty]" })),
        el("td", { style: "text-align: right; white-space: nowrap; vertical-align: top;" },
          c.deleted ? null : el("button", { class: "btn secondary small", text: "Keep", onclick: (e) => decide(e.target, c, "keep") }), " ",
          c.deleted ? null : el("button", { class: "btn ghost-danger small", text: "Hide", onclick: (e) => decide(e.target, c, "hide") })))))) : el("div", { class: "empty", text: "No reported comments." })));
  },

  async safety(main) {
    const r = await api("GET", "/admin/safety-record?days=90");
    const when = (d) => d ? new Date(d.endsWith && !d.endsWith("Z") ? d + "Z" : d).toLocaleString() : "";
    const dl = () => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([JSON.stringify(r, null, 2)], { type: "application/json" }));
      a.download = `wano-safety-record-${new Date().toISOString().slice(0, 10)}.json`; a.click();
    };
    main.replaceChildren(...head("Safety record", "Wano's safety rules as written, when each was switched on or off and by whom, and what they held back in the last 90 days. Rules are never edited: a new wording is a new version."),
      el("div", { class: "row", style: "margin-bottom: 12px;" }, el("button", { class: "btn secondary small", text: "Download record (JSON)", onclick: dl })),
      el("div", { class: "card" }, el("h3", { text: "Rules" }), ...r.policies.map((p) => el("div", { style: "margin: 10px 0;" },
        el("div", { style: "font-weight: 600;", text: `${p.title} (${p.id})` }),
        el("div", { class: "small", text: p.text }),
        p.follows && p.follows.length ? el("div", { class: "small muted", text: "Follows: " + p.follows.join("; ") }) : null,
        el("div", { class: "small muted", text: `Recorded ${when(p.recorded_at)}${p.switch ? " · switch: " + p.switch : " · always on"}` })))),
      el("div", { class: "card" }, el("h3", { text: "Switch history" }), r.switch_history.length ? el("table", {}, el("tbody", {},
        ...r.switch_history.map((h) => el("tr", {}, el("td", { text: when(h.at) }), el("td", { text: h.switch }),
          el("td", { text: h.on ? "on" : "off" }), el("td", { class: "muted", text: h.by ? "@" + h.by : "" })))))
        : el("div", { class: "empty", text: "No switch changes yet." })),
      el("div", { class: "card" }, el("h3", { text: "Held comments (90 days)" }),
        el("div", { class: "small", text: Object.entries(r.held_comments).map(([k, n]) => `${k.replace(/_/g, " ")}: ${n}`).join(" · ") || "None" }),
        el("h3", { text: "Comment reports (90 days)" }),
        el("div", { class: "small", text: Object.entries(r.comment_reports).map(([k, n]) => `${k}: ${n}`).join(" · ") || "None" })));
  },

  async failedups(main) {
    const r = await api("GET", "/admin/failed-uploads");
    const attemptsCard = el("div", { class: "card" }, el("div", { class: "small muted", text: "Loading posting attempts..." }));
    const stat = (n, label) => el("div", { class: "card", style: "text-align:center; flex: 1 1 140px;" },
      el("div", { style: "font-size: 26px; font-weight: 700;", text: num(n) }), el("div", { class: "small muted", text: label }));
    const send = el("button", { class: "btn", text: `Send to ${num(r.to_invite)} creators`, disabled: !r.to_invite, onclick: async (e) => {
      if (!(await confirmBox("Send the invite?", `${num(r.to_invite)} creators (${num(r.reachable_by_push)} with notifications on) get this once:\n\n${r.title}\n${r.message}\n\nNobody is ever sent it twice.`, "Send"))) return;
      const out = await act(e.target, () => api("POST", "/admin/failed-uploads/invite", { confirm: true }), "Invites sent");
      if (out) RENDER.failedups(main);
    } });
    main.replaceChildren(...head("Failed uploads", `Creators whose video never finished uploading in the last ${r.days} days and who haven't posted since. The video can't be recovered (it never reached Cloudflare), but it's usually still in their gallery. Best sent once the app version that keeps trying broken uploads is in the store.`),
      el("div", { class: "row", style: "gap: 10px; flex-wrap: wrap;" },
        stat(r.failed_posts, "posts that never arrived"), stat(r.creators, "creators affected"),
        stat(r.posted_since, "posted again since"), stat(r.already_invited, "already invited"), stat(r.to_invite, "to invite now")),
      el("div", { class: "card" }, el("h3", { text: "The message (push notification, opens their profile)" }),
        el("div", { style: "font-weight: 600;", text: r.title }), el("div", { text: r.message }),
        el("div", { class: "small muted", style: "margin: 8px 0;", text: `${num(r.reachable_by_push)} of ${num(r.to_invite)} have notifications on; the others can't be reached this way.` }),
        send),
      attemptsCard);
    const a = await api("GET", "/admin/upload-attempts?days=7").catch(() => null);
    if (!a) return;
    const pct = (n) => (a.attempts ? ` (${Math.round(n / a.attempts * 100)}%)` : "");
    attemptsCard.replaceChildren(el("h3", { text: `Posting attempts, last ${a.days} days` }),
      el("p", { class: "small muted", text: "Each time the app gets an upload address, then whether a post followed. Counting started with this server update, so the first days are thin." }),
      el("table", {}, el("tbody", {},
        ...[["Upload addresses handed out", a.attempts, ""], ["Became a post", a.posted, pct(a.posted)],
          ["Refused by the server (see reasons)", a.refused, pct(a.refused)], ["Gave up, then posted from a later try", a.tried_again, pct(a.tried_again)],
          ["No post at all", a.no_post, pct(a.no_post)]].map(([k, v, p]) => el("tr", {}, el("td", { text: k }), el("td", { text: num(v) + p }))),
        ...Object.entries(a.by_platform).map(([k, v]) => el("tr", {}, el("td", { class: "muted", text: `${k}: posted` }), el("td", { text: `${num(v.posted)} of ${num(v.attempts)}` }))),
        el("tr", {}, el("td", { class: "muted", text: "Typical file size: posted / not posted" }), el("td", { text: `${a.size_mb.posted ?? "–"} MB / ${a.size_mb.not_posted ?? "–"} MB` })),
        ...(a.without_account ? [
          el("tr", {}, el("td", { text: "Without an account: video editor" }), el("td", { text: num(a.without_account.video_editor) })),
          el("tr", {}, el("td", { text: "Without an account: others (must be 0 before \"Uploads need an account\" goes on)" }), el("td", { text: num(a.without_account.others) }))] : []))),
      el("h3", { text: `Why posts were refused (${num(a.refusals)})` }),
      a.refusal_reasons.length ? el("table", {}, el("tbody", {}, ...a.refusal_reasons.map((x) => el("tr", {}, el("td", { text: x.reason }), el("td", { text: num(x.count) })))))
        : el("div", { class: "empty", text: "None yet." }));
  },
  async places(main) {
    const q = el("input", { type: "search", maxlength: 60, placeholder: "Search: Ntinda, Kololo, Yaba..." });
    const cc = el("input", { type: "text", maxlength: 2, placeholder: "ug", style: "width: 4em;" });
    const box = el("div", {});
    const stats = await api("GET", "/admin/places/search?q=").catch(() => null);
    const draw = async () => {
      if (q.value.trim().length < 2) { box.replaceChildren(el("div", { class: "empty", text: "Type at least 2 letters." })); return; }
      const r = await api("GET", `/admin/places/search?q=${encodeURIComponent(q.value.trim())}${cc.value.trim() ? "&country=" + encodeURIComponent(cc.value.trim().toLowerCase()) : ""}`);
      box.replaceChildren(r.items.length ? el("table", {}, el("thead", {}, el("tr", {}, ...["Place", "Kind", "", ""].map((h) => el("th", { text: h })))),
        el("tbody", {}, ...r.items.map((p) => el("tr", {},
          el("td", {}, el("div", { style: "font-weight: 600;", text: p.label }), p.hidden ? el("span", { class: "pill grey", text: "Hidden" }) : null),
          el("td", { text: p.kind }),
          el("td", {}, el("button", { class: "btn small secondary", text: "Rename", onclick: async (e) => {
            const name = prompt("New name for " + p.name, p.name);
            if (name && name.trim() && name.trim() !== p.name && await act(e.target, () => api("POST", `/admin/places/${encodeURIComponent(p.id)}`, { name: name.trim() }), "Renamed")) draw();
          } })),
          el("td", {}, el("button", { class: "btn small secondary", text: p.hidden ? "Show" : "Hide", onclick: async (e) => {
            if (await act(e.target, () => api("POST", `/admin/places/${encodeURIComponent(p.id)}`, { hidden: !p.hidden }), p.hidden ? "Shown" : "Hidden")) draw();
          } }))))))
        : el("div", { class: "empty", text: "Nothing found." }));
    };
    let t = null;
    q.addEventListener("input", () => { clearTimeout(t); t = setTimeout(draw, 250); });
    cc.addEventListener("input", () => { clearTimeout(t); t = setTimeout(draw, 250); });
    main.replaceChildren(...head("Places", "The towns and neighbourhoods people can search and pick as their area (behind the Places switch). Hide a wrong or duplicate place, or fix its spelling; Home Ground cities and battles are separate and don't change."),
      el("div", { class: "card" },
        el("div", { class: "row", style: "gap: 8px;" }, q, cc, el("span", { class: "small muted", text: "country code (optional)" })),
        stats ? el("div", { class: "small muted", style: "margin: 6px 0;", text: `${num(stats.places)} places: ${num(stats.by_kind.city || 0)} cities, ${num(stats.by_kind.town || 0)} towns, ${num(stats.by_kind.area || 0)} neighbourhoods.` }) : null,
        box,
        el("div", { class: "small muted", style: "margin-top: 10px;", text: "Place names: © OpenStreetMap contributors, and GeoNames (CC BY 4.0)." })));
    draw();
  },
  async flyers(main) {
    const [r, opt] = await Promise.all([api("GET", "/admin/flyers"), api("GET", "/admin/flyers/options")]);
    const local = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    const now = new Date();
    let editing = null;
    let image = null;
    let uploadRevision = 0, motionRevision = 0;
    let motion = null, picturePending = false, motionPending = false;
    const motionInput = el("input", { type: "file", accept: "video/mp4" });
    const motionPreview = el("img", { alt: "Motion strip preview", style: "display:none;width:100%;max-width:300px;" });
    const refreshSave = () => { save.disabled = picturePending || motionPending || !image; };
    const showMotion = () => {
      motionPreview.style.display = motion ? "block" : "none";
      if (motion) motionPreview.src = motion;
      else motionPreview.removeAttribute("src");
    };
    const clearMotion = () => {
      ++motionRevision;
      motion = null; motionPending = false; motionInput.value = "";
      showMotion(); refreshSave();
    };
    motionInput.addEventListener("change", async () => {
      const file = motionInput.files[0];
      if (!file) return;
      const revision = ++motionRevision;
      motionPending = true; refreshSave();
      const form = new FormData(); form.append("file", file);
      try {
        const res = await fetch(API + "/admin/flyers/motion", { method: "POST", headers: { Authorization: "Bearer " + token }, body: form });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Motion upload failed");
        if (revision !== motionRevision) return;
        motion = data.motion; showMotion(); toast("Motion strip ready");
      } catch (e) { if (revision === motionRevision) toast(e.message, true); }
      finally { if (revision === motionRevision) { motionPending = false; refreshSave(); } }
    });
    const f = {
      business: el("input", { type: "text", maxlength: 40, placeholder: "Mama Rose Kitchen" }),
      line: el("input", { type: "text", maxlength: 80, placeholder: "Lunch from 10,000 UGX, Kampala Road" }),
      category: el("select", {}, ...opt.categories.map((c) => el("option", { value: c.id, text: c.name + (c.adults_only ? " · 18+ with known birthday" : "") }))),
      countries: el("select", { multiple: true, size: 6 }, ...opt.countries.map((c) => el("option", { value: c.id, text: c.name }))),
      cities: el("select", { multiple: true, size: 6 }),
      button: el("select", {}, el("option", { value: "whatsapp", text: "WhatsApp" }), el("option", { value: "call", text: "Call" }),
        el("option", { value: "link", text: "Visit (website)" })),
      button_value: el("input", { type: "text", maxlength: 500, placeholder: "+256772123456" }),
      starts: el("input", { type: "datetime-local", value: local(now) }),
      ends: el("input", { type: "datetime-local", value: local(new Date(now.getTime() + 14 * 864e5)) }),
      cap: el("input", { type: "number", min: 1, placeholder: "No limit" }),
      testers: el("input", { type: "text", maxlength: 400, placeholder: "eilmer, nasir" }),
      topics: el("select", {multiple: true, size: 5}, ...(opt.clip_topics || []).map(t => el("option", {value:t.id, text:t.name}))),
      allViewers: el("input", { type: "checkbox" }),
      testersOnly: el("input", { type: "checkbox" }),
      file: el("input", { type: "file", accept: "image/jpeg,image/png,image/webp" }),
      active: el("input", { type: "checkbox", checked: true }),
      circleQ: el("input", { type: "search", maxlength: 60, placeholder: "Type a place: Ntinda" }),
      circlePick: el("select", {}, el("option", { value: "", text: "No circle (whole cities or countries)" })),
      circleKm: el("select", {}, ...[["0.5", "500 m"], ["1", "1 km"], ["3", "3 km"], ["10", "10 km"]].map(([v, t]) => el("option", { value: v, text: t, selected: v === "1" }))),
    };
    const reachLine = el("div", { class: "small muted", text: "" });
    let circleLabel = {};
    const showReach = async () => {
      const pid = f.circlePick.value;
      if (!pid) { reachLine.textContent = ""; return; }
      try {
        const r = await api("GET", `/admin/places/reach?place_id=${encodeURIComponent(pid)}&km=${encodeURIComponent(f.circleKm.value)}`);
        reachLine.textContent = `Reaches ${r.label} adults who picked an area inside (${r.places_inside} places in the circle).`;
      } catch (e) { reachLine.textContent = ""; }
    };
    let circleTimer = null;
    f.circleQ.addEventListener("input", () => {
      clearTimeout(circleTimer);
      circleTimer = setTimeout(async () => {
        const q = f.circleQ.value.trim();
        if (q.length < 2) return;
        const cc = picked(f.countries)[0] || "";
        const r = await api("GET", `/admin/places/search?q=${encodeURIComponent(q)}${cc ? "&country=" + cc : ""}`).catch(() => ({ items: [] }));
        const keep = f.circlePick.value;
        f.circlePick.replaceChildren(el("option", { value: "", text: "No circle (whole cities or countries)" }),
          ...r.items.filter((p) => !p.hidden).map((p) => { circleLabel[p.id] = p.label; return el("option", { value: p.id, text: p.label, selected: p.id === keep }); }));
      }, 250);
    });
    f.circlePick.addEventListener("change", showReach);
    f.circleKm.addEventListener("change", showReach);
    const preview = el("img", { style: "max-width: 160px; max-height: 120px; border-radius: 8px; display: none; margin-top: 6px;", alt: "" });
    const picked = (sel) => [...sel.selectedOptions].map((o) => o.value);
    const fillCities = (keep) => {
      const cs = picked(f.countries);
      f.cities.replaceChildren(...opt.cities.filter((c) => cs.includes(c.country))
        .map((c) => el("option", { value: c.id, text: c.name, selected: (keep || []).includes(c.id) })));
    };
    f.countries.addEventListener("change", () => fillCities(picked(f.cities)));
    f.button.addEventListener("change", () => { f.button_value.placeholder = f.button.value === "link" ? "https://..." : "+256772123456"; });
    f.file.addEventListener("change", async () => {
      const file = f.file.files && f.file.files[0];
      if (!file) return;
      const revision = ++uploadRevision;
      image = null;
      preview.style.display = "none";
      picturePending = true; refreshSave();
      const form = new FormData();
      form.append("file", file);
      try {
        const res = await fetch(API + "/admin/flyers/image", { method: "POST", headers: { Authorization: "Bearer " + token }, body: form });
        const data = await res.json().catch(() => null);
        if (!res.ok) throw new Error((data && data.detail) || "Upload failed");
        if (revision !== uploadRevision) return;
        image = data;
        preview.src = safeUrl(data.image_small) || "";
        preview.style.display = "block";
        toast("Picture ready");
      } catch (e) { if (revision === uploadRevision) toast(e.message, true); }
      finally { if (revision === uploadRevision) { picturePending = false; refreshSave(); } }
    });
    const fill = (x) => {
      ++uploadRevision; ++motionRevision;
      picturePending = motionPending = false; motionInput.value = ""; motion = x?.motion || null; showMotion();
      f.file.value = "";
      save.disabled = false;
      editing = x ? x.id : null;
      image = x ? { image: x.image, image_small: x.image_small } : null;
      f.business.value = x ? x.business : ""; f.line.value = x ? x.line || "" : "";
      f.category.value = x ? x.category : "food"; f.button.value = x ? x.button : "whatsapp";
      f.button_value.value = x ? x.button_value : "";
      [...f.countries.options].forEach((o) => { o.selected = !!x && x.countries.includes(o.value); });
      fillCities(x ? x.cities : []);
      f.starts.value = x ? local(new Date(x.starts_at + "Z")) : local(new Date());
      f.ends.value = x ? local(new Date(x.ends_at + "Z")) : local(new Date(Date.now() + 14 * 864e5));
      f.cap.value = x && x.daily_cap ? x.daily_cap : "";
      f.testers.value = x && x.testers ? x.testers.join(", ") : "";
      [...f.topics.options].forEach(o => { o.selected = (x?.clip_topics || []).includes(o.value); });
      f.allViewers.checked = !!x?.all_viewers;
      f.testersOnly.checked = !!(x && x.testers_only);
      f.active.checked = x ? x.active : true;
      const c = x && x.circle;
      f.circleQ.value = "";
      f.circlePick.replaceChildren(el("option", { value: "", text: "No circle (whole cities or countries)" }),
        ...(c ? [el("option", { value: c.place_id, text: c.label, selected: true })] : []));
      if (c) { circleLabel[c.place_id] = c.label; f.circleKm.value = String(c.km); }
      showReach();
      preview.src = image ? (safeUrl(image.image_small) || safeUrl(image.image) || "") : "";
      preview.style.display = image ? "block" : "none";
      save.textContent = x ? "Save changes" : "Create flyer";
    };
    const save = el("button", { class: "btn", text: "Create flyer", onclick: async (e) => {
      if (picturePending || motionPending) return;
      const s = new Date(f.starts.value), en = new Date(f.ends.value);
      if (isNaN(s) || isNaN(en)) return toast("Pick a start and end", true);
      if (!image) return toast("Upload the flyer picture first", true);
      const body = { business: f.business.value.trim(), line: f.line.value.trim(), category: f.category.value,
        countries: picked(f.countries), cities: picked(f.cities), motion, image: image.image, image_small: image.image_small || null,
        button: f.button.value, button_value: f.button_value.value.trim(), starts_at: s.toISOString(), ends_at: en.toISOString(),
        daily_cap: f.cap.value ? Number(f.cap.value) : null, active: f.active.checked,
        testers: f.testers.value.split(/[\s,]+/).map((t) => t.replace(/^@/, "").trim()).filter(Boolean),
        testers_only: f.testersOnly.checked, all_viewers: f.allViewers.checked, clip_topics: picked(f.topics),
        circle: f.circlePick.value ? { place_id: f.circlePick.value, km: Number(f.circleKm.value) } : null };
      const circleWhere = body.circle ? ` within ${f.circleKm.selectedOptions[0].text} of ${circleLabel[body.circle.place_id] || body.circle.place_id}` : "";
      const where = body.testers_only ? `only the testers: ${body.testers.join(", ")}` : body.cities.length ? body.cities.map((c) => (opt.cities.find((x) => x.id === c) || { name: c }).name).join(", ")
        : body.countries.map((c) => (opt.countries.find((x) => x.id === c) || { name: c }).name).join(", ") + (circleWhere ? "" : " (whole country)");
      const whereAll = body.all_viewers ? "All viewers, including guests and all ages" : body.testers_only ? where : where + circleWhere;
      if (body.active && !(await confirmBox(editing ? "Save this flyer?" : "Start this flyer?",
        `${body.business}\nShown in: ${whereAll}\n${when(body.starts_at)} to ${when(body.ends_at)}\nOnly while the Local flyers switch is on.`, editing ? "Save" : "Start"))) return;
      const call = editing ? api("PUT", `/admin/flyers/${encodeURIComponent(editing)}`, body) : api("POST", "/admin/flyers", body);
      if (await act(e.target, () => call, editing ? "Saved" : "Flyer created")) RENDER.flyers(main);
    }});
    const pct = (x) => (x === null || x === undefined ? "–" : `${(x * 100).toFixed(1)}%`);
    const placeName = (id) => (opt.cities.find((c) => c.id === id) || opt.countries.find((c) => c.id === id) || { name: id }).name;
    const showReport = async (x) => {
      const rep = await act(null, () => api("GET", `/admin/flyers/${encodeURIComponent(x.id)}/report`));
      if (!rep) return;
      const t = (rows, key, label) => el("table", {}, el("thead", {}, el("tr", {}, ...[label, "Seen", "People", "Tapped", "Closed"].map((h) => el("th", { text: h })))),
        el("tbody", {}, ...rows.map((d) => el("tr", {}, el("td", { text: key === "place" ? placeName(d.place) : d[key] }), el("td", { text: num(d.shown) }), el("td", { text: num(d.reach) }),
          el("td", { text: num(d.tap) }), el("td", { text: num(d.close) })))));
      const tot = rep.days.reduce((a, d) => ({ shown: a.shown + d.shown, reach: a.reach + d.reach, tap: a.tap + d.tap }), { shown: 0, reach: 0, tap: 0 });
      main.replaceChildren(...head(`${x.business}: last 30 days`, `Seen ${num(tot.shown)} times, ${num(tot.tap)} taps on ${x.button === "whatsapp" ? "WhatsApp" : x.button === "call" ? "Call" : "Visit"}. "People" counts each person once a day, so a regular viewer counts once for each day they saw it. Copy these numbers for the business.`),
        el("button", { class: "btn secondary", text: "Back to flyers", onclick: () => RENDER.flyers(main) }),
        el("div", { class: "two" }, el("div", { class: "card" }, el("h3", { text: "By place" }), rep.places.length ? t(rep.places, "place", "City / country") : el("div", { class: "empty", text: "Not shown yet." })),
          el("div", { class: "card" }, el("h3", { text: "By day" }), rep.days.length ? t(rep.days, "day", "Day") : el("div", { class: "empty", text: "Not shown yet." }))));
    };
    main.replaceChildren(...head("Local flyers", "Businesses' flyers in the Wano TV ticket's spot on shorts, marked Sponsored, with a WhatsApp, Call or Visit button. Standard flyers use the countries and cities you pick and exclude under-18s. All-viewer flyers also reach guests and all ages worldwide; drinks only to people whose birthday shows 18+. Turn on the Local flyers switch for any to show."),
      el("div", { class: "two" },
        el("div", { class: "card" }, el("h3", { text: "Make a flyer" }),
          el("label", { class: "field" }, el("span", { text: "Business name (40)" }), f.business),
          el("label", { class: "field" }, el("span", { text: "One line (80)" }), f.line),
          el("label", { class: "field" }, el("span", { text: "Picture (JPG, PNG or WebP, square or wide works best)" }), f.file, preview),
          el("label", { class: "field" }, el("span", { text: "Optional silent motion strip (MP4, first 10 seconds, 3:1 recommended)" }), motionInput, motionPreview),
          el("button", { class: "btn secondary", text: "Remove motion strip", onclick: clearMotion }),
          el("label", { class: "field" }, el("span", { text: "Advertiser category" }), f.category),
          el("label", { class: "field" }, el("span", { text: "Show on clips about (empty = all topics)" }), f.topics),
          el("label", { class: "row" }, f.allViewers, el("span", { text: "All viewers worldwide, including guests and under-18s" })),
          el("div", { class: "two", style: "gap: 10px;" },
            el("label", { class: "field" }, el("span", { text: "Countries (Ctrl/Cmd-click for more)" }), f.countries),
            el("label", { class: "field" }, el("span", { text: "Cities (none = whole country)" }), f.cities)),
          el("label", { class: "field" }, el("span", { text: "Circle round a place (optional, needs the Places switch): only people who picked an area inside" }), f.circleQ),
          el("div", { class: "two", style: "gap: 10px;" }, f.circlePick, f.circleKm),
          reachLine,
          el("div", { class: "two", style: "gap: 10px;" },
            el("label", { class: "field" }, el("span", { text: "Button" }), f.button),
            el("label", { class: "field" }, el("span", { text: "Number or link" }), f.button_value)),
          el("div", { class: "two", style: "gap: 10px;" },
            el("label", { class: "field" }, el("span", { text: "Starts" }), f.starts),
            el("label", { class: "field" }, el("span", { text: "Ends" }), f.ends)),
          el("label", { class: "field" }, el("span", { text: "Views per day (optional)" }), f.cap),
          el("label", { class: "field" }, el("span", { text: "Testers: usernames who see it anywhere, first (optional, up to 10)" }), f.testers),
          el("label", { class: "row", style: "gap: 8px; margin: 4px 0;" }, f.testersOnly, el("span", { text: "Testers only (a preview: nobody else sees it)" })),
          el("label", { class: "row", style: "gap: 8px; margin: 8px 0;" }, f.active, el("span", { text: "On" })),
          el("div", { class: "row", style: "gap: 8px;" }, save, el("button", { class: "btn secondary", text: "Clear", onclick: () => fill(null) }))),
        el("div", { class: "card" }, el("h3", { text: "All flyers (last 30 days)" }), r.items.length ? el("table", {}, el("thead", {}, el("tr", {},
            ...["", "Seen", "People", "Tapped", "Tap rate", ""].map((h) => el("th", { text: h })))),
          el("tbody", {}, ...r.items.map((x) => el("tr", {},
            el("td", {}, el("div", { style: "font-weight: 600;", text: x.business }), el("div", { class: "small muted", text: x.line || "" }),
              el("div", { class: "row small", style: "margin-top: 4px; gap: 6px;" },
                el("span", { class: "pill " + (x.active ? "green" : "grey"), text: x.active ? "On" : "Off" }),
                x.testers_only ? el("span", { class: "pill", text: "Testers only" }) : null,
                el("span", { class: "muted", text: (x.all_viewers ? "All viewers worldwide" : (x.cities.length ? x.cities : x.countries).map(placeName).join(", ")) + (x.clip_topics?.length ? " · " + x.clip_topics.map(k => opt.clip_topics?.find(t => t.id === k)?.name || k).join(", ") : "") + (x.circle ? ` · ${x.circle.km} km round ${x.circle.label}` : "") + " · until " + when(x.ends_at) }))),
            el("td", { text: num(x.stats.shown) }), el("td", { text: num(x.stats.reach) }), el("td", { text: num(x.stats.tap) }),
            el("td", { text: pct(x.stats.tap_rate) }),
            el("td", { style: "text-align: right; white-space: nowrap;" },
              el("button", { class: "btn small secondary", text: "Numbers", onclick: () => showReport(x) }), " ",
              el("button", { class: "btn small secondary", text: "Edit", onclick: () => { fill(x); f.business.focus(); } }), " ",
              el("button", { class: "btn small secondary", text: x.active ? "Pause" : "Resume", onclick: async (e) => {
                if (await act(e.target, () => api("POST", `/admin/flyers/${encodeURIComponent(x.id)}/active`, { active: !x.active }), x.active ? "Paused" : "Resumed")) RENDER.flyers(main);
              } }))))))
          : el("div", { class: "empty", text: "No flyers yet." }))));
  },

  async whatsnew(main) {
    const r = await api("GET", "/admin/announcements");
    const keys = Object.keys(FEATURES);
    let editing = null;
    const f = {
      title: el("input", { type: "text", maxlength: 60, placeholder: "New: quick posts on your photo" }),
      body: el("textarea", { rows: 3, maxlength: 180, placeholder: "One or two short sentences." }),
      button: el("input", { type: "text", maxlength: 24, placeholder: "Make one (optional)" }),
      action: el("select", {}, ...r.actions.map((a) => el("option", { value: a, text: ACTION_TEXT[a] || a }))),
      feature: el("select", {}, el("option", { value: "", text: "Always (no switch)" }),
        ...keys.map((k) => el("option", { value: k, text: (FEATURES[k] || [k])[0] }))),
      min_app: el("input", { type: "text", maxlength: 10, placeholder: "2.3.28" }),
      priority: el("input", { type: "number", min: 0, max: 100, value: 0 }),
      active: el("input", { type: "checkbox" }),
    };
    const fill = (a) => {
      editing = a ? a.id : null;
      f.title.value = a ? a.title : ""; f.body.value = a ? a.body : ""; f.button.value = a ? a.button : "";
      f.action.value = a ? a.action : "none"; f.feature.value = a && a.feature ? a.feature : "";
      f.min_app.value = a && a.min_app ? a.min_app : ""; f.priority.value = a ? a.priority : 0; f.active.checked = !!(a && a.active);
      save.textContent = a ? "Save changes" : "Add announcement";
    };
    const save = el("button", { class: "btn", text: "Add announcement", onclick: async (e) => {
      const body = { title: f.title.value.trim(), body: f.body.value.trim(), button: f.button.value.trim(), action: f.action.value,
        feature: f.feature.value || null, min_app: f.min_app.value.trim() || null, priority: Number(f.priority.value) || 0,
        active: f.active.checked };
      if (body.active && !(await confirmBox("Send this out?", `"${body.title}"\nPeople see it the next time they open the app (one a day at most, once each)${body.feature ? ", while " + ((FEATURES[body.feature] || [body.feature])[0]) + " is on" : ""}.`, "Send"))) return;
      const call = editing ? api("PUT", `/admin/announcements/${encodeURIComponent(editing)}`, body) : api("POST", "/admin/announcements", body);
      if (await act(e.target, () => call, editing ? "Saved" : "Added")) RENDER.whatsnew(main);
    }});
    const pct = (x) => (x === null || x === undefined ? "–" : `${Math.round(x * 100)}%`);
    main.replaceChildren(...head("What's new", "Short announcements in the app. One a day at most, each once per person, never to brand-new accounts, only while its switch is on and on apps new enough. Turn on the What's new switch for any of them to go out."),
      el("div", { class: "two" },
        el("div", { class: "card" }, el("h3", { text: "Write one" }),
          el("label", { class: "field" }, el("span", { text: "Title (60)" }), f.title),
          el("label", { class: "field" }, el("span", { text: "Message (180)" }), f.body),
          el("div", { class: "two", style: "gap: 10px;" },
            el("label", { class: "field" }, el("span", { text: "Button text" }), f.button),
            el("label", { class: "field" }, el("span", { text: "Button opens" }), f.action)),
          el("div", { class: "two", style: "gap: 10px;" },
            el("label", { class: "field" }, el("span", { text: "Only while this is on" }), f.feature),
            el("label", { class: "field" }, el("span", { text: "From app version" }), f.min_app)),
          el("label", { class: "field" }, el("span", { text: "Priority (higher goes first)" }), f.priority),
          el("label", { class: "row", style: "gap: 8px; margin: 8px 0;" }, f.active, el("span", { text: "On (sends it out)" })),
          el("div", { class: "row", style: "gap: 8px;" }, save,
            el("button", { class: "btn secondary", text: "Clear", onclick: () => fill(null) }),
            el("button", { class: "btn secondary", text: "Add Claude's 3 drafts", onclick: async (e) => {
              if (await act(e.target, () => api("POST", "/admin/announcements/suggested"), "Drafts added (all off)")) RENDER.whatsnew(main);
            } }))),
        el("div", { class: "card" }, el("h3", { text: "All announcements" }), r.items.length ? el("table", {}, el("thead", {}, el("tr", {},
            ...["", "Shown", "Tapped", "Closed", "Tap rate", ""].map((h) => el("th", { text: h })))),
          el("tbody", {}, ...r.items.map((a) => el("tr", {},
            el("td", {}, el("div", { style: "font-weight: 600;", text: a.title }), el("div", { class: "small muted", text: a.body }),
              el("div", { class: "row small", style: "margin-top: 4px; gap: 6px;" },
                el("span", { class: "pill " + (a.active ? "green" : "grey"), text: a.active ? "On" : "Off" }),
                a.feature ? el("span", { class: "muted", text: "while " + ((FEATURES[a.feature] || [a.feature])[0]) }) : null,
                a.min_app ? el("span", { class: "muted", text: "app " + a.min_app + "+" }) : null)),
            el("td", { text: num(a.shown) }), el("td", { text: num(a.tap) }), el("td", { text: num(a.close) }),
            el("td", { text: pct(a.tap_rate) }),
            el("td", { style: "text-align: right;" }, el("button", { class: "btn small secondary", text: "Edit", onclick: () => { fill(a); f.title.focus(); } }))))))
          : el("div", { class: "empty", text: "None yet. \"Add Claude's 3 drafts\" gives you three to edit." }))));
  },

  async challenges(main) {
    const rows = await api("GET", "/admin/challenges");
    const now = new Date();
    const local = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    const f = {
      hashtag: el("input", { type: "text", placeholder: "#danceug", maxlength: 41 }),
      title: el("input", { type: "text", placeholder: "Dance challenge", maxlength: 80 }),
      description: el("textarea", { rows: 2, maxlength: 500, placeholder: "What to post (optional)" }),
      starts: el("input", { type: "datetime-local", value: local(now) }),
      ends: el("input", { type: "datetime-local", value: local(new Date(now.getTime() + 7 * 864e5)) }),
      prize: el("input", { type: "number", min: 0, max: 100000, value: 0 }),
    };
    const create = el("button", { class: "btn", text: "Create challenge", onclick: async (e) => {
      const s = new Date(f.starts.value), en = new Date(f.ends.value);
      if (isNaN(s) || isNaN(en)) return toast("Pick a start and end", true);
      const body = { hashtag: f.hashtag.value.trim(), title: f.title.value.trim(), description: f.description.value.trim(),
        starts_at: s.toISOString(), ends_at: en.toISOString(), prize_points: Number(f.prize.value) || 0 };
      const ok = await confirmBox("Create this challenge?", `${body.title} (#${body.hashtag.replace(/^#/, "")})\n${when(body.starts_at)} to ${when(body.ends_at)}\nPeople see it in the app straight away once Challenges is switched on.`, "Create");
      if (!ok) return;
      if (await act(e.target, () => api("POST", "/challenges", body), "Challenge created")) RENDER.challenges(main);
    }});
    const pill = { live: "green", upcoming: "amber", ended: "grey" };
    main.replaceChildren(...head("Challenges", "Hashtag challenges, up to 14 days each. Posts with the hashtag join automatically."),
      el("div", { class: "two" },
        el("div", { class: "card" }, el("h3", { text: "New challenge" }),
          el("label", { class: "field" }, el("span", { text: "Hashtag" }), f.hashtag),
          el("label", { class: "field" }, el("span", { text: "Title" }), f.title),
          el("label", { class: "field" }, el("span", { text: "Description" }), f.description),
          el("div", { class: "two", style: "gap: 10px;" },
            el("label", { class: "field" }, el("span", { text: "Starts" }), f.starts),
            el("label", { class: "field" }, el("span", { text: "Ends" }), f.ends)),
          el("label", { class: "field" }, el("span", { text: "Prize points for the winner (optional)" }), f.prize), create),
        el("div", { class: "card" }, el("h3", { text: "All challenges" }), rows.length ? el("table", {}, el("tbody", {}, ...rows.map((c) => el("tr", {},
          el("td", {}, el("div", { style: "font-weight: 600;", text: "#" + c.hashtag }), el("div", { class: "small muted", text: c.title }),
            el("div", { class: "small muted", text: `${when(c.starts_at)} – ${when(c.ends_at)}` })),
          el("td", {}, el("span", { class: "pill " + pill[c.state], text: c.state })),
          el("td", { style: "text-align: right;" }, c.state !== "ended" ? el("button", { class: "btn ghost-danger small", text: "End now", onclick: async (e) => {
            if (!(await confirmBox(`End #${c.hashtag} now?`, "The leaderboard freezes as it is now.", "End now", true))) return;
            if (await act(e.target, () => api("POST", `/admin/challenges/${c.id}/end`), "Challenge ended")) RENDER.challenges(main);
          }}) : null))))) : el("div", { class: "empty", text: "No challenges yet." }))));
  },

  async picks(main) {
    const rows = (await api("GET", "/admin/picks")).items;
    const video = el("input", { type: "text", placeholder: "Post link or post ID", maxlength: 500 });
    const note = el("input", { type: "text", placeholder: "Why we love it (optional, shown on Discover)", maxlength: 140 });
    const add = el("button", { class: "btn", text: "Make it a Wano Pick", onclick: async (e) => {
      if (!video.value.trim()) return toast("Paste a post link or ID", true);
      const ok = await confirmBox("Feature this post?", "It shows on Discover for 7 days and the creator gets a Wano Pick badge and a notification.", "Feature");
      if (!ok) return;
      if (await act(e.target, () => api("POST", "/admin/picks", { video: video.value.trim(), note: note.value.trim() }), "Added to Wano Picks")) RENDER.picks(main);
    }});
    const checkOut = el("div", { class: "small" });
    const check = el("button", { class: "btn secondary", text: "Check this post", onclick: async (e) => {
      if (!video.value.trim()) return toast("Paste a post link or ID", true);
      let c;
      try { c = await api("GET", `/admin/picks/check?video=${encodeURIComponent(video.value.trim())}`); }
      catch (err) { return toast(err.message || "Couldn't check that post", true); }
      const pct = (x) => (x === null || x === undefined ? "–" : `${Math.round(x * 100)}%`);
      checkOut.replaceChildren(
        el("div", { style: "font-weight: 600; margin-top: 8px;", text: `@${c.creator || "?"}${c.category ? " · " + c.category : " · no category"}${c.ai_label ? " · AI label" : ""}` }),
        el("div", { text: `${num(c.views)} views · ${pct(c.watched_to_end_rate)} watched to the end (usual for them: ${pct(c.creators_usual_rate)})` }),
        el("div", { text: `${num(c.likes)} likes · ${num(c.comments)} comments · ${num(c.shares)} shares · ${num(c.reports)} reports` }),
        ...c.warnings.map((w) => el("div", { style: "color: #B42318; font-weight: 600;", text: "⚠ " + w })));
    }});
    const who = el("input", { type: "text", placeholder: "@username or post link", maxlength: 200 });
    const tv = el("button", { class: "btn secondary", text: "Mark as featured on Wano TV", onclick: async (e) => {
      if (!who.value.trim()) return toast("Type a username", true);
      const ok = await confirmBox("Mark as featured on Wano TV?", "They get the final step of the Road to Wano TV and a notification. 18+ only. You can take the step back, but not the notification.", "Mark");
      if (!ok) return;
      await act(e.target, () => api("POST", "/admin/wano-tv", { creator: who.value.trim() }), "Marked as featured on Wano TV");
    }});
    const tvUndo = el("button", { class: "btn secondary", text: "Undo", onclick: async (e) => {
      if (!who.value.trim()) return toast("Type a username", true);
      const ok = await confirmBox("Take back 'On Wano TV'?", "The step goes back to not done. A notification already sent can't be recalled.", "Take back");
      if (!ok) return;
      await act(e.target, () => api("POST", "/admin/wano-tv/undo", { creator: who.value.trim() }), "Taken back");
    }});
    main.replaceChildren(...head("Wano Picks", "Posts the Wano team features on Discover. Each stays 7 days."),
      el("div", { class: "two" },
        el("div", { class: "card" }, el("h3", { text: "New pick" }),
          el("label", { class: "field" }, el("span", { text: "Post" }), video),
          el("label", { class: "field" }, el("span", { text: "Note" }), note), el("div", { class: "row" }, check, add), checkOut,
          el("h3", { text: "Road to Wano TV", style: "margin-top: 24px;" }),
          el("p", { class: "small muted", text: "The last step on a creator's ladder: featured on Wano TV. Creators credited on a live Wano TV title get it by themselves." }),
          el("label", { class: "field" }, el("span", { text: "Creator" }), who), el("div", { class: "row" }, tv, tvUndo)),
        el("div", { class: "card" }, el("h3", { text: "Live picks" }), rows.length ? el("table", {}, el("tbody", {}, ...rows.map((p) => el("tr", {},
          el("td", {}, el("div", { style: "font-weight: 600;", text: "@" + (p.creator.username || "") }),
            el("div", { class: "small muted", text: p.note || p.title || "" }),
            el("div", { class: "small muted", text: "until " + when(p.until) })),
          el("td", { style: "text-align: right;" }, el("button", { class: "btn ghost-danger small", text: "Remove", onclick: async (e) => {
            if (!(await confirmBox("Remove this pick?", "It leaves Discover now. The creator keeps the badge.", "Remove", true))) return;
            if (await act(e.target, () => api("DELETE", `/admin/picks/${p.id}`), "Pick removed")) RENDER.picks(main);
          }})))))) : el("div", { class: "empty", text: "No picks yet." }))));
  },

  async studio(main) {
    const [accts, pend, done] = await Promise.all([api("GET", "/admin/studio/accounts"),
      api("GET", "/admin/studio/drafts?state=pending"), api("GET", "/admin/studio/drafts")]);
    counts.studio = pend.items.length; drawNav();
    const active = accts.items.filter((a) => a.active);
    const acct = el("select", {}, ...active.map((a) => el("option", { value: a.id, text: "@" + a.username })));
    const file = el("input", { type: "file", accept: "video/mp4,video/quicktime,video/*", id: "studio-file" });
    const title = el("input", { type: "text", maxlength: 100, placeholder: "Title" });
    const caption = el("textarea", { rows: 4, maxlength: 2200, placeholder: "Caption with #hashtags" });
    const ai = el("input", { type: "checkbox", id: "studio-ai" });
    const local = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    const when = el("input", { type: "datetime-local", value: local(new Date(Date.now() + 864e5)) });
    const progress = el("div", { class: "small muted", id: "studio-progress" });
    const save = el("button", { class: "btn", id: "studio-save", text: "Upload and send for review", onclick: async (e) => {
      const f = file.files && file.files[0];
      if (!acct.value) return toast("Add a house account first", true);
      if (!f) return toast("Choose a video file", true);
      if (f.size > 190 * 1024 * 1024) return toast("Video is over 190 MB", true);
      e.target.disabled = true;
      try {
        progress.textContent = "Getting an upload link…";
        const up = await api("POST", "/admin/studio/upload-url", { filename: f.name });
        progress.textContent = "Uploading " + Math.round(f.size / 1048576) + " MB to Cloudflare…";
        const fd = new FormData(); fd.append("file", f);
        const r = await fetch(up.upload_url, { method: "POST", body: fd });
        if (!r.ok) throw new Error("Upload to Cloudflare failed (" + r.status + ")");
        const at = new Date(when.value);
        await api("POST", "/admin/studio/drafts", { account_id: acct.value, stream_uid: up.stream_uid,
          title: title.value.trim(), caption: caption.value, ai: ai.checked, scheduled_for: isNaN(at) ? null : at.toISOString() });
        progress.textContent = "";
        toast("Draft saved: waiting for approval");
        RENDER.studio(main);
      } catch (err) { progress.textContent = ""; toast(err.message, true); }
      finally { e.target.disabled = false; }
    }});
    const who = el("input", { type: "text", placeholder: "@username", maxlength: 200 });
    const addAcct = el("button", { class: "btn secondary small", text: "Make house account", onclick: async (e) => {
      if (!who.value.trim()) return;
      const ok = await confirmBox("Make this a Wano house account?", "Studio posts can then be published on it after approval.", "Confirm");
      if (ok && await act(e.target, () => api("POST", "/admin/studio/accounts", { creator: who.value.trim(), on: true }), "House account added")) RENDER.studio(main);
    }});
    const card = (d) => el("div", { class: "card", "data-draft": d.id },
      el("div", { class: "row space" }, el("div", {}, el("div", { style: "font-weight: 700;", text: "@" + (d.account.username || "") + " · " + (d.title || "(no title)") }),
        el("div", { class: "small muted", text: (d.scheduled_for ? "for " + when_(d.scheduled_for) : "post now") + (d.ai ? " · Made with AI" : "") })),
        el("a", { class: "btn secondary small", href: "https://watch.videodelivery.net/" + d.stream_uid, target: "_blank", rel: "noopener", text: "Watch" })),
      el("p", { class: "small", style: "white-space: pre-wrap;", text: d.caption || "" }),
      el("div", { class: "row" },
        el("button", { class: "btn small", text: "Approve", onclick: async (e) => {
          const ok = await confirmBox("Approve this post?", "It goes live on @" + d.account.username + (d.scheduled_for ? " at " + when_(d.scheduled_for) : " now") + ".", "Approve");
          if (ok && await act(e.target, () => api("POST", `/admin/studio/drafts/${d.id}/approve`, {}), "Approved")) RENDER.studio(main);
        }}),
        el("button", { class: "btn ghost-danger small", text: "Reject", onclick: async (e) => {
          const note = window.prompt ? (window.prompt("Why? (optional)") || "") : "";
          if (await act(e.target, () => api("POST", `/admin/studio/drafts/${d.id}/reject`, { note }), "Rejected")) RENDER.studio(main);
        }})));
    const when_ = (v) => { try { return new Date(v + (String(v).endsWith("Z") ? "" : "Z")).toLocaleString(); } catch (e) { return v; } };
    const history = done.items.filter((d) => d.state !== "pending").slice(0, 30);
    main.replaceChildren(...head("Studio", "Posts for Wano's own accounts. Nothing goes live until you approve it."),
      el("div", { class: "two" },
        el("div", { class: "card" }, el("h3", { text: "New post" }),
          active.length ? null : el("p", { class: "small muted", text: "No house accounts yet. Add one below." }),
          el("label", { class: "field" }, el("span", { text: "Account" }), acct),
          el("label", { class: "field" }, el("span", { text: "Video" }), file),
          el("label", { class: "field" }, el("span", { text: "Title" }), title),
          el("label", { class: "field" }, el("span", { text: "Caption" }), caption),
          el("label", { class: "field" }, el("span", { text: "Goes live at" }), when),
          el("label", { class: "row", style: "gap: 8px; margin: 8px 0;" }, ai, el("span", { text: "Made with AI (AI voice or visuals)" })),
          save, progress,
          el("h3", { text: "House accounts", style: "margin-top: 24px;" }),
          el("div", { class: "small", text: accts.items.map((a) => "@" + a.username).join(", ") || "None" }),
          el("div", { class: "row", style: "gap: 8px; margin-top: 8px;" }, who, addAcct)),
        el("div", {}, el("h3", { text: `Waiting for approval (${pend.items.length})` }),
          ...(pend.items.length ? pend.items.map(card) : [el("div", { class: "empty", text: "Nothing waiting." })]),
          el("h3", { text: "Recent", style: "margin-top: 16px;" }),
          ...(history.length ? history.map((d) => el("div", { class: "small", style: "padding: 6px 0;",
            text: `${d.state === "approved" ? "✓" : "✕"} @${d.account.username} · ${d.title || "(no title)"}${d.note ? " — " + d.note : ""}` })) : [el("div", { class: "small muted", text: "No history yet." })]))));
  },

  async films(main, filter) {
    const LABEL = { new: "New", reviewing: "Under review", shortlisted: "Shortlisted", selected: "Selected", declined: "Declined", licensed: "Licensed", published: "Published" };
    const r = await api("GET", "/admin/film-submissions" + (filter ? `?status=${filter}` : ""));
    counts.films = r.counts.new || 0; drawNav();
    const tabs = el("div", { class: "tabs" },
      el("button", { class: filter ? "" : "on", text: "All", onclick: () => RENDER.films(main) }),
      ...r.statuses.map((s) => el("button", { class: filter === s ? "on" : "", text: `${LABEL[s]} (${num(r.counts[s])})`, onclick: () => RENDER.films(main, s) })));
    const detail = el("div");
    const open = async (ref) => {
      const f = await api("GET", `/admin/film-submissions/${encodeURIComponent(ref)}`);
      const pick = el("select", {}, ...r.statuses.map((s) => el("option", { value: s, text: LABEL[s], selected: s === f.status })));
      const note = el("textarea", { rows: 3, placeholder: "Team notes (only the team sees these)" }); note.value = f.note || "";
      const line = (k, v) => v ? el("div", { style: "margin: 4px 0;" }, el("span", { class: "muted", text: k + ": " }), v) : null;
      const link = (u) => safeUrl(u) ? el("a", { href: u, target: "_blank", rel: "noopener", text: u }) : u;
      detail.replaceChildren(el("div", { class: "card" },
        el("h3", { text: `${f.film_title} · ${f.ref}` }),
        el("div", { class: "two" },
          el("div", {},
            line("Filmmaker", `${f.full_name} · ${f.email}${f.phone ? " · " + f.phone : ""}`),
            line("From", [f.city, f.country].filter(Boolean).join(", ")),
            line("Company", f.production_company),
            line("Film", `${f.category} · ${f.runtime} · ${f.year_completed} · represents ${f.country_represented}`),
            line("Languages", `${f.languages} · subtitles: ${f.subtitles}`),
            line("Watch", link(f.viewing_link)), line("Password", f.viewing_password), line("Trailer", f.trailer_link ? link(f.trailer_link) : null),
            line("Sent", when(f.created_at)),
            el("p", { style: "white-space: pre-wrap; margin-top: 10px;", text: f.synopsis })),
          f.poster ? el("img", { src: f.poster, alt: "Poster", style: "max-width: 100%; max-height: 420px; border-radius: 10px;" }) : el("div", { class: "empty", text: "No poster" })),
        el("div", { class: "row", style: "gap: 8px; margin-top: 12px; align-items: flex-start;" }, pick, note),
        el("button", { class: "btn", style: "margin-top: 8px;", text: "Save", onclick: async (e) => {
          if (await act(e.target, () => api("POST", `/admin/film-submissions/${encodeURIComponent(f.ref)}/status`, { status: pick.value, note: note.value }), "Saved")) RENDER.films(main, filter);
        } }),
        (f.history || []).length ? el("div", { class: "small muted", style: "margin-top: 8px;", text: f.history.map((h) => `${LABEL[h.status] || h.status} by ${h.by} ${String(h.at).slice(0, 16).replace("T", " ")}`).join(" · ") }) : null));
      detail.scrollIntoView({ behavior: "smooth" });
    };
    const table = r.rows.length ? el("table", {}, el("thead", {}, el("tr", {}, ...["Sent", "Film", "Filmmaker", "Category", "Status", ""].map((h) => el("th", { text: h })))),
      el("tbody", {}, ...r.rows.map((f) => el("tr", {}, el("td", { text: String(f.created_at || "").slice(0, 10) }),
        el("td", {}, el("div", { style: "font-weight: 600;", text: f.film_title }), el("div", { class: "small muted", text: f.ref })),
        el("td", { text: `${f.full_name} · ${f.country}` }), el("td", { text: `${f.category} · ${f.runtime}` }),
        el("td", { text: LABEL[f.status] || f.status }),
        el("td", {}, el("button", { class: "btn small secondary", text: "Open", onclick: () => open(f.ref) }))))))
      : el("div", { class: "empty", text: "No submissions here yet." });
    main.replaceChildren(...head("Film submissions", "Films sent from wanoafrica.com/tv/submit. Every submission is saved here, even if the email to the team fails. Move each one along: New → Under review → Shortlisted → Selected → Licensed → Published (or Declined)."),
      tabs, el("div", { class: "card" }, table), detail);
  },

  async tvtitles(main, openId) {
    const r = await api("GET", "/admin/tv/titles");
    const STATUS = { live: "Live", hidden: "Hidden", coming_soon: "Coming soon" };
    const detail = el("div");
    const table = r.items.length ? el("table", {}, el("thead", {}, el("tr", {}, ...["", "Title", "Status", "Film", "Subtitles", "Ready?", ""].map((h) => el("th", { text: h })))),
      el("tbody", {}, ...r.items.map((t) => el("tr", {},
        el("td", {}, safeUrl(t.poster) ? el("img", { src: t.poster, alt: "", style: "width: 40px; height: 60px; object-fit: cover; border-radius: 4px;" }) : null),
        el("td", {}, el("div", { style: "font-weight: 600;", text: t.title }), el("div", { class: "small muted", text: `${t.type === "series" ? `Series · ${t.episodes} episodes` : "Film"} · ${t.age_rating || ""}` })),
        el("td", { text: STATUS[t.status] + (t.status === "coming_soon" && t.coming_at ? ` (${t.coming_at})` : "") }),
        el("td", { text: t.film === "cloudflare" ? "Cloudflare" : t.film === "spaces" ? "Spaces" : (t.episodes ? "In episodes" : "—") }),
        el("td", { text: String(t.captions) }),
        el("td", { class: "small", text: tvWaitingForPremiere(t) ? "Scheduled" : t.problems.length ? `${t.problems.length} to fix` : "✓" }),
        el("td", {}, el("button", { class: "btn small secondary", text: "Open", onclick: () => tvEditor(main, detail, t.id) }))))))
      : el("div", { class: "empty", text: "No titles yet." });
    main.replaceChildren(...head("Wano TV titles", "Add and edit what's on Wano TV. A new title starts Hidden; it can only go Live once everything the app needs is there. Films can be uploaded straight to Cloudflare from here, or keep using a film already on Spaces by pasting its master.m3u8 link."),
      el("div", { class: "row", style: "gap: 8px; margin-bottom: 12px;" }, el("button", { class: "btn", text: "New title", onclick: () => tvEditor(main, detail, null) })),
      el("div", { class: "card" }, table), detail);
    if (openId) tvEditor(main, detail, openId);
  },

  async tvnumbers(main, days) {
    days = days || 30;
    const r = await api("GET", `/admin/tv/stats?days=${days}`);
    const playback = await api("GET", `/admin/tv/playback-stats?days=${days}`);
    const tabs = el("div", { class: "tabs" }, ...[7, 30, 60].map((d) => el("button", { class: d === days ? "on" : "", text: `${d} days`, onclick: () => RENDER.tvnumbers(main, d) })));
    const max = Math.max(1, ...r.by_day.map((d) => d.plays));
    const bars = el("div", { style: "display: flex; align-items: flex-end; gap: 2px; height: 90px; margin: 8px 0;" },
      ...r.by_day.map((d) => el("div", { title: `${d.day}: ${d.plays} plays`, style: `flex: 1; background: var(--primary); opacity: .8; border-radius: 2px 2px 0 0; height: ${Math.max(2, Math.round(d.plays / max * 88))}px;` })));
    const H = ["Title", "Plays", "Started", "Finished", "Avg watched", "My List", "Hearts", "Reminders", "Downloads", "Strip shown → taps"];
    const playbackTable = el("div", { class: "card" }, el("h3", {text: "Playback sessions · trailers and episodes"}), el("p", {class:"small muted",text:"Sessions count actual playback. Completion and average position include signed-out devices. Only new app versions report these."}), el("table", {}, el("thead", {}, el("tr", {}, ...["Title / episode", "Starts", "Completed", "Avg reached", "Errors", "Stopped at <25 / 25–50 / 50–75 / 75–100%"].map(x => el("th",{text:x})))), el("tbody",{}, ...playback.items.map(x => el("tr", {}, el("td",{text:(r.items.find(t => t.id === x.title_id)?.title || x.title_id) + (x.trailer ? " · Trailer" : " · " + (x.episode || "Film"))}), ...[x.starts,x.completed,x.avg_pct === null ? "–" : x.avg_pct + "%",x.errors,(x.stopped_quarters || []).join(" / ")].map(v => el("td",{text:String(v)})))))));
    main.replaceChildren(...head("Wano TV numbers", "Plays count once per person per title per day (signed in or not). Started, finished and how far people get come from signed-in viewers. Plays are kept 60 days."),
      tabs, playbackTable,
      el("div", { class: "card" }, el("div", { class: "row space" }, el("h3", { text: `${num(r.total_plays)} plays` }), el("div", { class: "small muted", text: `${num(r.people_started)} people have ever started a title` })), bars),
      el("div", { class: "card" }, r.items.length ? el("table", {}, el("thead", {}, el("tr", {}, ...H.map((h) => el("th", { text: h })))),
        el("tbody", {}, ...r.items.map((x) => el("tr", {}, el("td", { text: x.title + (x.status !== "live" ? ` (${x.status.replace("_", " ")})` : "") }),
          el("td", { text: num(x.plays) }), el("td", { text: num(x.started) }), el("td", { text: num(x.finished) }),
          el("td", { text: x.avg_pct === null ? "–" : x.avg_pct + "%" }), el("td", { text: num(x.my_list) }), el("td", { text: num(x.loves) }),
          el("td", { text: num(x.reminders) }), el("td", { text: num(x.downloads) }), el("td", { text: `${num(x.strip_shown)} → ${num(x.strip_taps)}` })))))
        : el("div", { class: "empty", text: "No titles yet." })));
  },

  async siteenq(main) {
    const r = await api("GET", "/admin/site/enquiries");
    counts.siteenq = r.counts.new || 0; drawNav();
    const KIND = { advertise: "Advertising", press: "Press", partner: "Partnership" };
    main.replaceChildren(...head("Website enquiries", "Advertising, press and partnership messages sent from wanoafrica.com. Every one is saved here, even if the email to the team fails."),
      el("div", { class: "card" }, r.rows.length ? el("table", {}, el("thead", {}, el("tr", {}, ...["Sent", "Type", "From", "Message", "Status", ""].map((h) => el("th", { text: h })))),
        el("tbody", {}, ...r.rows.map((x) => el("tr", {}, el("td", { class: "small", text: when(x.created_at) }), el("td", { text: KIND[x.kind] || x.kind }),
          el("td", {}, el("div", { style: "font-weight: 600;", text: x.name + (x.company ? " · " + x.company : "") }),
            el("div", { class: "small muted", text: [x.email, x.phone, x.country, x.budget ? "Budget: " + x.budget : ""].filter(Boolean).join(" · ") })),
          el("td", { class: "small", style: "white-space: pre-wrap; max-width: 420px;", text: x.message }),
          el("td", { text: x.status }),
          el("td", {}, x.status === "new" ? el("button", { class: "btn small secondary", text: "Mark done", onclick: async (e) => {
            if (await act(e.target, () => api("POST", `/admin/site/enquiries/${x.id}/status`, { status: "done" }), "Done")) RENDER.siteenq(main);
          } }) : null))))) : el("div", { class: "empty", text: "No enquiries yet." })));
  },

  async sitemail(main) {
    const r = await api("GET", "/admin/site/newsletter");
    const dl = el("button", { class: "btn", text: "Download the list (CSV)", onclick: async (e) => {
      e.target.disabled = true;
      try {
        const res = await fetch(API + "/admin/site/newsletter.csv", { headers: { Authorization: "Bearer " + token } });
        if (!res.ok) throw new Error("Couldn't download (" + res.status + ")");
        const url = URL.createObjectURL(await res.blob());
        const a = el("a", { href: url, download: "wano-mailing-list.csv" }); document.body.append(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 5000);
      } catch (err) { toast(err.message, true); } finally { e.target.disabled = false; }
    } });
    main.replaceChildren(...head("Mailing list", "People who signed up on wanoafrica.com for news about new films on Wano TV. Each email they get must include their unsubscribe link: wanoafrica.com/unsubscribe?token=…"),
      el("div", { class: "card" }, el("div", { class: "row space" }, el("h3", { text: `${num(r.counts.subscribed)} subscribed · ${num(r.counts.unsubscribed)} unsubscribed` }), dl),
        r.rows.length ? el("table", {}, el("thead", {}, el("tr", {}, ...["Email", "Interests", "From page", "Joined", ""].map((h) => el("th", { text: h })))),
          el("tbody", {}, ...r.rows.slice(0, 200).map((x) => el("tr", {}, el("td", { text: x.id }), el("td", { text: (x.interests || []).join(", ") }),
            el("td", { text: x.source || "" }), el("td", { class: "small", text: when(x.created_at) }),
            el("td", { class: "small muted", text: x.unsubscribed_at ? "unsubscribed" : "" })))))
          : el("div", { class: "empty", text: "Nobody yet." })));
  },

  async sitedel(main) {
    const r = await api("GET", "/admin/site/deletion");
    counts.sitedel = r.counts.new || 0; drawNav();
    main.replaceChildren(...head("Account deletion requests", "From wanoafrica.com/help/delete-account, for people who can't delete their account in the app (Profile > Settings > Delete account). Before deleting, check the request comes from the account's own email or phone: reply to them first. Google Play and the App Store require this page."),
      el("div", { class: "card" }, r.rows.length ? el("table", {}, el("thead", {}, el("tr", {}, ...["Sent", "Ref", "Account", "Reply to", "Reason", "Status", ""].map((h) => el("th", { text: h })))),
        el("tbody", {}, ...r.rows.map((x) => el("tr", {}, el("td", { class: "small", text: when(x.created_at) }), el("td", { text: x.ref }),
          el("td", { text: x.account }), el("td", { text: x.email }), el("td", { class: "small", text: x.reason || "" }), el("td", { text: x.status }),
          el("td", {}, x.status === "new" ? el("div", { class: "row", style: "gap: 6px;" },
            el("button", { class: "btn small secondary", text: "Done", onclick: async (e) => { if (await act(e.target, () => api("POST", `/admin/site/deletion/${x.ref}/status`, { status: "done" }), "Marked done")) RENDER.sitedel(main); } }),
            el("button", { class: "btn small secondary", text: "Not theirs", onclick: async (e) => { if (await act(e.target, () => api("POST", `/admin/site/deletion/${x.ref}/status`, { status: "rejected" }), "Marked")) RENDER.sitedel(main); } })) : null)))))
        : el("div", { class: "empty", text: "No requests." })));
  },

  async tvstrip(main) {
    const r = await api("GET", "/admin/tv-strip?days=7");
    const pct = (x) => (x === null || x === undefined ? "–" : `${Math.round(x * 100)}%`);
    main.replaceChildren(...head("Wano TV strip", "The last 7 days: how often each film's ticket was shown, tapped (opened the film) and closed. Turn on with the Wano TV strip switch."),
      el("div", { class: "card" }, r.items.length ? el("table", {}, el("thead", {}, el("tr", {},
          ...["Film", "Shown", "Tapped", "Closed", "Tap rate", "Close rate"].map((h) => el("th", { text: h })))),
        el("tbody", {}, ...r.items.map((x) => el("tr", {}, el("td", { text: x.title }), el("td", { text: num(x.shown) }),
          el("td", { text: num(x.tap) }), el("td", { text: num(x.close) }), el("td", { text: pct(x.tap_rate) }),
          el("td", { text: pct(x.close_rate) }))))) : el("div", { class: "empty", text: "Nothing shown yet." }),
        el("div", { class: "small muted", style: "margin-top: 10px;", text: `Rules: after ${Math.round(r.rules.after_portion * 100)}% of a video, never in the first ${r.rules.min_posts} posts, ${r.rules.gap_posts}+ posts apart, at most ${r.rules.max_per_session} a session, ${r.rules.show_seconds} s on screen, none for a week after ${r.rules.closes_per_week} closes.` })),
      stripRulesCard(r.rules, () => RENDER.tvstrip(main)));
  },

  async categories(main) {
    const r = await api("GET", "/admin/category-requests");
    counts.categories = r.items.length; drawNav();
    main.replaceChildren(...head("Points categories", "What creators can pick when posting (the More categories switch shows all 16). Suggestions from creators whose content fits none:"),
      el("div", { class: "two" },
        el("div", { class: "card" }, el("h3", { text: "Suggested by creators" }), r.items.length ? el("table", {}, el("tbody", {},
          ...r.items.map((q) => el("tr", {}, el("td", {}, el("div", { style: "font-weight: 600;", text: q.name }),
            el("div", { class: "small muted", text: `${q.count} ${q.count === 1 ? "person" : "people"}` })),
            el("td", { style: "text-align: right;" }, el("button", { class: "btn small secondary", text: "Dismiss", onclick: async (e) => {
              if (await act(e.target, () => api("DELETE", `/admin/category-requests/${encodeURIComponent(q.id)}`), "Dismissed")) RENDER.categories(main);
            } })))))) : el("div", { class: "empty", text: "No suggestions yet." })),
        el("div", { class: "card" }, el("h3", { text: "Categories now" }),
          el("div", { class: "small muted", text: "Adding one is a small server change: ask Claude." }),
          ...r.categories.map((c) => el("div", { text: c.label })))));
  },

  async cities(main) {
    const r = await api("GET", "/admin/home-ground/requests");
    counts.cities = r.items.length; drawNav();
    const name = el("input", { type: "text", placeholder: "City name", maxlength: 60 });
    const cc = el("input", { type: "text", placeholder: "Country code, e.g. ug", maxlength: 2, style: "width: 120px;" });
    const resetName = el("input", { type: "text", placeholder: "@username", maxlength: 40 });
    const add = async (btn, n, c) => {
      if (await act(btn, () => api("POST", "/admin/home-ground/cities", { name: n, country: c }), `Added ${n}`)) RENDER.cities(main);
    };
    main.replaceChildren(...head("Home Ground cities", `${num(r.cities)} cities people can pick. Requests from people who couldn't find theirs:`),
      el("div", { class: "two" },
        el("div", { class: "card" }, el("h3", { text: "Requested cities" }), r.items.length ? el("table", {}, el("tbody", {},
          ...r.items.map((q) => el("tr", {}, el("td", {}, el("div", { style: "font-weight: 600;", text: q.name }),
            el("div", { class: "small muted", text: `${q.country_name} · ${q.count} ${q.count === 1 ? "person" : "people"}` })),
            el("td", { style: "text-align: right;" }, el("button", { class: "btn small", text: "Add", onclick: (e) => add(e.target, q.name, q.country) })))))) :
          el("div", { class: "empty", text: "No requests yet." })),
        el("div", { class: "card" }, el("h3", { text: "Add a city" }),
          el("label", { class: "field" }, el("span", { text: "City" }), name),
          el("label", { class: "field" }, el("span", { text: "Country (2 letters)" }), cc),
          el("button", { class: "btn", text: "Add city", onclick: (e) => add(e.target, name.value.trim(), cc.value.trim().toLowerCase()) }))),
      el("div", { class: "card" }, el("h3", { text: "Reset someone's Home Ground" }),
        el("div", { class: "small muted", text: "Clears their city so they can pick again now, without the 30-day wait." }),
        el("label", { class: "field" }, el("span", { text: "Username" }), resetName),
        el("button", { class: "btn", text: "Reset", onclick: async (e) => {
          const u = resetName.value.trim().replace(/^@/, "");
          if (u && await act(e.target, () => api("POST", "/admin/home-ground/reset", { username: u }), `Reset @${u}`)) resetName.value = "";
        } })));
  },

  async switches(main) {
    const f = await api("GET", "/admin/features");
    const list = el("div", { class: "card" });
    const draw = (state) => list.replaceChildren(...Object.keys(FEATURES).map((k) => {
      const t = el("button", { class: "toggle" + (state[k] ? " on" : ""), "aria-label": FEATURES[k][0], "aria-pressed": state[k] ? "true" : "false", "data-key": k });
      t.addEventListener("click", async () => {
        const on = !state[k];
        const sure = await confirmBox(`Turn ${FEATURES[k][0]} ${on ? "on" : "off"}?`,
          on ? "Everyone with the new app version sees it the next time they open the app." : "It disappears from the app the next time people open it. Nothing is deleted.",
          on ? "Turn on" : "Turn off", !on);
        if (!sure) return;
        t.disabled = true;
        const r = await act(null, () => api("PUT", "/admin/features", { key: k, on }), `${FEATURES[k][0]} is ${on ? "on" : "off"}`);
        t.disabled = false;
        if (r) draw(r);
      });
      return el("div", { class: "switch-row" }, el("div", { class: "txt" }, el("div", { class: "name", text: FEATURES[k][0] }),
        el("div", { class: "small muted", text: FEATURES[k][1] })), t);
    }));
    draw(f);
    const kill = el("button", { class: "btn ghost-danger", text: "Turn everything off", onclick: async (e) => {
      if (!(await confirmBox("Turn every feature off?", "Use this if something goes wrong. The app goes back to how it was before; nothing is deleted.", "Turn all off", true))) return;
      const r = await act(e.target, () => api("POST", "/admin/features/all-off"), "Everything is off");
      if (r) draw(r);
    }});
    main.replaceChildren(...head("Feature switches", "New features stay hidden until you switch them on here. Changes reach people the next time they open the app."),
      list, el("div", { class: "row space" }, el("span", { class: "small muted", text: "Emergency" }), kill));
  },

  async updates(main) {
    const v = await api("GET", "/admin/app-version");
    const ROWS = [
      ["android", "Android", "Android phones on an older version see \"Update Available\" with only an Update Now button, which opens Google Play."],
      ["ios", "iPhone", "iPhones on an older version see the same box; Update Now opens the App Store."],
      ["all", "Older apps (both)", "Used when the Android or iPhone box is empty, and possibly by old versions such as 2.1.1. Set it last, only when both stores have the new version for everyone."],
    ];
    const card = el("div", { class: "card" });
    const draw = (state) => card.replaceChildren(...ROWS.map(([key, label, help]) => {
      const cur = state[key];
      const forced = cur && cur !== "0.0.0";
      const input = el("input", { type: "text", value: cur || "", placeholder: key === "all" ? "0.0.0 (not forced)" : "empty (not forced)",
        "aria-label": `${label} minimum version`, style: "max-width: 160px;" });
      const save = el("button", { class: "btn small", text: "Save", onclick: async (e) => {
        const val = input.value.trim();
        if (val && !/^\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(val)) { toast("Write the version like 2.3.32", true); return; }
        const off = !val || val === "0.0.0";
        const sure = await confirmBox(off ? `Stop forcing updates on ${label}?` : `Force ${label} users to ${val}?`,
          off ? "Nobody will be asked to update on this platform." :
            `Everyone on ${label} with an older version gets a full-screen update box they can't close, the next time they open Wano. ` +
            `Only do this when ${val} is in the store for 100% of users. If it's still rolling out or waiting for review, people will be stuck.`,
          off ? "Stop forcing" : `Force ${val}`, !off);
        if (!sure) return;
        const r = await act(e.target, () => api("PUT", "/admin/app-version", { platform: key, version: val || null }),
          off ? `${label}: not forced` : `${label}: everyone below ${val} must update`);
        if (r) draw(r);
      }});
      return el("div", { class: "switch-row" },
        el("div", { class: "txt" }, el("div", { class: "name", text: `${label}: ${forced ? "update required below " + cur : "not forced"}` }),
          el("div", { class: "small muted", text: help })),
        el("div", { class: "row" }, input, save));
    }));
    draw(v);
    const rp = await api("GET", "/admin/review-prompt").catch(() => ({ android: false, ios: false }));
    const rcard = el("div", { class: "card" });
    const drawReview = (state) => rcard.replaceChildren(el("h3", { text: "Ask for a store rating" }),
      el("p", { class: "small muted", text: "When on, the app asks the store to show its own rating card on someone's 10th video in a session (every app version does this). Google keeps it rare and Apple shows it at most 3 times a year. More good ratings lift Wano in store search. Best turned on once the newest version has reached most people." }),
      ...[["android", "Android"], ["ios", "iPhone"]].map(([key, label]) => el("div", { class: "switch-row" },
        el("div", { class: "txt" }, el("div", { class: "name", text: `${label}: ${state[key] ? "asking" : "not asking"}` })),
        el("button", { class: "btn small" + (state[key] ? " secondary" : ""), text: state[key] ? "Stop asking" : "Start asking", onclick: async (e) => {
          const r = await act(e.target, () => api("PUT", "/admin/review-prompt", { platform: key, on: !state[key] }),
            state[key] ? `${label}: not asking` : `${label}: asking for ratings`);
          if (r) drawReview(r);
        } }))));
    drawReview(rp);
    main.replaceChildren(...head("Force update", "Make everyone on an old app version update before using Wano. Clear a box (or 0.0.0) to stop forcing. Below: asking for store ratings."),
      card, rcard,
      el("div", { class: "card" }, el("p", { class: "small muted", text:
        "Order: 1) the new version is at 100% in Google Play / released in the App Store, 2) set Android and iPhone here, 3) open Wano on a phone with an old version to see the box." })));
  },

  async speed(main) {
    const days = 7;
    const [p, si, pv] = await Promise.all([api("GET", `/admin/perf?days=${days}`), api("GET", "/admin/small-images"),
      api("GET", `/admin/perf/versions?days=${days}`).catch(() => ({ rows: [] }))]);
    const MET = { first_video: "App open → first video", video_start: "Video on screen → playing", stall: "Buffering pause", feed_load: "Feed page load", swipe_lag: "Longest freeze in a swipe" };
    const s = (ms) => ms == null ? "–" : (ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`);
    const t = si.totals || {};
    const mb = (b) => `${((b || 0) / 1048576).toFixed(1)} MB`;
    const walked = Object.entries(si.backlog_walked || {}).map(([k, v]) => `${k}: ${v ? "all done" : "in progress"}`).join(" · ") || "not started";
    const table = p.rows.length ? el("table", {}, el("thead", {}, el("tr", {}, ...["Country", "Connection", "What", "Count", "Typical (p50)", "Slow (p90)"].map((h) => el("th", { text: h })))),
      el("tbody", {}, ...p.rows.map((r) => el("tr", {}, el("td", { text: r.country }), el("td", { text: r.net }), el("td", { text: MET[r.metric] || r.metric }),
        el("td", { text: num(r.count) }), el("td", { text: s(r.p50) }), el("td", { text: s(r.p90) })))))
      : el("div", { class: "empty", text: "No timings yet. Turn on \"Speed measurement\" in Feature switches; phones on the new app start sending within a minute." });
    main.replaceChildren(...head("Speed", `How fast Wano is on real phones over the last ${days} days, by country.`),
      el("div", { class: "card" }, el("h3", { text: "Small picture copies" }),
        el("div", { text: `${num(si.done || 0)} pictures done · ${num(si.failed || 0)} failed · ${num(si.working || 0)} in progress` }),
        el("div", { class: "small muted", text: `Backlog: ${walked}` }),
        t.n ? el("div", { class: "small muted", text: `Originals ${mb(t.orig)} → avatars ${mb(t.w320)}, previews ${mb(t.w480)}` }) : null),
      el("div", { class: "card" }, el("h3", { text: "Timings" }), table),
      el("div", { class: "card" }, el("h3", { text: "By app version" }),
        el("p", { class: "small muted", text: "Is a new version slower or buffering more than the one before? Compare \"Buffering pause\" and \"Video on screen → playing\" between versions. Versions from 2.3.59 send their number; older iPhone apps are known from their connection; older Android apps show as \"older Android\"." }),
        pv.rows.length ? el("table", {}, el("thead", {}, el("tr", {}, ...["App version", "What", "Count", "Typical (p50)", "Slow (p90)"].map((h) => el("th", { text: h })))),
          el("tbody", {}, ...pv.rows.map((r) => el("tr", {}, el("td", { text: r.version }), el("td", { text: MET[r.metric] || r.metric }),
            el("td", { text: num(r.count) }), el("td", { text: s(r.p50) }), el("td", { text: s(r.p90) })))))
          : el("div", { class: "empty", text: "No timings by version yet." })));
  },

  async kampala(main) {
    const draw = (r, links) => {
      const pct = (v) => v == null ? "–" : `${v}%`;
      const linkOf = (name) => `wanoafrica.com/get?by=${name}`;
      const seg = (s) => el("div", { class: "card" }, el("h3", { text: s.name }),
        el("table", {}, el("thead", {}, el("tr", {}, ...["Week of", "Active", "Came back", "Came back %", "New", "New who came back", "Posted"].map((h) => el("th", { text: h })))),
          el("tbody", {}, ...s.rows.slice().reverse().map((x) => el("tr", {},
            el("td", { text: x.week + (x.partial ? " (so far)" : "") }), el("td", { text: num(x.active) }),
            el("td", { text: num(x.returning) }), el("td", { text: pct(x.return_pct) }), el("td", { text: num(x.new) }),
            el("td", { text: `${num(x.new_back)} (${pct(x.new_back_pct)})` }), el("td", { text: num(x.posters) }))))));
      const cr = r.creators || [];
      const creators = el("div", { class: "card" }, el("h3", { text: `${r.city.name} creators, last 4 weeks` }),
        el("p", { class: "small muted", text: "Who to thank, feature in Wano Picks and ask to bring friends." }),
        cr.length ? el("table", {}, el("thead", {}, el("tr", {}, ...["Creator", "Posts", "Views", "Followers", "Their invite link"].map((h) => el("th", { text: h })))),
          el("tbody", {}, ...cr.map((c) => el("tr", {}, el("td", { text: `@${c.username || c.id}${c.name ? " · " + c.name : ""}` }),
            el("td", { text: num(c.posts) }), el("td", { text: num(c.views) }), el("td", { text: num(c.followers) }),
            el("td", { class: "small", text: c.username ? linkOf(String(c.username).toLowerCase()) : "–" })))))
          : el("div", { class: "empty", text: "No posts from this city in the last 4 weeks." }));
      const lr = (links && links.rows) || [];
      const linkCard = el("div", { class: "card" }, el("h3", { text: "Invite links, last 30 days" }),
        el("p", { class: "small muted", text: "Taps on wanoafrica.com/get?by=name. Each phone goes straight to its store; a phone counts once per link per day. Installs from each link show in Play Console (campaign = the name) and App Store Connect (campaign get_name)." }),
        lr.length ? el("table", {}, el("thead", {}, el("tr", {}, ...["Link", "Android", "iPhone", "Other", "Total", "Last tap"].map((h) => el("th", { text: h })))),
          el("tbody", {}, ...lr.map((x) => el("tr", {}, el("td", { text: x.by === "none" ? "(no name)" : linkOf(x.by) }),
            el("td", { text: num(x.android) }), el("td", { text: num(x.ios) }), el("td", { text: num(x.other) }),
            el("td", { text: num(x.total) }), el("td", { text: x.last })))))
          : el("div", { class: "empty", text: "No taps yet. Give each creator their own link: wanoafrica.com/get?by=theirname" }));
      const again = el("button", { class: "btn secondary small", text: "Refresh", onclick: async (e) => {
        const out = await act(e.target, () => api("GET", "/admin/city-report?city=kampala&fresh=true"), "Updated");
        if (out) draw(out, await api("GET", "/admin/get-links").catch(() => null));
      } });
      main.replaceChildren(...head(`${r.city.name} growth`, "The number that matters: people who come back every week, not downloads. Active = signed in and watched, liked, posted or sent a message that week (Monday to Sunday, UTC). Came back % = of last week's active people, how many were active again."),
        el("div", { class: "card" },
          el("div", { text: `${num(r.members.city)} accounts in ${r.city.name} (Home Ground or profile city) · ${num(r.members.country)} in ${r.segments[1].name}` }),
          el("div", { class: "small muted", text: `Updated ${r.generated_at} UTC` }), again),
        ...r.segments.map(seg), linkCard, creators);
    };
    const [rep, links] = await Promise.all([api("GET", "/admin/city-report?city=kampala"), api("GET", "/admin/get-links").catch(() => null)]);
    draw(rep, links);
  },

  async storage(main) {
    const draw = async (r) => {
      const items = r.items || [];
      const mins = (sec) => `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
      const scan = el("button", { class: "btn", text: r.scanned_at ? "Check again" : "Check Cloudflare", onclick: async (e) => {
        const out = await act(e.target, () => api("POST", "/admin/stream-storage/scan"), "Checked");
        if (out) draw(out);
      } });
      const del = el("button", { class: "btn danger", text: `Delete these ${num(items.length)} videos`, disabled: !items.length, onclick: async (e) => {
        if (!(await confirmBox("Delete from Cloudflare?", `${num(items.length)} videos (${num(r.unused_minutes)} minutes) that no post, draft or upload in progress uses. This can't be undone. Each one is checked against the posts again first.`, "Delete", true))) return;
        const out = await act(e.target, () => api("POST", "/admin/stream-storage/delete", { uids: items.map((x) => x.uid), confirm: true }), "Deleted");
        if (out) draw(await api("GET", "/admin/stream-storage"));
      } });
      const table = items.length ? el("table", {}, el("thead", {}, el("tr", {}, ...["Uploaded", "Length", "State", "Name", ""].map((h) => el("th", { text: h })))),
        el("tbody", {}, ...items.slice(0, 500).map((x) => el("tr", {}, el("td", { text: (x.created || "").slice(0, 10) }), el("td", { text: mins(x.seconds || 0) }),
          el("td", { text: x.state || "" }), el("td", { class: "small", text: x.name || "" }),
          el("td", {}, x.thumbnail ? el("a", { href: x.thumbnail, target: "_blank", rel: "noopener", text: "picture" }) : null)))))
        : el("div", { class: "empty", text: r.scanned_at ? "Nothing to clean up." : "Not checked yet." });
      main.replaceChildren(...head("Video storage", "Cloudflare charges for every stored minute every month. These are videos in Cloudflare that no post, draft or upload in progress uses, at least 7 days old. Look through them, then delete."),
        el("div", { class: "card" },
          r.scanned_at ? el("div", { text: `${num(r.videos)} videos (${num(r.minutes)} minutes) in Cloudflare · ${num(r.unused)} unused (${num(r.unused_minutes)} minutes)` }) : null,
          r.scanned_at ? el("div", { class: "small muted", text: `Checked ${String(r.scanned_at).slice(0, 16).replace("T", " ")} UTC` }) : null,
          el("div", { class: "row", style: "gap: 8px; margin-top: 8px;" }, scan, del)),
        el("div", { class: "card" }, table, items.length > 500 ? el("div", { class: "small muted", text: `Showing the first 500 of ${num(items.length)}.` }) : null));
    };
    draw(await api("GET", "/admin/stream-storage"));
  },

  async log(main) {
    const rows = await api("GET", "/admin/log?limit=200");
    const describe = (r) => {
      const d = r.detail || {};
      switch (r.action) {
        case "feature": return `Turned ${(FEATURES[d.key] || [d.key])[0]} ${d.on ? "on" : "off"}`;
        case "features_all_off": return "Turned every feature off";
        case "upload_invites": return `Invited ${d.invited} creators to post a failed video again`;
        case "stream_cleanup": return `Deleted ${d.deleted} unused videos from Cloudflare (${d.minutes} minutes)`;
        case "review_prompt": return `Ask for ratings on ${d.platform}: ${d.on ? "on" : "off"}`;
        case "app_version": return d.to && d.to !== "0.0.0" ? `Force update: ${d.platform} must be ${d.to} or newer` : `Force update off for ${d.platform}`;
        case "pool": return `Set the ${d.month} pool: ${money(d.pool_usd)}`;
        case "distribute": return `Shared out ${d.month}: ${money(d.paid_usd)} to ${d.creators} creators`;
        case "payout_approve": return `Marked a ${money(d.usd)} payout as paid`;
        case "payout_reject": return `Rejected a ${money(d.usd)} payout`;
        case "not_original": return "Marked a post as not original";
        case "original_again": return "Cleared a post's not-original mark";
        case "challenge_create": return `Created #${d.hashtag}`;
        case "challenge_end": return `Ended #${d.hashtag}`;
        case "moderation_restore": return "Restored a reported post";
        case "moderation_remove": return "Removed a reported post";
        case "flyer_create": return `Made a flyer: ${d.business}`;
        case "flyer_update": return `Changed the flyer: ${d.business}`;
        case "flyer_active": return d.on ? "Resumed a flyer" : "Paused a flyer";
        default: return r.action;
      }
    };
    main.replaceChildren(...head("Activity log", "Everything done on this page, newest first."),
      el("div", { class: "card" }, rows.length ? el("table", {}, el("tbody", {}, ...rows.map((r) => el("tr", {},
        el("td", { class: "small muted", style: "white-space: nowrap;", text: when(r.at) }),
        el("td", { text: describe(r) }),
        el("td", { class: "small muted", text: "@" + (r.admin.username || r.admin.id) }))))) : el("div", { class: "empty", text: "Nothing yet." })));
  },
};


// ------------------------------------------------------------ Wano TV titles editor

async function apiForm(path, form) {
  let r;
  try { r = await fetch(API + path, { method: "POST", headers: { "Authorization": "Bearer " + token }, body: form }); }
  catch (e) { throw new ApiError(0, "Can't reach the server. Check your internet."); }
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) { let m = data && data.detail; if (Array.isArray(m)) m = m.map((d) => d.msg).join("; "); throw new ApiError(r.status, m || "Upload failed (" + r.status + ")"); }
  return data;
}

// Resumable upload to Cloudflare Stream (tus), 50 MB at a time, retrying a broken chunk.
async function tusUpload(url, file, onPct) {
  const CHUNK = 50 * 1024 * 1024;
  let offset = 0, tries = 0;
  while (offset < file.size) {
    try {
      const r = await fetch(url, { method: "PATCH", headers: { "Tus-Resumable": "1.0.0", "Upload-Offset": String(offset),
        "Content-Type": "application/offset+octet-stream" }, body: file.slice(offset, offset + CHUNK) });
      if (!r.ok) throw new Error("Cloudflare said " + r.status);
      offset = Number(r.headers.get("Upload-Offset")) || Math.min(file.size, offset + CHUNK);
      tries = 0;
      onPct(Math.round(offset / file.size * 100));
    } catch (e) {
      if (++tries > 6) throw new Error("The upload kept failing: " + e.message + ". Press Upload again to retry.");
      await new Promise((ok) => setTimeout(ok, 2000 * tries));
      try {
        const h = await fetch(url, { method: "HEAD", headers: { "Tus-Resumable": "1.0.0" } });
        const o = Number(h.headers.get("Upload-Offset"));
        if (Number.isFinite(o)) offset = o;
      } catch (e2) { /* try the same chunk again */ }
    }
  }
}

function readText(file) {
  return new Promise((ok, bad) => { const fr = new FileReader(); fr.onload = () => ok(String(fr.result || "")); fr.onerror = () => bad(new Error("Couldn't read the file")); fr.readAsText(file); });
}

function stripRulesCard(rules, done) {
  const LABELS = [["after_portion", "Show after this share of a video (0-1)", 0.05], ["min_posts", "Not in the first … posts", 1], ["gap_posts", "Posts between two strips", 1],
    ["max_per_session", "Most per session", 1], ["show_seconds", "Seconds on screen", 1], ["closes_per_week", "Closes before it stops for a week", 1]];
  const inputs = {};
  return el("div", { class: "card" }, el("h3", { text: "Change the rules" }),
    el("p", { class: "small muted", text: "Takes effect the next time each app opens. No app update needed." }),
    el("div", { class: "two" }, ...LABELS.map(([k, label, step]) => { inputs[k] = el("input", { type: "number", step, value: rules[k] }); return el("label", { class: "field" }, el("span", { text: label }), inputs[k]); })),
    el("button", { class: "btn", text: "Save rules", onclick: async (e) => {
      const body = {};
      for (const [k] of LABELS) body[k] = k === "after_portion" ? Number(inputs[k].value) : Math.round(Number(inputs[k].value));
      if (await act(e.target, () => api("POST", "/admin/tv/strip-rules", body), "Rules saved")) done();
    } }));
}

async function tvEditor(main, box, id) {
  const t = id ? await api("GET", `/admin/tv/titles/${id}`) : { type: "movie", status: "hidden", genres: [], credits: { distributor: "Wano TV", cast: [], people: [] }, trailer: {}, captions: [], seasons: [], problems: [], clips: [] };
  const reload = () => tvEditor(main, box, t.id);
  const inp = (v, attrs) => { const i = el("input", { type: "text", ...(attrs || {}) }); i.value = v === undefined || v === null ? "" : v; return i; };
  const area = (v, rows) => { const a = el("textarea", { rows: rows || 3 }); a.value = v || ""; return a; };
  const f = {
    title: inp(t.title, { maxlength: 120 }), slug: inp(t.slug, { maxlength: 80, placeholder: "e.g. one-field (the web address)" }),
    type: el("select", {}, el("option", { value: "movie", text: "Film", selected: t.type !== "series" }), el("option", { value: "series", text: "Series", selected: t.type === "series" })),
    genres: inp((t.genres || []).join(", "), { placeholder: "Documentary, Drama" }), year: inp(t.year || new Date().getFullYear(), { type: "number" }),
    release_date: inp(t.release_date ? String(t.release_date).slice(0, 10) : "", { type: "date" }), duration_minutes: inp(t.duration_minutes || 0, { type: "number" }),
    age_rating: inp(t.age_rating || "All", { maxlength: 10, placeholder: "All, 13+, 16+, 18+" }), description: inp(t.description, { maxlength: 300, placeholder: "One line" }),
    synopsis: area(t.synopsis, 5), poster: inp(t.poster), backdrop: inp(t.backdrop), thumb: inp((t.trailer || {}).thumbnail),
    trailer: inp((t.trailer || {}).video_url, { placeholder: "https://… (public trailer link)" }), trailer_s: inp((t.trailer || {}).duration_seconds || 0, { type: "number" }),
    director: inp((t.credits || {}).director), cast: inp(((t.credits || {}).cast || []).join(", ")), production: inp((t.credits || {}).production),
    distributor: inp((t.credits || {}).distributor || "Wano TV"),
    people: area(((t.credits || {}).people || []).map((p) => [p.name, p.role, p.username ? "@" + p.username : ""].filter(Boolean).join(" | ")).join("\n"), 4),
    video_url: inp(t.cf_uid ? "" : t.video_url, { placeholder: t.cf_uid ? "Film uploaded to Cloudflare (paste a Spaces link to replace it)" : "https://…/wano-tv/<folder>/master.m3u8" }),
    featured: el("input", { type: "checkbox" }), popularity: inp(t.popularity || 0, { type: "number" }), coming_at: inp(t.coming_at || "", { type: "date" }), premiere_at: inp(t.premiere_at ? new Date(t.premiere_at).toISOString().slice(0,16) : "", { type: "datetime-local" }),
  };
  f.featured.checked = !!t.featured;
  const field = (label, node, hint) => el("label", { class: "field" }, el("span", { text: label }), node, hint ? el("div", { class: "small muted", text: hint }) : null);
  const picture = (label, input) => {
    const file = el("input", { type: "file", accept: ".jpg,.jpeg,.png,.webp" });
    file.onchange = async () => {
      if (!t.id) { toast("Save the title first, then add pictures", true); return; }
      const fd = new FormData(); fd.append("file", file.files[0]);
      const out = await act(null, () => apiForm(`/admin/tv/titles/${t.id}/art-file`, fd), "Uploaded: press Save");
      if (out) input.value = out.url;
    };
    return field(label, el("div", {}, input, el("div", { style: "margin-top: 4px;" }, file)));
  };
  const body = () => ({
    slug: f.slug.value.trim(), title: f.title.value.trim(), type: f.type.value,
    genres: f.genres.value.split(",").map((g) => g.trim()).filter(Boolean), year: Number(f.year.value) || 0,
    release_date: f.release_date.value || null, duration_minutes: Number(f.duration_minutes.value) || 0, age_rating: f.age_rating.value.trim() || "All",
    description: f.description.value.trim(), synopsis: f.synopsis.value.trim(), poster: f.poster.value.trim(), backdrop: f.backdrop.value.trim(),
    trailer: { thumbnail: f.thumb.value.trim(), video_url: f.trailer.value.trim(), duration_seconds: Number(f.trailer_s.value) || 0 },
    credits: { director: f.director.value.trim(), production: f.production.value.trim(), distributor: f.distributor.value.trim(),
      cast: f.cast.value.split(",").map((c) => c.trim()).filter(Boolean),
      people: f.people.value.split("\n").map((l) => l.split("|").map((x) => x.trim())).filter((p) => p[0] && p[1])
        .map((p) => ({ name: p[0], role: p[1], username: (p[2] || "").replace(/^@/, "") })) },
    video_url: f.video_url.value.trim() || (t.cf_uid ? t.video_url : ""), featured: f.featured.checked,
    popularity: Number(f.popularity.value) || 0, coming_at: f.coming_at.value || null, premiere_at: f.premiere_at.value ? f.premiere_at.value + ":00Z" : null,
  });
  const save = el("button", { class: "btn", text: t.id ? "Save" : "Create (Hidden)", onclick: async (e) => {
    const out = await act(e.target, () => t.id ? api("PUT", `/admin/tv/titles/${t.id}`, body()) : api("POST", "/admin/tv/titles", body()), "Saved");
    if (out) RENDER.tvtitles(main, out.id);
  } });

  const sections = [];
  if (t.id) {
    // Status
    const statusBtn = (s, label) => el("button", { class: "btn small " + (t.status === s ? "" : "secondary"), text: label, disabled: t.status === s, onclick: async (e) => {
      const yes = s !== "live" || await confirmBox(`Make "${t.title}" live?`, "It shows in the app for everyone straight away." + (t.status === "coming_soon" ? " Everyone who tapped Remind me gets a notification." : ""), "Go live");
      if (yes && await act(e.target, () => api("POST", `/admin/tv/titles/${t.id}/status`, { status: s }), "Done")) RENDER.tvtitles(main, t.id);
    } });
    const notify = el("button", { class: "btn small secondary", text: t.notified_at ? "Everyone was told " + when(t.notified_at) : "Tell everyone (push)", disabled: !!t.notified_at || t.status !== "live", onclick: async (e) => {
      if (await confirmBox("Send \"New on Wano TV\" to everyone?", `Everyone with notifications on gets "New on Wano TV: ${t.title}" (not people in their night hours, and not under-age accounts for rated titles). This can only be done once per title.`, "Send"))
        if (await act(e.target, () => api("POST", `/admin/tv/titles/${t.id}/notify`), "Sending")) reload();
    } });
    sections.push(el("div", { class: "card" }, el("h3", { text: "Status" }),
      el("div", { class: "row", style: "gap: 8px; flex-wrap: wrap;" }, statusBtn("live", "Live"), statusBtn("coming_soon", "Coming soon"), statusBtn("hidden", "Hidden"), notify),
      tvWaitingForPremiere(t) ? el("p", {class:"small muted",text:"Scheduled: episodes unlock automatically at their release times."}) : t.problems.length ? el("ul", { class: "small", style: "color: #b42318; margin-top: 10px;" }, ...t.problems.map((p) => el("li", { text: p }))) : el("p", { class: "small muted", text: "Ready: the app can show this title." })));

    sections.push(filmCard(t, null, reload));

    if (t.type === "series") sections.push(episodesCard(t, reload));

    // Clips in the feed
    const vid = el("input", { type: "text", placeholder: "Post link, e.g. https://wanoafrica.com/v/…" });
    sections.push(el("div", { class: "card" }, el("h3", { text: "Clips in the feed" }),
      el("p", { class: "small muted", text: "Feed posts from this title. With the \"Clips: Watch the full film\" switch on, they show a button that opens the title." }),
      ...(t.clips || []).map((c) => el("div", { class: "row space small", style: "padding: 4px 0;" }, el("a", { href: postLink(c), target: "_blank", rel: "noopener", text: c }),
        el("button", { class: "btn small secondary", text: "Remove", onclick: async (e) => { if (await act(e.target, () => api("DELETE", `/admin/tv/clips/${c}`), "Removed")) reload(); } }))),
      el("div", { class: "row", style: "gap: 8px; margin-top: 8px;" }, vid, el("button", { class: "btn small", text: "Link post", onclick: async (e) => {
        if (vid.value.trim() && await act(e.target, () => api("POST", `/admin/tv/titles/${t.id}/clips`, { video: vid.value.trim() }), "Linked")) reload();
      } }))));
  }

  box.replaceChildren(el("div", { class: "card" }, el("h3", { text: t.id ? `Edit: ${t.title}` : "New title" }),
    el("div", { class: "two" },
      el("div", {}, field("Title", f.title), field("Web address (slug)", f.slug, "wanoafrica.com/tv/<this>. Lower case and dashes."),
        field("Kind", f.type), field("Genres", f.genres, "Comma separated. Not \"Series\" or \"Film\"."),
        el("div", { class: "row", style: "gap: 8px;" }, field("Year", f.year), field("Release date", f.release_date), field("Minutes", f.duration_minutes), field("Rating", f.age_rating)),
        field("One-line description", f.description), field("Story (synopsis)", f.synopsis),
        field("Coming soon date (optional)", f.coming_at, "Shown on the Coming soon row."),
        field("Automatic series premiere (UTC)", f.premiere_at, "19:00 Tanzania = 16:00 UTC. Only ready, released episodes become playable."),
        el("label", { class: "row", style: "gap: 8px; margin: 8px 0;" }, f.featured, el("span", { text: "Featured (top of Wano TV)" })), field("Popularity boost (0-1000)", f.popularity)),
      el("div", {}, picture("Poster (portrait)", f.poster), picture("Landscape picture", f.backdrop), picture("Trailer picture", f.thumb),
        field("Trailer link", f.trailer), field("Trailer length (seconds)", f.trailer_s),
        field("Director", f.director), field("Cast", f.cast, "Comma separated"), field("Production", f.production), field("Distributor", f.distributor),
        field("Credits with Wano profiles", f.people, "One per line: Name | Role | @username (the @username links their Wano profile)"),
        t.type === "series" ? null : field("Film on Spaces (optional)", f.video_url, "Or upload the film below, after saving."))),
    save), ...sections);
  box.scrollIntoView({ behavior: "smooth" });
}

// Film, subtitles and offline copy, for a title (ep = null) or one episode.
function filmCard(t, ep, reload) {
  const src = ep || t;
  const q = ep ? `?episode=${ep.id}` : "";
  const status = el("div", { class: "small muted" });
  const what = src.cf_uid ? (src.cf_ready ? "Film on Cloudflare: ready" : "Film on Cloudflare: processing (press Check)") : src.video_url ? "Film on Spaces" : "No film yet";
  const file = el("input", { type: "file", accept: "video/*,.mp4,.mov,.mkv" });
  const upload = el("button", { class: "btn small", text: "Upload film to Cloudflare", onclick: async (e) => {
    const fl = file.files[0];
    if (!fl) { toast("Pick the film file first", true); return; }
    e.target.disabled = true;
    try {
      const up = await api("POST", `/admin/tv/titles/${t.id}/film${q}`, { filename: fl.name, size: fl.size });
      await tusUpload(up.upload_url, fl, (p) => { status.textContent = `Uploading… ${p}% (keep this tab open)`; });
      status.textContent = "Uploaded. Cloudflare is preparing it: press Check in a few minutes.";
      toast("Film uploaded");
    } catch (err) { status.textContent = ""; toast(err.message, true); }
    finally { e.target.disabled = false; }
  } });
  const check = src.cf_uid || src.download_cf_uid ? el("button", { class: "btn small secondary", text: "Check film", onclick: async (e) => {
    const out = await act(e.target, () => api("POST", `/admin/tv/titles/${t.id}/film/check${q}`));
    if (out) { toast(out.ready === null ? (out.download && out.download.ready ? "Offline copy ready" : "Offline copy still processing") : out.ready ? "Ready to play" : `Still processing${out.pct ? " (" + out.pct + "%)" : ""}`); reload(); }
  } }) : null;
  const offFile = el("input", { type: "file", accept: ".mp4,.mov" });
  const offline = el("button", { class: "btn small secondary", text: src.download_ready ? "Offline copy ✓" : "Make offline copy", onclick: async (e) => {
    if (src.cf_uid) {
      const out = await act(e.target, () => api("POST", `/admin/tv/titles/${t.id}/offline${q}`, { filename: "" }));
      if (out) { toast(out.ready ? "Offline copy ready" : "Cloudflare is making it: press Check film later"); reload(); }
      return;
    }
    const fl = offFile.files[0];
    if (!fl) { toast("Pick an MP4 of the film first (720p is plenty)", true); return; }
    e.target.disabled = true;
    try {
      const up = await api("POST", `/admin/tv/titles/${t.id}/offline${q}`, { filename: fl.name, size: fl.size });
      await tusUpload(up.upload_url, fl, (p) => { status.textContent = `Uploading the offline copy… ${p}%`; });
      status.textContent = "Uploaded. Press Check film in a few minutes to finish it."; toast("Offline copy uploaded"); reload();
    } catch (err) { status.textContent = ""; toast(err.message, true); }
    finally { e.target.disabled = false; }
  } });
  const lang = el("input", { type: "text", placeholder: "Language code: en, fr, sw, lg", maxlength: 12, style: "width: 170px;" });
  const label = el("input", { type: "text", placeholder: "Label: English", maxlength: 40, style: "width: 150px;" });
  const subs = el("input", { type: "file", accept: ".srt,.vtt" });
  const addSubs = el("button", { class: "btn small secondary", text: "Add subtitles", onclick: async (e) => {
    if (!subs.files[0] || !lang.value.trim()) { toast("Pick the .srt/.vtt file and type the language code", true); return; }
    const text = await readText(subs.files[0]);
    if (await act(e.target, () => api("POST", `/admin/tv/titles/${t.id}/captions${q}`, { language: lang.value.trim().toLowerCase(), label: label.value.trim(), text }), "Subtitles added")) reload();
  } });
  return el("div", { class: ep ? "" : "card", style: ep ? "border-top: 1px solid var(--border); padding-top: 10px; margin-top: 10px;" : "" },
    ep ? el("div", { style: "font-weight: 600;", text: `Episode ${ep.number}: film and subtitles` }) : el("h3", { text: "Film" }),
    el("p", { class: "small", text: what }),
    t.type === "series" && !ep ? el("p", { class: "small muted", text: "A series plays its episodes: add the films in Episodes below." }) :
      el("div", {},
        el("div", { class: "row", style: "gap: 8px; flex-wrap: wrap;" }, file, upload, check), status,
        el("div", { class: "small", style: "margin-top: 10px;" }, "Subtitles: ", (src.captions || []).length ? null : "none",
          ...(src.captions || []).map((c) => el("span", { style: "margin-right: 10px;" }, `${c.label} (${c.language}) `,
            el("button", { class: "btn small secondary", text: "×", onclick: async (e) => { if (await act(e.target, () => api("DELETE", `/admin/tv/titles/${t.id}/captions/${c.language}${q}`), "Removed")) reload(); } })))),
        el("div", { class: "row", style: "gap: 8px; flex-wrap: wrap; margin-top: 6px;" }, lang, label, subs, addSubs),
        el("div", { class: "row", style: "gap: 8px; flex-wrap: wrap; margin-top: 10px;" }, src.cf_uid ? null : offFile, offline,
          el("span", { class: "small muted", text: "For Download to watch later (Downloads switch)." }))));
}

function episodesCard(t, reload) {
  const eps = (t.seasons || []).flatMap((s) => s.episodes || []);
  const sea = el("input", { type: "number", value: 1, style: "width: 70px;" });
  const no = el("input", { type: "number", value: (eps.length ? Math.max(...eps.map((e) => e.number)) + 1 : 1), style: "width: 70px;" });
  const ti = el("input", { type: "text", placeholder: "Episode title", maxlength: 120 });
  const mins = el("input", { type: "number", placeholder: "Minutes", style: "width: 90px;" });
  const syn = el("textarea", { rows: 2, placeholder: "What happens (optional)" });
  const link = el("input", { type: "text", placeholder: "Film on Spaces (optional): https://…/wano-tv/<this title's folder>/…/master.m3u8" });
  const thumb = el("input", { type: "text", placeholder: "Episode picture link (optional)" });
  const publish = el("input", { type: "datetime-local" });
  const enabled = el("input", { type: "checkbox" });
  const pic = el("input", { type: "file", accept: ".jpg,.jpeg,.png,.webp" });
  pic.onchange = async () => { const fd = new FormData(); fd.append("file", pic.files[0]); const out = await act(null, () => apiForm(`/admin/tv/titles/${t.id}/art-file`, fd), "Picture uploaded"); if (out) thumb.value = out.url; };
  const fill = (e) => { publish.value = e.publish_at ? new Date(e.publish_at).toISOString().slice(0,16) : ""; enabled.checked = e.is_published !== false; sea.value = e.season; no.value = e.number; ti.value = e.title; mins.value = e.duration_minutes || ""; syn.value = e.synopsis || ""; link.value = e.cf_uid ? "" : (e.video_url || ""); thumb.value = e.thumbnail || ""; ti.focus(); };
  return el("div", { class: "card" }, el("h3", { text: `Episodes (${eps.length})` }),
    ...eps.map((e) => el("div", { style: "padding: 8px 0; border-bottom: 1px solid var(--border);" },
      el("div", { class: "row space" }, el("div", {}, el("strong", { text: `S${e.season} E${e.number} · ${e.title}` }), el("span", { class: "small muted", text: ` · ${e.duration_minutes || "?"} min · ${e.is_published === false ? "Hidden" : e.publish_at ? "Unlocks " + new Date(e.publish_at).toLocaleString("en-GB", {timeZone:"Africa/Dar_es_Salaam"}) + " EAT" : "Available"}` })),
        el("div", { class: "row", style: "gap: 6px;" }, el("button", { class: "btn small secondary", text: "Edit", onclick: () => fill(e) }),
          el("button", { class: "btn small secondary", text: "Remove", onclick: async (ev) => {
            if (await confirmBox(`Remove episode ${e.number}?`, "Its film and subtitles stay in storage; only the episode is taken off the title.", "Remove", true))
              if (await act(ev.target, () => api("DELETE", `/admin/tv/titles/${t.id}/episodes/${e.id}`), "Removed")) reload();
          } }))),
      filmCard(t, e, reload))),
    el("h3", { text: "Add or change an episode", style: "margin-top: 14px;" }),
    el("div", { class: "row", style: "gap: 8px; flex-wrap: wrap;" }, el("span", { class: "small", text: "Season" }), sea, el("span", { class: "small", text: "Episode" }), no, ti, mins),
    el("label", { class: "field" }, el("span", { text: "Unlock date and time (UTC; 19:00 Tanzania = 16:00 UTC)" }), publish),
    el("label", {}, enabled, " Ready for release (otherwise kept hidden)"),
    el("div", { style: "margin-top: 6px;" }, syn), el("div", { style: "margin-top: 6px;" }, link),
    el("div", { class: "row", style: "gap: 8px; margin-top: 6px;" }, thumb, pic),
    el("button", { class: "btn small", style: "margin-top: 8px;", text: "Save episode", onclick: async (ev) => {
      const out = await act(ev.target, () => api("PUT", `/admin/tv/titles/${t.id}/episodes`, { season: Number(sea.value), number: Number(no.value), title: ti.value.trim(),
        publish_at: publish.value ? publish.value + ":00Z" : null, is_published: enabled.checked, synopsis: syn.value.trim(), duration_minutes: Number(mins.value) || 0, thumbnail: thumb.value.trim(), video_url: link.value.trim() }), "Episode saved: upload its film below it");
      if (out) reload();
    } }));
}

// ------------------------------------------------------------ pieces

function lastMonth() {
  const d = new Date(); d.setUTCDate(1); d.setUTCMonth(d.getUTCMonth() - 1);
  return d.toISOString().slice(0, 7);
}
function postLink(id) { return "https://wanoafrica.com/v/" + encodeURIComponent(id); }
function openCreator(q) { current = "creators"; drawNav(); RENDER.creators($("main"), q); }

const METHOD = { mobile_money: "Mobile money", pix: "Pix", bank: "Bank" };

function payoutRow(p, state, main) {
  const a = p.payout_account || {};
  const warn = [];
  if (!p.user.account_active) warn.push("This account is banned or closed.");
  if (p.shared_account_with.length) warn.push(`${p.shared_account_with.length} other account(s) use the same ${METHOD[a.method] || "account"}. Often one person with several accounts.`);
  return el("tr", {},
    el("td", {}, el("a", { href: "#creators", onclick: (e) => { e.preventDefault(); openCreator(p.user.id); }, text: p.user.username ? "@" + p.user.username : p.user.id }),
      el("div", { class: "small muted", text: p.user.display_name || "" }),
      ...warn.map((w) => el("div", { class: "warn", text: w }))),
    el("td", { style: "font-weight: 700;", text: money(p.usd) }),
    el("td", {}, el("div", { text: METHOD[a.method] || a.method || "—" }), el("div", { class: "mono", text: a.account || a.account_hint || "" }),
      el("div", { class: "small muted", text: [a.country && a.country.toUpperCase(), a.name].filter(Boolean).join(" · ") })),
    el("td", { class: "small muted", text: when(state === "requested" ? p.requested_at : p.decided_at) }),
    state === "requested" ? el("td", { style: "text-align: right; white-space: nowrap;" },
      el("button", { class: "btn small", text: "Mark as paid", onclick: async (e) => {
        const ok = await confirmBox(`Mark ${money(p.usd)} as paid?`,
          `Only press this after you've sent ${money(p.usd)} to ${METHOD[a.method] || ""} ${a.account || ""}.${warn.length ? "\n\n" + warn.join("\n") : ""}`, "Yes, I've paid");
        if (ok && await act(e.target, () => api("POST", `/points/admin/payouts/${p.id}/approve`), "Marked as paid")) RENDER.payouts(main);
      }}), " ",
      el("button", { class: "btn ghost-danger small", text: "Reject", onclick: async (e) => {
        const ok = await confirmBox(`Reject this ${money(p.usd)} payout?`, "The cash goes back to the creator's balance. Nothing is sent.", "Reject", true);
        if (ok && await act(e.target, () => api("POST", `/points/admin/payouts/${p.id}/reject`), "Rejected, cash returned")) RENDER.payouts(main);
      }})) : null);
}

function creatorCard(c, main, q) {
  const u = c.user, s = c.creator_status, e = c.earnings, pts = c.points;
  const pic = safeUrl(u.profile_picture);
  const acct = c.payout_account;
  return el("div", {},
    el("div", { class: "card" }, el("div", { class: "row" },
      pic ? el("img", { src: pic, alt: "", style: "width: 52px; height: 52px; border-radius: 50%; object-fit: cover;" }) : null,
      el("div", { style: "flex: 1; min-width: 0;" },
        el("div", { style: "font-weight: 600; font-size: 17px;", text: u.display_name || u.username }),
        el("div", { class: "muted", text: `@${u.username} · ${num(u.followers_count)} followers · joined ${when(u.created_at)}` }),
        el("div", { class: "small muted", text: [u.phone_number || "no phone", u.email, u.country && u.country.toUpperCase()].filter(Boolean).join(" · ") })),
      el("span", { class: "pill " + (s.eligible ? "green" : "grey"), text: s.eligible ? "Wano Creator" : "Not a Wano Creator yet" }),
      u.is_active ? null : el("span", { class: "pill red", text: "Banned / closed" }))),
    el("div", { class: "two" },
      el("div", { class: "card" }, el("h3", { text: "Wano Creator checklist (last 28 days)" }),
        el("ul", { class: "checks" }, ...s.checks.map((ch) => el("li", {},
          el("span", { class: "dot " + (ch.ok ? "ok" : "no"), text: ch.ok ? "✓" : "✕" }),
          el("span", { text: (CHECKS[ch.key] || (() => ch.key))(ch) }))))),
      el("div", { class: "card" }, el("h3", { text: "Points and cash" }),
        el("table", {}, el("tbody", {},
          el("tr", {}, el("td", { class: "muted", text: "Points" }), el("td", { text: `${num(pts.balance)} (+${num(pts.pending)} pending)` })),
          el("tr", {}, el("td", { class: "muted", text: `Posting days ${pts.month}` }), el("td", { text: `${pts.posting_days} of ${pts.days_needed}` })),
          el("tr", {}, el("td", { class: "muted", text: "Cash" }), el("td", { text: money(e.cash_usd) })),
          el("tr", {}, el("td", { class: "muted", text: "On hold" }), el("td", { text: e.held_usd ? money(e.held_usd) + (e.held_until ? ` until ${when(e.held_until)}` : "") : "—" })),
          el("tr", {}, el("td", { class: "muted", text: "Last share" }), el("td", { text: e.last_share ? `${e.last_share.month}: ${money(e.last_share.usd)}` : "—" })),
          el("tr", {}, el("td", { class: "muted", text: "Paid to" }), el("td", { text: acct ? `${METHOD[acct.method] || acct.method} ${acct.account}` : "No payout account" })))))),
    el("div", { class: "card" }, el("h3", { text: "Recent posts" }), c.posts.length ? el("table", {}, el("tbody", {}, ...c.posts.map((p) => {
      const thumb = safeUrl(p.thumbnail);
      const marked = p.originality === "rejected";
      return el("tr", {},
        el("td", { style: "width: 56px;" }, thumb ? el("img", { class: "thumb", src: thumb, alt: "", onerror: (e) => { e.target.style.visibility = "hidden"; } }) : el("div", { class: "thumb" })),
        el("td", {}, el("div", { text: p.title || "(no title)" }),
          el("div", { class: "small muted", text: `${num(p.views)} views · ${when(p.created_at)}` }),
          el("a", { class: "small", href: postLink(p.id), target: "_blank", rel: "noopener noreferrer", text: "Open post" })),
        el("td", { style: "text-align: right;" }, marked
          ? el("div", {}, el("span", { class: "pill red", text: "Not original" }), " ",
              el("button", { class: "btn secondary small", text: "Undo", onclick: async (ev) => {
                const ok = await confirmBox("Clear the not-original mark?", "The strike goes away and the post can earn again from now. Points already taken away don't come back.", "Clear it");
                if (ok && await act(ev.target, () => api("POST", `/points/admin/videos/${p.id}/original-again`), "Cleared")) RENDER.creators(main, q);
              }}))
          : el("button", { class: "btn ghost-danger small", text: "Not original", onclick: async (ev) => {
              const ok = await confirmBox("Mark this post as not original?",
                "Use this for copied or reposted content. The post stops earning, its points are taken back (and cash already shared from them), and the creator can't be a Wano Creator for 90 days.", "Mark not original", true);
              if (ok && await act(ev.target, () => api("POST", `/points/admin/videos/${p.id}/not-original`), "Marked not original")) RENDER.creators(main, q);
            }})));
    }))) : el("div", { class: "empty", text: "No posts." })));
}

async function moderate(btn, v, action, main) {
  const ok = await confirmBox(action === "remove" ? "Remove this post?" : "Restore this post?",
    action === "remove" ? "The post is deleted for everyone." : "The post goes back up and its flags are cleared.",
    action === "remove" ? "Remove" : "Restore", action === "remove");
  if (ok && await act(btn, () => api("POST", `/reports/admin/videos/${v.id}/decision`, { action }), action === "remove" ? "Removed" : "Restored")) RENDER.moderation(main);
}

// ------------------------------------------------------------ start

async function start() {
  if (!token) return showLogin();
  let me;
  try { me = await api("GET", "/admin/me"); }
  catch (e) { if (e.status !== 401) logout(e.status === 403 ? "That account isn't an admin." : e.message); return; }
  $("who").textContent = "@" + me.username;
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  const want = (location.hash || "").slice(1);
  go(PAGES.some(([id]) => id === want) ? want : "overview");
}

start();
