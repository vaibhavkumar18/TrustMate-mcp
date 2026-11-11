# Provider Integrations and Constraints

Business logic depends on normalized provider protocols in `app/domain/interfaces/providers.py`; provider selection is performed by `app/providers/factory.py`. Most live provider adapters are in `app/providers/implementations.py`; the optional Trustpilot Data Solutions adapter is isolated in `app/providers/trustpilot.py`.

| Capability | Current adapter | Notes |
|---|---|---|
| Product discovery | Channel3 | Product offers, merchant domains, prices, country/currency request settings |
| Business presence | Foursquare Places | Primary place/business lookup; strict name and seller-domain corroboration |
| Business fallback | OpenStreetMap/Nominatim | Limited public place data; no seller transaction-review evidence |
| URL reputation | URLhaus | URL/host threat-intelligence signal; no-hit is neutral, not a safety guarantee |
| Domain registration | RDAP | Registration dates/status/DNS metadata; personal registrant details are not used |
| Website evidence | Native HTTPX/SSL adapter | HTTPS/TLS, contact presence and policy-page evidence with SSRF protections |
| Seller service reviews | Trustpilot Data Solutions Insights (optional) | Exact registered-domain match; verified whole-business service reviews only; not enabled by default |
| AI explanations | OpenRouter | Used only by `explain_score`; never computes scores or ranks offers |

## Channel3

- Product discovery is country/currency aware and applies a maximum-price filter at the provider request, with local validation as a second boundary.
- The Channel3 retailer domain is the seller identity source. Its affiliate/purchase URL remains the offer URL and must not be used as the seller's canonical domain.
- Real account coverage for every target country/currency must be verified. Do not assume global availability from the adapter's existence.
- Product/offer data is subject to Channel3's terms; do not persist raw affiliate URLs, price, inventory, variants, or availability unless permitted.

## Foursquare

- Used for public business/place presence, not online transaction reviews.
- A result contributes to business scoring only after a place ID, qualifying name match, official website, and same registered merchant domain are corroborated.
- The search locality is a retrieval hint, not proof of the merchant's headquarters. Current resolution order is configured seller-locality override, seller country-code domain hint, then target-market fallback. The fallback can return a less relevant physical location for cross-border `.com` sellers; strict domain corroboration remains mandatory.
- Persistent storage of Foursquare data remains disabled pending account-specific retention/attribution review. Preserve required attribution when displaying data.

## OpenStreetMap / Nominatim

- Provides limited fallback business information and attribution (`© OpenStreetMap contributors`). It does not provide equivalent seller transaction-review evidence.
- The public Nominatim policy requires a maximum of one request per second and an identifying User-Agent. The implementation's limiter is process-local; production must use one worker unless a shared limiter or separately operated service is introduced.
- Bulk `rank_offers` seller fan-out disables public Nominatim fallback. Single-seller flows may use the configured fallback where allowed.
- Preserve attribution and review OpenStreetMap ODbL obligations before persistence/redistribution.

## URLhaus

- URLhaus can report known malware-distribution URLs; it is not a comprehensive phishing/scam or merchant-reliability service.
- A no-hit result is neutral and limited in scope. Historical-only matches do not automatically mean a current active threat.
- The provider factory wraps the HTTP adapter in `AuthorizationGatedURLhausProvider`. Live requests require all three settings: `URLHAUS_AUTHORIZED=true`, `URLHAUS_LIVE_TEST=true`, and a configured `URLHAUS_AUTH_KEY`. Defaults fail closed and return unverified evidence without an HTTP request.
- The authorization flag is an operator acknowledgement, not legal advice or proof of provider permission. Do not set it until the intended use and applicable terms/license have been independently reviewed. The default configuration leaves URLhaus disabled.

## RDAP

- Uses IANA bootstrap discovery and HTTPS RDAP services.
- Domains are normalized with the Public Suffix List; lookups are bounded and fail closed on invalid/private hosts, unsafe redirects, transport failures, or malformed responses.
- Registration, expiration, nameserver, status, and DNSSEC signals are normalized. Personal registration/entity details are not exposed or persisted.
- Domain evidence is not a separate weighted trust factor in scoring version 2.3.

## Native website verification

- Only validated public HTTPS merchant hosts on port 443 are inspected.
- DNS results are validated; pinned connections preserve hostname SNI/Host and certificate validation. The transport retries alternate validated public A/AAAA addresses within a bounded deadline and emits exactly one canonical `Host` header. Redirects are manual, bounded, and must remain HTTPS/443 and public.
- Page count and response size are bounded. The adapter does not bypass CAPTCHA, authentication, bot protection, or access restrictions. HTTP 400/403, blocked pages and generic TLS handshake failures remain unverified; they are not fraud signals. HTTP 400 responses caused by malformed duplicate Host headers are addressed by canonical header generation.
- Raw HTML and personal contact details are not persisted. A homepage 403/blocked response is not treated as proof of fraud.


## Trustpilot Data Solutions Insights (optional seller reviews)

- The adapter is disabled by default (`REVIEW_PROVIDER=none`). It can be enabled only after obtaining a Trustpilot Data Solutions API key with **Insights API** entitlement. Trustpilot's official onboarding currently asks interested users to register on the Data Solutions waitlist; a Display API key does not grant the full service-reviews endpoint. See the [Data Solutions onboarding](https://developers.trustpilot.com/data-solutions-get-started/) and [API reference](https://developers.trustpilot.com/data-solutions-api/).
- The adapter finds the Business Unit by exact merchant domain, corroborates the returned website/domain again, then fetches service-review pages. Fuzzy name search is not used. Ambiguous profile matches remain unverified.
- Only records with `isVerified=true` and no `reviewedLocationName` are counted. Outlet/location reviews, unverified records, product reviews and Foursquare POI ratings never count as online seller transaction reviews.
- TRUSTMATE uses the average of the verified service-review sample and the number actually inspected. The existing 10-review and 3.5/5 thresholds remain unchanged. The Trustpilot profile's lifetime review count is diagnostic only and does not satisfy the threshold.
- Review text and consumer identity fields are discarded. Only rating/count/date aggregates are kept in memory for at most 24 hours; no raw review record is persisted. If product requirements later change to retain or display consumer/review data, review Trustpilot's refresh, deletion and display requirements before implementing storage.
- A `403` on the service-reviews endpoint is reported as `insights_api_access_required`; it is not converted to a zero score or a negative score. Missing profile, API outage, rate limit and malformed responses remain unverified.
- Enable with `REVIEW_PROVIDER=trustpilot` and `TRUSTPILOT_API_KEY=...`. Use `TRUSTPILOT_MAX_REVIEW_PAGES` to bound inspected records (10 reviews per API page). The provider is not a guaranteed source for all retailers: an absent profile or fewer than ten verified service reviews leaves the Reviews factor unverified or capped under the existing scoring policy.

## OpenRouter

- The only LLM-assisted operation is `explain_score`.
- The deterministic trust object is computed before the LLM call. The LLM receives sanitized normalized evidence, not raw provider payloads, website HTML, URLhaus data, personal registration details, or credentials.
- The explainer is disabled by default and requires explicit configuration. Failures use a deterministic, clearly labeled fallback; LLM output must not alter score, label, confidence, or ranking.

## Live verification status

Adapters and mocked tests do not prove live-provider behavior. Channel3, Foursquare, Nominatim, RDAP, website checks, and OpenRouter require deliberate manual verification with valid configuration. URLhaus additionally requires explicit authorization/licensing confirmation before an operator enables both `URLHAUS_AUTHORIZED` and `URLHAUS_LIVE_TEST` and supplies an Auth-Key.
