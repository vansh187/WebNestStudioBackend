# Playground packages, Day 1: backend notes

For the frontend team. This covers the relay v2 readiness review (B4) and our comments on the database proxy draft (B5.2 / B5.3). The proxy is already built against the draft and ships switched off (`DB_PROXY_ENABLED=false`).

## B4. Relay v2 readiness

None of these is a blocker. The estimates are backend time once the v2 contract arrives.

| v2 feature | Current state | Change needed | Estimate |
|---|---|---|---|
| Wildcard hosts (`host_suffixes`, one level) | `JsonProviderRegistry.for_host` is an exact dict lookup. `TargetParser` already rejects IP literals, ports and user info. | In `for_host`: exact match first, then a suffix match where the remaining part is exactly one DNS label (so `a.b.supabase.co` is refused). Registry validation for `host_suffixes`. The DNS public-address check and IP pinning already apply to every host. | 0.5 day |
| `no_admin_key` rule | `TestModeRuleFactory` maps rule names to rule objects. `RelayService` never names a rule. | A new `NoAdminKeyRule` (decode the JWT payload without verifying it and refuse `role: service_role`), one factory entry, and the name added to `KNOWN_RULES`. `RelayService` stays as it is. | 0.5 day |
| `paid_key` rule + per-provider daily cap | Quotas are per user (`codelab_relay_daily_usage(user_id, day)`) plus a global row. | Migration 014: a `provider_id` column in the primary key, or a sibling table. `RateLimiter.consume` takes the provider id. The upsert pattern (count only while under the cap, refund on relay failure) carries over. | 1 day |
| Header-key credentials (`x-api-key`, `api-key`, `apikey`) | `LivePatternScanner` already scans every forwarded header value, including decoded Basic auth. | Add the headers to each provider's `forward_request_headers` (the CORS allow-list is built from these). | none |
| `X-WebNest-Relay-Paid: 1` | `ResponseBuilder` sets the relay headers. CORS exposes `EXPOSED_RELAY_HEADERS` plus each provider's response headers. | Set the header from a provider flag and add it to `EXPOSED_RELAY_HEADERS`. | 0.25 day |
| Registry growth | JSON validated at startup. New entries can ship `enabled: false`. Live-looking hosts are refused. | Validation for the new fields. | included above |

Coding rules: still met, and tests enforce them. No `staticmethod`/`classmethod`, every function body has a top-level `try`, protocols for services, and concrete classes only in `dependencies.py`.

## B5. Database proxy: comments on the draft

What exists now: `POST /api/codelab/db/query` and `POST /api/codelab/db/test` work for Postgres (Supabase, Neon), MySQL (Aiven, TiDB, PlanetScale) and MongoDB Atlas. `GET /api/codelab/db/status` is new, public and cached for 5 minutes: it returns `{enabled, engines: {engine: {ports, hosts: [{pattern, provider, tls_verified}], available}}, limits}`, so the playground can show hints without hard-coding hosts.

### Codes added to the draft table

| HTTP | code | When |
|---|---|---|
| 400 | `db_bad_request` | Body not JSON, wrong shape, empty SQL, unterminated quote or comment, Mongo field missing for the operation |
| 403 | `db_user_blocked` | Account blocked with `DB_PROXY_BLOCKED_USER_IDS` |
| 413 | `db_payload_too_large` | Request body over 256 KB |
| 429 | `db_rate_limited` with `scope: "busy"` | Too many queries running at once across all learners. `retry_after: 2` |
| 503 | `db_disabled` | `DB_PROXY_ENABLED=false`, or the engine's driver isn't available |

`db_statement_blocked` carries `blocked` (what was refused, for example `"COPY"` or `"pg_read_file()"`). `db_host_not_allowed` carries `host`. `db_timeout` carries `timeout_seconds`.

### Behaviour to confirm

1. **`413 db_result_too_large`** returns the part that fit: `columns` and `rows` (or `documents`), `row_count`/`count`, `truncated: true`, `max_rows`, `max_bytes` and `ms`. A write whose result was cut off is **rolled back**, and the message says so.
2. **Value encoding** (SQL): `Decimal` becomes a string (no precision lost), dates and times become ISO 8601, `bytea`/BLOB becomes `"\\x…"` hex, NaN and infinity become strings, JSON/arrays keep their structure. The Python shim must convert these back if it wants psycopg2-like types.
3. **Params:** positional `%s` with a list, or `%(name)s` with an object. At most 100. Scalars or flat lists only.
4. **MongoDB request:** `database` (or the URL path), `collection`, `operation`, `filter`, `update` (object or pipeline list), `document`, `documents` (≤ 500), `pipeline` (≤ 50 stages), `options: {limit, skip, sort, projection, upsert}`. Write operations add `write: {inserted_id | inserted_ids | matched_count, modified_count, upserted_id | deleted_count}`. `list_collection_names` returns the names in `documents`. `find` caps at 500, and asking for more returns 413 with the first 500.
5. **One statement per request.** A trailing `;` is fine. The blocklist (B5.3) is a usage policy, not the security boundary, because the learner owns the database. It refuses dynamic SQL hidden in function and `DO` bodies (`EXECUTE 'COPY …'`), so a function body that merely mentions such a word is refused too. The security boundary is the host allowlist, the public-address check, TLS and the limits.
6. **Statement timeout** is `SET LOCAL statement_timeout` inside a transaction, so it also holds on Supabase's transaction pooler (6543). Statements that can't run in a transaction (`VACUUM`, `… CONCURRENTLY`, `CREATE/DROP DATABASE`) and transaction control (`BEGIN`, `COMMIT`, `ROLLBACK`, …) run outside one, with a session timeout instead. On the transaction pooler that session timeout may not carry over, so those few statements are bounded only by the proxy's overall time limit.
7. **CORS:** `/api/codelab/db/*` allows only `https://www.webneststudio.co.in` by default, with no credentials, and exposes `X-WebNest-DB-Error` and `Retry-After`. Local development sets `DB_PROXY_CORS_ORIGINS`.
8. **MongoDB `mongodb+srv://`:** the proxy reads the SRV and TXT records itself, checks every host, and hands the driver that host list. Queries and credentials only ever go to those checked hosts.
9. **Busy (`429`, `scope: "busy"`)** is returned straight away when every worker is taken, including workers still finishing a query that already timed out. Busy, DNS and host refusals don't count against the learner's quota; a query is counted once it is about to reach the database.
10. **URL options:** only these are accepted, and anything else is `db_bad_url`. This blocks options such as libpq `host=`/`hostaddr=`/`sslmode=disable` and pymongo `proxyHost`.
   - Postgres: `sslmode` (require/verify-*/prefer), `channel_binding`, Neon's `options=endpoint=…`.
   - MySQL: `ssl-mode`, `ssl={…}`, `sslaccept`.
   - Mongo: `retryWrites`, `w`, `authSource`, `authMechanism` (SCRAM only), `replicaSet`, `retryReads`, `readPreference`, `tls=true`. `appName` is ignored.
11. **Rate limits** (60/min, 1,000/day per learner) are kept in memory, so the day count restarts with the server. Should it need to survive restarts, it moves to a table later without an API change.

### Decisions still open (B5.5 item 3)

- **TLS verification.** Neon, TiDB, PlanetScale and Atlas certificates are verified against public CAs. Supabase's pooler and Aiven sign with their own CAs, so for those the connection is encrypted but the certificate isn't verified. To verify them too, we would bundle their CA certificates (`DB_PROXY_CA_FILE` is ready for this).
- **Supabase host.** The direct host (`db.<ref>.supabase.co`) is IPv6-only on many plans. If the Render network check shows no IPv6, the learner hint should say "use the pooler host". Both are allowlisted today.
- **Atlas IP allowlist.** Render's free tier has no fixed outbound IP. Choose between paid static outbound IPs and documenting `0.0.0.0/0` for practice clusters only.
- **Aiven host pattern.** We allow `*.aivencloud.com`. Confirm it against a real Aiven practice host, since some Aiven hosts may use a deeper name.
