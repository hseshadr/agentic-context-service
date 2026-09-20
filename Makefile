.PHONY: bootstrap format lint unit integration bdd security eval ui ui-install up seed demo down clean verify

bootstrap format lint unit integration bdd security eval ui ui-install up seed demo down clean verify:
	uv run poe $@
