# Wano backend: security and bug fixes (Nov 2026)

All 67 findings from the code review are addressed. These were verified with an attack suite in
`tests/pressure/` (73 tests in two rounds, each attempting to exploit a finding against the real app on an
in-memory database).

| Run | Attack suite | Original tests |
| --- | --- | --- |
| Before fixes | 8 / 58 pass (50 attacks succeeded) | 39 / 39 |
| After fixes | 58 / 58 pass | 39 / 39 |
| Round 2 (bypass attempts on the fixes) | 15 / 15 pass after one more fix | 39 / 39 |

Run the tests on Python 3.11:

    pip install -r requirements.txt pytest pytest-asyncio==0.23.8 mongomock-motor httpx
    pytest tests

## Must do before deploying

1. **Drop the old email index.** Run `db.users.dropIndex("email_1")` once. Startup then creates
   the new partial unique indexes on `email` and `phone_number`. Until you drop it, phone-only
   signups and account deletion can still fail.
2. **Add the `stream_uid` index.** Run `db.videos.createIndex({stream_uid: 1}, {partialFilterExpression: {stream_uid: {$type: "string"}}})`.
   Optional: `pending_videos {status: 1, updated_at: 1}`.
3. **Deploy the updated `lambda.mjs` to the stitch Lambda.** No shared secret is needed:
   `/video/v2/presigned-upload` stays open to callers without a token, as before, because the
   Lambda calls it without credentials.
   - Optional, Lambda only: `EXTRA_MEDIA_HOSTS`, a comma-separated list of extra hosts the
     Lambda may fetch from.
4. **Check `CLOUDFLARE_STREAM_WEBHOOK_SECRET` is set.** If it is missing, the webhook now
   rejects every request.
5. **Set `DEBUG=true` only in dev.** It now defaults to false, which turns off GraphiQL,
   introspection and tracebacks.

## Behavior changes the mobile app may notice

- **Registration needs a verified OTP.** `/auth/register` now requires a verified OTP for that
  email or phone (verified within the last 30 minutes). New users come back `is_verified: true`.
- **Usernames are restricted.** They must match `^[a-zA-Z0-9_.]{3,30}$`, so no `@`, `+` or `-`.
- **Code requests are capped at 10 per day.** This applies per account for password reset and per email or phone for signup; extra requests get 429.
- **OTPs lock after 5 wrong guesses.** The 5th wrong guess returns 429 and the code is wiped;
  the user must request a new one.
- **Login throttling.** Login returns 429 after 10 failures in 15 minutes. Banned users can't
  log in.
- **Tokens still don't expire.** A password reset logs out every existing session (tokens now
  carry `iat`, checked against the user's last password change).
- **Profiles are trimmed for other viewers.** `email`, `phone_number`, `date_of_birth` and
  `gender` come back `null` unless you are the owner or an admin. `GET /users/` requires login
  and returns at most 100 users.
- **Privacy is enforced.** Private and unlisted videos are visible only to their creator, on
  REST, GraphQL and in the feeds.
- **Views are counted only by `POST /videos/{id}/watch`.** `GET /videos/{id}` no longer counts
  a view.
- **Upload endpoints need login** (except `/video/v2/presigned-upload`, which the Lambda calls without one). Uploads are capped at 200 MB for video, 20 MB for images and
  50 MB for audio; presigned uploads at 2 GB. SVG is rejected.
- **Stitch input is restricted.** Clips must be Cloudflare Stream URLs and audio must be on the
  Spaces CDN or be a library song. The limit is 20 clips.
- **Comment rules.** Commenting on a video with comments disabled returns 403. Replies to
  replies are still allowed.
- **Page sizes are capped.** The cap is usually 50; drafts and user lists allow up to 100.
- **Admin-only endpoints.** These now require an admin: broadcast, Recombee tools, metrics
  flush and status, and `/debug/tasks`.

## Known remaining limits

- **Per-process limits.** The login throttle and the broadcast lock live in each process. With
  several workers, each keeps its own count. Move them to Redis later.
- **Unbounded data.** Followers are still stored as arrays inside the user document, though now
  paginated. `/recombee/ingest-videos`, `/music/songs` and the TV catalog still load everything.
- **Forgot-password still leaks a little.** It returns 429 on a repeat request within a minute,
  which reveals that the account exists.
- **Dependency pins.** `requirements.txt` pins do not install on Python 3.12+. Use 3.11, as the
  Dockerfile does.

## New: share links (like YouTube watch pages)

The app's Share button should copy `https://<SHARE_BASE_URL>/v/<video_id>`.

| Route | What it does |
| --- | --- |
| `GET /v/{id}` | Watch page. It plays the post (Cloudflare player, or photos for photo posts) and carries link-preview tags for WhatsApp, iMessage, Facebook and X, plus Google video data (JSON-LD). It also shows "Open in Wano" and store buttons, and the iOS Smart App Banner. |
| `GET /embed/{id}` | Bare player, used for X's player card. |
| `GET /sitemap.xml`, `/sitemaps/videos-{n}.xml` | Google video sitemap. Public videos only, 5,000 per page. |
| `GET /robots.txt` | Lets crawlers into `/v/`, keeps them out of `/api/`, `/graphql` and `/embed/`. |
| `GET /.well-known/apple-app-site-association` | iOS Universal Links for `/v/*`. |
| `GET /.well-known/assetlinks.json` | Android App Links. |

How each privacy setting behaves:

| Setting | Behavior |
| --- | --- |
| Public | Preview, plays, and listed on Google. |
| Unlisted | Preview and plays for anyone with the link. Never indexed (noindex, kept out of the sitemap). |
| Private | Shows the same generic "not available" page as an unknown ID. |
| Deleted, scheduled, still processing, or banned creator | Same generic "not available" page. |

Unlisted posts now also open by ID in the API for anyone (`GET /videos/{id}`, GraphQL `video`, plus like, bookmark and watch). Lists, feeds and search still show only public posts.

The pages run no scripts. Every user-supplied value is escaped. CSP and `X-Frame-Options` are set. The pages don't count views.

### Settings (all optional; each feature turns on when its value is set)

| Setting | Example |
| --- | --- |
| `SHARE_BASE_URL` | `https://wanoafrica.com` (default) |
| `APP_SCHEME` | `wano`, giving `wano://v/{id}` |
| `IOS_APP_IDS` | `TEAMID.com.wanoafrica.app` (comma-separated) |
| `IOS_APP_STORE_ID`, `IOS_APP_STORE_URL` | App Store numeric id, and store link |
| `ANDROID_PACKAGE` | `com.wanoafrica.app` |
| `ANDROID_SHA256_FINGERPRINTS` | Signing cert SHA-256, `AB:CD:...` (comma-separated). Use the Play App Signing key from Play Console. |
| `ANDROID_PLAY_STORE_URL` | Play Store link |

### Hosting requirements

- `SHARE_BASE_URL` must route `/v/*`, `/embed/*`, `/sitemap*`, `/robots.txt` and `/.well-known/*` to this backend, over HTTPS.
- The `/.well-known/*` paths must not redirect.

### App work (Nasir)

- Share button copies the `/v/{id}` link.
- iOS: add `applinks:<domain>` to Associated Domains. Android: add an intent filter with `autoVerify` for `https://<domain>/v/*`. In Expo these go in `app.json` (`ios.associatedDomains`, `android.intentFilters`).
- Handle both `https://<domain>/v/{id}` and `wano://v/{id}` by opening that video.
- Optional: deferred deep links after install (Branch, AppsFlyer or similar).

### After deploying

- Check previews with Facebook's Sharing Debugger, X's Card Validator, and a WhatsApp message to yourself.
- Submit `/sitemap.xml` in Google Search Console.
- WhatsApp shows the preview card. Inline playback inside WhatsApp is limited to a few big platforms, so tapping the card opens the page or the app.

## New: For You feed (Recombee fixes + mixed feed)

### Bugs fixed

| # | Problem | Fix |
| --- | --- | --- |
| 1 | Every logged-out viewer shared one Recombee user (`temp-user`), so they all got the same feed and it never learned. | Each device gets its own profile, `anon-<X-Device-Id>`, which is merged into the account at signup or login. |
| 2 | Videos sent to Recombee without `is_ready_to_stream` were silently filtered out of the feed. | One item builder (`recommendations.item_values`) is used everywhere. Older videos that have a stream count as ready. Admin `POST /api/v1/recombee/sync-items` re-sends the whole catalog. |
| 3 | View, like, comment and bookmark counts were sent once and never updated. | Changed counts are pushed to Recombee every 5 minutes. |
| 4 | The same creator filled the feed, and already-watched videos came back. | Recombee is asked for `diversity=0.3`. The same creator is never shown twice in a row, at most 2 times per page. Videos watched in the last 60 days and videos shown in the last 24 h are skipped. |
| 5 | Comments were sent as bookmarks (deleting a comment wiped a real bookmark); shares and "not interested" were never sent. | Comment = cart addition, share = purchase, not interested = rating -1 (and the post never comes back). New endpoints: `POST /videos/{id}/share`, `POST /videos/{id}/not-interested`. |
| 6 | New users started with a blank profile. | Interest tags and country are sent to Recombee as user properties. Users with tags get a booster toward videos with matching hashtags. |

### Mixed feed

A page is built from four sources: personal (Recombee, about 70%), fresh (posts under 72 h with fewer than 500 views, about 15%), trending (last 7 days, ranked by engagement per hour, about 10%) and following (about 5%). The rules above are then applied.

- **If Recombee is down:** its share of the page is filled from popular recent posts, so the feed never goes empty.
- **Who decides what can be shown:** the database (public, active, ready, published, creator not banned), never Recombee's copy.

### A/B test and measurement

- **Rollout setting:** `FOR_YOU_MIX_ROLLOUT` (default 50) is the percent of viewers who get the mixed feed. The rest get Recombee only, with the same rules. Each viewer always stays in the same group.
- **New fields on feed items:** `source` (personal, fresh, trending, following, popular or featured) and `variant` (mix or control).
- **What gets stored:** every served item is saved to `feed_impressions` (kept 30 days). `POST /videos/{id}/watch` adds the share watched to it.
- **Report:** admin `GET /api/v1/metrics/for-you?days=7` shows, per variant and per source: impressions, watch rate, average share watched, completion rate (90% or more) and skip rate (under 20%).
- **Deciding the winner:** after about a week, compare `mix` against `control`. Set the rollout to 100 if mix wins, or tune the source shares in `app/services/for_you.py`.

### Deploy steps

1. Deploy. The new indexes are created on startup.
2. Call `POST /api/v1/recombee/sync-items` as an admin once. It also creates any missing Recombee properties. Check that its `showable_in_feed` count roughly matches your number of public videos.
3. In the Recombee admin, open the For You scenario and check:
   - `created_at` is a timestamp property.
   - `hashtags` and the user `tags` are sets.
   - The scenario doesn't hide already-viewed items in a way that conflicts with these rules.

### App changes (mobile)

- Send a stable `X-Device-Id` header (a random UUID stored on first launch) on `/feed/v2`, `/videos/{id}/watch`, `/auth/login/json` and `/auth/register`. This is the biggest fix for logged-out users.
- Keep sending `recomm_id` and `portion` to `/watch` for every video that scrolls off.
- Call `/share` when the share sheet completes. Add a "Not interested" option that calls `/not-interested`.
- `source` and `variant` are new optional fields; older app builds ignore them.

## New: Trending (momentum-based, anti-gaming)

### Problems found

| # | Problem |
| --- | --- |
| 1 | GraphQL `trendingVideos` said "last 24 hours" but had no time limit: it returned the 10 most-viewed videos of all time, the same old hits forever. It also didn't filter out processing videos or banned creators. |
| 2 | GraphQL `feed` ("Trending in your country") showed the newest posts with a placeholder score of 0.8. Nothing about it was trending. |
| 3 | The For You trending slot divided lifetime totals by age, so a video that went viral 6 days ago and stopped kept ranking. It ignored finish and skip rates and had no country. |
| 4 | Views could be farmed: every `/watch` call counted. 200 calls from one phone counted 200 views. |
| 5 | Likes (like/unlike toggling), comments and shares could be spammed by one account. Creators' own engagement counted toward their own video. |
| 6 | One malformed video document could crash trending. |

### How trending works now (`app/services/trending.py`)

- **Hourly engagement:** views, finished watches (90% or more), skips (under 20%), likes, comments and shares are recorded per video per hour in `video_stats_hourly` (kept 3 days).
- **Momentum:** only the last 48 h count, and each hour counts half as much as the one 12 h newer. Finished watches, comments and shares weigh more than views; skips subtract. Score = momentum × (0.4 + smoothed finish rate).
- **To trend:** a video needs at least 5 engagements from real accounts. Logged-out views count at 0.1 weight, so fake device IDs can't buy trending.
- **Anti-gaming:**
  - A view counts once per viewer per video per 6 h (`view_dedupe`). Logged-out views are also capped at 1,000 per IP per hour; signed-in views are not IP-capped, because mobile carriers share IPs.
  - Each account's like, comment and share counts once per video (`engagement_once`).
  - A creator's own engagement never counts.
- **Country:** the viewer's country comes first, then the rest of Wano. `?country=` overrides it.
- **Right after deploy:** until hourly data builds up, videos from the last 7 days with at least 5 likes, comments or shares fill in, ranked after the momentum list.
- **Speed and freshness:** the ranking is cached for 60 s, and many simultaneous requests compute it once. Every request re-checks the database, so a deleted or private video drops out immediately.

### Where it's used

- New `GET /api/v1/feed/trending?country=&limit=` (public; uses the viewer's country when signed in) for a Trending tab.
- The For You trending slot (a rising post is labeled `trending`, not `fresh`).
- GraphQL `trendingVideos` and `feed`. `feed` now gives reason "Trending in your country" or "Trending on Wano", and its score is relative to the top video.

### Deploy note

Startup creates the new indexes and TTLs (`video_stats_hourly`, `view_dedupe`, `engagement_once`). Trending improves over the first 48 h as engagement data builds up.

### Known limits

- Many fresh real accounts working together (a farm of verified accounts) can still push a video; account-age weighting is a possible next step.
- The IP cap and the 60 s cache are per server process.

## Round 3 pressure test: views, trending, For You (19 tests; 11 broke the code, all fixed)

| # | Attack or problem | Fix |
| --- | --- | --- |
| D1 | A fake `X-Forwarded-For` header dodged the per-IP cap: 1,500 fake views went through. | The client IP is the entry our own proxy appended (rightmost). New setting `TRUSTED_PROXY_HOPS` (default 1): set it to the number of proxies in front of the API. |
| D2 | One IP could add 300 logged-out views to one video. | At most 50 logged-out views per IP per video per hour. Other videos from the same carrier IP still count. |
| D3 | Two requests counting the same view at the same instant caused a 500. | A duplicate-key race now means "already counted". |
| D4 | Old app builds (no device ID) counted 1 view across all logged-out viewers. | Without a device ID, each IP address is treated as one viewer. |
| D5 | The IP tracker grew without limit (memory exhaustion). | Capped at 100,000 entries: oldest dropped in O(1), idle ones scanned at most once a minute. |
| D6 | `NaN` or `Infinity` as a watch portion caused a 500. | Rejected with 422. The 422 handler no longer echoes the bad value. |
| D7 | 100 accounts skipping a rival's video cut its trending score from 92 to 15. | Skips can remove at most 40% of a video's momentum, and skippers don't add views. A sabotaged video keeps at least half its score. |
| D9 | If the trending computation failed, `/feed/trending` returned a 500. | It serves the last good ranking (or an empty list) and retries in 10 s; For You carries on. |
| D13 | The A/B report read every impression in Python. | It's one MongoDB aggregate. |
| D14 | Each For You page loaded a heavy viewer's entire watch history. | Only the latest 3,000 watches, plus every "not interested". |

These held up first time: unlisted videos never trend, even with real engagement; lowercase country codes work; a device ID can't impersonate a user account; logging in without a device ID merges nothing; 20,000 hourly stats rows compute in time.

API change: 422 responses now list `loc`, `msg` and `type` for each error. FastAPI's `input` and `url` fields are dropped.

## Compatibility with the builds already in the stores (4 fixes)

Reviewed against both the store build and the v2 build before deploying.
Covered by `tests/pressure/test_app_compat.py` (10 tests).

| # | What would have broken | Fix |
| --- | --- | --- |
| A1 | Accounts whose stored email kept its capitals (1,507 of 8,922 live users) could no longer log in or reset their password: the identifier is lowercased, the stored address is not, and the comparison is case-sensitive. Forgot-password returned its generic success, so no code ever arrived. | `_find_user_by_identifier` tries the exact, indexed match first and only then retries ignoring capitals, so an ordinary login still costs one indexed lookup. Registration's duplicate-email checks ignore capitals too, so the same address can't be taken twice in different cases. |
| A2 | Views stopped counting for every store-build user: counting moved to `POST /{id}/watch`, which only v2 calls. `views_count` would have frozen, milestone notifications stopped, and those views never reached trending. | `GET /videos/{id}` counts the view again, through the same `count_view_once` dedupe, so a build that calls both endpoints still counts one view per viewer per video per 6 h. |
| A3 | For You emptied out for logged-out viewers: with no device ID they all share one viewer id, and each page excludes everything that id was shown in the last 24 h. The exclusion list grew with the day's traffic until pages came back short, then empty. | No impressions are read, written or matched for the shared legacy viewer. A real viewer's exclusion list is also capped at the newest `IMPRESSION_MAX` (1,000). |
| A4 | Stitched posts died mid-upload: any job left `waiting` for 30 minutes was failed, and the sweep runs whenever anyone starts a stitch. The app uploads clips in the background and resumes across launches, so a slow post was cancelled by a stranger's upload and its clips could no longer start it. | `waiting` (clips still uploading) gets its own limit, `STALE_WAITING_MINUTES` (24 h). `processing` (stitching itself) keeps the 30-minute one. |

Still worth checking on the server, not fixed in code: `TRUSTED_PROXY_HOPS` must match the number of proxies in front of the API, and uvicorn must be started with `--proxy-headers`. If the API sees only the proxy's address, the login lockout becomes per account rather than per device (10 wrong passwords lock anyone out for 15 minutes), and all logged-out views of a video count as one per 6 h.

---

## New tab (GET /api/v1/feed/)

Attack tests first (tests/pressure/test_new_feed.py: 15 of 20 failed), then fixed,
then a second round on the fix (tests/pressure/test_new_feed_round2.py, 12 tests).

Problems found
1. Paging by skip repeats videos whenever someone posts while the viewer scrolls.
2. One creator posting many videos takes over the tab (30 posts took 8 of 10 slots).
3. "Not interested" was ignored by New.
4. exclude_watched could return empty pages.
5. Old posts with no playable link (empty remoteUrl_CF and remoteUrl) showed as blank videos.
6. One malformed video document (e.g. views_count "lots") returned 500 for the whole page.
7. Videos of accounts deactivated before their videos still showed.
8. Junk paging values were accepted.

What changed
- New cursor paging (app/services/new_feed.py). The app sends `cursor=start`, then the
  `X-Next-Cursor` response header each time. New uploads no longer shift pages; every post
  is shown once. CORS exposes X-Next-Cursor for the web client.
- With a cursor: at most 2 posts per creator per page, never back to back. Held-back posts
  come back on later pages (up to 20 carried; beyond that it's a flood and the extras stay on
  the profile and For You).
- With a cursor, exclude_watched keeps scanning to fill the page.
- Both modes: "not interested" always hidden for logged-in viewers; unplayable legacy posts
  hidden; deactivated creators hidden; bad documents no longer break the page.
- skip is capped at 100000; a bad cursor returns 400; cursor with saved or non-newest sort returns 400.
- Old app builds (skip paging) behave as before apart from the filters above, so nothing
  breaks before the app update. The creator cap and no-repeat paging need the app update.

App change (Nasir)
- New tab: first request `GET /api/v1/feed/?cursor=start&limit=20` (plus `authenticated=false`
  when logged out, `exclude_watched=true` if wanted). Next page: `cursor=<X-Next-Cursor>`.
  No X-Next-Cursor header = end of feed. Don't send skip with cursor.

---

## New tab: "new videos" bubble and your own pinned posts

Attack tests first (tests/pressure/test_new_interactive.py: 22 of 26 failed, the 4 passing
were old-app guards), built, then a second round (test_new_interactive_round2.py) that found
3 more holes, fixed: small pages with pins could push a post out of the feed, a shared video
shown on top could appear again further down, and a future-dated post could sit on top of New
forever and keep the bubble lit.

### GET /api/v1/feed/new-count?since=<id of the newest video on screen>
Add `authenticated=false` when logged out. Read-only, cheap; poll every `poll_after_seconds` (60)
while the New tab is open. Response (Cache-Control: private, max-age=15):

    {"count": 5, "more": false,
     "creators": [{"id": "...", "username": "...", "display_name": "...", "profile_picture": "..."}],
     "newest_at": "2026-11-05T18:02:11Z", "last_post_at": "2026-11-05T18:02:11Z",
     "poll_after_seconds": 60}

- count = posts New would show above `since` (same rules: public, playable, not yours, not
  blocked, not "not interested", active creators). Capped at 99 (`more: true` = "99+").
- creators = up to 3 newest distinct creators, for the avatars on the bubble. Public fields only.
- last_post_at = newest post on New, for "You're all caught up · last post 3h ago".
- A private or deleted `since` falls back to the time inside its id (no date probing).

App: show "▲ N new videos" with the avatars when count > 0; tap or pull = reload New with
`cursor=start`. count == 0 on pull = "You're all caught up · last post <last_post_at ago>"
with Watch Trending / Post something.

### include_own=true on the New tab
`GET /api/v1/feed/?cursor=start&include_own=true` (logged in): up to 3 of your own public posts
from the last 24 h are pinned on top (after a shared video_id, if any), each with
`"pinned": true` and `"status": "processing"` or `"live"` (views in views_count + buffered_views).
Processing posts may have no playable URL yet: show a "Processing…" card, don't play it.
First page only, never repeated, never shown to anyone else, ignored on profile/saved and when
logged out. Without the flag nothing changes (old app builds).

### Hardening that came with it
- Cursor carries the shared video so it is never repeated further down (old cursors still work).
- Posts dated in the future are ignored by New and the bubble.

---

## Creator growth: first viewers, streaks, creator stats (branch creators-growth)

Server-only: merge and deploy, no app update needed for 1 and 2. Attack tests first
(tests/pressure/test_creators.py: 27 of 30 failed), built, then a gaming/sabotage round
(test_creators_round2.py, 12 tests). Found on the way: For You's popular backup (used when
Recombee runs short) could show viewers their own posts. Fixed.

### 1. Every new post gets first viewers (For You "fresh" slots)
Before: fresh slots picked at random from the 200 newest posts. In the test, 33 of 120 new
posts never got a single viewer, and one creator posting 40 videos got 30 of them tested.
Now (app/services/creators.py):
- A post under 48 h is tested on 30 For You viewers (fewest viewers / longest waiting first).
- It keeps being shown only if those viewers stayed: avg watch >= 40% or finish rate >= 25%,
  up to 300 For You viewers. Posts people skip leave the pool (Recombee can still pick them).
- Max 3 posts per creator per day in the pool (flooding and delete/repost buy nothing).
- Only viewers For You chose count toward the test: watches by link or by a ring of accounts
  don't pass it, and bots loading pages without scrolling don't use it up.

### 2. Posting streaks
Consecutive UTC days with at least one public, playable post (several posts in a day = one day).
3+ days: test audience 50 instead of 30, cap 600 instead of 300, higher rank among posts that
passed. Never saves a post people skip. One grouped query for all creators on a page, cached 2 min.

### 3. GET /api/v1/creators/me/stats?days=7   (logged in, days 1-30)
    {"period_days": 7,
     "totals": {"views", "previous_views", "views_change_pct", "finished", "avg_watch_pct",
                "likes", "comments", "shares", "posts"},
     "streak": {"current": 3, "posted_today": true, "boost_active": true, "next_milestone": 7},
     "posts": [{"id", "title", "thumbnail", "created_at", "views", "avg_watch_pct",
                "finish_rate_pct", "likes", "comments", "shares",
                "reach_stage": "testing" | "boosted" | "done"}]}     # newest 20
Only the caller's own posts; nothing about who watched. The creator's own views and engagement
never count. Totals start counting from deploy (no backfill).

### New data
- video_reach: per post (For You impressions, views, watch portion, finishes; fy_* = For You viewers).
- creator_daily: per creator per day, kept 120 days (TTL index).
- Indexes added in app/core/database.py (creator_daily, videos by creator/active/privacy/date).

### How to check on staging after deploy
1. Post 2-3 test videos from a test account. Within minutes they should appear for other test
   accounts in For You with "source": "fresh" (A/B "mix" group).
2. Watch one fully from 2-3 accounts, then GET /api/v1/creators/me/stats as the poster:
   views/finished/avg_watch_pct move; reach_stage "testing".
3. GET /api/v1/metrics/for-you?days=1 (admin): watch rate of "fresh" vs "personal".

---

## Safety check for new posts (branch safety-check, on top of creators-growth)

Server-only. Attack tests first (tests/pressure/test_moderation.py: 13 of 19 failed), built,
then round 2 (test_moderation_round2.py, 10 tests: side doors, report rings, stale Recombee).

- A report counts only from a trusted account: 3+ days old, active, not the creator, and under
  20 counted reports in 24 h. Others are still saved and emailed but can't move a post.
- 1 counted report: the post leaves the first-viewer pool at once (stays in New, profile, Recombee).
- 3 counted reports: held for review. privacy -> "private" (old value kept in
  moderation.prev_privacy), so every public surface drops it; Recombee gets privacy=private.
  The creator still sees it: New pinned status "under_review", stats reach_stage "under_review".
  The creator can't change its privacy while held (REST 409, GraphQL error). Withdrawn reports
  never release a hold.
- Creators under 24 h old: first-viewer test of 10 and cap 100 (instead of 30 / 300).

Admin review (user_type admin):
- GET  /api/v1/reports/admin/queue            held first, then most flagged; pending report counts by category.
- POST /api/v1/reports/admin/videos/{id}/decision  {"action": "restore" | "remove", "note": "..."}
  restore: old privacy back, flags reset, pending reports "dismissed". remove: post deleted,
  pending reports "resolved". The same reporters can't re-report after a review.

Check on staging: report a test post from 3 accounts older than 3 days -> it disappears from
For You/New/trending/share link; the creator sees "under_review"; restore it from the queue.
New accounts can't be used: make them older in the DB for the test, or use real ones.

---

## Real thumbnails (branch real-thumbnails, on top of safety-check)

Server-only. Checked live first: of the 500 newest posts, 6 (1.2%) show the grey placeholder;
the rest have real thumbnails (stored in the wano-africadev bucket, which is the live one —
left as is). Attack tests first (test_thumbnails.py: 8 of 16 failed against the old code;
the 8 passing were guards), then round 2 (webhook replays, scheduled posts, scale).

- Feeds (New, For You, trending, profile, saved), creator stats and Recombee show Cloudflare's own
  frame when a post has no thumbnail or the placeholder. Real thumbnails and photo posts unchanged.
- Only Cloudflare's hosts (videodelivery.net, customer-*.cloudflarestream.com) with a 32-hex id.
- When a video becomes ready, the frame is stored on the post (never over a real thumbnail).
  The webhook's ready steps are now upload_video.mark_stream_ready(uid) (same behaviour).
- One-off backfill (admin): POST /api/v1/recombee/fix-thumbnails  (dry run; ?apply=true to store).

Check on staging: POST /api/v1/recombee/fix-thumbnails -> "would_fix" should be small;
run with ?apply=true; the placeholder posts in New show a video frame.

---

## Stuck-upload rescue (branch stuck-uploads, on top of real-thumbnails)

Server-only. Attack tests first (test_stuck_uploads.py: 14 of 14 failed — nothing existed),
built, then round 2 (test_stuck_uploads_round2.py, 11: real Cloudflare responses, crash
mid-check, rescue racing the webhook, broken push tokens).

Why: a post only appears when Cloudflare's "video ready" webhook arrives. If that message is
missed, the post stays "processing" forever and the creator sees a failed upload.

What it does (app/services/stuck_uploads.py, runs every 5 min, started in app/main.py):
- Posts processing for 10+ min (up to 7 days old), with a Cloudflare video id: ask Cloudflare.
- Ready there: marked ready exactly like the webhook (now upload_video.mark_stream_ready):
  thumbnail, Recombee, creator notified once.
- Failed there, missing there, or still processing after 24 h: marked failed
  (videos.upload_check.state = "failed"), creator gets "Your video couldn't be processed.
  Please try uploading it again." once (notification type "video_failed"). Post stays hidden;
  a late webhook still brings it back. The creator's pinned post on New shows status "failed".
- Still processing: checked again after 5, 15, 30, then 60 min.
- Max 100 checks per run; each post is claimed first, so several servers don't double-check.
- Cloudflare API down or credentials missing: nothing is failed, it retries later.
- Never touches photo posts, deleted posts, or posts without a Cloudflare id.

Admin: POST /api/v1/recombee/rescue-uploads runs it now and returns
{"checked", "ready", "failed", "still_processing", "errors"}.

Needs CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_STREAM_API_TOKEN set (already used for uploads).
App: optional — show "Upload failed, try again" for notification type "video_failed".

Check on staging: run POST /api/v1/recombee/rescue-uploads after deploy. The first run tells
you how many posts were stuck; "ready" ones appear in feeds right away.

---

## Wano TV film protection (branch tv-protect, on top of stuck-uploads)

Server-only, no app change. Attack tests (test_tv_protect.py 13, test_tv_protect_round2.py 10):
12 fail against the old catalogue code; the rest guard the new signing code (forged/expired/
tampered tokens, path tricks, foreign URLs injected into playlists, stampedes, 2-hour playlists,
storage errors, a late start getting segments that outlast a pause).

Before: the catalogue gave the app the film's public CDN address. Anyone with it could download
the full 1080p film (checked live with Middleton).

Now (app/services/tv_stream.py):
- /api/v1/tv/titles returns video_url = {BACKEND_URL}/api/v1/tv/stream/<token>/master.m3u8.
  The token is signed with SECRET_KEY and expires after 6 h (a fresh one every catalogue load).
- That endpoint reads the playlist from Spaces privately, points quality levels and subtitles back
  through the same token, and gives video segments Spaces links signed for 4 h-7 days.
  Anything outside the title's own folder is dropped. No login header needed by the player.
- Expired link: 403 "This link has expired" -> the app reloads the catalogue (it does on open).
- Trailers, posters and artwork stay public.
- Upload scripts now make new film files private (acl_for); trailers and art public.

Steps, in order:
1. Deploy. Check BACKEND_URL is the address phones reach (today https://devbe.wanoafrica.com).
2. On a phone: open Middleton in Wano TV, play, seek, turn subtitles on, pause a few minutes, resume.
3. Only then lock the files:  python -m scripts.protect_wano_tv  (dry run lists the files)
                              python -m scripts.protect_wano_tv --apply
4. Check the old public address now fails:
   https://wano-africadev.lon1.cdn.digitaloceanspaces.com/wanoafrica-dospaces-key/wano-tv/middleton/master.m3u8 -> 403
Undo if ever needed: set those keys back to public-read.

Trade-off to watch: film segments now come from the Spaces origin (London) with signed links,
not the CDN edge (Spaces CDN doesn't serve private files). If start-up or buffering in Africa is
noticeably worse, the long-term fix is moving films to Cloudflare Stream with signed URLs
(shorts already use Stream).

---

## Wano TV for logged-out viewers (branch tv-open, on top of tv-protect)

Server-only. Attack tests first (test_tv_open.py: 2 of 5 failed; the 3 passing are guards).
- GET /api/v1/tv/titles and /api/v1/tv/genres work logged out with authenticated=false (same
  rule as the feeds). Film links are still the expiring signed ones.
- History (GET /tv/history, POST /tv/history/{id}) still needs login.
- No token and no authenticated=false: 401, as before.
App: if Wano TV is hidden for logged-out users, it can now be shown (send authenticated=false).

---

## Growth notifications + weekly/monthly posting recap (branch growth-notifications, on top of tv-open)

Server-only (push notifications the app already receives). Attack tests first:
test_growth_notifications.py (23, all failed: nothing existed) and test_posting_recaps.py
(16, all failed). Everything runs in app/services/growth_notifications.py, started in app/main.py.

Found and fixed on the way: the stuck-upload rescue awaited the blocking Expo sender, which
blocked the server while sending and then logged a false "did not send". The test stand-in for
push is now blocking like the real one, so this can't hide again (test UR7).

### Followers: "Amina just posted" (every 2 min)
- One alert to followers when a post goes live (public, playable, not held for review),
  including scheduled posts when they publish and rescued uploads.
- Per creator: one follower alert per 24 h (their first post of the day).
- Per follower: max 5 a day, none in quiet hours (22:00-08:00 in their country's time),
  none if either side blocked the other, none if they turned new_posts off.
- Each post is claimed first: no doubles across runs or servers. Failed Expo batches are logged,
  not retried in a loop. Posts from before the server started aren't announced (no deploy blast).
- Sent in Expo batches of 100. data: {"video_id", "type": "new_post"} (same as view milestones).

### Creators: tips (max 1 a day, none in quiet hours, off with creator_tips)
- When a post finishes its first-viewer test: "Your new video's first 30 viewers watched 78% on
  average. It's being shown to more people 🚀" (or a tip if they skipped). Once per post.
- Streak reminder, 19:00-22:00 local, creators with a 2+ day streak who haven't posted today:
  "🔥 Your 4-day streak ends tonight. Post something to keep it going." Once a day.

### Creators: weekly / monthly recap (every 30 min, creator's own time zone)
- Monday 09:00-12:00: "Last week you posted 5 videos, 2 more than the week before, and got
  1,240 views 🎉". A quiet week after an active one: "You didn't post last week. Your followers
  are waiting — share something today 🎬". Nobody who hasn't posted in 2 weeks is nagged.
- 1st of the month 10:00-13:00: "In October you posted 18 videos and got 12.4k views 🎉".
  When the 1st is a Monday, only the monthly one.
- Counts live public and unlisted posts (not private, deleted, scheduled or processing).
  Weeks run Monday-Sunday. Once per creator per week / month; off with creator_tips.
- GET /api/v1/creators/me/stats now also returns
  "posting": {"this_week", "last_week", "this_month", "last_month"}.

### Switches
GET / PUT /api/v1/notifications/preferences  {"new_posts": bool, "creator_tips": bool}
(logged in; default on; only these two fields are stored). The app can add two toggles any time.

Push only: nothing goes into the in-app notifications list, so older app builds are unaffected.
Time zones are by country (standard time, no daylight saving); unknown countries use UTC.
Streak days are UTC days (Kampala's day flips at 03:00), as in creators.py.

### Check on staging
1. Two test accounts, B follows A. A posts: B gets "A just posted" within ~2 min; A posts again:
   B gets nothing more today.
2. PUT /api/v1/notifications/preferences {"new_posts": false} as B; A's next-day post: nothing.
3. GET /api/v1/creators/me/stats as A: "posting" counts match.
4. Recaps: wait for Monday morning, or run posting_recaps(db) from a shell with the clock set.

---

## Pre-push review and round 3 (on growth-notifications)

- Whole attack suite now also runs on a real MongoDB 8.3 (set WANO_REAL_MONGO=mongodb://host:port),
  each test on a fresh database with the app's real indexes (unique, TTL, partial).
  476/476 pass on both the in-memory mock and real MongoDB; 39/39 original tests pass.
- Checked with explain() on real MongoDB: New tab, first viewers, follower alerts, creator stats,
  streaks and recap scans all use indexes (no collection scans).
- Fixed: after a stuck-upload rescue, the "uploaded" push ran the blocking Expo sender on the
  server's event loop (froze the server while sending). Now runs in a thread (test UR8).
- Fixed: the posting recap scanned every recent creator every 30 min even on days no country is
  in its Monday / 1st window. Now it does nothing on those runs (test R6).
- Fixed: trending's and Wano TV's locks were bound to the first event loop that used them.
- Test harness: generated usernames were cut to 20 chars and could collide on a real unique index.
- New cross-feature attacks (test_cross_round3.py): a held post or a banned creator disappears
  from New, the bubble, For You, first viewers, trending, share page, sitemap and follower alerts
  at once; one report pauses only first viewers; turning creator tips off silences every creator
  notification; a late Cloudflare webhook after a failed rescue restores the post without
  announcing a day-old post as new.

---

## Atlas Performance Advisor fixes (branch db-advisor, on top of growth-notifications)

Checked the live cluster in Atlas (WanoApp, M10, MongoDB 8.0.37, AWS Paris, ~10k videos,
~9k users). Advisor: 6 index suggestions, 5 redundant indexes. Attack tests first
(test_db_advisor.py, 10; 5 failed on real MongoDB before the fix).

Slow queries and what changed:
- Posting a video checks no other creator uses the same Cloudflare video; the "video ready"
  webhook and deletes look videos up by Cloudflare id. All fell back to matching every
  video's URL (whole collection, ~146 ms). Now the URL fallback only looks at posts with no
  stored stream_uid, on a new index (stream_uid, creator_id). After the backfill below, these
  read a handful of documents instead of all of them (measured on real MongoDB: <= 100 of 3000).
- Registering a phone's push token searched every user (~247 ms). New index users.expo_push_tokens.
- Stuck-upload scan: covered by (isReadyToStream, created_at) (already on stuck-uploads).

Redundant indexes (each a prefix of a compound index that stays): the app no longer creates
videos.created_at_-1, videos.creator_id_1, videos.is_active_1_privacy_1, comments.user_id_1,
comments.video_id_1.

After deploying, once:
1. POST /api/v1/recombee/backfill-stream-uids            (admin; dry run shows how many)
   POST /api/v1/recombee/backfill-stream-uids?apply=true
2. python -m scripts.drop_redundant_indexes              (dry run)
   python -m scripts.drop_redundant_indexes --apply      (only drops an index if its cover exists)
3. Atlas > Performance Advisor a day later: the suggestions above should be gone.

Also seen in Atlas (not code):
- Billing: "We were unable to charge your credit card ... account suspended on 12/01/26."
- The Goodtown API (goodtown-api) connects to the same WanoApp cluster.
- No maintenance window and no security/operations contacts set.

---

## Database performance (branch db-performance, on top of db-advisor)

From Atlas Query Insights (live, 24 h) and scripts/perf_profile.py: the main endpoints profiled
with MongoDB's own profiler on production-sized data (9k users, 10k videos, follows, likes,
watch history, comments, notifications). Attack tests: test_db_performance.py (12; real-MongoDB
checks marked, they skip on the in-memory mock).

    WANO_REAL_MONGO=mongodb://127.0.0.1:27017 python -m scripts.perf_profile

Before -> after (docs read per request, real MongoDB):
- Login: 642 -> 1 (wrong username/email: 9000 -> 0). The unique email/phone indexes are partial,
  which MongoDB can't use for a plain {"email": x}; added plain lookup indexes. Also helps sign-up
  checks, forgot password and every other email/phone lookup.
- User search: 9002 -> 502, 23 -> 5 ms. Case-insensitive prefix via collation indexes instead of a
  /^term/i regex that checked every user; bounded at 500 candidates for 1-letter searches.
- For You: 25,448 -> 1,261 docs, 47 -> 37 round trips, 248 -> 30 ms. Streaks for ranking look back
  8 days (all the ranking uses) instead of 120; the popular backup is one shared pool rebuilt every
  5 min (walks a new likes_count index) instead of an unindexed sort on every request; its posts
  are re-checked by id so a post made private since never slips through.
- "Not interested": partial index (watch_history by user where not_interested), no longer reads the
  user's whole history (Atlas: ~195,000 docs read per doc returned).
- Sign-up codes: registration_otps.email and registration_whatsapp_otps.phone_number indexes.

Live cluster (Atlas Real Time): CPU 8%, memory 0.8 / 2 GB, a few IOPS. M10 is plenty for now.
Not code, worth acting on:
- A wano_app_dev database lives on the production cluster (and goodtown-api uses it too).
  Dev and other apps share production's CPU, memory and backups.
- Every query is a network round trip to Atlas in Paris: run the API in the same region (Paris or
  nearby) or move the cluster next to the API.
- Old app builds page New by skip (deep pages read thousands of posts); the cursor paging fixes
  this once the app update ships.
- Remaining regex searches (video descriptions, comment authors, email search) still scan; Atlas
  Search is the long-term fix.
