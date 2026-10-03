-- Phase 46: money is an integer, not a float.
--
-- Phase 45 stored ``cost_cents`` as REAL. Binary floating point cannot represent a fractional cent,
-- so the strict cost ceiling was computed from a number that was never exact -- and a bill that
-- drifts by a fraction of a cent per step drifts without bound. ``cost_micros`` is an INTEGER in
-- units of 1e-6 of a dollar (1 cent = 10,000 micros), which is exact at every step and sums exactly.

ALTER TABLE intent_step_spend ADD COLUMN cost_micros INTEGER NOT NULL DEFAULT 0;

-- 1 cent is 10,000 micros, so this is a change of scale, not a re-price. ``ROUND`` before the cast
-- because the stored value was itself a float: 0.1 cent is not representable exactly, and
-- ``CAST(0.1 * 10000)`` would otherwise truncate 999.999... to 999.
UPDATE intent_step_spend
SET cost_micros = CAST(ROUND(cost_cents * 10000) AS INTEGER)
WHERE cost_cents > 0;

-- The float column is gone; nothing may read it again.
ALTER TABLE intent_step_spend DROP COLUMN cost_cents;
