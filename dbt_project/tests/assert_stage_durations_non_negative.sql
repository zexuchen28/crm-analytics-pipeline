-- A singular test: it fails if it returns any rows.
-- Time in a stage can never be negative; that would mean stage history out of order.
select deal_id, stage, days_in_stage
from {{ ref('int_deal_stage_history') }}
where days_in_stage < 0
