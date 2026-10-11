FROM python:3.12.14-slim AS download
WORKDIR /opt/orbat-mapper
COPY orbat-mapper/release.json orbat-mapper/download.py orbat-mapper/LICENSE.upstream.txt ./
RUN python download.py

FROM python:3.12.14-slim
WORKDIR /app
COPY --from=download /opt/orbat-mapper/dist ./dist
COPY orbat-mapper/serve.py ./serve.py
RUN useradd --uid 10001 --create-home orbat
USER orbat
EXPOSE 8080
CMD ["python", "serve.py", "--host", "0.0.0.0", "--port", "8080"]
