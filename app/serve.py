"""Shared entry point for foreground development and managed API processes."""
import uvicorn

from app.config import settings


def main() -> None:
    uvicorn.run("app.main:app", host="127.0.0.1", port=settings.port)


if __name__ == "__main__":
    main()
