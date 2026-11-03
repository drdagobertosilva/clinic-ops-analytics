# Design decisions

Each decision follows the same format: context, decision, consequences. They come from running the
production system, not from a template.

## 1. Pseudonymise at load time, not at report time
**Context.** The data includes appointments and payments per patient, which is health data under
Chile's data-protection law (Law 21.719). The clinic also needed to join records across two EHRs and a
CRM that share no common ID.
**Decision.** Each loader replaces the national ID with `HMAC-SHA256(salt, normalised_id)` truncated to
16 hex characters, and the mobile number with the same scheme. The salt lives only on the clinic's
machine. Names, emails, addresses and message text are never written to the database.
**Consequences.** Cross-system joins still work (same ID → same key). The database holds no direct
identifier, but it is pseudonymised, not anonymous: birth year, sex, municipality and insurance plan are
kept for analysis, and in a small town that combination can single a person out. It is still personal data
under Law 21.719, so the database itself is handled as confidential. If the salt is lost, every key changes: the database can be rebuilt, but saved
reports that filter by key break. The ID check digit is dropped before hashing, so mistyped check digits
still map to the same patient.

## 2. Never delete; upsert on natural keys
**Context.** Sources come and go: the marketing agency's CRM was going to be switched off, and the legacy
EHR was decommissioned.
**Decision.** Every table has a natural key and every load is `INSERT … ON CONFLICT DO UPDATE`. There is
no delete path.
**Consequences.** Loads are idempotent and can be rerun at any time. A source that disappears leaves its
last snapshot intact. Corrections in the source overwrite the old value; history of the change itself is
not kept (acceptable at this scale).

## 3. A single migration cut-over date
**Context.** The new EHR imported a copy of the legacy history, so July 2026 exists in both systems.
**Decision.** One constant, `MIGRATION_CUTOVER = 2026-07-01`. Before it, the legacy EHR is the source of
truth; from it, the new EHR. Tables fed by both carry `in_series`, and every BI view filters on it.
**Consequences.** No double counting. The overlap month became a control: in the production system both
EHRs agreed to the peso on payments and within 2 of 383 appointments.

## 4. Normalise appointment status instead of trusting vendor fields
**Context.** Each EHR encodes attendance differently, and one of them has an `attendance` field that is
unreliable.
**Decision.** A single vocabulary — `attended`, `no_show`, `rescheduled`, `cancelled`, `booked`,
`block` — mapped from each vendor's appointment state.
**Consequences.** No-show rate means the same thing across 38 months and two systems, which is what made
the before/after comparison credible.

## 5. The database is derived; sources are the truth
**Context.** Manual fixes inside a reporting database are invisible and get lost.
**Decision.** The database is rebuilt from source files and APIs with one command. Any rule change goes
into code and the database is rebuilt.
**Consequences.** In production a full rebuild takes about 7 minutes, bound by the EHR API; this repo,
reading saved files, takes seconds. Every load is
logged in an `etl_load` table with rows, status and errors.

## 6. Controls before reports
**Context.** A dashboard built on silently wrong data is worse than no dashboard.
**Decision.** Every run ends with seven controls: bank statements reconcile to the peso, the database P&L
matches the accounting P&L, the migration month matches in both systems, the legacy total matches its
known value, no month has two sources in series, every provider is recognised, and no column in the
warehouse is named like a direct identifier (name, ID, email, address, phone). That last control checks
names, not content: it stops the obvious mistake (a loader passing a raw column through), not
re-identification from quasi-identifiers, which decision 1 handles. Any `FAIL` blocks reporting; a control
whose reference file is missing reports `SKIP`, never a silent pass.
**Consequences.** Reports are trusted by default, and new problems (an unknown provider, a missing bank
statement) surface the day they happen instead of at month end.
