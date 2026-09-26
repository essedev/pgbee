# Postgres with pgvector and the pgbee extension files, for CREATE EXTENSION pgbee.
# Build context: the output of `pgbee extension-files` (make extension-image does both).
ARG PG_MAJOR=17
FROM pgvector/pgvector:pg${PG_MAJOR}
ARG PG_MAJOR
LABEL org.opencontainers.image.source="https://github.com/essedev/pgbee" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.description="PostgreSQL with pgvector and the pgbee extension files"
COPY *.control *.sql /usr/share/postgresql/${PG_MAJOR}/extension/
