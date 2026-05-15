# Test-only image used by docker-compose to run the mock entitlement
# service. NEVER deployed to production — the registry itself does not
# import this code at runtime.
#
# Lean Python base + the minimal deps the mock needs. curl is included
# so the compose healthcheck can probe /healthz.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
      "fastapi==0.115.5" \
      "uvicorn[standard]==0.32.1" \
      "pydantic==2.10.3"

WORKDIR /app
COPY app.py .

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
