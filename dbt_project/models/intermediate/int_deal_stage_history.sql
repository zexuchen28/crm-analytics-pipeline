-- One row per stage a deal has entered, unpacked from the stage_history JSON array
-- stored on each deal. days_in_stage is NULL for the stage a deal is in right now.
-- The join to stage_definitions is a LEFT join on purpose: a stage the CRM adds but we
-- have not defined shows up as a NULL stage_order, which a test turns into a loud failure
-- instead of silently dropping the deal.
with deals as (
    select deal_id, raw_payload
    from {{ ref('stg_deals') }}
),

entries as (
    {{ json_array_elements('deals', 'deal_id', 'raw_payload', '$.stage_history') }}
),

parsed as (
    select
        deal_id,
        {{ json_value('element', 'stage') }}                  as stage,
        {{ to_ts(json_value('element', 'entered_at')) }}      as entered_at
    from entries
),

with_next as (
    select
        p.deal_id,
        p.stage,
        p.entered_at,
        defs.stage_order,
        defs.is_closed,
        defs.is_won,
        defs.counts_as_qualified,
        lead(p.entered_at) over (
            partition by p.deal_id order by p.entered_at, defs.stage_order
        ) as exited_at
    from parsed p
    left join {{ ref('stage_definitions') }} defs
        on defs.stage = p.stage
)

select
    deal_id,
    stage,
    stage_order,
    entered_at,
    exited_at,
    {{ days_between('entered_at', 'exited_at') }}   as days_in_stage,
    case when exited_at is null then 1 else 0 end    as is_current_stage,
    is_closed,
    is_won,
    counts_as_qualified
from with_next
