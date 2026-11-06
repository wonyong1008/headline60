"""Shared entry point for foreground development and managed API processes."""
import uvicorn

from app.config import settings


def main() -> None:
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
