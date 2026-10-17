-- A singular test: it fails if it returns any rows.
-- A deal with no unpacked stage history would silently drop out of the stage and
-- funnel numbers, so every deal must have at least one entry.
select deal_id
from {{ ref('stg_deals') }}
where deal_id not in (select deal_id from {{ ref('int_deal_stage_history') }})
