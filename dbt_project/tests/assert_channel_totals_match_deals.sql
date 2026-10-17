-- A singular test: it fails if it returns any rows.
-- Every deal belongs to exactly one channel, so the channel rows must add up to all deals,
-- and won amounts must add up too. A channel mix that doesn't sum to the total is a wrong number.
select c.n_channel_deals, d.n_deals, c.channel_won_amount, d.deal_won_amount
from (select sum(deals_created) as n_channel_deals, sum(won_amount) as channel_won_amount
      from {{ ref('fct_channel_performance') }}) c
cross join (select count(*) as n_deals, sum(won_amount) as deal_won_amount
            from {{ ref('fct_deals') }}) d
where c.n_channel_deals <> d.n_deals
   or c.channel_won_amount <> d.deal_won_amount
