FROM python:3.13-slim AS development

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements/ /app/requirements/
RUN python -m pip install --no-cache-dir -r requirements/dev.txt \
    && groupadd --system pricewatch \
    && useradd --system --gid pricewatch --create-home pricewatch

COPY --chown=pricewatch:pricewatch . /app/
RUN chown pricewatch:pricewatch /app \
    && install -d -o pricewatch -g pricewatch /var/lib/pricewatch/beat
USER pricewatch

EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
