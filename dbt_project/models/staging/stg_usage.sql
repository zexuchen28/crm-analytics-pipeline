select
    id                                                                        as usage_id,
    {{ json_value('payload', 'account_id') }}                                 as account_id,
    {{ to_date(json_value('payload', 'week_start')) }}                        as week_start,
    cast({{ json_value('payload', 'active_users') }} as {{ dbt.type_int() }}) as active_users,
    cast({{ json_value('payload', 'sessions') }} as {{ dbt.type_int() }})     as sessions,
    {{ to_ts('updated_at') }}                                                 as updated_at,
    {{ to_ts('_ingested_at') }}                                               as ingested_at
from {{ source('crm_raw', 'raw_usage') }}
