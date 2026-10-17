-- A singular test: it fails if it returns any rows.
-- The stage on the deal and the last stage in its history come from two places in the
-- same CRM record. If they disagree, one of them is wrong and so are the numbers built on it.
select d.deal_id, d.stage as deal_stage, h.stage as last_history_stage
from {{ ref('stg_deals') }} d
join {{ ref('int_deal_stage_history') }} h
    on h.deal_id = d.deal_id
   and h.is_current_stage = 1
where d.stage <> h.stage
