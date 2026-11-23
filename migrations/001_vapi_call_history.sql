-- Additive, idempotent storage for Vapi end-of-call conversation messages.
-- This migration does not delete or rewrite existing call data.

alter table public.call_transcripts
  add column if not exists transcript_status text not null default 'pending',
  add column if not exists updated_at timestamptz not null default now();

create table if not exists public.call_messages (
  id uuid primary key default gen_random_uuid(),
  call_log_id uuid not null references public.call_logs(id) on delete cascade,
  vapi_call_id text not null,
  message_index integer not null check (message_index >= 0),
  speaker_role text not null check (speaker_role in ('user', 'assistant')),
  message_type text not null default 'conversation',
  message_text text not null,
  message_timestamp timestamptz null,
  external_message_id text null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (call_log_id, message_index)
);

create index if not exists call_messages_vapi_call_id_idx
  on public.call_messages(vapi_call_id);

create index if not exists call_messages_call_log_id_idx
  on public.call_messages(call_log_id);

alter table public.call_messages enable row level security;

grant usage on schema public to service_role;
grant select, insert, update on table public.call_logs to service_role;
grant select, insert, update on table public.call_transcripts to service_role;
grant select, insert, update on table public.call_messages to service_role;
grant select, insert, update on table public.call_identity_verifications to service_role;

comment on table public.call_messages is
  'Permitted customer/assistant conversation messages from authenticated Vapi end-of-call reports.';
