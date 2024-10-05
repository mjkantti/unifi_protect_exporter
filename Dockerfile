FROM python:3-alpine

ENV VIRTUAL_ENV=/home/exporter/venv
ENV PROMETHEUS_DISABLE_CREATED_SERIES=True

WORKDIR /unifi
COPY . .

RUN addgroup -S exporter && adduser -S exporter -G exporter
USER exporter

RUN python -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

RUN pip install -r requirements.txt
EXPOSE 8222
CMD ["python", "/unifi/export.py"]
