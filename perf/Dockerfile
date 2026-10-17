# Benchmark runner (perf/bench.py). Run through scripts/perf.sh (make perf).
FROM python:3.12.15-slim-bookworm
RUN pip install --no-cache-dir "h11==0.16.0"
RUN useradd --uid 10001 --create-home bench
ENV PYTHONDONTWRITEBYTECODE=1
USER 10001
WORKDIR /perf
COPY bench.py ./
ENTRYPOINT ["python", "bench.py"]
