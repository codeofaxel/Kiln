# Kiln Privacy Policy

*Last updated: 2026-04-21 · Version 2.0*

> **Plain-English summary** — Kiln is operated by **Hadron Labs Inc.**, a
> Delaware C corporation headquartered in California. We collect the
> minimum data needed to run the product, never sell it, never hand it
> to ad networks, and give you meaningful rights to access and delete
> it. If you prefer to skip the legalese, the table in §3 is the short
> version.

---

## 1. Who we are

| Field | Value |
|---|---|
| Legal entity | **Hadron Labs Inc.** |
| Incorporated in | Delaware, USA |
| Operating address | California, USA |
| Privacy contact | [privacy@kiln3d.com](mailto:privacy@kiln3d.com) |
| General contact | [hello@kiln3d.com](mailto:hello@kiln3d.com) |
| DPO / US privacy officer | Adam Arreola, reachable at privacy@kiln3d.com |

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

| Category | Examples | Where it's stored | Legal basis (GDPR) |
|---|---|---|---|
| **Account identity** | Email address from OAuth (Google / Apple / GitHub), verified auth UID, display name, avatar URL, OAuth provider | Supabase Auth (managed), EU or US regions | Contract (§6(1)(b)) |
| **Entitlement metadata** | Tier (pro / business / enterprise), token ID (JTI), issue + expiry timestamps, hashed email, status, activation counts | Supabase DB (`pilot_entitlements` table) | Contract (§6(1)(b)) |
| **Payment data** | Stripe customer ID, subscription ID, invoice history, payment method fingerprint (never the card number itself) | Stripe (PCI-DSS certified); we see only references | Contract + legal obligation (§6(1)(b), (c)) |
| **Local product data** | Print job history, printer configuration, billing records, event logs | On your machine only, in `~/.kiln/` | Not applicable — we can't see it |
| **Support interactions** | Email you send to us, support ticket content | Our email provider + internal tooling | Legitimate interest (§6(1)(f)) |
| **Security telemetry** | Coarse IP bucket hash, hashed device fingerprint, client version, timestamped security event type — all cryptographically hashed before storage | Supabase (`license_security_events` table) | Legitimate interest — fraud + abuse prevention (§6(1)(f)) |
| **Cookies (site)** | Supabase auth session cookie, CSRF token | Your browser | Consent for non-essential (§6(1)(a)); contract for session cookies |
| **Fulfillment orders** | Ship-to address, model file, material + finish choice | Passed through to Craftcloud; not retained by us beyond the order record | Contract (§6(1)(b)) |

**What we deliberately do NOT collect:** advertising identifiers,
analytics / telemetry of product usage, browsing history, your
3D models (beyond fulfillment pass-through), your CAD prompts,
your G-code, cross-site tracking data, biometric data, precise
location data, inferences about your personality / political
views / religion / orientation, or data on minors.

## 4. How we use it (processing purposes)

Every piece of data above maps to one of these narrow purposes:

1. **Running your account** — authenticating you via OAuth, resolving
   your tier, binding your OAuth identity to your paid entitlement.
2. **Billing** — processing subscription payments, handling
   upgrades/downgrades, issuing refunds, mailing invoices.
3. **Fulfillment** — routing your print order to the manufacturer
   you selected; tracking status until delivery.
4. **Support** — responding to issues you open with us and
   troubleshooting bugs.
5. **Abuse and fraud prevention** — detecting credential stuffing,
   license-key sharing, payment chargebacks, and unauthorized
   access — via the minimal security telemetry described above.
6. **Legal compliance** — retaining billing records for tax and
   accounting purposes (typically 7 years); responding to lawful
   legal requests (see §10).
7. **Product improvement** — **only with your explicit opt-in** via
   aggregated, de-identified statistics. Telemetry is OFF by
   default and there is no per-user usage tracking.

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
| **Vercel Inc.** | Hosting for `kiln3d.com` and `app.kiln3d.com` | US | Standard Contractual Clauses (EU→US) |
| **Google (OAuth), Apple (Sign in with Apple), GitHub (OAuth)** | OAuth authentication only | US | Standard Contractual Clauses + each provider's own data policies |
| **Craftcloud (All3DP GmbH)** | Fulfillment order routing | Germany / EU | Not applicable — EU processor |
| **MyMiniFactory / Cults3D** | Marketplace search queries you initiate | UK / France | Standard Contractual Clauses |
| **Our email provider (SendGrid / Postmark / similar)** | Transactional email (welcome, receipts, sign-in links) | US | Standard Contractual Clauses |

We will publish any changes to this list with at least 30 days'
notice before a new subprocessor begins processing your data.

We do **not** share data with advertising networks, analytics
services, data brokers, cookie-consent platforms that resell
signals, marketing automation platforms, or any third party whose
business model depends on reselling user data.

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
emailing privacy@kiln3d.com.

## 7. Data retention

| Data category | Retention period |
|---|---|
| Account + entitlement records | While your account is active, plus 90 days after termination (so you can reinstate), then permanent deletion |
| Invoices + billing records | 7 years after the transaction — required by US and EU tax law |
| Stripe payment records | Per Stripe's retention policy (typically 7 years) — we cannot delete these before then, but can request anonymization where permitted |
| Security telemetry (hashed) | 90 days rolling — then automatic purge |
| Email support threads | 2 years from last reply, then deletion |
| Local data on your machine | **Indefinitely, until you delete it** — we cannot see it and cannot delete it for you |
| Fulfillment orders | Until delivery + 90 days (for refund + dispute window) |
| Web workshop session cookies | Browser session, up to 30 days |

If you delete your account via `/app/settings/account → Delete
account`, we begin the 90-day deletion window immediately and
cancel any recurring subscriptions at the next billing cycle.

## 8. Cookies and local storage

The **marketing site** (`kiln3d.com`) uses **no** cookies unless
you explicitly opt in to something (there's no "accept cookies"
banner because there's nothing to consent to by default).

The **web workshop** (`app.kiln3d.com`) uses:

- A **Supabase authentication cookie** (`sb-access-token`,
  `sb-refresh-token`) to keep you signed in. This is an
  essential cookie — without it the product can't work.
- A **CSRF protection token** for form submissions.
- **`localStorage`** for UI preferences (collapsed sidebars,
  recently-opened designs) — not transmitted to us.

We do **not** use Google Analytics, Facebook Pixel, Segment,
Mixpanel, Amplitude, or any third-party analytics product.

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
  [Do Not Sell or Share My Personal Information link](mailto:privacy@kiln3d.com?subject=Do%20Not%20Sell%20or%20Share%20-%20CCPA%20Request)
  is here for completeness even though it's a no-op for us.
- **Right to limit use of sensitive personal information** — we
  don't collect sensitive PI (as defined by §1798.140(ae)) that
  would require this right.
- **Right to non-discrimination** — exercising your rights will never
  result in worse service, higher fees, or reduced features.

**To exercise any right**, email
[privacy@kiln3d.com](mailto:privacy@kiln3d.com) from the email
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
privacy@kiln3d.com and we will delete it promptly.

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
please report it to security@kiln3d.com. We follow **coordinated
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
on request. Email dpa@kiln3d.com.

## 16. Changes to this policy

We will update this policy from time to time. For **material
changes** (new categories of data collected, new subprocessors,
new purposes), we will:

- Email you at least **30 days before the change takes effect**;
- Update the "Last updated" date and increment the version number
  at the top of this document;
- Preserve prior versions in the public Git history at
  https://github.com/codeofaxel/Kiln/blob/main/PRIVACY.md.

Non-material changes (typos, reorganization) are pushed
immediately and noted in Git history.

## 17. Contact

- **Privacy questions** — [privacy@kiln3d.com](mailto:privacy@kiln3d.com)
- **Legal / DPA requests** — [dpa@kiln3d.com](mailto:dpa@kiln3d.com)
- **Security issues** — [security@kiln3d.com](mailto:security@kiln3d.com)
- **General contact** — [hello@kiln3d.com](mailto:hello@kiln3d.com)
- **Postal mail** — Hadron Labs Inc., c/o Privacy Office, California, USA

We respond to privacy requests within 30 days (EU/UK) or 45 days
(CCPA) from receipt.
