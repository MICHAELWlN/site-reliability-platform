"""Small local service: expose one endpoint that proves it can answer HTTP."""

# FastAPI supplies routing and converts Python dictionaries into JSON responses.
from fastapi import FastAPI

# Uvicorn imports this object through the command's service.main:app argument.
app = FastAPI()


# Register GET /health: a browser, curl, or the monitor can request this path.
@app.get("/health")
def health():
    # FastAPI returns this dictionary as JSON with HTTP 200 by default.
    # This is a basic liveness check; it does not check databases or dependencies.
    return {"status": "healthy"}
