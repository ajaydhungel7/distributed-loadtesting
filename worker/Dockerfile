# Base image includes the k6 binary at /usr/bin/k6
FROM grafana/k6:0.53.0 AS k6

# Build the Python layer separately to keep the final image lean
FROM python:3.12-slim

# Copy k6 binary from the official image
COPY --from=k6 /usr/bin/k6 /usr/bin/k6

WORKDIR /worker

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py k6_runner.py metrics_emitter.py ./

# Run as non-root
RUN useradd --system --no-create-home workeruser
USER workeruser

CMD ["python", "main.py"]
