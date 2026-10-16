-- A singular test: it fails if it returns any rows.
-- A deal cannot close before it was created.
select deal_id, created_at, close_date
from {{ ref('stg_deals') }}
where close_date is not null
  and close_date < cast(created_at as date)
