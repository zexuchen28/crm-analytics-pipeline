.PHONY: install seed api sync sync-full mutate fail-demo clear-failures test

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
