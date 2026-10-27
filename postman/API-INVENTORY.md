# API inventory

Generated from the currently registered routes. Authentication reflects the route dependencies; ownership/admin checks may impose additional restrictions.

| Method | Path | Folder | Authentication |
| --- | --- | --- | --- |
| GET | `/debug/tasks` | System | Public |
| POST | `/api/v1/auth/register` | authentication | Public |
| POST | `/api/v1/auth/request-registration-otp` | authentication | Public |
| POST | `/api/v1/auth/verify-registration-otp` | authentication | Public |
| POST | `/api/v1/auth/login/json` | authentication | Public |
| GET | `/api/v1/auth/verify-email` | authentication | Public |
| POST | `/api/v1/auth/resend-verification` | authentication | Public |
| GET | `/api/v1/auth/check-verification/{username}` | authentication | Public |
| POST | `/api/v1/auth/forgot-password` | authentication | Public |
| POST | `/api/v1/auth/verify-reset-otp` | authentication | Public |
| POST | `/api/v1/auth/reset-password` | authentication | Public |
| GET | `/api/v1/users/search` | users | Bearer |
| PUT | `/api/v1/users/me` | users | Bearer |
| PATCH | `/api/v1/users/me` | users | Bearer |
| POST | `/api/v1/users/me/push-token` | users | Bearer |
| DELETE | `/api/v1/users/me/push-token` | users | Bearer |
| DELETE | `/api/v1/users/delete` | users | Bearer |
| DELETE | `/api/v1/users/ban/{user_id}` | users | Bearer |
| POST | `/api/v1/users/block_user/{target_user}` | users | Bearer |
| POST | `/api/v1/users/unblock_user/{target_user}` | users | Bearer |
| GET | `/api/v1/users/{user_id}/complete` | users | Bearer |
| GET | `/api/v1/users/{user_id}/raw` | users | Bearer |
| GET | `/api/v1/users/{user_id}/stats/detailed` | users | Bearer |
| GET | `/api/v1/users/me/liked-videos` | users | Bearer |
| GET | `/api/v1/users/me/bookmarked-videos` | users | Bearer |
| PATCH | `/api/v1/users/me/localization` | users | Bearer |
| PATCH | `/api/v1/users/me/theme` | users | Bearer |
| POST | `/api/v1/users/` | users | Public |
| GET | `/api/v1/users/` | users | Public |
| GET | `/api/v1/users/{user_id}` | users | Public |
| PUT | `/api/v1/users/{user_id}` | users | Bearer |
| POST | `/api/v1/users/{user_id}/follow` | users | Bearer |
| DELETE | `/api/v1/users/{user_id}/follow` | users | Bearer |
| GET | `/api/v1/users/me/profile` | users | Bearer |
| PUT | `/api/v1/users/me/tags` | users | Bearer |
| DELETE | `/api/v1/users/me/tags` | users | Bearer |
| POST | `/api/v1/users/me/tags/add` | users | Bearer |
| GET | `/api/v1/users/{user_id}/relationships` | users | Bearer |
| GET | `/api/v1/videos/scheduled` | scheduled posts | Bearer |
| PATCH | `/api/v1/videos/{video_id}/schedule` | scheduled posts | Bearer |
| DELETE | `/api/v1/videos/{video_id}/schedule` | scheduled posts | Bearer |
| POST | `/api/v1/videos/{video_id}/publish-now` | scheduled posts | Bearer |
| POST | `/api/v1/videos/post` | videos | Bearer |
| POST | `/api/v1/videos/stitch` | videos | Bearer |
| POST | `/api/v1/videos/` | videos | Bearer |
| GET | `/api/v1/videos/` | videos | Public |
| GET | `/api/v1/videos/search` | videos | Public |
| GET | `/api/v1/videos/{video_id}` | videos | Bearer |
| PUT | `/api/v1/videos/{video_id}` | videos | Bearer |
| DELETE | `/api/v1/videos/{video_id}` | videos | Bearer |
| POST | `/api/v1/videos/{video_id}/watch` | videos | Bearer |
| POST | `/api/v1/videos/{video_id}/like` | videos | Bearer |
| DELETE | `/api/v1/videos/{video_id}/like` | videos | Bearer |
| POST | `/api/v1/videos/{video_id}/bookmark` | videos | Bearer |
| DELETE | `/api/v1/videos/{video_id}/bookmark` | videos | Bearer |
| POST | `/api/v1/` | comments | Bearer |
| GET | `/api/v1/search` | comments | Bearer |
| GET | `/api/v1/user/{user_id}` | comments | Bearer |
| GET | `/api/v1/video/{video_id}` | comments | Bearer |
| GET | `/api/v1/{comment_id}/replies` | comments | Bearer |
| PUT | `/api/v1/{comment_id}` | comments | Bearer |
| DELETE | `/api/v1/{comment_id}` | comments | Bearer |
| POST | `/api/v1/{comment_id}/like` | comments | Bearer |
| GET | `/api/v1/feed/` | feed | Bearer |
| GET | `/api/v1/feed/v2` | feed | Bearer |
| POST | `/api/v1/metrics/flush` | metrics | Bearer |
| GET | `/api/v1/metrics/buffer-status` | metrics | Bearer |
| POST | `/api/v1/reports/videos/{video_id}/report` | reports | Bearer |
| GET | `/api/v1/reports/videos/{video_id}/reports` | reports | Bearer |
| GET | `/api/v1/reports/my-reports` | reports | Bearer |
| DELETE | `/api/v1/reports/reports/{report_id}` | reports | Bearer |
| GET | `/api/v1/reports/reports/{report_id}` | reports | Bearer |
| POST | `/api/v1/drafts/` | drafts | Bearer |
| GET | `/api/v1/drafts/` | drafts | Bearer |
| GET | `/api/v1/drafts/{draft_id}` | drafts | Bearer |
| PUT | `/api/v1/drafts/{draft_id}` | drafts | Bearer |
| PATCH | `/api/v1/drafts/{draft_id}` | drafts | Bearer |
| DELETE | `/api/v1/drafts/{draft_id}` | drafts | Bearer |
| GET | `/api/v1/drafts/count/me` | drafts | Bearer |
| GET | `/api/v1/music/songs` | music | Public |
| GET | `/api/v1/music/favorites` | music | Bearer |
| POST | `/api/v1/music/songs/{song_id}/favorite` | music | Bearer |
| DELETE | `/api/v1/music/songs/{song_id}/favorite` | music | Bearer |
| GET | `/api/v1/categories/` | categories | Bearer |
| GET | `/api/v1/config/version` | config | Public |
| POST | `/api/v1/recombee/test-first-item` | recombee | Public |
| POST | `/api/v1/recombee/ingest-videos` | recombee | Public |
| POST | `/api/v1/recombee/backfill-hashtags` | recombee | Public |
| POST | `/api/v1/notifications/broadcast` | notifications | Public |
| GET | `/api/v1/notifications/` | notifications | Bearer |
| POST | `/video/upload` | Upload Video | Public |
| POST | `/video/v2/presigned-upload` | Upload Video | Public |
| POST | `/video/presigned-upload` | Upload Video | Public |
| GET | `/video/{uid}` | Upload Video | Public |
| POST | `/video/check-stream-status` | Upload Video | Public |
| POST | `/video/cf-webhook` | Upload Video | Webhook HMAC signature |
| GET | `/graphql` | System | Public |
| POST | `/graphql` | System | Public |
| GET | `/` | System | Public |
| GET | `/health` | System | Public |

## GraphQL operations

POST `/graphql` for queries and mutations. WebSocket `/graphql` for subscriptions.

- Query: `me`, `user`, `users`, `video`, `videos`, `feed`, `videoComments`, `searchVideos`, `metricsStatus`
- Mutation: `compileVideo`, `createComment`, `updateComment`, `register`, `login`, `updateUser`, `createVideo`, `updateVideo`, `deleteVideo`, `likeVideo`, `unlikeVideo`, `followUser`, `unfollowUser`, `deleteComment`, `bookmarkVideo`, `unbookmarkVideo`, `flushMetrics`
- Subscription: `videoMetrics`, `newComments`, `followerCount`, `trendingVideos`
