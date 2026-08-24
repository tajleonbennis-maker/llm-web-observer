import os

import uvicorn


uvicorn.run(
    "llm_web_observer.app:app",
    host=os.environ.get("LWO_HOST", "0.0.0.0"),
    port=int(os.environ.get("LWO_PORT", "8080")),
)
