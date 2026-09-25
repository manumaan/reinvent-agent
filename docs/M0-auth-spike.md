# M0 spike: Builder ID auth and unattended reservations

**Question (design Q1):** can the Oct 8 reservation run work without the user present?
**Answer: yes, with one local sign-in shortly before the run (plan: evening of Oct 7).**

## What the Events API auth requires
From the developer guide's Authentication pages, read in full on 2026-09-25: *Endpoints and values*, *Signing an attendee in*, *Keeping the attendee signed in*, *Signing an attendee out*, *Handling tokens safely*.

| Item | Value |
|---|---|
| Flow | OAuth 2.0 authorization code + PKCE (S256). No client secret. |
| Authorize | `https://oauth.awsevents.com/oauth2/authorize` with `identity_provider=AWSBuilderID` (always) |
| Token | `https://oauth.awsevents.com/oauth2/token` |
| Client ID | `7vmom55m1qstvq8i71ph127bfq` (public, shared by every caller) |
| Redirect URI | `http://localhost:{8484..8489}/callback`, matched exactly. "Your application must run on the attendee's machine"; there is no hosted redirect. |
| Revoke | `https://oauth.awsevents.com/oauth2/revoke` (the refresh token; access tokens already issued stay valid for up to 60 min) |
| Scope | `openid email events/access` |
| Access token | JWT, 60 min |
| Refresh token | opaque, **30 days**, **may rotate**: when a refresh response carries a new one, the old one may stop working |
| Builder ID session | **separate lifetime, not extended by refreshing.** "A long-running application will eventually need an interactive sign-in, however recently it refreshed." The lifetime is not documented. |
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
1. **Sign in the evening before reservations open (Oct 7).** Refreshing keeps the access token fresh but cannot outlive the Builder ID session, whose lifetime is undocumented, so signing in a month early is not safe. At T-30 min the reservation job does a pre-flight refresh plus `GetSchedule`. If that fails, it notifies you straight away (SNS) with the one command to run, while there's still time.
2. **Only one writer at a time.** Rotation means two processes refreshing concurrently can invalidate each other. M3 wraps refresh in a DynamoDB conditional-write lease. In-process, `TokenProvider` already serializes with a lock.
3. **Sign-in happens in the local Streamlit app (or CLI)**, never in a hosted UI, because only a localhost redirect is allowed. The login binds the first free port from 8484–8489 and sends the identical `redirect_uri` on the token exchange.
4. **Fallback kept:** if the refresh fails at run time (revoked or expired), the reservation Lambda sends an SNS notification with the exact CLI command to run. The approved plan is kept, so re-running is one command.
5. **Sign-out** (`reinvent-agent auth logout`) revokes the refresh token, which also kills the copy pushed to Secrets Manager, and deletes local tokens. The Builder ID browser session is ended separately at https://profile.aws.amazon.com.

## Still to verify against the live API
`docs.aws.amazon.com` is now reachable, and the guide has been read. `api.awsevents.com` is still blocked from this sandbox, so these are open:
- Exact JSON field names for sessions and events (personal time fields are documented and now exact; the rest accept several spellings; see `models.py`)
- Exact reason codes on `ReserveSessions` failures
- Whether `reinvent2025` is served with `includePast=true` (design Q4)
