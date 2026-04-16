# Kiln Terms of Use

*Last updated: 2026-04-15 · Version 1.4*

## What Kiln Is

Kiln is open-source infrastructure software for controlling 3D printers and
interacting with third-party model marketplaces. It is a tool — not a
marketplace, print service, or content platform.

## Your Responsibility

By using Kiln you agree that:

1. **You are responsible for complying with all applicable laws** in your
   jurisdiction, including but not limited to regulations governing
   manufacturing, intellectual property, export controls, and product safety.

2. **You are responsible for what you print.** Kiln does not monitor, filter,
   or restrict the geometry of files you download, generate, slice, or send to
   your printer. The decision to print any object is yours alone.

3. **You are responsible for printer safety.** While Kiln includes safety
   systems that enforce physical limits (temperatures, speeds, G-code
   validation), these systems reduce risk — they do not eliminate it.
   Unattended 3D printing carries inherent fire and mechanical hazards.
   Follow your printer manufacturer's safety guidelines.

## Automated Control Risks

Kiln exposes tools over the Model Context Protocol (MCP) that third-party
AI agents — such as Claude Code, Cursor, ChatGPT, and other MCP clients —
can invoke to perform physical operations on your printer, including
homing, motion, heating, extrusion, bed leveling, file uploads, and
unattended print execution. Kiln agents may initiate printer motion,
heating, and material extrusion autonomously based on your instructions
or inferred intent. These agents operate on your behalf under your
configuration; Kiln does not select, direct, or supervise them.

Kiln validates tool inputs and refuses unsafe operations where they are
deterministically detectable (for example: off-bed geometry, missing
homing sequences, temperatures exceeding the printer's mechanical limits,
G-code with blocked commands, blocked command dialects). These validations
reduce but do not eliminate the risk of physical harm. Kiln cannot and
does not guarantee that the invoking agent will call tools correctly, in
the correct sequence, or in accordance with your intent.

**You remain solely responsible for:**
- Reviewing previews and agent-proposed actions before confirming them.
- Supervising unattended prints.
- Any physical damage, personal injury, lost work, or loss of equipment
  arising from agent behaviour or misbehaviour — including agents making
  mistakes, misinterpreting your intent, or invoking tools in harmful
  sequences.
- Complying with your printer manufacturer's operating, maintenance, and
  safety guidelines.

**Kiln assumes no liability for agent actions, agent errors, agent
misinterpretation of user intent, or any physical, financial, or other
consequences arising from the automated execution of tools in this
software. This disclaimer is in addition to the "No Warranty" section
below.**

## Third-Party Content

Kiln connects to third-party model marketplaces (MyMiniFactory, Cults3D,
Thingiverse, and others). Content from these services is governed by their
respective terms of use and licensing. Kiln does not host, curate, endorse,
or take responsibility for any third-party content.

## Third-Party Fulfillment

When you route print jobs to external fulfillment providers, those providers
operate under their own terms of service, content policies, and legal
obligations. Kiln facilitates the connection but is not a party to those
transactions.

## Orchestration Software Fees

When you place orders through Kiln's fulfillment service (external manufacturing
providers), Kiln charges an orchestration fee:

- **Orchestration fee**: 5% of the manufacturing provider's quoted price.
- **Minimum fee**: $0.25 per order.
- **Maximum fee**: $200.00 per order.
- **Free tier**: Your first 3 fulfillment orders each calendar month are
  fee-free to help you get started.

Fees are charged at the time the order is placed with the manufacturing provider.
The fee covers order routing, provider management, status tracking, and platform
infrastructure.

**All local printing is free.** Kiln never charges fees for printing on your own
printers, file management, slicing, status monitoring, or any other local
operation.

## Applicable Taxes

Depending on your jurisdiction, applicable taxes (sales tax, VAT, GST, or JCT)
may be added to the orchestration fee:

- **United States**: Sales tax applies in states where Kiln has nexus or where
  marketplace facilitator laws require collection (CA, TX, NY, WA, FL, IL, MA).
- **European Union**: VAT is charged at the buyer's country rate (19–25%).
  Businesses with a valid EU VAT ID may qualify for the reverse-charge
  mechanism — no VAT is collected by Kiln.
- **United Kingdom**: 20% VAT applies. B2B reverse charge available with a
  valid UK VAT number.
- **Canada**: Federal GST (5%) plus applicable provincial taxes (HST, PST, QST)
  depending on province.
- **Australia**: 10% GST. B2B reverse charge available for GST-registered buyers.
- **Japan**: 10% JCT (consumption tax). B2B reverse charge available.

Tax is calculated on the **orchestration fee only**, not on the manufacturing cost.
The manufacturing provider is responsible for any taxes on their charges.

You can preview the tax for any order using the ``tax_estimate`` tool before
placing an order. Provide your jurisdiction code and, if applicable, your
business tax ID to see if reverse charge applies.

Kiln displays the full fee + tax breakdown before charging. No hidden fees.

## Payment & Billing

- Payments are processed through Stripe (credit/debit cards) or Circle
  (USDC stablecoin on Solana or Base networks).
- Kiln never stores your credit card numbers — all card data is handled
  by Stripe in compliance with PCI DSS standards.
- You can view your billing history and current charges at any time using
  the ``billing_history`` and ``billing_status`` tools.
- Spend limits are enforced to protect against runaway costs:
  $500 per single order and $2,000 per calendar month by default.

## Refund Policy

- If a fulfillment order fails during manufacturing or is cancelled by the
  provider, Kiln will refund the orchestration fee automatically.
- If you cancel an order before it enters production, any authorized payment
  hold will be released.
- If your order is delivered with defects, contact the manufacturing provider
  directly for a reprint or refund of the manufacturing cost. Kiln will refund
  the orchestration fee upon confirmation that the provider has issued a refund.
- Refund requests must be made within 30 days of the original charge.
- Refunds are processed through the original payment method and may take
  3–10 business days to appear on your statement.

## No Warranty

Kiln is provided **"as is"** without warranty of any kind, express or implied.
The authors and contributors are not liable for any damages arising from the
use of this software, including but not limited to property damage, personal
injury, lost data, or legal liability resulting from objects you manufacture.

## Changes

These terms may be updated from time to time. Continued use of Kiln after an
update constitutes acceptance of the revised terms. Material changes will
prompt re-acceptance during setup.
