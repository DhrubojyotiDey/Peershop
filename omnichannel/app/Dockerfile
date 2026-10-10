# PeerShop Omnichannel webhook service — lean image, no heavy vision/ML deps
# (those live in the separate OCR worker image, Phase 3) since this process
# must stay fast and small: it's a pure I/O-bound request handler.
FROM python:3.13-slim

WORKDIR /app

RUN useradd -m -u 1000 omnichannel

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/media/incoming && chown -R omnichannel:omnichannel /app

USER omnichannel

EXPOSE 8100

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100"]
