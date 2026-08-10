"""Запуск локальной админки: python run.py → http://127.0.0.1:8765"""

import uvicorn

if __name__ == "__main__":
    print("\n  Админка: http://127.0.0.1:8765\n  Остановить: Ctrl+C\n")
    uvicorn.run("app.web:app", host="127.0.0.1", port=8765)
