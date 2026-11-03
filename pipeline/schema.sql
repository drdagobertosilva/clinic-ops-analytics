-- Clinic Ops Analytics: star schema for BI.
-- Rules: (1) no row is ever deleted, only inserted or updated (upsert on a natural key);
-- (2) no identifiable patient data: a patient is a salted hash (patient_key), and so is the phone
-- (phone_key). No names, national IDs, emails, addresses or message text, anywhere;
-- (3) tables fed by two EHRs carry in_series: 1 = counts for the unified series. Views filter on it,
-- so the migration overlap is never double counted.

PRAGMA journal_mode = WAL;

-- ---------------------------------------------------------------- control
CREATE TABLE IF NOT EXISTS etl_load (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started TEXT NOT NULL,
    finished TEXT,
    rows INTEGER,
    status TEXT,          -- ok | error | skipped
    detail TEXT
);

-- ---------------------------------------------------------------- dimensions
CREATE TABLE IF NOT EXISTS dim_date (
    date TEXT PRIMARY KEY,            -- YYYY-MM-DD
    year INTEGER, month INTEGER, year_month TEXT, quarter TEXT,
    iso_week TEXT,                    -- 2026-W43
    week_monday TEXT,                 -- Monday of that week (axis for weekly reports)
    weekday INTEGER,                  -- 1 = Monday … 7 = Sunday
    day_name TEXT,
    is_holiday INTEGER, is_business_day INTEGER   -- business day = Mon-Fri and not a holiday
);

-- Staff are identified by a stable slug and their role only: no names in the warehouse.
CREATE TABLE IF NOT EXISTS dim_provider (
    provider_id TEXT PRIMARY KEY,     -- stable slug, e.g. dentist-gp-1
    role TEXT,                        -- dentist | specialist | assistant | admin | agency
    specialty TEXT,
    active INTEGER
);

CREATE TABLE IF NOT EXISTS dim_patient (
    patient_key TEXT PRIMARY KEY,     -- salted HMAC of the normalised national ID
    phone_key TEXT,                   -- salted HMAC of the mobile number (joins CRM to EHR)
    birth_year INTEGER, sex TEXT, municipality TEXT, insurance_plan TEXT,
    registered_on TEXT, source TEXT
);

CREATE TABLE IF NOT EXISTS dim_bank_category (
    category TEXT PRIMARY KEY, label TEXT,
    pnl_group TEXT,     -- Revenue | Variable | Marketing | Fixed | Taxes | OwnerDraw | Personal | Excluded
    sort_order INTEGER
);

CREATE TABLE IF NOT EXISTS dim_procedure (
    code TEXT PRIMARY KEY, name TEXT, category TEXT, list_price INTEGER
);

-- ---------------------------------------------------------------- bank
CREATE TABLE IF NOT EXISTS fact_bank_statement (
    statement_no INTEGER PRIMARY KEY, year_month TEXT,
    opening_balance INTEGER, closing_balance INTEGER, n_transactions INTEGER,
    reconciles INTEGER                -- opening + transactions = closing, to the peso
);
CREATE TABLE IF NOT EXISTS fact_bank_transaction (
    txn_id TEXT PRIMARY KEY,          -- statement-ordinal
    statement_no INTEGER, ordinal INTEGER, date TEXT, year_month TEXT,
    amount INTEGER,                   -- + credit / - debit
    category TEXT                     -- FK dim_bank_category
);

-- ---------------------------------------------------------------- schedule (legacy + new EHR)
CREATE TABLE IF NOT EXISTS fact_appointment (
    appointment_uid TEXT PRIMARY KEY, -- legacy-123 / new-456
    source TEXT, appointment_id TEXT, in_series INTEGER,
    date TEXT, time TEXT, duration_min INTEGER,
    provider_id TEXT, patient_key TEXT, chair TEXT,
    source_status TEXT, status TEXT,  -- normalised: attended | no_show | rescheduled | cancelled | booked | block
    booked_by TEXT,                   -- staff | agency | ai_agent | online
    booked_on TEXT,
    lead_time_days INTEGER            -- days between booking and appointment
);

-- ---------------------------------------------------------------- clinic cash
-- One row per receipt: the money that came in. Summing amount here is safe.
CREATE TABLE IF NOT EXISTS fact_payment (
    payment_uid TEXT PRIMARY KEY, source TEXT, payment_id TEXT, in_series INTEGER,
    date TEXT, amount INTEGER, method TEXT,     -- cash | card | transfer
    patient_key TEXT, provider_id TEXT, active INTEGER
);

-- ---------------------------------------------------------------- card POS
CREATE TABLE IF NOT EXISTS fact_card_sale (
    txn_id TEXT PRIMARY KEY, date TEXT, kind TEXT,   -- sale | refund
    amount INTEGER, fee INTEGER, net_payout INTEGER
);

-- ---------------------------------------------------------------- CRM (marketing agency)
CREATE TABLE IF NOT EXISTS fact_crm_contact (
    contact_id TEXT PRIMARY KEY, phone_key TEXT,
    patient_key TEXT,                 -- only when the agency captured the national ID
    created_on TEXT, lead_source TEXT, service_interest TEXT
);

-- ---------------------------------------------------------------- marketing (weekly report)
CREATE TABLE IF NOT EXISTS fact_marketing_weekly (
    iso_week TEXT PRIMARY KEY, spend_meta INTEGER, spend_google INTEGER,
    conversations INTEGER, bookings INTEGER
);

-- ---------------------------------------------------------------- AI messaging agent
-- The vendor has no API or export: a monthly snapshot of its aggregate dashboard (no patient data).
CREATE TABLE IF NOT EXISTS fact_ai_agent_monthly (
    year_month TEXT, metric TEXT, value REAL, captured_on TEXT, partial INTEGER,
    PRIMARY KEY (year_month, metric)
);

CREATE INDEX IF NOT EXISTS ix_appt_date ON fact_appointment(date);
CREATE INDEX IF NOT EXISTS ix_appt_patient ON fact_appointment(patient_key);
CREATE INDEX IF NOT EXISTS ix_payment_date ON fact_payment(date);
CREATE INDEX IF NOT EXISTS ix_bank_month ON fact_bank_transaction(year_month);
CREATE INDEX IF NOT EXISTS ix_patient_phone ON dim_patient(phone_key);
