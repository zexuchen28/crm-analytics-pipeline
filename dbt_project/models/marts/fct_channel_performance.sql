-- Answers: which marketing channels bring the most qualified deals, and at what cost?
-- One row per channel. Deals with no campaign fall under 'Unattributed'.
-- Attribution is single-touch (the deal's source campaign); multi-touch needs touch data
-- the CRM does not provide here.
with deals as (
    select * from {{ ref('fct_deals') }}
),

deal_stats as (
    select
        channel,
        count(*)            as deals_created,
        sum(is_qualified)   as qualified_deals,
        sum(is_closed)      as closed_deals,
        sum(is_won)         as won_deals,
        sum(is_lost)        as lost_deals,
        sum(won_amount)     as won_amount
    from deals
    group by channel
),

spend as (
    select channel, count(*) as campaigns, sum(spend) as spend
    from {{ ref('stg_campaigns') }}
    group by channel
),

channels as (
    select channel from deal_stats
    union
    select channel from spend
)

select
    ch.channel,
    coalesce(sp.campaigns, 0)                                         as campaigns,
    coalesce(ds.deals_created, 0)                                     as deals_created,
    coalesce(ds.qualified_deals, 0)                                   as qualified_deals,
    coalesce(ds.closed_deals, 0)                                      as closed_deals,
    coalesce(ds.won_deals, 0)                                         as won_deals,
    coalesce(ds.lost_deals, 0)                                        as lost_deals,
    coalesce(ds.won_amount, 0)                                        as won_amount,
    coalesce(sp.spend, 0)                                             as spend,
    round({{ safe_divide('ds.qualified_deals', 'ds.deals_created') }}, 4) as qualification_rate,
    round({{ safe_divide('ds.won_deals', 'ds.closed_deals') }}, 4)        as win_rate,
    round({{ safe_divide('sp.spend', 'ds.qualified_deals') }}, 2)         as cost_per_qualified_deal,
    round({{ safe_divide('sp.spend', 'ds.won_deals') }}, 2)               as cost_per_won_deal
from channels ch
left join deal_stats ds on ds.channel = ch.channel
left join spend sp      on sp.channel = ch.channel
