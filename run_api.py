"""Entry point for the PaperLib web app (FastAPI backend + HTML dashboard).

Run it, then open http://127.0.0.1:8000 in a browser:

    python run_api.py

This is now the primary way to use PaperLib. The old Tkinter GUI (run.py) is
kept only as a legacy desktop client.
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("src.api:app", host="127.0.0.1", port=8000, reload=False)
