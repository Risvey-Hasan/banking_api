FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt -r requirements-dev.txt

COPY app ./app
COPY loadtest ./loadtest
# The suite ships in this image too, so the tests can be run with
# no Python on the host. See the 'tests' service in compose.
COPY tests ./tests
COPY pytest.ini .

CMD ["python", "-m", "loadtest.run"]