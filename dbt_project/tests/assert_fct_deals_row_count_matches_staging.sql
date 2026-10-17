-- A singular test: it fails if it returns any rows.
-- Joins in the mart must neither drop nor duplicate deals.
select s.n_staging, m.n_mart
from (select count(*) as n_staging from {{ ref('stg_deals') }}) s
cross join (select count(*) as n_mart from {{ ref('fct_deals') }}) m
where s.n_staging <> m.n_mart
