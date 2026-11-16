.PHONY: up down test load
up:
	docker compose up --build -d

down:
	docker compose down

test:
	python -m unittest discover -s tests -v

load:
	k6 run load/orders.js
