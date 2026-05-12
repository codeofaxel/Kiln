/**
 * Cookie-consent state for the marketing site (`kiln3d.com`).
 *
 * Strictly necessary cookies are always on; analytics + advertising
 * require explicit consent and default OFF.  Opt-in semantics —
 * GDPR-compliant by construction, also satisfies CCPA's opt-out
 * model because users always start in the un-tracked state.
 *
 * State lives in a first-party cookie (`kiln_consent`) with a
 * 1-year max-age, JSON-encoded and URL-encoded.  Cookie storage
 * (rather than localStorage) so writes propagate across subdomains
 * (kiln3d.com → app.kiln3d.com) and survive any future SSR phase.
 *
 * The pixel components (MetaPixel.astro, GoogleAnalytics.astro,
 * GoogleAdsConversion.astro) read this at script-init time via
 * the inline `readConsent()` helper they each inline.  This module
 * is the canonical source of truth that those inlines mirror.
 *
 * @see ../../components/CookieBanner.astro — the writer
 * @see ../../components/MetaPixel.astro — the readers
 * @see ../../../../PRIVACY.md §3.2 — disclosure
 */

export const CONSENT_COOKIE = "kiln_consent";
export const COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 365;

export type ConsentState = {
  necessary: true;
  analytics: boolean;
  advertising: boolean;
};

export const DEFAULT_CONSENT: ConsentState = {
  necessary: true,
  analytics: false,
  advertising: false,
};

export function readConsent(): ConsentState {
  if (typeof document === "undefined") return DEFAULT_CONSENT;
  try {
    const raw = document.cookie
      .split("; ")
      .find((c) => c.startsWith(`${CONSENT_COOKIE}=`))
      ?.split("=")[1];
    if (!raw) return DEFAULT_CONSENT;
    const parsed = JSON.parse(decodeURIComponent(raw)) as Partial<ConsentState>;
    return {
      necessary: true,
      analytics: parsed.analytics === true,
      advertising: parsed.advertising === true,
    };
  } catch {
    return DEFAULT_CONSENT;
  }
}

export function setConsent(
  partial: Partial<Omit<ConsentState, "necessary">>,
): void {
  if (typeof document === "undefined") return;
  const next: ConsentState = {
    necessary: true,
    analytics: partial.analytics === true,
    advertising: partial.advertising === true,
  };
  const value = encodeURIComponent(JSON.stringify(next));
  document.cookie =
    `${CONSENT_COOKIE}=${value}; ` +
    `path=/; ` +
    `max-age=${COOKIE_MAX_AGE_SECONDS}; ` +
    `SameSite=Lax; ` +
    `Secure`;
}

export function clearConsent(): void {
  if (typeof document === "undefined") return;
  document.cookie = `${CONSENT_COOKIE}=; path=/; max-age=0`;
}

export function hasConsentCookie(): boolean {
  if (typeof document === "undefined") return false;
  return document.cookie
    .split("; ")
    .some((c) => c.startsWith(`${CONSENT_COOKIE}=`));
}
