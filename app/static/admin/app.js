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
  ["moderation", "Reported posts"], ["challenges", "Challenges"], ["switches", "Feature switches"], ["log", "Activity log"],
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

  async log(main) {
    const rows = await api("GET", "/admin/log?limit=200");
    const describe = (r) => {
      const d = r.detail || {};
      switch (r.action) {
        case "feature": return `Turned ${(FEATURES[d.key] || [d.key])[0]} ${d.on ? "on" : "off"}`;
        case "features_all_off": return "Turned every feature off";
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
