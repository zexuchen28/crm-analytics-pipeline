-- A singular test: it fails if it returns any rows.
-- Every deal must carry a positive amount.
select deal_id, amount
from {{ ref('stg_deals') }}
where amount <= 0
