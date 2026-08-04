FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .[publish]

# Harvest state (siconfi shards, bulk downloads) is ephemeral in the
# container; the durable copy of everything published lives in R2.
ENV CARAMELO_PUBLISH_TARGET=r2

ENTRYPOINT ["caramelo"]
CMD ["run-all"]
