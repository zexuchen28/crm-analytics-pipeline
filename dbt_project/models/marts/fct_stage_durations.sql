-- Answers: in which sales stage do deals stay the longest?
-- One row per stage and buying motion, over stages a deal has already left.
-- (The stage a deal is in right now has no duration yet; closed stages are terminal.)
-- dwell_rank 1 = the stage where deals of that buying motion spend the most time on average.
with completed as (
    select
        h.deal_id,
        h.stage,
        h.stage_order,
        h.days_in_stage,
        d.buying_motion
    from {{ ref('int_deal_stage_history') }} h
    join {{ ref('stg_deals') }} d
        on d.deal_id = h.deal_id
    where h.days_in_stage is not null
      and h.is_closed = 0
),

summary as (
    select
        stage,
        stage_order,
        buying_motion,
        count(*)                          as completed_stage_visits,
        round(avg(days_in_stage), 2)      as avg_days_in_stage,
        round(min(days_in_stage), 2)      as min_days_in_stage,
        round(max(days_in_stage), 2)      as max_days_in_stage
    from completed
    group by stage, stage_order, buying_motion
)

select
    stage,
    stage_order,
    buying_motion,
    completed_stage_visits,
    avg_days_in_stage,
    min_days_in_stage,
    max_days_in_stage,
    row_number() over (partition by buying_motion order by avg_days_in_stage desc) as dwell_rank
from summary
