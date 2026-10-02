FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Tokyo

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

ENTRYPOINT ["python", "-m", "huckleberry_sprout_sync"]
CMD ["sync"]