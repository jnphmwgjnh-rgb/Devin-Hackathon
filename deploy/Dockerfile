FROM node:20-slim AS landing
WORKDIR /build/landing
COPY landing/package.json landing/package-lock.json ./
RUN npm ci
COPY landing/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /srv
COPY stage3_modelling/pyproject.toml stage3_modelling/pyproject.toml
COPY stage3_modelling/coldplate stage3_modelling/coldplate
RUN pip install --no-cache-dir -e ./stage3_modelling
COPY cfd-dashboard/backend cfd-dashboard/backend
COPY cfd-dashboard/frontend cfd-dashboard/frontend
COPY --from=landing /build/landing/dist landing/dist
COPY deploy/server.py deploy/server.py
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "deploy/server.py"]
