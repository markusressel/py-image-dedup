.PHONY: all docker clean test

docker:
	docker build -t markusressel/py-image-dedup .

test:
	pytest