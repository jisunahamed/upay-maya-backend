-- Additive, fail-closed consent fields for backend-initiated voice calls.
-- Existing consent values are not changed; NULL is treated as no permission.

alter table public.customers
  add column if not exists voice_call_consent boolean null,
  add column if not exists opted_out boolean null;

comment on column public.customers.voice_call_consent is
  'Explicit permission for backend-initiated voice calls; TRUE is required.';

comment on column public.customers.opted_out is
  'Explicit voice-contact opt-out; FALSE is required before a call.';
