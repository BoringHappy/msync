.PHONY: docker-up-postgres docker-up-external-db

docker-up-postgres:
	docker compose -f docker/docker-compose.yaml up -d

docker-up-external-db:
	docker compose -f docker/docker-compose.external-db.yaml up -d
