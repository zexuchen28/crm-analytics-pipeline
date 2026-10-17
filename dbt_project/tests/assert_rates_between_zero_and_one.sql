-- A singular test: it fails if it returns any rows.
select channel, qualification_rate, win_rate
from {{ ref('fct_channel_performance') }}
where qualification_rate < 0 or qualification_rate > 1
   or win_rate < 0 or win_rate > 1
