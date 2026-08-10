"""Клиент GraphQL API портала госзакупок РК (ows.goszakup.gov.kz/v3/graphql)."""

from __future__ import annotations

import time

import httpx

GRAPHQL_URL = "https://ows.goszakup.gov.kz/v3/graphql"
LOT_URL = "https://goszakup.gov.kz/ru/announce/index/{trd_buy_id}?tab=lots"

RETRIES = 4
BACKOFF = 3.0


class GoszakupError(RuntimeError):
    pass


class AuthError(GoszakupError):
    """Токен отсутствует, просрочен или не имеет доступа."""


class GoszakupClient:
    """Площадка регулярно отваливается по таймауту и отдаёт 5xx на глубоких
    выборках, поэтому все запросы идут через ретраи с нарастающей паузой."""

    def __init__(self, token: str, timeout: float = 120.0):
        if not token:
            raise AuthError("Токен не задан — вставьте его в веб-форме настроек.")
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._client.close()

    def query(self, query: str, variables: dict | None = None) -> dict:
        payload = self._post_with_retries(
            {"query": query, "variables": variables or {}}
        )
        if "errors" in payload and payload["errors"]:
            messages = "; ".join(
                e.get("message", str(e)) for e in payload["errors"]
            )
            raise GoszakupError(f"GraphQL вернул ошибку: {messages}")
        if "data" not in payload:
            raise GoszakupError(f"Неожиданный ответ площадки: {payload}")
        return payload["data"]

    def _post_with_retries(self, body: dict) -> dict:
        last: Exception | None = None

        for attempt in range(RETRIES):
            try:
                response = self._client.post(GRAPHQL_URL, json=body)
            except httpx.TimeoutException as exc:
                last = exc
                time.sleep(BACKOFF * (attempt + 1))
                continue
            except httpx.HTTPError as exc:
                raise GoszakupError(f"Сеть недоступна: {exc}") from exc

            # Токен не починится повтором — выходим сразу.
            if response.status_code in (401, 403):
                raise AuthError(
                    f"Площадка отклонила токен (HTTP {response.status_code}). "
                    "Проверьте, что токен активен и у него есть доступ к API."
                )
            if response.status_code >= 500 or response.status_code == 429:
                last = GoszakupError(f"Площадка вернула HTTP {response.status_code}")
                time.sleep(BACKOFF * (attempt + 1))
                continue

            response.raise_for_status()
            try:
                return response.json()
            except ValueError as exc:
                raise GoszakupError("Площадка вернула не-JSON.") from exc

        raise GoszakupError(
            f"Площадка не ответила за {RETRIES} попыток. Последняя ошибка: {last}"
        )

    def check_auth(self) -> dict:
        """Минимальный запрос, чтобы убедиться что токен рабочий."""
        data = self.query("{ Lots(limit: 1) { id lotNumber } }")
        return {"ok": True, "sample": data.get("Lots")}


def lot_link(trd_buy_id: int) -> str:
    return LOT_URL.format(trd_buy_id=trd_buy_id)
