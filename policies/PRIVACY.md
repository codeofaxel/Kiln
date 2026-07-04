# Kiln Privacy Policy

*Last updated: 2026-04-21 · Version 2.2*

> **Plain-English summary** — Kiln is operated by **Hadron Labs Inc.**, a
> Delaware C corporation headquartered in California. Most of Kiln runs
> **locally on your machine** (the CLI, the MCP server, slicing, printer
> control, fulfillment) — we can't see that data. The **web workshop at
> `app.kiln3d.com`** is a **git-style cloud layer for 3D designs** —
> branches, versions, PR-style reviews, orgs, and team collaboration —
> and that IS stored on our servers. We collect the minimum data
> needed to run the workshop, never sell it, never hand it to ad
> networks, and give you meaningful rights to access and delete it. If
> you prefer to skip the legalese, the table in §3 is the short version.

---

## 1. Who we are

| Field | Value |
|---|---|
| Legal entity | **Hadron Labs Inc.** |
| Incorporated in | Delaware, USA |
| Legal mailing address | c/o Harvard Business Services, Inc. (Registered Agent)<br>16192 Coastal Hwy, Lewes, DE 19958, USA |
| Privacy contact | [adam@kiln3d.com](mailto:adam@kiln3d.com) |
| General contact | [adam@kiln3d.com](mailto:adam@kiln3d.com) |
| DPO / US privacy officer | Adam Arreola, reachable at adam@kiln3d.com |

> **One inbox, transparent by design.** Kiln is a small team. Every
> contact channel in this policy — privacy requests, DPA requests,
> security reports, legal questions, DMCA notices, CCPA opt-outs,
> arbitration opt-outs, enterprise inquiries — all route to
> **adam@kiln3d.com**. Please put the topic in the subject line so
> the right thing gets prioritized (e.g., `[CCPA]`, `[DPA]`,
> `[Security]`, `[DMCA]`, `[Arbitration Opt-Out]`). When the team
> grows, we'll split these out and update this notice.

Throughout this policy, **"Kiln"**, **"we"**, **"us"**, or **"our"** means
Hadron Labs Inc. **"You"** means the individual or entity using the
Kiln software, web workshop, CLI, MCP server, or paid services.

## 2. What this policy covers

This policy describes how we collect, use, share, and protect
personal information across every surface of the product:

- the **open-source Kiln CLI + MCP server** you install locally;
- the **web workshop** at `app.kiln3d.com` and the marketing site at
  `kiln3d.com`;
- the **REST API** at `api.kiln3d.com`;
- **paid tier** accounts (Pro, Business, Enterprise) and the Stripe
  billing surface;
- **fulfillment orders** routed through manufacturing partners; and
- any other service we offer under the Kiln brand.

Our open-source repositories on GitHub are governed by GitHub's
policies for source code access; this policy covers data collected
when you **use** Kiln, not when you merely read its code.

## 3. What we collect — at a glance

**First and most important: free users never sign up for an
account.** Kiln's free tier runs entirely on your machine as local
software (CLI + MCP server). It talks to your printers directly
over your local network, slices your models in-process, and the
only thing it sends to our servers is a single anonymous "usage
heartbeat" once per UTC day (described in §3.1). The heartbeat is
keyed by a random UUID stored at `~/.kiln/installation_id` plus a
salted one-way hash of your OS-level machine ID — never your name,
email, IP, or any user identifier. It is on by default and
opt-out with `KILN_TELEMETRY=false`. Beyond the heartbeat, free
users have no account, no OAuth identity, no email on file, no
per-person tracking, no cookies — we cannot identify you, contact
you, or link your activity to a person.

The table below applies **only to users who explicitly chose
to create a paid-tier account** (Pro / Business / Enterprise) by
signing in through Google, Apple, or GitHub OAuth — plus two
rows that apply to everyone (local product data, which stays on
your machine by design; and support interactions, which exist
only if you write to us).

| Category | Applies to | Examples | Where it's stored | Legal basis (GDPR) |
|---|---|---|---|---|
| **Account identity** | Paid users only | Email address from OAuth (Google / Apple / GitHub), verified auth UID, display name, avatar URL, OAuth provider | Supabase Auth (managed), EU or US regions | Contract (§6(1)(b)) |
| **Entitlement metadata** | Paid users only | Tier (pro / business / enterprise), token ID (JTI), issue + expiry timestamps, hashed email, status, activation counts | Supabase DB (`pilot_entitlements` table) | Contract (§6(1)(b)) |
| **Payment data** | Paid users only | Stripe customer ID, subscription ID, invoice history, payment method fingerprint (never the card number itself) | Stripe (PCI-DSS certified); we see only references | Contract + legal obligation (§6(1)(b), (c)) |
| **Workshop content (git-for-3D)** | Paid users who push to the cloud | Designs you upload or generate, branch commits, version snapshots, release tags, PR-style change proposals, comments on PRs, reflog entries, cherry-picks, features + presets in your libraries | Supabase DB (`kiln_cloud_designs`, `kiln_cloud_branches`, `kiln_cloud_versions`, `kiln_cloud_meshes`, `kiln_cloud_feature_*`, `kiln_cloud_preset_*`, `kiln_cloud_releases`, `kiln_cloud_version_comments`, `kiln_cloud_reflog`) | Contract (§6(1)(b)) |
| **Rendered previews** | Paid users who push to the cloud | Auto-generated thumbnails + preview images of your designs so you can browse your library visually | Supabase Storage (`kiln_cloud_meshes` blob storage) | Contract (§6(1)(b)) |
| **Org + team data** | Paid users on Business / Enterprise who create or join orgs | Org names, membership rosters, team assignments, role grants, email addresses of people you invite to your org (before they accept) | Supabase DB (`kiln_cloud_orgs`, `kiln_cloud_org_memberships`, `kiln_cloud_memberships`, `kiln_cloud_team_memberships`, `kiln_cloud_org_teams`) | Contract (§6(1)(b)) |
| **Workshop access logs** | Paid users who push to the cloud | Who pushed / pulled / viewed / cloned which design + when, for audit trail + collaboration accountability | Supabase DB (`kiln_cloud_reflog`) | Legitimate interest — collaborative-work audit (§6(1)(f)) |
| **Usage heartbeats** | All Kiln installs (free + paid) | One row per install per UTC day. Anonymous installation UUID (random, generated locally at `~/.kiln/installation_id`, never derived from user identity) + a salted SHA-256 hash of the OS-level machine ID for unique-device counting. Records: Kiln version, printer model + adapter type + count, daily counts (prints / generations / decorations / textures / slices / downloads / print-hours), `pro_installed` flag, OS platform, paywall-denial counts. **No email, no IP, no hostname, no MAC, no file paths, no design content, no G-code.** Default ON; opt out with `KILN_TELEMETRY=false`. See §3.1 for full disclosure. | Supabase DB (`usage_heartbeats`) | Legitimate interest — product improvement, paywall integrity (§6(1)(f)) |
| **Local product data** | **Every user — free and paid** | Print job history, printer configuration, billing records, event logs — everything you do with physical printers | **On your machine only**, in `~/.kiln/`. We cannot see it and cannot retrieve it. | Not applicable — we can't see it |
| **Support interactions** | Anyone who emails us | Email you send to us, support ticket content | Our email provider + internal tooling | Legitimate interest (§6(1)(f)) |
| **Security telemetry** | Paid users (free users never hit authed endpoints) | Coarse IP bucket hash, hashed device fingerprint, client version, timestamped security event type — all cryptographically hashed before storage | Supabase (`license_security_events` table) | Legitimate interest — fraud + abuse prevention (§6(1)(f)) |
| **Cookies (workshop)** | Visitors to `app.kiln3d.com` (paid-tier web workshop) | Supabase auth session cookie, CSRF token | Your browser | Consent for non-essential (§6(1)(a)); contract for session cookies |
| **Cookies (marketing site)** | Visitors to `kiln3d.com` who explicitly grant consent via the cookie banner — only present if we're running paid acquisition campaigns and you've opted in | Consent record (`kiln_consent`); when granted, optional analytics + advertising cookies (see §3.2) | Your browser | Consent (§6(1)(a)) — opt-in; no strictly-necessary cookies on the marketing site |
| **Fulfillment orders** | Paid users who route through Craftcloud | Ship-to address, model file, material + finish choice | Passed through to Craftcloud; not retained by us beyond the order record | Contract (§6(1)(b)) |
| **Opt-in community datasets** | Any user (free or paid) who explicitly opts in | If you explicitly opt in via `community_share`, we accept anonymized print outcome records (printer model, material, settings hash, success/fail outcome) and recovery strategies — never your email, auth_user_id, tenant_id, file names, or geometry | Supabase DB (`community_prints`, `community_recoveries`) | Consent (§6(1)(a)) |

**What we deliberately do NOT collect:** browsing history outside
`kiln3d.com`, your 3D models (beyond fulfillment pass-through),
your CAD prompts, your G-code, file paths or filenames, IP
address or hostname (in heartbeats — see §3.2 for marketing-site
analytics, which is consent-gated), audience-resale-style
cross-site tracking data, biometric data, precise location data,
inferences about your personality / political views / religion /
orientation, or data on minors.

## 3.1 Usage heartbeats — what they are and aren't

The Kiln client (free or paid) sends one anonymous heartbeat per
install per UTC day to our Supabase backend. This is the only
product telemetry that runs by default. We describe it explicitly
because it's the kind of thing most products mention only in
passing.

**Identifiers we send:** a random UUID generated locally on first
run (stored at `~/.kiln/installation_id` with mode 0o600, never
derived from your name, email, IP, MAC, hostname, or any
user-identifying data) plus an opaque "device fingerprint" — a
one-way SHA-256 hash of your operating system's machine identifier
(`IOPlatformUUID` on macOS, `/etc/machine-id` on Linux,
`MachineGuid` on Windows), salted with a Kiln-specific string and
truncated to 32 hex characters. We cannot recover the underlying
machine ID from the fingerprint, and a third party using a
different salt cannot link our fingerprints to theirs. The
fingerprint identifies a *device* (an OS install on a piece of
hardware), not a person — stable across Kiln reinstalls, resets
when the OS itself is reinstalled. We use it to count unique
devices separately from unique installs (so reinstalls and Docker
runs don't inflate user-count estimates).

**What we send each day:**

- installation UUID + UTC date
- device fingerprint (or empty string if machine-ID resolution failed)
- Kiln version (e.g. `0.5.0`)
- printer model + adapter type (Bambu / Creality / OctoPrint / Moonraker / Serial) + printer count
- daily activity counts: prints, generations, decorations, textures, slices, downloads, print-hours
- `pro_installed` boolean (is `kiln-pro` installed alongside?)
- OS platform string (`darwin`, `linux`, `windows`)
- aggregate counts of textures used, decoration types, slicer profiles, marketplace sources, paywall denials — always counts, never user-attributable details

**What we never send:** email, IP address, MAC address, hostname,
username, file paths, design content, G-code, model bytes, prompts,
your CAD inputs, anything PII.

**What we use it for:** understanding which printers and OS
platforms our users actually run, prioritizing fixes for breaking
changes, answering "is Kiln growing?" without building per-user
analytics. We never use it for advertising, profiling, or sharing
with third parties.

**How to opt out:** set the environment variable
`KILN_TELEMETRY=false` (or `0`, `no`, `off`) before starting
Kiln. The heartbeat thread is a no-op when telemetry is disabled.

## 3.2 Marketing-site analytics

The marketing site (`kiln3d.com`) uses **Vercel Web Analytics** —
Vercel's first-party privacy-preserving analytics service. We
enabled it in May 2026 to answer "is the marketing site getting
traffic, where do visitors land, what's the bounce rate" before
turning on paid acquisition.

**Vercel Web Analytics processes:** page URL, referrer, anonymous
visitor count via a daily-rotating IP+user-agent hash (NEVER the
raw IP), browser/OS family, and country (from IP geolocation
performed at the edge before discarding the IP).

**Vercel Web Analytics does NOT use:** cookies, persistent
identifiers, cross-site tracking, fingerprinting beyond the
hashed visitor count, or any data that survives the daily salt
rotation. There is no consent banner because there's nothing to
consent to.

**Scope:** marketing site (`kiln3d.com`) only. The web workshop
(`app.kiln3d.com`) is **not** instrumented with Vercel Web
Analytics — its traffic is already captured by Supabase auth
events for authenticated users, and we don't want to add a
redundant data surface.

**Subprocessor:** Vercel Inc. (already our hosting subprocessor —
see §5). The same SCC posture for international transfers
applies; Vercel Web Analytics is not a separate vendor
relationship.

### Optional: third-party advertising + analytics (consent required)

If we're running paid acquisition campaigns, the marketing site
may additionally load **Meta Pixel** (Facebook + Instagram ad
attribution), **Google Analytics 4** (aggregate behavior), and
**Google Ads conversion tracking** (signup attribution). These
load **only** when both gates are open: (1) the corresponding
integration ID is configured for the deployment, and (2) you
have granted explicit consent via the cookie preferences banner
— `analytics` for GA4, `advertising` for Meta Pixel and Google
Ads. With either gate closed, no third-party script loads, no
event fires, no cookie is written. The cookie banner is not
shown when there is nothing to consent to.

**What gets shared (when active):**

- **Meta Pixel:** page views and standard events (e.g.
  `Lead`, `CompleteRegistration`, `Subscribe`, `Purchase`). For
  measurement quality, we send SHA-256-hashed versions of any
  identifiers you've already provided to us — typically your
  email if you signed up. Meta receives the hash, not the raw
  value, and cannot reverse it. Our server-side Conversions API
  forwarder (`api.kiln3d.com`) pairs each browser fire with a
  server fire sharing the same `event_id`, so Meta dedupes the
  pair and we don't double-count.
- **Google Analytics 4:** page views with anonymized IP, browser
  / OS family, country, referrer, in-session events.
- **Google Ads conversion tracking:** binary "signup conversion
  occurred" plus the click identifier (`gclid`) if you arrived
  via a Google ad.

**Withdrawing consent** stops new tracking events. Click "Cookie
preferences" in the site footer to re-open the banner and toggle
off advertising or analytics. This does not delete data already
collected — for that, use the rights process in §9.

**Subprocessors:** Meta Platforms, Inc. and Google LLC — see §5.

**California residents:** you may opt out of cross-context
behavioral advertising at the cookie banner. We honor this by
sending Limited Data Use (LDU) signals to Meta and equivalent
flags to Google.

## 4. How we use it (processing purposes)

Every piece of data above maps to one of these narrow purposes:

1. **Running your account** — authenticating you via OAuth, resolving
   your tier, binding your OAuth identity to your paid entitlement.
2. **Cloud storage + version control for your designs** — the web
   workshop is a git-style layer for 3D designs. To make that work
   we store the designs you push, the branches you create, every
   version in your commit history, the comments on your PRs, the
   releases you tag, and the access log that tells collaborators
   who did what and when. We auto-generate thumbnail previews so
   you can browse your library visually.
3. **Collaboration** — when you invite someone to an org, we store
   the pending invite (their email + your org + the role you
   granted) so we can authenticate them when they accept; we
   enforce permissions on every cloud endpoint so only people in
   your org can see your org's designs.
4. **Billing** — processing subscription payments, handling
   upgrades/downgrades, issuing refunds, mailing invoices.
5. **Rate limits + paywall enforcement** — the `usage_heartbeats`
   table lets us count per-tier usage (how many fulfillment orders,
   how many cloud writes, etc.) so we can enforce plan limits
   without reading your content.
6. **Fulfillment** — routing your print order to the manufacturer
   you selected; tracking status until delivery.
7. **Support** — responding to issues you open with us and
   troubleshooting bugs.
8. **Abuse and fraud prevention** — detecting credential stuffing,
   license-key sharing, payment chargebacks, unauthorized access,
   and abuse of the collaboration features (spam invites, bulk
   scraping) — via the minimal security telemetry described above.
9. **Legal compliance** — retaining billing records for tax and
   accounting purposes (typically 7 years); responding to lawful
   legal requests (see §10).
10. **Product improvement** — anonymous, aggregated, install-scoped
    usage heartbeats (one row per install per UTC day, keyed by a
    random UUID + a salted machine-ID hash; see §3.1). On by
    default; opt-out via `KILN_TELEMETRY=false`. The marketing
    site (`kiln3d.com`) additionally uses Vercel's first-party
    privacy-preserving Web Analytics — no cookies, no PII, no
    cross-site tracking; see §3.2. We never sell or share either
    dataset.

We do not use your data for advertising, profiling for commercial
purposes, or cross-context behavioral advertising. We do not sell
or "share" (as defined under CCPA §1798.140) personal information.

## 5. Who we share it with (subprocessors)

We keep the list of processors intentionally short and publish it
here. Each subprocessor is bound by a data-processing agreement
that limits their use of data to the purposes we've asked them to
perform.

| Subprocessor | What they process | Country | Safeguard for international transfers |
|---|---|---|---|
| **Supabase** | Account + entitlement data, auth sessions, OAuth identities, security telemetry | US (default region) | Standard Contractual Clauses (EU→US) |
| **Stripe, Inc.** | Card payments, subscription billing, invoices | US | Standard Contractual Clauses (EU→US); PCI-DSS Level 1 |
| **Circle Internet Financial, LLC** | USDC stablecoin payments (Solana / Base networks), if used | US | Standard Contractual Clauses (EU→US) |
| **Fly.io (Fly Software Inc.)** | Hosting for `api.kiln3d.com` | US | Standard Contractual Clauses (EU→US) |
| **Vercel Inc.** | Hosting for `kiln3d.com` and `app.kiln3d.com`; first-party privacy-preserving Web Analytics on `kiln3d.com` only (no cookies, no PII, daily-rotating IP hash — see §3.2) | US | Standard Contractual Clauses (EU→US) |
| **Meta Platforms, Inc.** | Ad measurement + retargeting on `kiln3d.com`, only when paid acquisition is active AND you've granted advertising consent (see §3.2) | US | Standard Contractual Clauses (EU→US); Meta's data-processing addendum |
| **Google LLC** (Analytics + Ads) | GA4 site analytics + Google Ads conversion tracking on `kiln3d.com`, only when active AND you've granted the corresponding consent (see §3.2) | US | Standard Contractual Clauses (EU→US); Google's data-processing terms |
| **Google (OAuth), Apple (Sign in with Apple), GitHub (OAuth)** | OAuth authentication only | US | Standard Contractual Clauses + each provider's own data policies |
| **Craftcloud (All3DP GmbH)** | Fulfillment order routing | Germany / EU | Not applicable — EU processor |
| **MyMiniFactory / Cults3D** | Marketplace search queries you initiate | UK / France | Standard Contractual Clauses |
| **Our email provider (SendGrid / Postmark / similar)** | Transactional email (welcome, receipts, sign-in links) | US | Standard Contractual Clauses |

We will publish any changes to this list with at least 30 days'
notice before a new subprocessor begins processing your data.

We do **not** sell data, share data with data brokers, or use
cookie-consent platforms or marketing automation tools whose
business model depends on reselling user data.

The advertising and analytics services listed above (Meta,
Google) are bound subprocessors under data-processing
agreements; they receive only what's described in §3.2 and only
with your explicit consent.

## 6. International data transfers

If you are in the European Economic Area (EEA), United Kingdom,
or Switzerland, your personal data is transferred to the United
States for processing. We rely on **Standard Contractual Clauses
(SCCs)** adopted by the European Commission (Decision 2021/914)
as the transfer mechanism for every subprocessor listed above.
Supplementary measures (encryption in transit + at rest, minimum
necessary data, audit logging) are applied per the EDPB's
recommendations following Schrems II.

You can request a copy of the SCCs we use with any subprocessor by
emailing adam@kiln3d.com.

## 7. Data retention

| Data category | Retention period |
|---|---|
| Account + entitlement records | While your account is active, plus 90 days after termination (so you can reinstate), then permanent deletion |
| Invoices + billing records | 7 years after the transaction — required by US and EU tax law |
| Stripe payment records | Per Stripe's retention policy (typically 7 years) — we cannot delete these before then, but can request anonymization where permitted |
| **Your designs + branches + versions + reflog** | Retained as long as your account is active. When you delete a design, it's removed from queries immediately and from backups within 30 days. When you delete your account, all designs you personally own are scheduled for deletion at the end of the 90-day grace period. |
| **Designs owned by an org** | Retained as long as the org exists. When you leave an org, your access ends but your commits remain attributed to you in the history (like GitHub). The org's admins can delete you from attributed history on request. |
| **Thumbnails / preview renders** | Regenerated on demand; retained alongside the design. Deleted with the design. |
| **Comments you posted on others' PRs** | Remain visible on the host design's history (to preserve review context, like GitHub) but the author name can be anonymized on request (your name becomes "a former collaborator"). |
| **Org + team data** | Retained while the org exists. When the last member of an org leaves, we notify the admins + give 30 days to wind down before deleting org data. Pending invites that are never accepted are purged after 30 days. |
| **Workshop access logs (reflog)** | 365 days rolling, then automatic purge. Auditors can request longer retention under a DPA. |
| **Usage heartbeats** | 90 days rolling — then aggregated into tier-level counters and raw rows purged. |
| **Opt-in community datasets** | Retained indefinitely as anonymous data. You can't delete a specific contribution once it's aggregated (we strip the auth_user_id on ingestion, so we can't trace records back to you). Only opt in if you're comfortable with permanent donation. |
| Security telemetry (hashed) | 90 days rolling — then automatic purge |
| Email support threads | 2 years from last reply, then deletion |
| Local data on your machine | **Indefinitely, until you delete it** — we cannot see it and cannot delete it for you |
| Fulfillment orders | Until delivery + 90 days (for refund + dispute window) |
| Web workshop session cookies | Browser session, up to 30 days |

If you delete your account via `/app/settings/account → Delete
account`, we begin the 90-day deletion window immediately and
cancel any recurring subscriptions at the next billing cycle.

## 8. Cookies and local storage

The **marketing site** (`kiln3d.com`) uses Vercel Web Analytics
(see §3.2) — cookieless by design, no consent required. If we're
running paid acquisition campaigns, the site additionally shows
a cookie preferences banner offering optional analytics + advertising
cookies (Google Analytics 4, Meta Pixel, Google Ads — see §3.2 for
the full list). Strictly necessary cookies are not used on the
marketing site; the only cookie ever written is your consent record
(`kiln_consent`) when you save a preference. If no banner appears
on your visit, no third-party tracking is active for that visit.

The **web workshop** (`app.kiln3d.com`) uses:

- A **Supabase authentication cookie** (`sb-access-token`,
  `sb-refresh-token`) to keep you signed in. This is an
  essential cookie — without it the product can't work.
- A **CSRF protection token** for form submissions.
- **`localStorage`** for UI preferences (collapsed sidebars,
  recently-opened designs) — not transmitted to us.

We do **not** use Segment, Mixpanel, Amplitude, or any cross-site
tracking platform whose business model is audience resale. The
only third-party analytics that may run on `kiln3d.com` are
Google Analytics 4 and Meta Pixel — and only with your explicit
consent (see §3.2). Neither is loaded on the web workshop
(`app.kiln3d.com`) at all. Vercel Web Analytics is first-party,
cookieless, and the only analytics that runs without consent.

## 9. Your rights

We respect the rights granted by the **General Data Protection
Regulation (GDPR)**, the **California Consumer Privacy Act (CCPA)
as amended by the California Privacy Rights Act (CPRA)**, and
similar US state privacy laws (Virginia CDPA, Colorado CPA,
Connecticut CTDPA, Utah UCPA, Texas DPSA, etc.).

**For all users, regardless of location:**

- **Right to access** — get a copy of the personal data we hold about you.
- **Right to correct / rectify** — fix inaccurate or incomplete data.
- **Right to delete / erase** — ask us to delete your personal data
  (subject to legal retention requirements above).
- **Right to portability** — receive your data in a machine-readable
  format (JSON / CSV).
- **Right to restrict processing** — pause certain processing while
  we investigate a dispute.
- **Right to object** — object to processing based on our legitimate
  interests (§4 point 5).
- **Right to withdraw consent** — for any processing you consented
  to, you can withdraw consent at any time without affecting
  processing that already occurred.

**For California residents (CCPA / CPRA):**

- **Right to know** — what categories of personal information we've
  collected, sold (we don't), or shared for cross-context behavioral
  advertising (we don't).
- **Right to delete** — subject to the exceptions in §1798.105(d).
- **Right to correct** — fix inaccurate personal information.
- **Right to opt out of sale or sharing** — **we don't sell or share
  personal information within the meaning of CCPA §1798.140.**
  [Do Not Sell or Share My Personal Information link](mailto:adam@kiln3d.com?subject=Do%20Not%20Sell%20or%20Share%20-%20CCPA%20Request)
  is here for completeness even though it's a no-op for us.
- **Right to limit use of sensitive personal information** — we
  don't collect sensitive PI (as defined by §1798.140(ae)) that
  would require this right.
- **Right to non-discrimination** — exercising your rights will never
  result in worse service, higher fees, or reduced features.

**To exercise any right**, email
[adam@kiln3d.com](mailto:adam@kiln3d.com) from the email
address on your Kiln account. We will respond within **30 days**
(or 45 days for CCPA requests, as permitted). We verify identity
by confirming the request came from the account email; for
sensitive requests (deletion, large exports) we may ask you to
confirm via a magic link to your account email.

**Right to lodge a complaint:** you can lodge a complaint with
your supervisory authority at any time:

- **EU/EEA residents** — your national data protection authority
  (e.g., CNIL in France, BfDI in Germany, AEPD in Spain).
- **UK residents** — the Information Commissioner's Office (ICO)
  at https://ico.org.uk.
- **California residents** — the California Attorney General
  (https://oag.ca.gov/privacy) or California Privacy Protection
  Agency (https://cppa.ca.gov).

## 10. Responding to legal requests

We will disclose your data in response to a **valid legal
process** (subpoena, court order, search warrant) when we have a
good-faith belief that the law requires it. Where legally
permitted, we will notify you before disclosure so you have the
opportunity to challenge the request.

We publish an annual **transparency report** enumerating the
legal requests we received and how we responded. The first
report will cover the 2026 calendar year.

## 11. Children

Kiln is **not directed to individuals under 16 years of age**
(EU/UK) or **under 13** (US/COPPA jurisdictions). We do not
knowingly collect personal data from children. If you believe
a child has provided us personal information, contact
adam@kiln3d.com and we will delete it promptly.

## 12. Data security

We apply industry-standard security controls:

- **Encryption in transit** — TLS 1.2+ on every connection.
- **Encryption at rest** — Supabase + Stripe + Fly.io all use
  AES-256 at-rest encryption for databases and storage.
- **Access controls** — Supabase Row-Level Security (RLS) on every
  sensitive table; service-role keys restricted to server-side
  environments and never shipped client-side. RLS policies
  audited publicly at
  https://github.com/codeofaxel/Kiln-pro/tree/main/scripts/audit_rls.py.
- **Secret management** — secrets stored in Fly.io's managed
  secrets, never committed to source control.
- **Least privilege** — internal admin access is granted per-task,
  logged, and expires after 24 hours.
- **Dependency scanning** — automated vulnerability scanning on every
  merged commit.
- **Device security** — local database files created with
  owner-only read/write permissions (0600 on Unix-like systems).

No security measure is absolute. If you discover a vulnerability,
please report it to adam@kiln3d.com. We follow **coordinated
disclosure** and will acknowledge within 3 business days.

## 13. Data breach notification

In the event of a personal-data breach that is likely to result
in a risk to your rights and freedoms, we will:

- Notify affected users **without undue delay and within 72 hours**
  of discovery (per GDPR Art. 33 + CCPA §1798.82);
- Notify relevant supervisory authorities where required;
- Document the breach, its effects, and our remediation actions in
  a permanent record.

Notifications are sent to the email address on your account.
Keep it current via `/app/settings/account`.

## 14. Automated decision-making

We do **not** make decisions that produce legal effects or
significantly affect you solely through automated means. Tier
resolution, billing, and entitlement checks are automated but
deterministic (not profile-based) and subject to human review on
dispute.

## 15. Enterprise customers — Data Processing Addendum

If you are using Kiln Business or Enterprise tier on behalf of an
organization, a **Data Processing Addendum (DPA)** incorporating
Standard Contractual Clauses and Article 28 GDPR terms is available
on request. Email adam@kiln3d.com.

## 16. Changes to this policy

We will update this policy from time to time. For **material
changes** (new categories of data collected, new subprocessors,
new purposes), we will:

- Email you at least **30 days before the change takes effect**;
- Update the "Last updated" date and increment the version number
  at the top of this document;
- Preserve prior versions in the public Git history at
  https://github.com/codeofaxel/Kiln/blob/main/policies/PRIVACY.md.

Non-material changes (typos, reorganization) are pushed
immediately and noted in Git history.

## 17. Contact

- **Privacy questions** — [adam@kiln3d.com](mailto:adam@kiln3d.com)
- **Legal / DPA requests** — [adam@kiln3d.com](mailto:adam@kiln3d.com)
- **Security issues** — [adam@kiln3d.com](mailto:adam@kiln3d.com)
- **General contact** — [adam@kiln3d.com](mailto:adam@kiln3d.com)
- **Postal mail** — Hadron Labs Inc., c/o Harvard Business Services, Inc. (Registered Agent), 16192 Coastal Hwy, Lewes, DE 19958, USA

We respond to privacy requests within 30 days (EU/UK) or 45 days
(CCPA) from receipt.
