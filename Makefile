.PHONY: all docker clean test

docker:
	docker build . --file Dockerfile --tag markusressel/py-image-dedup:latest

test:
	poetry run pytest