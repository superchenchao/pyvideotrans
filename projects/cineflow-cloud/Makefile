.PHONY: install test lint run

install:
	python -m pip install -e '.[dev]'

test:
	pytest -q

lint:
	ruff check .

run:
	uvicorn cineflow.api:app --host 0.0.0.0 --port 8080 --reload
