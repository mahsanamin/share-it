COMPOSE := docker compose
SERVICE := share-it

.DEFAULT_GOAL := help

.PHONY: help up down restart rebuild logs ps shell config clean

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "Usage: make <target>\n\nTargets:\n"} \
	     /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

up: ## Build (if needed) and start in background
	$(COMPOSE) up -d --build

down: ## Stop and remove the container
	$(COMPOSE) down

restart: ## Restart the container
	$(COMPOSE) restart $(SERVICE)

rebuild: ## Force rebuild the image and recreate the container
	$(COMPOSE) build --no-cache
	$(COMPOSE) up -d --force-recreate

logs: ## Follow container logs (Ctrl-C to exit)
	$(COMPOSE) logs -f --tail=100 $(SERVICE)

ps: ## Show container status
	$(COMPOSE) ps

shell: ## Open a shell inside the running container
	$(COMPOSE) exec $(SERVICE) /bin/sh

config: ## Print the resolved compose config
	$(COMPOSE) config

clean: ## Delete ALL uploaded files in ./data (asks for confirmation)
	@printf "This will delete every file in ./data. Continue? [y/N] "; \
	 read ans; [ "$$ans" = "y" ] || [ "$$ans" = "Y" ] || { echo "aborted"; exit 1; }; \
	 find ./data -mindepth 1 ! -name '.gitkeep' -exec rm -rf {} +; \
	 echo "data cleared."
