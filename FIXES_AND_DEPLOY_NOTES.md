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
3. **Set the new env var `LAMBDA_CALLBACK_SECRET`.** Use the same value on the backend and on
   the stitch Lambda. Without it, the Lambda's presign call gets a 401 and stitching fails.
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
- **Tokens expire.** Tokens now carry an expiry (`ACCESS_TOKEN_EXPIRE_MINUTES`, currently
  180 days). A password reset logs out every existing session.
- **Profiles are trimmed for other viewers.** `email`, `phone_number`, `date_of_birth` and
  `gender` come back `null` unless you are the owner or an admin. `GET /users/` requires login
  and returns at most 100 users.
- **Privacy is enforced.** Private and unlisted videos are visible only to their creator, on
  REST, GraphQL and in the feeds.
- **Views are counted only by `POST /videos/{id}/watch`.** `GET /videos/{id}` no longer counts
  a view.
- **Upload endpoints need login.** Uploads are capped at 200 MB for video, 20 MB for images and
  50 MB for audio; presigned uploads at 2 GB. SVG is rejected.
- **Stitch input is restricted.** Clips must be Cloudflare Stream URLs and audio must be on the
  Spaces CDN or be a library song. The limit is 20 clips.
- **Comment rules.** Replying to a reply returns 400. Commenting on a video with comments
  disabled returns 403.
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
