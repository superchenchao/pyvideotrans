.PHONY: install install-cloud test lint run upload-worker subtitle-worker asr-worker media-worker

install:
	python -m pip install -e '.[dev]'

install-cloud:
	python -m pip install -e '.[dev,aliyun]'

test:
	pytest -q

lint:
	ruff check .

run:
	uvicorn cineflow.api:app --host 0.0.0.0 --port 8080 --reload

upload-worker:
	uvicorn cineflow.upload_worker:app --host 0.0.0.0 --port 8094 --reload

subtitle-worker:
	uvicorn cineflow.subtitle_worker:app --host 0.0.0.0 --port 8095 --reload

asr-worker:
	uvicorn cineflow.asr_worker:app --host 0.0.0.0 --port 8091 --reload

media-worker:
	uvicorn cineflow.media_worker:app --host 0.0.0.0 --port 8093 --reload
