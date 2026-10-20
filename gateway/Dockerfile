FROM python:3.13.5-slim-bookworm
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt && useradd --uid 10001 --create-home gateway && mkdir /state && chown gateway /state
COPY src ./src
ENV PYTHONPATH=/app/src PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY migrations ./migrations
COPY scripts ./scripts
COPY alembic.ini ./
USER gateway
ENTRYPOINT ["python", "-m", "gateway"]
CMD ["uvicorn", "gateway.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
