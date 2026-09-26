# Postgres with pgvector and the aicol extension files, for CREATE EXTENSION aicol.
# Build context: the output of `aicol extension-files` (make extension-image does both).
ARG PG_IMAGE=pgvector/pgvector:pg17
FROM ${PG_IMAGE}
COPY *.control *.sql /usr/share/postgresql/17/extension/
