-- A singular test: it fails if it returns any rows.
-- A deal that was won must also count as qualified; otherwise the qualified-to-won funnel is broken.
select deal_id
from {{ ref('fct_deals') }}
where is_won = 1 and is_qualified = 0
