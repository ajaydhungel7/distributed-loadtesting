FROM python:3.12-slim

WORKDIR /worker

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py metrics_emitter.py ./

# Run as non-root
RUN useradd --system --no-create-home workeruser
USER workeruser

CMD ["python", "main.py"]
