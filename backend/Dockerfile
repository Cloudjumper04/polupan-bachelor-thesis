FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r backend/requirements.txt

COPY backend backend

CMD ["python", "-m", "compileall", "backend"]
