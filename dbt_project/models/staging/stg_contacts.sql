select
    id                                                  as contact_id,
    {{ json_value('payload', 'account_id') }}           as account_id,
    {{ json_value('payload', 'first_name') }}           as first_name,
    {{ json_value('payload', 'last_name') }}            as last_name,
    {{ json_value('payload', 'email') }}                as email,
    {{ json_value('payload', 'role') }}                 as role,
    {{ to_ts(json_value('payload', 'created_at')) }}    as created_at,
    {{ to_ts('updated_at') }}                           as updated_at,
    {{ to_ts('_ingested_at') }}                         as ingested_at
from {{ source('crm_raw', 'raw_contacts') }}
