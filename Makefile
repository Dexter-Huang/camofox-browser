IMAGE ?= geo-camofox-browser:local
CONTAINER ?= geo-camofox-browser
PORT ?= 9377

.PHONY: build up down clean test

build:
	docker build -t $(IMAGE) .

up:
	docker rm -f $(CONTAINER) 2>/dev/null || true
	docker run -d --restart unless-stopped --name $(CONTAINER) -p $(PORT):9377 $(IMAGE)

down:
	docker rm -f $(CONTAINER)

clean:
	docker image rm $(IMAGE)

test:
	python -m pytest tests -q
