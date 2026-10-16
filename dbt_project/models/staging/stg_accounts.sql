select
    id                                                                       as account_id,
    {{ json_value('payload', 'name') }}                                      as account_name,
    {{ json_value('payload', 'institution_type') }}                          as institution_type,
    {{ json_value('payload', 'state') }}                                     as state,
    cast({{ json_value('payload', 'enrollment') }} as {{ dbt.type_int() }})  as enrollment,
    {{ json_value('payload', 'owner') }}                                     as account_owner,
    {{ to_ts(json_value('payload', 'created_at')) }}                         as created_at,
    {{ to_ts('updated_at') }}                                                as updated_at,
    {{ to_ts('_ingested_at') }}                                              as ingested_at
from {{ source('crm_raw', 'raw_accounts') }}
