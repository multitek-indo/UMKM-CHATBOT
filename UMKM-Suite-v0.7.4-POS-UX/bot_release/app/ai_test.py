import asyncio
from app import ai

async def main():
    ok, message = await ai.test_connection()
    print(("OK: " if ok else "ERROR: ") + message)
    raise SystemExit(0 if ok else 1)

if __name__ == "__main__":
    asyncio.run(main())
