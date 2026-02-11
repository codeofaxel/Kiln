# Kiln — Open Tasks

Prioritized backlog of features and improvements.

## High Priority

- **Buy kiln3d.com domain** — Register `kiln3d.com` to match the PyPI package name. Use Namecheap, Cloudflare, or Porkbun.
- **Change Twitter handle to @kiln3d** — Switch from `@3dkiln` to `@kiln3d` for consistent branding across PyPI, domain, and social.
- **Landing page / docs site** — Single-page site with: what Kiln is, demo GIF, install command, whitepaper link, project docs link, GitHub link. GitHub Pages or similar. Host on `kiln3d.com`.
- **Demo video / GIF** — 60-second screen recording: install → discover printer → slice → print. Embed on landing page and README.
- **Claim `kiln3d` across registries** — Front-run the name on platforms we'll eventually publish to: Docker Hub (`kiln3d`), Homebrew tap (`kiln3d`), npm (`kiln3d` — for future JS client), crates.io (`kiln3d` — if Rust components ever happen). Free or near-free, painful to fix later.
- **Claim PyPI names** — Register `kiln-print`, `kiln-mcp`, and `kiln3d-octoprint` as pending publishers or publish placeholder packages.

## Pre-Launch (Ship Day)

- **Configure production crypto wallet addresses** — Replace placeholder/test destination addresses in Circle USDC payment flow with real receiving wallets (Solana and/or Ethereum/Base). Update `KILN_CIRCLE_WALLET_*` env vars in production config. Verify with a small test transfer before going live.
- **Stripe production setup** — Complete Stripe onboarding: (1) Switch from test API keys to live keys, (2) Set up webhook endpoint in Stripe dashboard pointing to production URL, (3) Configure webhook signing secret (`KILN_STRIPE_WEBHOOK_SECRET`), (4) Add customer + payment method for off-session payments, (5) Verify PCI compliance settings, (6) Test end-to-end payment with a real card in live mode. Do NOT go live until webhook signature verification is confirmed working.

## Medium Priority

_(No medium-priority tasks remaining.)_

## Low Priority / Future Ideas

- **Local model cache/library** — Agents save generated or downloaded models locally with tagged metadata (source, prompt, dimensions, print history) so they can reuse them across jobs without re-downloading or re-generating.
- **Pre-commit hooks** — Add black, ruff, mypy, pytest smoke tests for contributor DX.
- **Dependabot config** — `.github/dependabot.yml` for automated dependency updates.
