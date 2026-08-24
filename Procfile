# Railway reads its start command from the service settings, not from here.
# This file exists so the command is written down in the repository as well,
# and so anything else that understands a Procfile starts it the same way.
#
# Do not change the entrypoint: `app:app` is the composition root.
web: uvicorn app:app --host 0.0.0.0 --port $PORT
