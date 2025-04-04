.PHONY: all docker clean test

docker:
	sudo docker build . --file Dockerfile --tag markusressel/py-image-dedup:latest

test:
	cd tests; pytest