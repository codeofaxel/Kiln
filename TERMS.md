# Kiln Terms of Use

*Last updated: 2026-04-24 · Version 2.4*

> **Plain-English summary** — These terms govern your use of Kiln,
> operated by Hadron Labs Inc. Kiln has two sides:
>
> 1. **Locally-installed software** — the CLI, the MCP server,
>    slicing, printer control, and fulfillment routing all run on
>    **your machine**. We can't see that data. You're responsible
>    for what you print, for printer safety, and for the actions of
>    any AI agent you authorize to drive the MCP tools.
> 2. **The web workshop at `app.kiln3d.com`** — a **git-style cloud
>    layer for 3D designs**: versioned designs, branches, commits,
>    PR-style reviews, releases, cherry-picks, and team
>    collaboration via orgs. What you push here lives on our
>    servers.
>
> We provide the software "as is," cap our liability, and require
> binding arbitration for disputes (with a 30-day opt-out and
> small-claims exception). If any of that is a dealbreaker, don't
> use Kiln.
>
> **One inbox, transparent by design.** Every "email us at…"
> reference in these Terms routes to **adam@kiln3d.com**. Kiln is
> a small team; one founder reads everything. Put the topic in the
> subject line so the right thing gets prioritized (e.g., `[DMCA]`,
> `[Arbitration Opt-Out]`, `[Billing]`, `[Security]`).

---

## 1. The agreement

These Terms of Use (the **"Terms"**) form a binding legal
agreement between you and **Hadron Labs Inc.**, a Delaware
corporation with its principal place of business in California
("**Kiln**", "**we**", "**us**", or "**our**"). By downloading,
installing, accessing, or using any part of the Kiln software or
services — including the open-source Kiln CLI, the MCP server, the
web workshop at `app.kiln3d.com`, the REST API at `api.kiln3d.com`,
the marketing site at `kiln3d.com`, and any paid tier
subscription — you agree to these Terms. If you don't agree,
don't use Kiln.

If you are using Kiln on behalf of an organization, you represent
that you have authority to bind that organization, and "you"
refers to both you personally and the organization.

These Terms incorporate our [Privacy Policy](https://kiln3d.com/privacy)
by reference.

## 2. Eligibility

You may use Kiln only if:

1. You are **at least 16 years old** (EU/UK) or **at least 13 years
   old** (other jurisdictions), and of legal age to form a binding
   contract in your jurisdiction;
2. You are not barred from using Kiln under the laws of the United
   States or the laws of your country of residence (including the
   export controls and sanctions restrictions in §19);
3. You are not located in, organized under the laws of, or a
   resident of a country or territory subject to **US
   comprehensive sanctions** (currently: Cuba, Iran, North Korea,
   Syria, Russia, Belarus, the Crimea region of Ukraine, the
   Donetsk and Luhansk regions, and any others added by OFAC); and
4. You are not on the **US Specially Designated Nationals (SDN)
   List**, the **Entity List**, or any equivalent restricted-parties
   list maintained by any other government.

You are responsible for complying with all applicable laws in
your jurisdiction, including laws governing manufacturing,
intellectual property, export controls, product safety, consumer
protection, and privacy.

## 3. Your account

### 3.1 Account creation

Paid tiers and the web workshop require an account. You create one
by signing in via Google, Apple, GitHub, or an email magic link.
We rely on these identity providers to verify your email address;
you must ensure the email tied to your OAuth identity is
current and accessible.

### 3.2 Account security

You are responsible for:

- Keeping your account credentials and session tokens confidential;
- All activity that occurs under your account, whether or not
  authorized by you — **including all actions taken by AI agents
  you have configured or authorized**;
- Promptly notifying us at adam@kiln3d.com if you suspect
  unauthorized access.

We are not liable for losses arising from your failure to secure
your account.

### 3.3 One account per person or organization

You may not create multiple accounts to evade quotas, rate limits,
fee tiers, the fulfillment free-tier allowance, or any other
system limit. We may suspend or terminate accounts created in bad
faith to circumvent restrictions.

## 4. License to use Kiln

### 4.1 Open-source components

The Kiln CLI, the MCP server, and the public tool surface are
released under the **GNU Affero General Public License v3.0
(AGPL-3.0)**. Your rights and obligations for that code are
governed by the license text in the `LICENSE` file of the Kiln
repository. These Terms do not restrict the rights granted to you
by AGPL-3.0 for that code.

### 4.2 Paid services

Subject to your compliance with these Terms and payment of
applicable fees, Hadron Labs Inc. grants you a **limited,
non-exclusive, non-transferable, non-sublicensable, revocable
license** to access and use:

- the web workshop at `app.kiln3d.com`,
- the REST API at `api.kiln3d.com`,
- the paid tier features unlocked by your subscription, and
- the proprietary components of the Kiln platform that are not
  released under AGPL-3.0 (including `kiln-pro` and the
  orchestration infrastructure)

for your internal business or personal use. You may not sublicense,
resell, or redistribute the paid services except as expressly
permitted in writing by us.

### 4.3 Reserved rights

All rights not expressly granted are reserved. The Kiln name, the
kiln-arch logomark, the `kiln3d.com` and `app.kiln3d.com` domain
trade dress, and all other Kiln brand assets are the trademarks
of Hadron Labs Inc. You may not use them without our prior
written permission, except for nominative fair use (e.g.,
referring to the product in documentation or articles).

## 5. Subscriptions, billing, and refunds

### 5.1 Paid tiers

Kiln offers paid subscription tiers (currently Pro, Business, and
Enterprise). Tier features, limits, and prices are listed at
[kiln3d.com/pricing](https://kiln3d.com/pricing). We may change
tier features or pricing with **at least 30 days' notice** to
your account email; changes take effect at your next renewal.

### 5.2 Auto-renewal

Unless you cancel before the renewal date, your subscription
**automatically renews** at the end of each billing cycle
(monthly or annual, depending on the plan you chose) and your
payment method is charged the then-current price. This is the
same model used by services like Netflix, Spotify, and GitHub.
You authorize Hadron Labs Inc. (via Stripe) to charge your
payment method on each renewal until you cancel.

### 5.3 Cancellation

You can cancel your subscription at any time via
`/app/settings/billing` or by emailing adam@kiln3d.com.
Cancellation takes effect **at the end of the current paid
period** — you retain access through the period you've already
paid for. We do not issue pro-rated refunds for partial billing
periods (see §5.6 for exceptions).

### 5.4 Failed payments and dunning

If a renewal charge fails, we will:

1. Notify you by email immediately;
2. Automatically retry the charge up to 4 times over 15 days;
3. Grace your entitlement for up to **15 days** from the failed
   charge — your tier remains active while we retry;
4. Mark the entitlement as "payment_failed" after 15 days, which
   reverts you to the free tier until payment is resolved;
5. Cancel the subscription after 30 days of continuous failure.

You can update your payment method at any time via the Stripe
billing portal, accessible from `/app/settings/billing`.

### 5.5 Free trials

Any free trial we offer will clearly state its duration and the
recurring charge that begins at its end. You must cancel before
the trial ends to avoid being charged. One free trial per
customer.

### 5.6 Refunds

- **Monthly subscriptions:** non-refundable except where required by
  law. Cancel to stop future charges.
- **Annual subscriptions:** refundable within **14 days of the
  initial charge** for any reason, pro-rated for the days used.
  After 14 days, non-refundable.
- **Fulfillment orchestration fees:** if a fulfillment order
  fails during manufacturing or is cancelled by the provider,
  we refund the orchestration fee automatically. For delivery
  defects, contact the manufacturer for a reprint or refund of
  the manufacturing cost; we refund the orchestration fee upon
  confirmation the provider has issued their refund.
- **Refund requests** must be made within 30 days of the
  original charge.
- Refunds are processed through the original payment method and
  may take 3–10 business days to appear on your statement.

### 5.7 Chargebacks

If you dispute a charge directly with your bank or card network
without first contacting us, we may suspend or terminate your
account while the chargeback is resolved. Before initiating a
chargeback, please email adam@kiln3d.com — we almost always
resolve billing issues without card-network involvement.

### 5.8 Spend caps on fulfillment orders

To protect you from runaway charges — including charges caused by
an AI agent or automated workflow misfiring — we cap how much you
can spend through Kiln on external fulfillment (Craftcloud and
any future fulfillment provider routed through Kiln):

- **Per-order cap**: a single fulfillment order may not exceed
  **$500.00 USD**, summing the manufacturing provider's quoted
  price and any orchestration fee charged under §6.
- **Monthly cap**: your cumulative fulfillment spend in any
  calendar month (UTC) may not exceed **$2,000.00 USD**, summed
  over every fulfillment order placed that month, including each
  order's manufacturer total and orchestration fee.

If a job would exceed either cap, Kiln refuses the order **before
any charge is created**. You receive a clear error identifying
which cap was reached; nothing is deducted from your card.

These caps apply to all paid tiers and protect your downside
only — they do not commit Kiln to any minimum order size, nor
guarantee that an order under the cap will be accepted. We may
adjust these caps in future versions of these Terms under the
change-notification rules in §22; an increase or decrease is a
material change and will be communicated at least 30 days before
it takes effect.

**Adjusting your caps.** Paid users can raise either cap above
the default through the web app at
`app.kiln3d.com/settings/billing/spend-caps`. Cap raises require
two-factor authentication and are limited to values at or above
the protective default ($500 / $2,000); you may not lower a cap
below those defaults. Every change is logged, and you receive an
email confirmation with a 24-hour revert link. Per-order one-shot
approvals — for orders that exceed your standing caps without
changing them — are also available via the web app when an order
is refused at the cap. **No CLI or MCP tool can raise your caps
or self-approve an order on your behalf**; only an action
initiated from the browser can change them.

## 6. Orchestration fees for fulfillment

When you route a print job to an external fulfillment provider
through Kiln, we charge an **orchestration fee** for order
routing, provider management, status tracking, and platform
infrastructure:

- **Orchestration fee**: 5% of the manufacturing provider's quoted price
- **Minimum fee**: $0.25 per order
- **Maximum fee**: $200.00 per order
- **Free tier**: your first 3 fulfillment orders each calendar month are fee-free

Fees are charged at the time the order is placed with the
manufacturing provider. **Local printing — on your own printers,
using your own materials — is and will remain free.** We never
charge for file management, slicing, status monitoring, or any
other operation that doesn't involve an external manufacturer.

## 7. Applicable taxes

Depending on your jurisdiction, applicable taxes (sales tax, VAT,
GST, or JCT) may be added to subscription fees and orchestration
fees:

| Jurisdiction | Tax | Reverse charge available? |
|---|---|---|
| United States | State sales tax (CA, TX, NY, WA, FL, IL, MA and others per nexus) | No |
| European Union | VAT at buyer's country rate (19–25%) | Yes, with valid EU VAT ID |
| United Kingdom | 20% VAT | Yes, with valid UK VAT number |
| Canada | Federal GST (5%) + applicable provincial tax (HST / PST / QST) | No |
| Australia | 10% GST | Yes, for GST-registered buyers |
| Japan | 10% JCT (consumption tax) | Yes |

Tax on orchestration fees is calculated on the orchestration fee
only, not on the manufacturing cost. The manufacturing provider is
responsible for any taxes on their charges.

Preview the tax for any order with the `tax_estimate` tool before
placing it. We display the full fee + tax breakdown before
charging. **No hidden fees.**

## 8. Your responsibilities — 3D printing safety

By using Kiln you acknowledge and agree that:

1. **You are responsible for what you print.** Kiln does not
   monitor, filter, or restrict the geometry of files you
   download, generate, slice, or send to your printer. The
   decision to print any object is yours alone. You are
   responsible for ensuring that every object you produce
   complies with applicable laws — including laws governing
   firearms, weapons, regulated medical devices, automotive
   safety parts, and intellectual property (patent, trademark,
   copyright, and trade secret laws).

2. **You are responsible for printer safety.** Kiln includes
   safety systems that enforce physical limits — temperature
   caps, speed limits, G-code validation, bed-boundary checks,
   and blocked command detection. **These reduce risk; they do
   not eliminate it.** Unattended 3D printing carries inherent
   fire and mechanical hazards. Follow your printer
   manufacturer's operating, maintenance, and safety guidelines.
   Keep a working smoke detector and fire extinguisher near any
   unattended printer.

3. **You are responsible for maintaining your printer.**
   Well-maintained hardware is a prerequisite for any of Kiln's
   safety checks to work. Worn thermistors, loose wiring,
   improperly-levelled beds, and degraded hotends are outside
   Kiln's ability to detect.

## 9. Automated control risks — AI-agent operation

This section is deliberately prominent because it describes the
risk profile of Kiln's most distinctive feature.

Kiln exposes tools over the Model Context Protocol (MCP) that
third-party AI agents — such as Claude, Claude Code, Cursor,
ChatGPT, and other MCP clients — can invoke to perform physical
operations on your printer, including homing, motion, heating,
extrusion, bed levelling, file uploads, and unattended print
execution. Kiln agents may initiate printer motion, heating, and
material extrusion **autonomously based on your instructions or
inferred intent**. These agents operate on your behalf under
your configuration; **Kiln does not select, direct, or supervise
them.**

Kiln validates tool inputs and refuses unsafe operations where
they are deterministically detectable (off-bed geometry, missing
homing sequences, temperatures exceeding the printer's
mechanical limits, G-code with blocked commands, blocked command
dialects). **These validations reduce but do not eliminate the
risk of physical harm.** Kiln cannot and does not guarantee that
the invoking agent will call tools correctly, in the correct
sequence, or in accordance with your intent.

**You remain solely responsible for:**

- Reviewing previews and agent-proposed actions before confirming them.
- Supervising unattended prints.
- Any physical damage, personal injury, lost work, or loss of
  equipment arising from agent behavior or misbehavior — including
  agents making mistakes, misinterpreting your intent, or invoking
  tools in harmful sequences.
- Complying with your printer manufacturer's operating,
  maintenance, and safety guidelines.

**Hadron Labs Inc. assumes no liability for agent actions, agent
errors, agent misinterpretation of user intent, or any physical,
financial, or other consequences arising from the automated
execution of tools in this software. This disclaimer is in
addition to §15 (Disclaimer of Warranties) and §16 (Limitation
of Liability).**

## 10. Acceptable use

You agree **not to** use Kiln to:

1. Violate any applicable law, regulation, sanction, or
   third-party right.
2. Manufacture items that are illegal in your jurisdiction,
   including firearms, firearm components, firearm accessories
   that are regulated in your jurisdiction, counterfeit
   currency, counterfeit goods, counterfeit trademarks, or
   items that infringe the intellectual-property rights of others.
3. Manufacture medical devices, automotive safety parts,
   aerospace components, structural load-bearing parts, or
   other items subject to certification requirements you have
   not met.
4. Print objects depicting child sexual abuse material (CSAM) or
   other content illegal under the laws of the United States.
5. Circumvent Kiln's safety systems, rate limits, entitlement
   checks, authentication, or other technical protection measures.
6. Reverse-engineer, decompile, or attempt to derive the source
   code of proprietary (non-AGPL) components of Kiln, except to
   the extent such restrictions are prohibited by applicable law.
7. Use Kiln to build a product that directly competes with Kiln
   without a separate written agreement.
8. Resell access to Kiln or the paid services to third parties
   without our written authorization.
9. Interfere with or disrupt the integrity or performance of
   Kiln's services, including through denial-of-service
   attacks, malicious code, or attempts to gain unauthorized
   access.
10. Scrape, harvest, or otherwise collect data from other users —
    including bulk-downloading other people's designs, branches,
    versions, PR comments, reflog entries, or org membership rosters.
11. Impersonate another person, entity, or Kiln representative —
    including in PR descriptions, commit messages, branch names,
    comments, or org invites.
12. Send spam, bulk unsolicited messages, or any content that
    violates the acceptable-use policies of integrated third
    parties (Google, Apple, GitHub, Stripe, Circle, Craftcloud,
    MyMiniFactory, Cults3D).
13. Abuse the workshop's collaboration features: sending unsolicited
    org or team invites, forking designs to harass their authors,
    opening PRs in bad faith, or using reflog access to build
    profiles of other users' activity patterns.
14. Use the opt-in community datasets (`community_prints`,
    `community_recoveries`) to attempt to re-identify or
    fingerprint individual contributors.
15. Circumvent the workshop's permission model — attempting to
    read or modify designs, branches, versions, or org data you
    have not been granted access to by the owner or by your role.

We may suspend or terminate your account for violations of this
§10 without prior notice where we believe an immediate response
is needed to protect users or third parties.

## 11. Your content and intellectual property

### 11.1 Your content stays yours

You retain all right, title, and interest in and to the files,
models, prompts, configurations, branches, version commits, PR
descriptions, PR comments, release notes, cherry-picks, features,
presets, and every other artifact you upload to or generate with
Kiln ("**Your Content**"). We do not claim ownership of Your
Content. When you push a new version of a design to the web
workshop's git-for-3D layer, you own every commit in the history
just like you'd own every commit you push to a personal GitHub
repo.

### 11.2 The license you grant to us

To operate the service on your behalf, you grant us a
**worldwide, non-exclusive, royalty-free, sub-licensable
license** to host, store, copy, transmit, process, and modify
Your Content **solely to the extent necessary to provide the
Kiln service to you** — for example:

- storing your commit history in the git-for-3D cloud layer so
  you can pull past versions;
- auto-generating thumbnail previews so you can browse your
  library visually;
- propagating commits, branches, PR comments, and releases to
  collaborators you have explicitly granted access to via an
  org or team role;
- routing a model file to a fulfillment provider you selected;
- storing slicer configurations, features, or presets you
  choose to save to the cloud;
- retaining event logs for debugging.

This license ends when Your Content is deleted, subject to our
legitimate retention requirements documented in the Privacy
Policy (backups, legal holds).

We will **not** use Your Content to train machine-learning
models, to generate derivative products, for marketing, or for
any purpose other than operating the service for you —
**unless you explicitly opt in via a clearly-labeled consent
flow.** Today we have no ML pipeline trained on user content,
and we are not collecting data for that purpose. If we ever
build one (for example, to improve slice-quality prediction or
failure-mode detection), we will ask your permission first and
let you decline without losing any features. This commitment
is stronger than the industry norm because Kiln is built by
and for people who care about IP ownership of what they design
and print.

### 11.3 Your representations about Your Content

You represent and warrant that you have all rights necessary to
upload and use Your Content through Kiln, and that Your Content
does not infringe any third party's IP rights, violate any law,
or breach any agreement you are subject to.

### 11.4 Designs owned by organizations

When you create or push to a design that belongs to an
organization (an "**Org Design**"), the design itself is owned by
the organization, not by you personally — even if you authored
individual commits to it. While you are a member of the org:

- You can push, pull, open PRs, comment, and release per the role
  the org's admin has granted you;
- Your individual commits remain attributed to you in the reflog
  and version history (the "authorship" of each commit);
- The underlying design files belong to the org and cannot be
  extracted by you alone.

When you leave an org (or are removed):

- Your access to the org's designs ends immediately;
- Your authorship attribution on prior commits remains in the
  history, so collaborators can see who wrote which part
  (standard git-style audit trail);
- On your written request, we will anonymize your authorship on
  Org Design commits — we replace your name with "a former
  collaborator." We cannot remove your commits from the history
  without destroying other contributors' work, so we do not
  offer that.

Ownership of the org itself (and any Org Designs) transfers to
the remaining admins if you, as owner, leave. If no admin
remains, the org enters a 30-day wind-down period during which
a new owner can be appointed; if none is, the org and all its
designs are permanently deleted.

### 11.5 Public designs and the community datasets

If Kiln adds a "make design public" feature in the future, and
you explicitly opt in to making a design public, you grant Kiln
and other users a non-exclusive right to view and clone the
public version of that design, subject to any license you
attach to it (e.g., CC-BY-NC-SA, MIT, proprietary). You remain
the copyright owner; public sharing does not transfer ownership.

If you opt in to contribute to the **community datasets**
(`community_prints`, `community_recoveries`) by explicitly
calling a `community_share` tool, you grant Kiln and every user
a **perpetual, irrevocable, royalty-free, worldwide license** to
use the anonymized contribution for research, training of
success-rate models, and aggregate analytics that benefit the
Kiln user community. The ingestion pipeline strips your email,
auth_user_id, tenant_id, file names, and geometry before storage
— only printer model, material, settings hash, outcome, and
quality grade are retained. **Because the data is anonymized at
ingestion, we cannot identify or retract individual
contributions once they're in the dataset.** Only opt in if you
are comfortable with permanent donation of that (anonymized)
record.

### 11.6 Feedback

If you submit feedback, feature requests, or suggestions to us
("**Feedback**"), you grant us a **perpetual, irrevocable,
royalty-free, worldwide license** to use Feedback for any
purpose without obligation. Don't send us Feedback you want to
keep proprietary.

## 12. Third-party services and content

Kiln connects to third-party services — Google / Apple / GitHub
OAuth, Stripe, Circle, Supabase, Fly.io, Vercel, Craftcloud,
MyMiniFactory, Cults3D, and others. Your use of those services
is subject to each provider's own terms of use and privacy
policy. We are not a party to the contract between you and any
third-party service, and we are not responsible for acts,
omissions, content, or availability of third-party services.

Content downloaded from third-party marketplaces is governed by
the respective marketplace's terms and the licenses attached to
individual listings. You are responsible for verifying the
license on any model you download before you print, sell, or
redistribute it.

## 13. DMCA and copyright

If you believe content accessible through Kiln infringes your
copyright, send a notice under the Digital Millennium Copyright
Act (17 U.S.C. §512) to our designated agent:

**DMCA Agent, Hadron Labs Inc.**
Email: [adam@kiln3d.com](mailto:adam@kiln3d.com)

Your notice must include all elements required by §512(c)(3):

1. Physical or electronic signature of the copyright owner or
   authorized agent;
2. Identification of the copyrighted work claimed to be infringed;
3. Identification of the infringing material and information
   reasonably sufficient to let us locate it (including URL);
4. Your contact information;
5. A statement that you have a good-faith belief that the use is
   not authorized by the copyright owner, its agent, or the law;
6. A statement, under penalty of perjury, that the information in
   the notice is accurate and that you are authorized to act on
   behalf of the copyright owner.

**Counter-notices** may be sent to the same email and must
include the elements required by §512(g)(3).

We will respond to valid DMCA notices by removing or disabling
access to the identified material and will terminate the accounts
of users who are repeat infringers.

## 14. Suspension and termination

### 14.1 By you

You can terminate your account at any time via
`/app/settings/account → Delete account` or by emailing
adam@kiln3d.com. Termination cancels all active subscriptions
effective at the end of the current paid period.

### 14.2 By us

We may suspend or terminate your account, without prior notice,
if:

1. You materially breach these Terms (including §10 Acceptable
   Use);
2. You pose a security risk to us or other users;
3. We are required to do so by law or by an order of a court or
   governmental authority;
4. Your account has been inactive for 12+ months and has no active
   paid subscription;
5. We discontinue the service or any part of it — we will provide
   at least 90 days' notice for a full discontinuation.

For non-urgent violations, we will provide notice and a
reasonable opportunity to cure where practicable.

### 14.3 Effect of termination

On termination, the following cascade applies:

**Access:**

- Your access to paid services ends immediately;
- Access to org-owned designs via any org you were a member of
  ends immediately;
- Sessions on every device are invalidated.

**Your personal designs and workshop content:**

- All designs, branches, versions, releases, cherry-picks,
  features, presets, and related metadata that you personally
  own (not org-owned) are retained for **90 days** in case you
  reinstate. After 90 days they are permanently deleted along
  with your account.

**Org-owned designs (where you contributed):**

- Org-owned designs remain with the org. Your individual commits
  remain attributed to you in the history (like GitHub); the
  org's admins can request anonymization of your authorship
  (replacing your name with "a former collaborator") per §11.4.
- Orgs that you personally own transition to the remaining
  admins or enter a 30-day wind-down per §11.4.

**PR comments on others' designs:**

- Comments you posted on others' PRs remain visible on the host
  design's history to preserve review context. On written
  request to adam@kiln3d.com we will anonymize your authorship.

**Community dataset contributions:**

- Anonymized contributions to `community_prints` and
  `community_recoveries` remain in the dataset — they cannot be
  retracted because we can't trace records back to you after
  ingestion (see §11.5).

**Billing:**

- Outstanding charges remain due;
- Refunds are governed by §5.6;
- Invoices and payment records are retained per Privacy Policy
  §7 retention schedule (7 years for tax compliance).

**Surviving sections:**

The following Sections survive termination: §4.3 (reserved
rights), §5.6 (refunds), §5.7 (chargebacks), §8 (your
responsibilities), §9 (automated control risks), §11.4 (org
designs + authorship attribution), §11.5 (public + community
data licenses), §11.6 (feedback), §15 (warranties), §16
(liability), §17 (indemnification), §19 (export), §20 (governing
law), §21 (arbitration), §23 (notices), §24 (general).

## 15. Disclaimer of warranties

**KILN, THE WEB WORKSHOP, THE REST API, THE CLI, THE MCP SERVER,
THE PAID SERVICES, AND ANY CONTENT, TOOLS, OR INTEGRATIONS
MADE AVAILABLE THROUGH THEM ARE PROVIDED "AS IS" AND "AS
AVAILABLE" WITHOUT WARRANTIES OF ANY KIND, WHETHER EXPRESS,
IMPLIED, OR STATUTORY.**

Without limiting the foregoing, **we expressly disclaim** all
warranties of merchantability, fitness for a particular purpose,
non-infringement, quiet enjoyment, system integration,
accuracy, title, and any warranties arising from a course of
dealing, usage, or trade practice.

We do not warrant that:

- The service will be uninterrupted, secure, or error-free;
- Defects will be corrected;
- The service will meet your requirements;
- Any data will be preserved against loss or corruption;
- AI-agent actions will be correct, safe, or in accordance with
  your intent.

Some jurisdictions do not allow exclusion of certain implied
warranties; in those jurisdictions, the exclusions above apply
to the maximum extent permitted by law.

## 16. Limitation of liability

**TO THE MAXIMUM EXTENT PERMITTED BY LAW:**

1. **Excluded damages.** Hadron Labs Inc., its officers,
   directors, employees, contractors, and affiliates will not be
   liable for any **indirect, incidental, consequential,
   special, exemplary, or punitive damages**, including lost
   profits, lost revenue, lost data, lost goodwill, loss of
   anticipated savings, business interruption, or procurement
   of substitute goods or services, even if we have been
   advised of the possibility of such damages.

2. **Liability cap.** Our total cumulative liability to you
   arising out of or related to these Terms or your use of Kiln,
   for all claims of every kind, **will not exceed the greater
   of (a) $100 USD, or (b) the fees you paid to us in the 12
   months preceding the event giving rise to the claim.**

3. **3D printing + AI-agent physical harm.** To the extent
   permitted by law, the limitations in (1) and (2) above apply
   to any claim arising from physical damage, property loss,
   personal injury, or death caused by printer operation, AI-agent
   actions, fulfillment orders, or any other operation of Kiln.
   Some jurisdictions do not permit the exclusion or limitation of
   liability for personal injury or death caused by negligence; in
   those jurisdictions, our liability is limited only to the
   extent permitted by law.

4. **Essential bargain.** You understand and agree that the
   limitations in this §16 are an essential element of the
   bargain between you and us, and that absent these limitations
   we would not provide the service at the fees we charge or at
   all.

## 17. Indemnification

You agree to **defend, indemnify, and hold harmless** Hadron Labs
Inc., its officers, directors, employees, contractors, and
affiliates from and against any claims, damages, losses,
liabilities, costs, and expenses (including reasonable
attorneys' fees) arising out of or related to:

1. Your use or misuse of Kiln;
2. Your Content or the objects you manufacture;
3. Your violation of these Terms, including §10 (Acceptable Use);
4. Your violation of any applicable law or third-party right;
5. Actions taken by AI agents you authorized or configured;
6. Your failure to supervise unattended prints or follow printer
   manufacturer guidance.

We reserve the right to assume the exclusive defense and control
of any matter subject to indemnification by you, at your
expense. You agree to cooperate with our defense of any claim.

## 18. Representations by you

You represent and warrant, on each date you use Kiln, that:

1. You have full legal right and authority to enter into these
   Terms;
2. All information you provide to us is accurate, current, and
   complete;
3. You will use Kiln in compliance with these Terms and applicable
   law; and
4. Neither you nor any entity on whose behalf you use Kiln is
   located in a sanctioned jurisdiction (§2) or listed on any
   restricted-parties list (§19).

## 19. Export controls and sanctions

Kiln is subject to **US export control laws**, including the
Export Administration Regulations (15 C.F.R. §§ 730–774) and
the economic-sanctions programs administered by the US Treasury
Department's Office of Foreign Assets Control (OFAC).

You agree that you will not — directly or indirectly — export,
re-export, transfer, or release Kiln or any of its technical data:

1. To any country, entity, or person subject to US sanctions or
   export restrictions listed in §2;
2. For any use prohibited under US export laws, including the
   design or production of nuclear, chemical, or biological
   weapons, missile technology, or unmanned aerial vehicles; or
3. In contravention of export or import laws of your own
   jurisdiction.

You are responsible for obtaining any licenses or authorizations
required for your use of Kiln under the laws applicable to you.

## 20. Governing law

These Terms, and any dispute arising out of or related to these
Terms or your use of Kiln, are governed by the **laws of the
State of Delaware, USA**, without regard to its conflict-of-laws
rules. The United Nations Convention on Contracts for the
International Sale of Goods does not apply.

## 21. Binding arbitration and class-action waiver

**READ THIS SECTION CAREFULLY — IT AFFECTS YOUR LEGAL RIGHTS,
INCLUDING YOUR RIGHT TO FILE A LAWSUIT IN COURT.**

### 21.1 Agreement to arbitrate

You and Hadron Labs Inc. agree that any dispute, claim, or
controversy arising out of or relating to these Terms, the
Privacy Policy, Kiln, or the relationship between you and us
(a "**Dispute**") will be resolved by **binding arbitration**
conducted by the **American Arbitration Association (AAA)**
under its **Consumer Arbitration Rules** (or, for Business or
Enterprise tier customers, its Commercial Arbitration Rules), as
modified by this §21.

The arbitration will be conducted in **San Francisco County,
California, USA**, unless you and we agree otherwise. For
individual consumer claims under $10,000, at your option, you
may elect to participate in the arbitration by telephone,
videoconference, or written submission only (no in-person
appearance required). A single arbitrator will preside.

The arbitrator will issue a reasoned written award, which is
final and binding. Judgment on the award may be entered in any
court of competent jurisdiction.

### 21.2 Small-claims exception

Either you or we may bring an **individual claim in small-claims
court** in a jurisdiction where both parties are subject to
personal jurisdiction, **so long as the claim remains in small
claims**. If a small-claims case is transferred, removed, or
appealed outside of small-claims court, this §21 resumes and the
case must continue in arbitration.

### 21.3 Class-action waiver

**YOU AND WE EACH AGREE THAT CLAIMS MAY BE BROUGHT ONLY IN YOUR
OR OUR INDIVIDUAL CAPACITY, AND NOT AS A PLAINTIFF OR CLASS
MEMBER IN ANY PURPORTED CLASS, COLLECTIVE, CONSOLIDATED, PRIVATE
ATTORNEY GENERAL, OR REPRESENTATIVE PROCEEDING.** The
arbitrator may not consolidate the claims of more than one
person and may not preside over any form of class or
representative proceeding. If this class-action waiver is found
unenforceable, then the entire §21 (except the small-claims
exception) will be null and void, but the remainder of these
Terms will continue to apply.

### 21.4 30-day opt-out right

**You can opt out of this arbitration agreement** by emailing
[adam@kiln3d.com](mailto:adam@kiln3d.com)
**within 30 days of first accepting these Terms** (or of first
accepting any material change to this §21). Include your name,
email, and a clear statement that you opt out of the arbitration
agreement. Opting out does not affect any other part of these
Terms.

If you opt out, all Disputes between you and us will be resolved
in the state or federal courts located in Delaware (or, at your
option for consumer claims, in the state or federal courts
located in your district of residence), and you and we consent
to the personal jurisdiction of those courts.

### 21.5 Federal Arbitration Act

These arbitration provisions are governed by the **Federal
Arbitration Act** (9 U.S.C. § 1 et seq.) and federal arbitration
law. They will remain in effect after termination of these
Terms or your account.

### 21.6 Fees

For consumer claims of $10,000 or less, Hadron Labs Inc. will
pay all AAA filing, administration, and arbitrator fees. For
larger claims, AAA's standard fee allocation applies, and either
party may seek fee-shifting per applicable law.

### 21.7 Injunctive relief carve-out

Either party may bring an action in court — without first
arbitrating — to protect its intellectual property rights or to
prevent unauthorized access to the service (injunctive relief
only). Any damages claim remains subject to §21.1.

## 22. Changes to these Terms

We may update these Terms from time to time. When we make
changes, we will:

1. Update the "Last updated" date and increment the version
   number at the top;
2. Preserve prior versions in public Git history at
   [github.com/codeofaxel/Kiln/blob/main/TERMS.md](https://github.com/codeofaxel/Kiln/blob/main/TERMS.md);
3. For **material changes** (changes to fees, cancellation
   terms, liability, arbitration, acceptable-use restrictions,
   or other terms that meaningfully affect your rights), email
   you at the address on your account **at least 30 days before
   the change takes effect**, and require re-acceptance during
   your next signin if possible.

Continued use of Kiln after the effective date of a change
constitutes acceptance of the revised Terms. If you don't agree
with a change, cancel your subscription before the effective date.

## 23. Notices

We communicate with you via the email address on your account.
It is your responsibility to keep that email current and
accessible. Notices from us are deemed received when sent to
your account email.

You may send notices to us at:

**Hadron Labs Inc., Legal Department**
Email: [adam@kiln3d.com](mailto:adam@kiln3d.com)
Postal mail: Hadron Labs Inc., c/o Harvard Business Services, Inc. (Registered Agent), 16192 Coastal Hwy, Lewes, DE 19958, USA

Notices are effective on receipt.

## 24. General

### 24.1 Entire agreement

These Terms (together with the Privacy Policy, any DPA, and any
other agreements expressly incorporated by reference) constitute
the entire agreement between you and us regarding Kiln and
supersede all prior or contemporaneous understandings.

### 24.2 Severability

If any provision of these Terms is held to be invalid or
unenforceable, that provision will be struck or modified only to
the minimum extent necessary, and the remaining provisions will
remain in full force and effect.

### 24.3 No waiver

Our failure to enforce any provision is not a waiver. A waiver
is effective only if in writing signed by an authorized
representative of Hadron Labs Inc.

### 24.4 Assignment

You may not assign or transfer these Terms or any rights or
obligations hereunder without our prior written consent. We may
assign these Terms in connection with a merger, acquisition,
reorganization, sale of assets, or by operation of law.

### 24.5 No agency; no third-party beneficiaries

Nothing in these Terms creates a partnership, agency, joint
venture, or employment relationship between you and us. There
are no third-party beneficiaries to these Terms.

### 24.6 Force majeure

Neither party is liable for failure or delay in performance
caused by acts beyond its reasonable control, including acts of
God, natural disasters, war, terrorism, civil unrest, labor
actions, government actions, internet or telecommunications
failures, power outages, and pandemics. The affected party will
notify the other and use reasonable efforts to resume performance.

### 24.7 Headings; interpretation

Headings are for convenience only and do not affect
interpretation. "Including" means "including without
limitation." References to "you" and "we" include successors
and permitted assigns.

### 24.8 Language

These Terms are drafted in English. Any translation is provided
for convenience, and the English version controls in the event
of inconsistency.

### 24.9 US Government rights

If you are a US Government end user, Kiln and its documentation
are "Commercial Items" as defined in FAR 2.101, consisting of
"Commercial Computer Software" and "Commercial Computer Software
Documentation" per 48 C.F.R. 12.212 and 48 C.F.R. 227.7202.
Consistent with those provisions, Kiln is licensed to US
Government end users only as Commercial Items and with only
those rights granted to all other end users under these Terms.

## 25. Contact

- **General support** — [adam@kiln3d.com](mailto:adam@kiln3d.com)
- **Billing** — [adam@kiln3d.com](mailto:adam@kiln3d.com)
- **Legal / agreements** — [adam@kiln3d.com](mailto:adam@kiln3d.com)
- **DMCA notices** — [adam@kiln3d.com](mailto:adam@kiln3d.com)
- **Security** — [adam@kiln3d.com](mailto:adam@kiln3d.com)
- **Privacy** — [adam@kiln3d.com](mailto:adam@kiln3d.com)
- **Enterprise DPA** — [adam@kiln3d.com](mailto:adam@kiln3d.com)
- **Arbitration opt-out** — [adam@kiln3d.com](mailto:adam@kiln3d.com)
- **Postal** — Hadron Labs Inc., c/o Harvard Business Services, Inc. (Registered Agent), 16192 Coastal Hwy, Lewes, DE 19958, USA

---

**By using Kiln, you confirm that you have read, understood,
and agreed to these Terms of Use.**
