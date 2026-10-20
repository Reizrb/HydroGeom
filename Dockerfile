# Container for HydroGeomAPI and HydroGeomSite.
# The data files are NOT copied in: the API reads them from Hugging Face
# (or from the address in the CG_DATA_URL setting).
FROM python:3.12-slim

WORKDIR /app
COPY hydrogeomapi/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -c "import duckdb; duckdb.sql('INSTALL httpfs'); duckdb.sql('INSTALL spatial')"

COPY hydrogeomapi/app.py ./hydrogeomapi/app.py
COPY hydrogeomsite ./hydrogeomsite

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --app-dir hydrogeomapi --host 0.0.0.0 --port ${PORT}"]
