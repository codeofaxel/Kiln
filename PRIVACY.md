# Kiln Privacy Policy

*Last updated: 2026-02-12 · Version 1.0*

## What Kiln Collects

Kiln is locally-installed software. Your data stays on your machine unless you
explicitly use features that connect to external services.

### Data stored locally (on your machine only)

- **Print job history**: File names, timestamps, printer assignments, and
  completion status.
- **Billing records**: Fee calculations, charge amounts, payment status, and
  provider transaction IDs (not card numbers).
- **Printer configuration**: Hostnames, API keys for your printers, fleet
  settings.
- **Event logs**: System events for debugging and audit purposes.

### Data shared with third parties (only when you use these features)

| Feature | Data shared | Third party |
|---------|-------------|-------------|
| Fulfillment orders | Model file, shipping address, material choice | Craftcloud, Sculpteo (manufacturing providers) |
| Card payments | Payment amount, currency | Stripe |
| USDC payments | Wallet address, payment amount | Circle |
| Marketplace browsing | Search queries | MyMiniFactory, Cults3D, Thingiverse (deprecated) |

Kiln does **not** share data with advertising networks, analytics services, or
data brokers. Kiln does **not** have telemetry or usage tracking.

## Payment Data

- **Credit card numbers are never stored by Kiln.** Card processing is handled
  entirely by Stripe. Kiln only stores a reference ID for each transaction.
- **USDC payments** are processed by Circle. Kiln stores wallet addresses and
  transaction hashes for audit purposes.
- **Billing history** (amounts, dates, order IDs) is stored locally in your
  Kiln database for your records.

## Data Retention

- Local data is retained indefinitely unless you delete it.
- You can delete your local database at any time by removing
  `~/.kiln/kiln.db`.
- Billing records should be retained for at least 7 years for tax compliance
  in most jurisdictions.

## Your Rights

- **Access**: Use `billing_history` and `billing_status` to view all your
  billing data at any time.
- **Deletion**: Delete your local database to remove all Kiln-stored data.
  Note: transaction records at payment providers (Stripe, Circle) are subject
  to their own retention policies.
- **Portability**: Billing data is stored in SQLite and can be exported with
  standard database tools.

## Data Security

- Database files are created with restricted permissions (owner-only read/write).
- API keys for printers and payment providers are stored locally and never
  transmitted to Kiln servers (there are no Kiln servers).
- All communication with external APIs uses TLS/HTTPS.

## Changes

This policy may be updated from time to time. Material changes will be noted
in release notes.

## Contact

Kiln is open-source software. For privacy questions, open an issue at
https://github.com/Kiln3D/kiln/issues.
