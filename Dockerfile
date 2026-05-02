FROM python:3.11

WORKDIR /app

# Install Python deps before copying source so this layer is cached
COPY pyproject.toml .
RUN pip install --no-cache-dir \
    "fastapi>=0.115" \
    "uvicorn[standard]>=0.32" \
    "httpx>=0.27" \
    "pydantic>=2.9" \
    "python-multipart>=0.0.20" \
    "sse-starlette>=2.1" \
    "numpy>=2.0" \
    "pillow>=11.0" \
    "rasterio>=1.4" \
    "shapely>=2.0" \
    "pyproj>=3.6" \
    "mercantile>=1.2" \
    "trimesh>=4.5" \
    "pycollada>=0.8" \
    "pygltflib>=1.16" \
    "fast-simplification>=0.1" \
    "scipy>=1.13"

COPY . .

RUN mkdir -p /app/output /app/mapng_ai/cache

ENV HOST=0.0.0.0
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "mapng_ai.app:app", "--host", "0.0.0.0", "--port", "8000"]
