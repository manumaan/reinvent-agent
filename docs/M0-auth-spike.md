# M0 spike: Builder ID auth and unattended reservations

**Question (design Q1):** can the Oct 8 reservation run work without the user present?
**Answer: yes, with one local sign-in no earlier than 30 days before the run.**

## What the Events API auth requires
From the developer guide's Authentication pages: *Signing an attendee in*, *Endpoints and values*, *Keeping the attendee signed in*.

| Item | Value |
|---|---|
| Flow | OAuth 2.0 authorization code + PKCE (S256). No client secret. |
| Authorize | `https://oauth.awsevents.com/oauth2/authorize` with `identity_provider=AWSBuilderID` (always) |
| Token | `https://oauth.awsevents.com/oauth2/token` |
| Client ID | `7vmom55m1qstvq8i71ph127bfq` (public, shared by every caller) |
| Redirect URI | `http://localhost:8484/callback` **only**. There is no hosted redirect, so sign-in must run locally. |
| Scope | `openid email events/access` |
| Access token | JWT, 60 min |
| Refresh token | opaque, **30 days**, **may rotate**: when a refresh response carries a new one, the old one may stop working |
| API | `https://api.awsevents.com/v1/...` with `Authorization: Bearer <access token>` |
| Throttling | `429` + `Retry-After` (seconds left in the current minute). A refused request spends no quota. |

## Resulting design
```
laptop:  reinvent-agent auth login        → PKCE flow, tokens in ~/.config/reinvent-agent/tokens.json (0600)
laptop:  reinvent-agent auth push-secret  → copies tokens to Secrets Manager (DataStack.BuilderIdTokens)
cloud:   TokenProvider(SecretsManagerTokenStore)
           - refreshes when <2 min of access-token life remains, or on a 401
           - writes the rotated refresh token back *before* using the new access token
```
Implemented in `src/reinvent_agent/events_api/auth.py`; tests are in `tests/test_auth.py`.

## Consequences for the plan
1. **Sign in between Sep 8 and Oct 8** so the refresh token is still valid at reservation open. A daily keep-alive refresh (EventBridge, M3) keeps the 30-day window rolling through the conference.
2. **Only one writer at a time.** Rotation means two processes refreshing concurrently can invalidate each other. M3 wraps refresh in a DynamoDB conditional-write lease. In-process, `TokenProvider` already serializes with a lock.
3. **The web UI cannot sign users in directly** because only a localhost redirect is allowed. The "Connect AWS Builder ID" button in the design becomes "run `reinvent-agent auth login && reinvent-agent auth push-secret`". That is fine for a single-user build. A multi-user version would need a small local helper app.
4. **Fallback kept:** if the refresh fails at run time (revoked or expired), the reservation Lambda sends an SNS notification with the exact CLI command to run. The approved plan is kept, so re-running is one command.

## Still to verify against the live API
Blocked from this sandbox: `api.awsevents.com` and `docs.aws.amazon.com` are not in the network allowlist.
- Exact JSON field names (the models accept several spellings for now; see `models.py`)
- Exact reason codes on `ReserveSessions` failures
- Whether `reinvent2025` is still served (design Q4)
- Numeric quotas from the *Quotas and throttling* page
