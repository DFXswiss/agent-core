FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE teams.yaml ./
COPY src ./src
RUN pip install --no-cache-dir .

EXPOSE 8787
CMD ["python", "-m", "agent_core"]
