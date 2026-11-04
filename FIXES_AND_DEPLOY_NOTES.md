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
