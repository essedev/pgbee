# Postgres with pgvector and the pgbee extension files, for CREATE EXTENSION pgbee.
# Build context: the output of `pgbee extension-files` (make extension-image does both).
ARG PG_MAJOR=17
FROM pgvector/pgvector:pg${PG_MAJOR}
ARG PG_MAJOR
COPY *.control *.sql /usr/share/postgresql/${PG_MAJOR}/extension/
