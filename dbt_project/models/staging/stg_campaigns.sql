select
    id                                                                       as campaign_id,
    {{ json_value('payload', 'name') }}                                      as campaign_name,
    {{ json_value('payload', 'channel') }}                                   as channel,
    cast({{ json_value('payload', 'spend') }} as {{ dbt.type_numeric() }})   as spend,
    {{ to_date(json_value('payload', 'start_date')) }}                       as start_date,
    {{ to_ts('updated_at') }}                                                as updated_at,
    {{ to_ts('_ingested_at') }}                                              as ingested_at
from {{ source('crm_raw', 'raw_campaigns') }}
