# One image, two roles: the virtual CDU simulator and the bridge.
#   docker run hydragate python -m hydragate sim ...
#   docker run hydragate python -m hydragate bridge ...
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY spec ./spec
COPY config.yaml ./

RUN pip install --no-cache-dir .

EXPOSE 8080 15020

CMD ["python", "-m", "hydragate", "bridge"]
