#
# Dockerfile for whatsoverhead
#

# use slim python image
FROM python:3.9-slim

# env
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# set work dir
WORKDIR /app

# install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# install python deps
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt 

# env & app code
COPY .env /app/
COPY whatsoverhead.py /app/
COPY config/ /app/config/
COPY templates/ /app/templates/
COPY static /app/static/

# lets gooo
CMD ["uvicorn", "whatsoverhead:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]

# EOF
