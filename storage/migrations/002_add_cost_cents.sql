-- Phase 45: the cost of each step, frozen at the moment it was incurred.
--
-- Phase 44 computed an intent's bill by re-pricing the stored tokens at read time. That is not a
-- ledger: API prices change, and a rate overridden in the environment retroactively re-prices work
-- that was already done. The cost is now written once, when the step runs, and never derived again.
--
-- ``REAL`` rather than ``INTEGER``: a cheap step costs a fraction of a cent, and integer cents would
-- truncate it to zero -- so a long run of small steps would accumulate no cost and never trip the
-- ceiling, which is the exact failure the ceiling exists to prevent.

ALTER TABLE intent_step_spend ADD COLUMN cost_cents REAL NOT NULL DEFAULT 0;

-- Backfill the rows that predate the column. They are priced at the rate that was in force when
-- this migration was written -- the only rate available, because the original one was never
-- recorded -- and the result is then frozen, like every row after it.
--
-- The rates are the Phase 44 defaults in ``tools/token_budget`` (0.25c / 1K prompt, 1.0c / 1K
-- completion). They are inlined on purpose: a migration records the decision it made, and reading
-- the current environment here would make the patch non-deterministic.
UPDATE intent_step_spend
SET cost_cents = (prompt_tokens / 1000.0) * 0.25 + (completion_tokens / 1000.0) * 1.0
WHERE cost_cents = 0 AND (prompt_tokens > 0 OR completion_tokens > 0);
