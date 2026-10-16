-- raw_payload is kept because the stage history (a JSON array) is unpacked
-- in the intermediate layer, not here.
select
    id                                                                          as deal_id,
    {{ json_value('payload', 'account_id') }}                                   as account_id,
    {{ json_value('payload', 'campaign_id') }}                                  as campaign_id,
    {{ json_value('payload', 'name') }}                                         as deal_name,
    {{ json_value('payload', 'buying_motion') }}                                as buying_motion,
    {{ json_value('payload', 'stage') }}                                        as stage,
    cast({{ json_value('payload', 'amount') }} as {{ dbt.type_numeric() }})     as amount,
    {{ to_ts(json_value('payload', 'created_at')) }}                            as created_at,
    {{ to_date(json_value('payload', 'close_date')) }}                          as close_date,
    {{ to_ts('updated_at') }}                                                   as updated_at,
    {{ to_ts('_ingested_at') }}                                                 as ingested_at,
    payload                                                                     as raw_payload
from {{ source('crm_raw', 'raw_deals') }}
