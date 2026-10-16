.PHONY: install seed api sync sync-full mutate fail-demo clear-failures test dbt-debug dbt-build pipeline sync-bq dbt-build-bq

install:
	pip install -r requirements.txt

seed:
	python -m mock_crm_api.seed

api:
	uvicorn mock_crm_api.main:app --port 8000

sync:
	python -m extract.run

sync-full:
	python -m extract.run --full-refresh

mutate:
	curl -s -X POST "localhost:8000/admin/mutate?n_updates=10&n_new=3"

fail-demo:
	curl -s -X POST localhost:8000/admin/failures -H 'content-type: application/json' -d '{"fail_on_page": 2, "fail_times": 999}'

clear-failures:
	curl -s -X POST localhost:8000/admin/failures -H 'content-type: application/json' -d '{}'

test:
	python -m unittest discover -s tests -t . -v

dbt-debug:
	cd dbt_project && dbt debug --profiles-dir .

dbt-build:
	cd dbt_project && dbt build --profiles-dir .

pipeline: sync dbt-build

sync-bq:
	python -m extract.run --target bigquery

dbt-build-bq:
	cd dbt_project && RAW_SCHEMA=crm_raw dbt build --profiles-dir . --target bigquery
