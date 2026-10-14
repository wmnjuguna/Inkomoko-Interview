# syntax=docker/dockerfile:1

# The fault proxy in front of Fineract (Part J). Standard library only: nothing to install.
FROM python:3.13.16-alpine3.24

RUN addgroup -S -g 10001 proxy && adduser -S -D -H -u 10001 -G proxy proxy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY --chown=root:root fault_proxy.py ./

USER proxy
EXPOSE 8081 8092

ENTRYPOINT ["python", "/app/fault_proxy.py"]
