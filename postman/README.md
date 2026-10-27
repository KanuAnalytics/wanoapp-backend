# WanoApp Postman collection

## Import and start

1. Import **WanoApp.postman_collection.json** into Postman.
2. Optionally import **WanoApp.postman_environment.json** and select **WanoApp Local**. Collection variables also work without an environment.
3. Set `base_url` to your running backend, e.g. `http://localhost:8000`, without a trailing slash. The backend needs its usual `.env`, MongoDB and relevant service configuration.
4. Set `username` and `password` to an existing account. Run **Authentication → Login Json**. Successful login automatically saves `token` and `current_user_id`. Registration and GraphQL login/register save these too.
5. Set the target resource IDs and real media URLs before sending other requests. Open each request's documentation for its body schema, parameters, auth and source location.

An active environment takes precedence over collection variables. Response scripts update collection variables and matching variables in the selected environment. Remove stale local overrides if a request uses an unexpected value. `token` contains the JWT only; Postman adds `Bearer`.

## Coverage and files

- **100/100 HTTP operations** from the actual FastAPI OpenAPI schema, including upload routes, system routes and GraphQL transport endpoints.
- **26 individual GraphQL operations:** 9 queries and 17 mutations.
- **139 collection requests** total, including workflow variants and FastAPI documentation URLs.
- **4 WebSocket subscriptions**, with ready-to-copy protocol frames in `wanoapp.subscriptions.json`.
- `API-INVENTORY.md`: every registered operation and its authentication.
- `wanoapp.openapi.json`: exported Swagger/OpenAPI schema.
- `wanoapp.graphql`: exported GraphQL schema, including nested response fields such as comment replies and video creators.
- `validation-report.json`: offline verification results.

## Variables

| Variables | Purpose |
| --- | --- |
| `base_url`, `ws_url` | HTTP and WebSocket server origins, with no trailing slash. Change both for another server; use HTTPS/WSS where appropriate. |
| `token`, `current_user_id` | Populated by successful authentication. |
| `username`, `password`, `new_password`, `email`, `phone_number` | Test-account credentials/contact details. Defaults are fake examples. |
| `otp`, `verification_token` | Enter the real OTP or email-verification link token; these are not the access token. |
| `user_id` | Target user for lookup/follow/block. Kept separate from `current_user_id` so login does not turn follow requests into self-follows. |
| `video_id`, `comment_id`, `draft_id`, `report_id` | Existing MongoDB IDs. Successful REST creation requests save the corresponding ID when returned. |
| `song_id`, `category_id`, `subcategory_id` | IDs from the relevant server data. Song IDs may differ from MongoDB IDs. |
| `video_url`, `stream_url`, `stream_uid` | Real uploaded media URL, Cloudflare playback URL and Cloudflare UID. `stream_uid` is not a MongoDB video ID. |
| `image_url`, `audio_url`, `thumbnail_url` | Real media URLs. Direct upload captures the URL for its returned media type. |
| `search_query`, `scenario`, `recomm_id` | Search text, configured Recombee scenario, and recommendation tracking/pagination ID. Set recommendation IDs from actual feed responses when needed. |
| `scheduled_at`, `timezone` | Future ISO timestamp with timezone and IANA timezone name. If blank, the collection pre-request script supplies a timestamp 24 hours ahead for that request. |
| `expo_push_token` | Real device push token for add/remove requests. |
| `webhook_signature` | Signature for the exact webhook request bytes. See that request's documentation. |

## Common workflows

**Accounts:** Use REST registration or login. Email is the default registration example; WhatsApp alternatives are in **Workflow Examples**. Registration needs either email or phone number, a unique username/contact, and a password containing an uppercase letter and a digit. Registration OTP request/verification are separate operations; enter the code actually received. Password reset is Forgot Password → Verify Reset Otp → Reset Password, using the real reset OTP. Email verification uses `verification_token` from the verification link.

**Video/photo upload:** In **Upload Video → Upload Video**, select a local file for the `video` form-data field. Set query `isAudio`/`isImage` as appropriate. Do not manually set a multipart Content-Type boundary. Presigned routes return upload instructions, not a completed upload; follow the returned direct-upload/TUS protocol. For video posts set both `video_url` and `stream_url`, then call **Videos → Post Video**. Use **Workflow Examples → Create photo post** for an `images` body. Set the real Cloudflare UID separately before checking stream status.

**Scheduling:** Use **Workflow Examples → Create scheduled video post**. Then list scheduled posts, reschedule, publish now or cancel using its `video_id`. The creator must own the scheduled post. A real, future `scheduled_at` is required; the blank default is filled at request time.

**Comments/drafts/reports:** Create against an existing video. REST creation responses save IDs where returned. A reply example supplies `parent_id`. Draft examples include a complete clip. Report visibility and updates/deletes can require resource ownership; banning a user requires an admin.

**Feeds:** Authenticated access is the default. Anonymous variants explicitly use `authenticated=false` and No Auth. Saved feeds require authentication. For v2 choose a valid Recombee `scenario`; enable optional `next_recomm_id` after obtaining a real recommendation ID.

Optional query filters without defaults are present but unchecked. Defaulted query parameters are enabled. Enable/edit filters in Postman's Params tab. Full request schemas in request descriptions list optional body fields that a particular example omits. Localization has `country` in the query and `languages`/`tribes` in JSON; theme updates use query parameters only.

## GraphQL subscriptions in Postman

HTTP collection v2.1 requests do not represent a WebSocket session. Subscription examples are therefore supplied separately rather than as HTTP requests that would fail.

1. Create a WebSocket request to `{{ws_url}}/graphql` and set the WebSocket subprotocol to `graphql-transport-ws` (`Sec-WebSocket-Protocol` header if needed).
2. Connect and send the `connection_init` JSON frame from `wanoapp.subscriptions.json`.
3. Wait for `connection_ack`.
4. Copy a subscription's `subscribe_frame`, fill its variables with real IDs, and send it. Reuse unique IDs for concurrent subscriptions.
5. Send `{"id":"<subscription id>","type":"complete"}` to stop that subscription, then disconnect when done.

Available subscriptions: `videoMetrics`, `newComments`, `followerCount`, `trendingVideos`. The current subscription resolvers do not require authentication; connection initialization does not need a token. HTTP queries/mutations that require authentication use the normal Authorization header. All four subscription documents and input values were validated against the real schema.

## Verification and existing backend limitations

The generator loaded the actual application with dummy configuration and network connections disabled. It did not read project secrets, start the server lifespan, invoke API handlers, or contact MongoDB/external services. Coverage was checked against the application's registered OpenAPI operations. **40 JSON example bodies** (including variants) passed FastAPI/Pydantic model validation; **26 GraphQL operations and 4 subscriptions** passed GraphQL document and variable validation. All referenced collection variables are defined. This verifies request structure, not live service behavior. Real credentials, IDs, media, OTP delivery and configured integrations are still needed for successful responses.

Source findings that affect usage:

- Swagger's OAuth2 token URL points at `/api/v1/auth/login`, but the registered login route is `/api/v1/auth/login/json`.
- Comments are mounted directly at `/api/v1/`, `/api/v1/search`, `/api/v1/video/{video_id}`, etc.; there is no `/comments` prefix.
- GraphQL `createComment` currently reads `input.parent_comment_id`, while its schema defines `parentId`; its schema-valid request can fail at runtime. The backend code was left unchanged.
- Some GraphQL auth paths create/expect legacy `hashed_...` passwords, while REST uses bcrypt. Prefer REST registration/login when testing REST account flows.
- `/video/cf-webhook` requires its HMAC signature even though OpenAPI does not describe a security scheme for it. The sample body/header are supplied explicitly.
- Broadcast sends real pushes to all registered devices. Ingestion/backfill, deletion, banning, publishing, metrics flush and other writes have actual side effects. Send these individually with intended test data rather than running the entire collection indiscriminately.

## Maintain the collection after API changes

Update `WanoApp.postman_collection.json` directly when adding or changing an API. Add the request to the appropriate folder with its actual method, path, authentication, parameters, example body and brief documentation. You can also edit the collection in Postman and export it back to this file using collection v2.1 format.

Add any new variables to both the collection and `WanoApp.postman_environment.json`, using placeholder values rather than real credentials or tokens. Update `API-INVENTORY.md` and the relevant OpenAPI/GraphQL reference files when those APIs change.

`validation-report.json` records the initial offline validation; it does not automatically validate later edits. Recheck changed requests against the current backend schema and update the report only after verifying them. The temporary generation script has been removed; this folder is the maintained API collection.
