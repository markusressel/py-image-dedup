PHONY: all clean test

docker:
	docker build -t markusressel/py-image-dedup .

test:
	pytest