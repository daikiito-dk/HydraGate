SIM_PORT ?= 15020
HTTP_PORT ?= 8080
PY := uv run

.PHONY: install sim bridge test contracts clean

install:
	uv sync

sim:
	$(PY) hydragate-sim --port $(SIM_PORT)

bridge:
	$(PY) hydragate-bridge --http-port $(HTTP_PORT)

test:
	$(PY) pytest -q

contracts:
	$(PY) pytest tests/test_contracts.py -q

clean:
	rm -rf .pytest_cache dist build *.egg-info
