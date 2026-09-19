.PHONY: bootstrap format lint unit integration bdd security eval up seed demo down clean verify

bootstrap format lint unit integration bdd security eval up seed demo down clean verify:
	uv run poe $@
