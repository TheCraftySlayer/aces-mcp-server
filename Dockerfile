FROM python:3.12-slim-bookworm

# Microsoft ODBC Driver 18 for the SQL tools. curl and gnupg are only needed to add
# Microsoft's package source, so they're removed again in the same layer.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg unixodbc \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
 && apt-get purge -y --auto-remove curl gnupg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ARG ACES_BUILD_TAG=dev
ENV ACES_BUILD_TAG=$ACES_BUILD_TAG
# .dockerignore is an allowlist, so this copies only the runtime files.
COPY . .

# Run as an unprivileged user. The server writes only its cache files, its signing secret
# and, if ACES_LOG_FILE is set, its log (TASK_CACHE_FILE, ACES_FILE_LINK_CACHE_FILE and
# ACES_SERVER_SECRET_FILE: /tmp by default, /home/... on Azure).
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin aces
USER aces

ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000
CMD ["python", "server.py"]
