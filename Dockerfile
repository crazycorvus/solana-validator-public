FROM python:3.12.7-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir ".[analysis]"
COPY config ./config
COPY analysis ./analysis
ENTRYPOINT ["itb"]
CMD ["--help"]
