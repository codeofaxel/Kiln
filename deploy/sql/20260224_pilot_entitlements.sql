-- Pilot entitlement metadata store (privacy-preserving).
-- Stores only license/admin metadata. No print/job payloads.

create table if not exists public.pilot_entitlements (
  id bigserial primary key,
  jti text not null unique,
  email text not null,
  email_hash text not null,
  tier text not null check (tier in ('free', 'pro', 'business', 'enterprise')),
  issued_at timestamptz not null default now(),
  expires_at timestamptz not null,
  key_hash text not null unique,
  key_hint text not null,
  max_activations integer not null default 3 check (max_activations > 0 and max_activations <= 100),
  status text not null default 'active' check (status in ('active', 'revoked', 'expired')),
  notes text not null default '',
  revoked_at timestamptz,
  revoked_reason text not null default ''
);

create index if not exists idx_pilot_entitlements_email on public.pilot_entitlements (email);
create index if not exists idx_pilot_entitlements_email_hash on public.pilot_entitlements (email_hash);
create index if not exists idx_pilot_entitlements_expires_at on public.pilot_entitlements (expires_at);
create index if not exists idx_pilot_entitlements_status on public.pilot_entitlements (status);

create table if not exists public.license_security_events (
  id bigserial primary key,
  jti text not null,
  email_hash text not null,
  event_type text not null check (event_type in ('activation', 'validation', 'revocation_check', 'refresh')),
  device_hash text not null default '',
  ip_coarse_hash text not null default '',
  client_version text not null default '',
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_license_security_events_jti on public.license_security_events (jti);
create index if not exists idx_license_security_events_event on public.license_security_events (event_type);
create index if not exists idx_license_security_events_created_at on public.license_security_events (created_at);
create unique index if not exists idx_license_security_events_activation_dedupe
  on public.license_security_events (jti, device_hash)
  where event_type = 'activation' and device_hash <> '';

-- Recommended retention policy:
-- delete from public.license_security_events where created_at < now() - interval '90 days';
