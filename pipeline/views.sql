-- Semantic layer: what BI consumes. Views already apply in_series = 1 (no migration overlap) and the
-- business rules that should not be re-derived in every report.

DROP VIEW IF EXISTS v_appointment;
CREATE VIEW v_appointment AS
SELECT a.*, substr(a.date, 1, 7) AS year_month,
       CASE WHEN a.status IN ('attended', 'no_show') THEN 1 ELSE 0 END AS counts_for_no_show
FROM fact_appointment a
WHERE a.in_series = 1 AND a.status <> 'block';

DROP VIEW IF EXISTS v_payment;
CREATE VIEW v_payment AS
SELECT p.*, substr(p.date, 1, 7) AS year_month FROM fact_payment p WHERE p.in_series = 1 AND p.active = 1;

-- Monthly cash P&L from the bank. operating_result = revenue + operating costs (costs are negative);
-- owner draws and personal spending mixed into the business account are shown apart, not as costs.
DROP VIEW IF EXISTS v_pnl_monthly;
CREATE VIEW v_pnl_monthly AS
SELECT t.year_month,
       SUM(CASE WHEN c.pnl_group = 'Revenue'   THEN t.amount ELSE 0 END) AS revenue,
       SUM(CASE WHEN c.pnl_group = 'Variable'  THEN t.amount ELSE 0 END) AS variable_cost,
       SUM(CASE WHEN c.pnl_group = 'Marketing' THEN t.amount ELSE 0 END) AS marketing,
       SUM(CASE WHEN c.pnl_group = 'Fixed'     THEN t.amount ELSE 0 END) AS fixed_cost,
       SUM(CASE WHEN c.pnl_group = 'Taxes'     THEN t.amount ELSE 0 END) AS taxes,
       SUM(CASE WHEN c.pnl_group IN ('Revenue','Variable','Marketing','Fixed','Taxes') THEN t.amount ELSE 0 END) AS operating_result,
       SUM(CASE WHEN c.pnl_group = 'OwnerDraw' THEN t.amount ELSE 0 END) AS owner_draw,
       SUM(CASE WHEN c.pnl_group = 'Personal'  THEN t.amount ELSE 0 END) AS personal_mixed_in
FROM fact_bank_transaction t JOIN dim_bank_category c USING (category)
GROUP BY t.year_month;

-- Schedule: no-show and chair hours by month and provider. The normalised status is the source of truth.
DROP VIEW IF EXISTS v_schedule_monthly;
CREATE VIEW v_schedule_monthly AS
SELECT year_month, provider_id,
       COUNT(*) AS appointments,
       SUM(status = 'attended') AS attended,
       SUM(status = 'no_show') AS no_show,
       SUM(status = 'rescheduled') AS rescheduled,
       SUM(status = 'cancelled') AS cancelled,
       ROUND(1.0 * SUM(status = 'no_show') / NULLIF(SUM(counts_for_no_show), 0), 4) AS no_show_rate,
       ROUND(SUM(CASE WHEN status = 'attended' THEN duration_min ELSE 0 END) / 60.0, 1) AS hours_attended,
       COUNT(DISTINCT CASE WHEN status = 'attended' THEN patient_key END) AS patients_seen
FROM v_appointment GROUP BY year_month, provider_id;

-- Attendance by who booked and how far ahead: the analysis that exposed the agency's cold leads.
DROP VIEW IF EXISTS v_attendance_by_lead_time;
CREATE VIEW v_attendance_by_lead_time AS
SELECT booked_by,
       CASE WHEN lead_time_days IS NULL THEN 'unknown'
            WHEN lead_time_days < 1 THEN '0: <24h'
            WHEN lead_time_days <= 3 THEN '1: 1-3d'
            WHEN lead_time_days <= 7 THEN '2: 4-7d'
            ELSE '3: >7d' END AS lead_time_band,
       SUM(counts_for_no_show) AS base,
       ROUND(1.0 * SUM(status = 'attended') / NULLIF(SUM(counts_for_no_show), 0), 4) AS attendance_rate
FROM v_appointment GROUP BY booked_by, lead_time_band;

DROP VIEW IF EXISTS v_collections_monthly;
CREATE VIEW v_collections_monthly AS
SELECT year_month, method, source, COUNT(*) AS payments, SUM(amount) AS amount
FROM v_payment GROUP BY year_month, method, source;

-- Card: what the POS says it sold vs what the bank paid out (catches missing payouts and the real fee).
DROP VIEW IF EXISTS v_card_reconciliation;
CREATE VIEW v_card_reconciliation AS
WITH pos AS (SELECT substr(date, 1, 7) AS year_month, SUM(amount) AS pos_gross, SUM(fee) AS pos_fee,
                    SUM(net_payout) AS pos_net
             FROM fact_card_sale WHERE kind = 'sale' GROUP BY 1),
     bank AS (SELECT year_month, SUM(amount) AS bank_card_payout FROM fact_bank_transaction
              WHERE category = 'CARD_PAYOUT' GROUP BY 1)
SELECT pos.*, bank.bank_card_payout, bank.bank_card_payout - pos.pos_net AS difference
FROM pos LEFT JOIN bank USING (year_month);

-- CRM -> patient: a contact is a patient if its national ID or its mobile matches the EHR.
DROP VIEW IF EXISTS v_crm_contact_patient;
CREATE VIEW v_crm_contact_patient AS
SELECT c.contact_id, c.created_on, c.service_interest, c.lead_source,
       COALESCE(c.patient_key, (SELECT p.patient_key FROM dim_patient p
                                WHERE p.phone_key = c.phone_key AND c.phone_key IS NOT NULL LIMIT 1)) AS patient_key,
       CASE WHEN c.patient_key IS NOT NULL THEN 'national_id' WHEN c.phone_key IS NOT NULL
            AND EXISTS (SELECT 1 FROM dim_patient p WHERE p.phone_key = c.phone_key) THEN 'phone' END AS matched_by
FROM fact_crm_contact c;

-- Lead -> visit -> payment funnel (first attended visit and payments AFTER the CRM record was created).
-- Temporal correlation, not attribution: the patient may have arrived through another channel.
DROP VIEW IF EXISTS v_crm_funnel;
CREATE VIEW v_crm_funnel AS
SELECT x.*,
       (SELECT MIN(date) FROM v_appointment a WHERE a.patient_key = x.patient_key AND a.status = 'attended'
          AND a.date >= x.created_on) AS first_visit_after_lead,
       (SELECT SUM(amount) FROM v_payment p WHERE p.patient_key = x.patient_key
          AND p.date >= x.created_on) AS paid_after_lead
FROM v_crm_contact_patient x;

-- Weekly board: schedule + cash + ad spend in one row per ISO week.
-- "New patient" = first attended visit in their whole history (does not tell the channel apart).
DROP VIEW IF EXISTS v_weekly;
CREATE VIEW v_weekly AS
WITH first_visit AS (SELECT patient_key, MIN(date) AS d FROM v_appointment
                     WHERE status = 'attended' AND patient_key IS NOT NULL GROUP BY patient_key),
     wk AS (SELECT DISTINCT iso_week, week_monday FROM dim_date),
     sch AS (SELECT d.iso_week, SUM(a.status = 'attended') AS attended, SUM(a.status = 'no_show') AS no_show,
                    SUM(a.counts_for_no_show) AS no_show_base
             FROM v_appointment a JOIN dim_date d ON d.date = a.date GROUP BY d.iso_week),
     new AS (SELECT d.iso_week, COUNT(*) AS new_patients FROM first_visit f JOIN dim_date d ON d.date = f.d
             GROUP BY d.iso_week),
     cash AS (SELECT d.iso_week, SUM(p.amount) AS collections FROM v_payment p JOIN dim_date d ON d.date = p.date
              GROUP BY d.iso_week)
SELECT wk.iso_week, wk.week_monday, sch.attended, sch.no_show,
       ROUND(1.0 * sch.no_show / NULLIF(sch.no_show_base, 0), 4) AS no_show_rate,
       new.new_patients, cash.collections,
       m.spend_meta, m.spend_google, m.conversations, m.bookings,
       CASE WHEN m.iso_week IS NOT NULL THEN ROUND(1.0 * (COALESCE(m.spend_meta, 0) + COALESCE(m.spend_google, 0))
            / NULLIF(new.new_patients, 0)) END AS ad_cost_per_new_patient
FROM wk
LEFT JOIN sch ON sch.iso_week = wk.iso_week
LEFT JOIN new ON new.iso_week = wk.iso_week
LEFT JOIN cash ON cash.iso_week = wk.iso_week
LEFT JOIN fact_marketing_weekly m ON m.iso_week = wk.iso_week
WHERE COALESCE(sch.attended, 0) + COALESCE(sch.no_show, 0) > 0 OR m.iso_week IS NOT NULL
   OR cash.collections IS NOT NULL;
