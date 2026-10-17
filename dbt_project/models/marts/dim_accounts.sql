-- One row per institution, with deal history, customer status and the contact mix that
-- hints at which buying motion the account is likely to follow.
with accounts as (
    select * from {{ ref('stg_accounts') }}
),

deal_stats as (
    select
        account_id,
        count(*)                                              as total_deals,
        sum(case when is_closed = 0 then 1 else 0 end)        as open_deals,
        sum(is_won)                                           as won_deals,
        sum(won_amount)                                       as won_amount,
        min(case when is_won = 1 then closed_at end)          as customer_since
    from {{ ref('fct_deals') }}
    group by account_id
),

contact_stats as (
    select
        account_id,
        count(*)                                                        as contacts,
        sum(case when role = 'professor' then 1 else 0 end)             as professor_contacts,
        sum(case when role = 'instructional_designer' then 1 else 0 end) as instructional_designer_contacts
    from {{ ref('stg_contacts') }}
    group by account_id
)

select
    a.account_id,
    a.account_name,
    a.institution_type,
    a.state,
    a.enrollment,
    case
        when a.enrollment >= {{ var('segment_large_min_enrollment') }} then 'Large'
        when a.enrollment >= {{ var('segment_mid_min_enrollment') }}   then 'Mid-size'
        else 'Small'
    end                                                       as account_segment,
    a.account_owner,
    coalesce(ds.total_deals, 0)                               as total_deals,
    coalesce(ds.open_deals, 0)                                as open_deals,
    coalesce(ds.won_deals, 0)                                 as won_deals,
    coalesce(ds.won_amount, 0)                                as won_amount,
    case when coalesce(ds.won_deals, 0) > 0 then 1 else 0 end as is_customer,
    ds.customer_since,
    coalesce(cs.contacts, 0)                                  as contacts,
    coalesce(cs.professor_contacts, 0)                        as professor_contacts,
    coalesce(cs.instructional_designer_contacts, 0)           as instructional_designer_contacts,
    a.created_at,
    a.updated_at
from accounts a
left join deal_stats ds    on ds.account_id = a.account_id
left join contact_stats cs on cs.account_id = a.account_id
