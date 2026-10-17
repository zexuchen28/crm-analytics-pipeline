-- One row per deal, enriched with the facts the business questions need.
-- Flags are 1/0 integers so they can be summed to get counts.
with deals as (
    select * from {{ ref('stg_deals') }}
),

campaigns as (
    select campaign_id, channel from {{ ref('stg_campaigns') }}
),

stage_defs as (
    select stage, is_closed, is_won from {{ ref('stage_definitions') }}
),

history as (
    select
        deal_id,
        max(counts_as_qualified)                                  as is_qualified,
        max(case when is_closed = 1 then entered_at end)          as closed_at
    from {{ ref('int_deal_stage_history') }}
    group by deal_id
)

select
    d.deal_id,
    d.account_id,
    d.campaign_id,
    coalesce(c.channel, 'Unattributed')                           as channel,
    d.buying_motion,
    d.stage,
    sd.is_closed,
    sd.is_won,
    case when sd.is_closed = 1 and sd.is_won = 0 then 1 else 0 end as is_lost,
    coalesce(h.is_qualified, 0)                                   as is_qualified,
    d.amount,
    case when sd.is_won = 1 then d.amount else 0 end              as won_amount,
    d.created_at,
    h.closed_at,
    case when sd.is_closed = 1
         then {{ days_between('d.created_at', 'h.closed_at') }} end as cycle_days,
    d.updated_at
from deals d
left join campaigns c  on c.campaign_id = d.campaign_id
left join stage_defs sd on sd.stage = d.stage
left join history h    on h.deal_id = d.deal_id
