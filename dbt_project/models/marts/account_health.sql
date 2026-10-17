-- Answers: which customer institutions are using the product less and need follow-up?
-- One row per institution that has weekly usage data.
-- Compares the average weekly active users of the latest 4 weeks with the 4 weeks before.
-- Thresholds are variables in dbt_project.yml.
with ranked as (
    select
        account_id,
        week_start,
        active_users,
        row_number() over (partition by account_id order by week_start desc) as weeks_ago
    from {{ ref('stg_usage') }}
),

summary as (
    select
        account_id,
        count(*)                                                        as weeks_of_data,
        max(week_start)                                                 as latest_week,
        max(case when weeks_ago = 1 then active_users end)              as latest_active_users,
        avg(case when weeks_ago <= 4 then active_users end)             as recent_avg_active_users,
        avg(case when weeks_ago between 5 and 8 then active_users end)  as prior_avg_active_users,
        sum(case when weeks_ago between 5 and 8 then 1 else 0 end)      as prior_weeks
    from ranked
    group by account_id
),

with_change as (
    select
        s.*,
        {{ safe_divide('(s.recent_avg_active_users - s.prior_avg_active_users)', 's.prior_avg_active_users') }} as usage_change
    from summary s
)

select
    c.account_id,
    a.account_name,
    a.account_segment,
    a.enrollment,
    c.weeks_of_data,
    c.latest_week,
    c.latest_active_users,
    round(c.recent_avg_active_users, 1)                    as recent_avg_active_users,
    round(c.prior_avg_active_users, 1)                     as prior_avg_active_users,
    round(c.usage_change, 4)                               as usage_change,
    case
        when c.prior_weeks < 4 or c.usage_change is null            then 'insufficient_data'
        when c.usage_change <= {{ var('usage_decline_threshold') }} then 'declining'
        when c.usage_change >= {{ var('usage_growth_threshold') }}  then 'growing'
        else 'stable'
    end                                                    as usage_trend,
    case
        when c.prior_weeks = 4 and c.usage_change <= {{ var('usage_decline_threshold') }} then 1
        else 0
    end                                                    as needs_follow_up,
    round({{ safe_divide('c.recent_avg_active_users', 'a.enrollment') }}, 4) as adoption_rate
from with_change c
left join {{ ref('dim_accounts') }} a
    on a.account_id = c.account_id
