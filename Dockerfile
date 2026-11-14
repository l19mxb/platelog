FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --only-binary=:all: -r requirements.txt
COPY platelog ./platelog
USER 65532:65532
EXPOSE 8765 8766
HEALTHCHECK --interval=5s --timeout=3s --start-period=15s --retries=3 \
  CMD ["python", "-c", "from urllib.request import urlopen; assert b'synthetic-preview' in urlopen('http://127.0.0.1:8765/healthz', timeout=2).read()"]
ENTRYPOINT ["python", "-m", "platelog"]
CMD ["preview", "--host", "0.0.0.0"]
