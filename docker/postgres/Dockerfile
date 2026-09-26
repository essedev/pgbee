# Postgres with pgvector and the pgbee extension files, for CREATE EXTENSION pgbee.
# Build context: the output of `pgbee extension-files` (make extension-image does both).
ARG PG_IMAGE=pgvector/pgvector:pg17
FROM ${PG_IMAGE}
COPY *.control *.sql /usr/share/postgresql/17/extension/
