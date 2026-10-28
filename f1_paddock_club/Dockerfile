FROM docker.io/tailscale/tailscale:stable AS ts

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY --from=ts /usr/local/bin/tailscaled /usr/local/bin/tailscale /usr/local/bin/
COPY app/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt
COPY app /app
COPY run.sh /run.sh
RUN chmod +x /run.sh
WORKDIR /app
CMD ["/run.sh"]
